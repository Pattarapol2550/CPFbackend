from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime
from zoneinfo import ZoneInfo  # 🟢 เพิ่มสำหรับจัดการโซนเวลาประเทศไทย
import CoolProp.CoolProp as CP
import math
from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional

app = FastAPI(title="Ammonia Compressor 7-Set Smart Diagnostics API (TH Timezone)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_DETAILS = "mongodb://localhost:27017" 
client = AsyncIOMotorClient(MONGO_DETAILS)
database = client.compressor_db
metrics_collection = database.get_collection("compressor_diagnostics")


# --- DATA MODELS (Pydantic) ---
class CompressorDataInput(BaseModel):
    compressor_id: str = Field(..., example="COMP-01") 
    timestamp: Optional[datetime] = Field(default=None) 
    run_hours: Optional[float] = Field(default=None, description="ชั่วโมงการทำงานสะสม (ถ้ามี)")
    slide_valve_percent: float = Field(..., example=71)
    sp_kg: float = Field(..., description="Suction Pressure (kg/cm^2)", example=1.3)
    st_c: float = Field(..., description="Suction Temperature (C)", example=-8.0)
    dp_kg: float = Field(..., description="Discharge Pressure (kg/cm^2)", example=14.0)
    dt_c: float = Field(..., description="Discharge Temperature (C)", example=81.0)
    current_amp: float = Field(..., description="Motor Current (A)", example=155)
    op_kg: float = Field(default=3.0, description="Oil Pressure")
    ot_c: float = Field(default=58.0, description="Oil Temp")
    oil_filter_drop: float = Field(default=-0.1, description="Oil Filter Pressure Drop")


# --- MAP SPEC COMPRESSOR LINE DUCK (มีนบุรี 2) ---
COMPRESSOR_SPECS = {
    "COMP-01": {"name": "High Stage #1", "max_displacement_m3h": 650.0},
    "COMP-02": {"name": "Booster #2",     "max_displacement_m3h": 450.0},
    "COMP-03": {"name": "Booster #3",     "max_displacement_m3h": 450.0},
    "COMP-04": {"name": "Booster #4",     "max_displacement_m3h": 450.0},
    "COMP-05": {"name": "High Stage #5", "max_displacement_m3h": 650.0},
    "COMP-06": {"name": "High Stage #6", "max_displacement_m3h": 650.0},
    "COMP-07": {"name": "High Stage #7", "max_displacement_m3h": 650.0},
}


# --- CORE LOGIC DIAGNOSIS ENGINE ---
def diagnose_compressor(data: CompressorDataInput) -> dict:
    fluid = 'Ammonia'
    voltage = 380.0
    power_factor = 0.85
    
    spec = COMPRESSOR_SPECS.get(data.compressor_id, {"name": "Unknown", "max_displacement_m3h": 500.0})
    max_displacement = spec["max_displacement_m3h"]

    p_suc_pa = (data.sp_kg * 98066.5) + 101325
    p_dis_pa = (data.dp_kg * 98066.5) + 101325
    t_suc_k = data.st_c + 273.15
    t_dis_k = data.dt_c + 273.15

    try:
        h1 = CP.PropsSI('H', 'P', p_suc_pa, 'T', t_suc_k, fluid)        
        h2 = CP.PropsSI('H', 'P', p_dis_pa, 'T', t_dis_k, fluid)        
        h3 = CP.PropsSI('H', 'P', p_dis_pa, 'Q', 0, fluid)              
        h4 = h3 
        
        rho_suction = CP.PropsSI('D', 'P', p_suc_pa, 'T', t_suc_k, fluid) 

        v_actual_m3h = max_displacement * (data.slide_valve_percent / 100.0)
        mass_flow_rate_kgs = (v_actual_m3h * rho_suction * 0.85) / 3600.0

        q_cooling_effect_jkg = h1 - h4
        calculated_ql_kw = mass_flow_rate_kgs * (q_cooling_effect_jkg / 1000.0)

        cycle_cop = q_cooling_effect_jkg / (h2 - h1) if (h2 - h1) > 0 else 0
        power_kw = (math.sqrt(3) * voltage * data.current_amp * power_factor) / 1000
        actual_cop = calculated_ql_kw / power_kw if power_kw > 0 else 0

        t_sat_suc_k = CP.PropsSI('T', 'P', p_suc_pa, 'Q', 1, fluid)
        superheat = data.st_c - (t_sat_suc_k - 273.15)
        
        if -2 < superheat < 2:
            sensor_status = "Warning"
            sensor_diag = "เสี่ยงน้ำยาเหลวหลุดเข้าเครื่อง (Low Superheat) ให้รีบเช็กวาล์วลดความดัน"
        elif 2 <= superheat <= 12:
            sensor_status = "Normal"
            sensor_diag = "เซนเซอร์ปกติ สภาวะไอขาเข้าแห้งสนิทสมบูรณ์แบบ ปลอดภัยต่อคอมเพรสเซอร์"
        else:
            sensor_status = "Abnormal"
            sensor_diag = "ค่า Superheat สูงเกินเกณฑ์ ไอเดือดยวดผิดปกติ หรือเซนเซอร์ฝั่งดูด (SP/ST) อาจจะเพี้ยน"
        
        condenser_status = "Abnormal" if data.dp_kg > 15.5 or data.dt_c > 95 else "Normal"
        condenser_diag = "ระบบระบายความร้อนปกติ แรงดันฝั่งจ่ายปลอดภัยดี" if condenser_status == "Normal" else "เตือน คอยล์ร้อนระบายความร้อนไม่ทันหรือสกรูสะสมความร้อนสูงเกินไป"

        diff_oil_press = data.op_kg - data.sp_kg
        oil_status = "Abnormal" if diff_oil_press < 1.5 else ("Warning" if data.ot_c >= 58 else "Normal")
        oil_diag = "ระบบน้ำมันหล่อลื่นและไส้กรองทำงานปกติ" if oil_status == "Normal" else "อุณหภูมิน้ำมันเครื่องค่อนข้างสูงกว่าเกณฑ์เฉลี่ย ควรเฝ้าระวังระบบ Oil Cooler"

        return {
            "compressor_name_spec": spec["name"],
            "max_displacement_m3h": max_displacement,
            "power_kw": round(power_kw, 2),
            "superheat_k": round(superheat, 2),
            "t_sat_theory_c": round(t_sat_suc_k - 273.15, 2),
            "cycle_cop": round(cycle_cop, 2),
            "actual_cop": round(actual_cop, 2),
            "calculated_ql_kw": round(calculated_ql_kw, 2),
            "mass_flow_rate_kgs": round(mass_flow_rate_kgs, 4),
            "systems": {
                "sensor": {"status": sensor_status, "text": sensor_diag},
                "condenser": {"status": condenser_status, "text": condenser_diag},
                "oil": {"status": oil_status, "text": oil_diag}
            }
        }
    except Exception as e:
        raise ValueError(f"Core Thermodynamic Calculation Error: {str(e)}")


# --- API ENDPOINTS ---
@app.post("/api/metrics", status_code=status.HTTP_201_CREATED)
async def save_compressor_data(payload: CompressorDataInput):
    try:
        diag_results = diagnose_compressor(payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    
    # 🟢 ปรับปรุง: ถ้าไม่ได้ระบุเวลามา ให้แสตมป์เวลาปัจจุบันเป็นเวลาประเทศไทย (Asia/Bangkok) ทันที
    tz_th = ZoneInfo("Asia/Bangkok")
    record_time = payload.timestamp if payload.timestamp else datetime.now(tz_th)
    
    # ดักกรณีหน้าบ้านส่งข้อมูลติด ISO string แบบไม่มี Timezone ให้แปลงเข้าหาเวลาไทย
    if record_time.tzinfo is None:
        record_time = record_time.replace(tzinfo=tz_th)
        
    document = {
        "compressor_id": payload.compressor_id,
        "timestamp": record_time,
        "run_hours": payload.run_hours, 
        "slide_valve_percent": payload.slide_valve_percent,
        "inputs_snapshot": {
            "sp": payload.sp_kg, "st": payload.st_c,
            "dp": payload.dp_kg, "dt": payload.dt_c,
            "amp": payload.current_amp, "op": payload.op_kg,
            "ot": payload.ot_c, "filter_drop": payload.oil_filter_drop
        },
        "diagnosis": diag_results
    }
    
    result = await metrics_collection.insert_one(document)
    return {"status": "Success", "id": str(result.inserted_id)}

@app.get("/api/metrics/{compressor_id}")
async def get_dashboard_data(compressor_id: str, limit: int = 30):
    cursor = metrics_collection.find({"compressor_id": compressor_id}).sort("timestamp", -1).limit(limit)
    data_list = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        data_list.append(doc)
    return data_list

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)