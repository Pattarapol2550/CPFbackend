import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime, timezone, timedelta
from typing import List, Optional
import CoolProp.CoolProp as CP
import math

app = FastAPI(title="Ammonia Compressor Expert Diagnostics API")

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()
MONGO_DETAILS = os.getenv("MONGO_DETAILS")
client = AsyncIOMotorClient(MONGO_DETAILS)
database = client.thermoCPF
metrics_collection = database.get_collection("compressor_data")

# --- DATA MODELS ---
class CompressorDataInput(BaseModel):
    compressor_id: str
    timestamp: Optional[datetime] = None
    run_hours: Optional[float] = Field(default=0, ge=0)
    slide_valve_percent: float = Field(..., ge=0, le=100)
    sp_kg: float 
    st_c: float
    dp_kg: float 
    dt_c: float
    current_amp: float = Field(..., ge=0)
    op_kg: float = Field(..., ge=0)
    ot_c: float
    oil_filter_drop: float
    liquid_temp_c: Optional[float] = 0.0
    fan_pump_kw: Optional[float] = 0.0
    evaporator_room_temp_c: Optional[float] = 0.0
    condenser_temp_c: Optional[float] = 0.0

COMPRESSOR_SPECS = {
    "COMP-01": {"name": "High Stage #1", "max_displacement_m3h": 650.0},
    "COMP-02": {"name": "Booster #2",     "max_displacement_m3h": 450.0},
    "COMP-03": {"name": "Booster #3",     "max_displacement_m3h": 450.0},
    "COMP-04": {"name": "Booster #4",     "max_displacement_m3h": 450.0},
    "COMP-05": {"name": "High Stage #5", "max_displacement_m3h": 650.0},
    "COMP-06": {"name": "High Stage #6", "max_displacement_m3h": 650.0},
    "COMP-07": {"name": "High Stage #7", "max_displacement_m3h": 650.0},
}

# --- CORE LOGIC ---
def diagnose_compressor(data: CompressorDataInput) -> dict:
    fluid = 'Ammonia'
    voltage = 380.0
    power_factor = 0.85
    volumetric_efficiency = 0.85
    
    # ดึงค่า Displacement ตามรุ่นเครื่อง
    spec = COMPRESSOR_SPECS.get(data.compressor_id, {"max_displacement_m3h": 500.0})
    max_displacement = spec["max_displacement_m3h"]
    
    p_suc_pa = (data.sp_kg * 98066.5) + 101325
    p_dis_pa = (data.dp_kg * 98066.5) + 101325
    t_suc_k = data.st_c + 273.15
    t_dis_k = data.dt_c + 273.15
    
    h1 = CP.PropsSI('H', 'P', p_suc_pa, 'T', t_suc_k, fluid)
    h2 = CP.PropsSI('H', 'P', p_dis_pa, 'T', t_dis_k, fluid)
    h_liq = CP.PropsSI('H', 'P', p_dis_pa, 'Q', 0, fluid)
    
    v_suction = CP.PropsSI('V', 'P', p_suc_pa, 'T', t_suc_k, fluid)
    mass_flow = (((max_displacement * (data.slide_valve_percent / 100)) / 3600) * volumetric_efficiency) / v_suction
    ql_kw = mass_flow * ((h1 - h_liq) / 1000)
    power_kw = (math.sqrt(3) * voltage * data.current_amp * power_factor) / 1000
    
    # Logic Checks
    t_sat_suc = CP.PropsSI('T', 'P', p_suc_pa, 'Q', 1, fluid) - 273.15
    superheat_suc = data.st_c - t_sat_suc
    sensor_status = "Normal" if 2 <= superheat_suc <= 20 else "Warning"
    
    approach_cond = (CP.PropsSI('T', 'P', p_dis_pa, 'Q', 1, fluid) - 273.15) - data.condenser_temp_c
    condenser_status = "Normal" if approach_cond < 15 else "Warning"

    return {
        "calculated_ql_kw": round(ql_kw, 2),
        "power_kw": round(power_kw, 2),
        "actual_cop": round(ql_kw / power_kw if power_kw > 0 else 0, 2),
        "superheat_suc": round(superheat_suc, 2),
        "status": {"sensor": sensor_status, "condenser": condenser_status}
    }

# --- API ENDPOINTS ---
@app.post("/api/metrics")
async def save_data(payload: CompressorDataInput):
    diag = diagnose_compressor(payload)
    tz_th = timezone(timedelta(hours=7))
    record_time = payload.timestamp if payload.timestamp else datetime.now(tz_th)
    
    document = {
        "compressor_id": payload.compressor_id,
        "timestamp": record_time,
        "run_hours": payload.run_hours,
        "slide_valve_percent": payload.slide_valve_percent,
        "inputs_snapshot": payload.dict(exclude={'compressor_id', 'timestamp', 'run_hours'}),
        "diagnosis": diag
    }
    result = await metrics_collection.insert_one(document)
    return {"status": "Success", "id": str(result.inserted_id)}

@app.get("/api/metrics/{compressor_id}")
async def get_dashboard_data(compressor_id: str, limit: int = 200):
    cursor = metrics_collection.find({"compressor_id": compressor_id}).sort("timestamp", -1).limit(limit)
    data_list = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        data_list.append(doc)
    return data_list

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)