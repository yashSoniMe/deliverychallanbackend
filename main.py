import os
import random
import logging
import smtplib
import traceback
import requests
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi.middleware.cors import CORSMiddleware
import socket

# Force Python's socket library to resolve IPv4 addresses only
orig_getaddrinfo = socket.getaddrinfo

def ipv4_only_getaddrinfo(*args, **kwargs):
    responses = orig_getaddrinfo(*args, **kwargs)
    return [res for res in responses if res[0] == socket.AF_INET]

socket.getaddrinfo = ipv4_only_getaddrinfo
# ------------------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voucher-manager")

# ------------------------------------------------------------------------------
# 1. APP INITIALIZATION & CORS
# ------------------------------------------------------------------------------
app = FastAPI(title="Voucher Manager Backend", version="2.8.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# 2. ENVIRONMENT & DATABASE SETUP
# ------------------------------------------------------------------------------
MONGO_URI = os.getenv(
    "MONGO_URI",
)

# SMTP Config - Pull directly from Environment Variables
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

APPS_SCRIPT_URL = os.getenv(
    "APPS_SCRIPT_URL",
)
APPS_SCRIPT_SECRET = os.getenv("APPS_SCRIPT_SECRET")

DC_NO_PREFIX = "DC-"
DC_NO_WIDTH = 4

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.get_database("voucher_db")
beneficiaries_col = db.get_collection("beneficiaries")
items_col = db.get_collection("items")
vouchers_col = db.get_collection("vouchers")
counters_col = db.get_collection("counters")
users_col = db.get_collection("users")

# In-memory store: { "email": {"otp": "123456", "expires_at": datetime} }
otp_store: Dict[str, dict] = {}


# ------------------------------------------------------------------------------
# 3. DIRECT SMTP EMAIL DISPATCH & HELPERS
# ------------------------------------------------------------------------------
def send_email_otp(target_email: str, otp: str):
    # ... setup MIME message ...
    
    try:
        logger.info(f"Connecting to SMTP server {SMTP_SERVER}:{SMTP_PORT}...")
        
        if SMTP_PORT == 465:
            # SSL connection
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=12)
        else:
            # TLS connection
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=12)
            server.starttls()

        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, target_email, msg.as_string())
        server.quit()
        logger.info(f"Successfully delivered OTP email to {target_email}")

    except Exception as e:
        logger.error(f"Failed to dispatch OTP email: {e}")
        raise HTTPException(status_code=500, detail=f"Email dispatch failed: {str(e)}")

    msg = MIMEMultipart()
    msg['From'] = f"DAMS <{SMTP_EMAIL}>"
    msg['To'] = target_email
    msg['Subject'] = f"{otp} is your DeliveryChallan App Verification Code"

    body = (
        f"Hello,\n\n"
        f"Your verification OTP is: {otp}\n\n"
        f"This code will expire in 1 minute.\n"
        f"Don't Share this code with anyone."
    )
    msg.attach(MIMEText(body, 'plain'))

    try:
        logger.info(f"Connecting to SMTP server {SMTP_SERVER}:{SMTP_PORT}...")
        
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=12)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=12)
            server.starttls()

        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, target_email, msg.as_string())
        server.quit()
        logger.info(f"Successfully delivered OTP email to {target_email}")

    except Exception as e:
        logger.error(f"Failed to dispatch OTP email: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Email dispatch failed: {str(e)}")


async def get_next_dc_no(user_email: str) -> str:
    """Generates an independent, incremental DC sequence for each user email."""
    doc = await counters_col.find_one_and_update(
        {"_id": f"dc_no_{user_email}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    seq = doc["seq"]
    return f"{DC_NO_PREFIX}{seq:0{DC_NO_WIDTH}d}"


# ------------------------------------------------------------------------------
# 4. PYDANTIC MODELS
# ------------------------------------------------------------------------------
class SendOTPRequest(BaseModel):
    email: EmailStr = Field(..., example="user@gmail.com")

class VerifyOTPRequest(BaseModel):
    email: EmailStr = Field(..., example="user@gmail.com")
    otp: str = Field(..., example="123456")

class RegisterUserRequest(BaseModel):
    email: EmailStr = Field(..., example="user@gmail.com")
    company_name: str = Field(..., example="Acme Jewels Pvt Ltd")
    address_line_1: str = Field(..., example="101 Gold Market Road")
    address_line_2: str = Field(default="", example="CG Road, Navrangpura")
    gstin: str = Field(..., example="24AAAAA0000A1Z5")
    pan_no: str = Field(default="", example="ABCDE1234F")
    mobile_no: str = Field(..., example="9876543210")

class UpdateUserProfileRequest(BaseModel):
    user_email: EmailStr = Field(..., example="user@gmail.com")
    company_name: str
    address_line_1: str
    address_line_2: str = ""
    gstin: str = ""
    mobile_no: str = ""

class Beneficiary(BaseModel):
    user_email: Optional[EmailStr] = Field(default=None, example="user@gmail.com")
    name: str = Field(..., example="Shree Hari Jewellers")
    address: str = Field(..., example="CG Road, Ahmedabad")
    state_code: str = Field(..., example="24")
    gstin: str = Field(..., example="24AAAAA0000A1Z5")
    pan_no: str = Field(..., example="ABCDE1234F")

class Item(BaseModel):
    user_email: Optional[EmailStr] = Field(default=None, example="user@gmail.com")
    description: str = Field(..., example="Gold Ornament 22K")
    hsn_code: str = Field(..., example="7113")
    purity: str = Field(..., example="916")

class VoucherItemInput(BaseModel):
    item_name: str = Field(..., example="Gold Ornament 22K")
    hsn_code: str = Field(..., example="7113")
    purity: str = Field(..., example="916")
    gross_weight: float = Field(..., example="12.50")
    net_weight: float = Field(..., example="11.80")

class VoucherRequest(BaseModel):
    user_email: EmailStr = Field(..., example="sender@gmail.com")
    beneficiary_name: str
    recipient_email: str
    items: List[VoucherItemInput]
    approx_value: float
    mode_of_transport: str = Field(default="", example="By Courier")
    note: str = Field(default="", example="for repair and polish")


# ------------------------------------------------------------------------------
# 5. AUTHENTICATION & USER PROFILE ENDPOINTS
# ------------------------------------------------------------------------------
@app.post("/api/auth/send-otp")
async def send_otp(data: SendOTPRequest):
    email = data.email.lower().strip()
    otp = str(random.randint(100000, 999999))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

    send_email_otp(email, otp)
    otp_store[email] = {"otp": otp, "expires_at": expires_at}
    return {"message": "OTP sent successfully to your email."}


@app.post("/api/auth/verify-otp")
async def verify_otp(data: VerifyOTPRequest):
    email = data.email.lower().strip()
    record = otp_store.get(email)

    if not record or datetime.now(timezone.utc) > record["expires_at"]:
        raise HTTPException(status_code=400, detail="OTP expired or not requested.")

    if record["otp"] != data.otp.strip():
        raise HTTPException(status_code=400, detail="Invalid OTP.")

    del otp_store[email]

    user_doc = await users_col.find_one_and_update(
        {"email": email},
        {
            "$set": {"email": email, "last_login": datetime.now(timezone.utc)},
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)}
        },
        upsert=True,
        return_document=True
    )

    is_registered = bool(user_doc.get("company_name"))
    return {
        "message": "OTP verified successfully.",
        "user_id": str(user_doc["_id"]),
        "email": email,
        "is_registered": is_registered,
        "user_data": {
            "company_name": user_doc.get("company_name", ""),
            "address_line_1": user_doc.get("address_line_1", ""),
            "address_line_2": user_doc.get("address_line_2", ""),
            "gstin": user_doc.get("gstin", ""),
            "pan_no": user_doc.get("pan_no", ""),
            "mobile_no": user_doc.get("mobile_no", "")
        } if is_registered else None
    }


@app.get("/api/users/profile")
async def get_user_profile(email: EmailStr = Query(...)):
    email_clean = email.lower().strip()
    user_doc = await users_col.find_one({"email": email_clean})

    if not user_doc:
        raise HTTPException(status_code=404, detail="User profile not found")

    return {
        "company_name": user_doc.get("company_name", ""),
        "address_line_1": user_doc.get("address_line_1", ""),
        "address_line_2": user_doc.get("address_line_2", ""),
        "gstin": user_doc.get("gstin", ""),
        "pan_no": user_doc.get("pan_no", ""),
        "mobile_no": user_doc.get("mobile_no", "")
    }


@app.put("/api/users/profile")
async def update_user_profile(data: UpdateUserProfileRequest):
    email_clean = data.user_email.lower().strip()

    update_payload = {
        "company_name": data.company_name.strip(),
        "address_line_1": data.address_line_1.strip(),
        "address_line_2": data.address_line_2.strip(),
        "gstin": data.gstin.strip().upper(),
        "mobile_no": data.mobile_no.strip(),
        "updated_at": datetime.now(timezone.utc)
    }

    result = await users_col.update_one({"email": email_clean}, {"$set": update_payload})

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User profile not found")

    return {"message": "Profile updated successfully"}


@app.post("/api/auth/register-user")
async def register_user(data: RegisterUserRequest):
    email = data.email.lower().strip()

    update_payload = {
        "company_name": data.company_name.strip(),
        "address_line_1": data.address_line_1.strip(),
        "address_line_2": data.address_line_2.strip(),
        "gstin": data.gstin.strip().upper(),
        "pan_no": data.pan_no.strip().upper(),
        "mobile_no": data.mobile_no.strip(),
        "updated_at": datetime.now(timezone.utc)
    }

    result = await users_col.find_one_and_update(
        {"email": email},
        {"$set": update_payload},
        upsert=True,
        return_document=True
    )

    return {
        "message": "User company details registered successfully.",
        "user_id": str(result["_id"]),
        "email": email,
        "is_registered": True
    }


# ------------------------------------------------------------------------------
# 6. BENEFICIARY ENDPOINTS (USER ISOLATED)
# ------------------------------------------------------------------------------
@app.get("/api/beneficiaries", response_model=List[dict])
async def get_beneficiaries(user_email: Optional[str] = Query(None)):
    query = {}
    if user_email:
        query["user_email"] = user_email.lower().strip()

    beneficiaries = []
    cursor = beneficiaries_col.find(query)
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        beneficiaries.append(doc)
    return beneficiaries


@app.post("/api/beneficiaries/add", status_code=201)
async def add_beneficiary(
    beneficiary: Beneficiary, 
    user_email: Optional[str] = Query(None)
):
    data = beneficiary.model_dump()
    target_email = user_email or data.get("user_email")

    if target_email:
        data["user_email"] = target_email.lower().strip()

    result = await beneficiaries_col.insert_one(data)
    return {"message": "Beneficiary saved", "id": str(result.inserted_id)}


# ------------------------------------------------------------------------------
# 7. ITEM ENDPOINTS (USER ISOLATED)
# ------------------------------------------------------------------------------
@app.get("/api/items", response_model=List[dict])
async def get_items(user_email: Optional[str] = Query(None)):
    query = {}
    if user_email:
        query["user_email"] = user_email.lower().strip()

    items = []
    cursor = items_col.find(query)
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        items.append(doc)
    return items


@app.post("/api/items/add", status_code=201)
async def add_item(
    item: Item, 
    user_email: Optional[str] = Query(None)
):
    data = item.model_dump()
    target_email = user_email or data.get("user_email")

    if target_email:
        data["user_email"] = target_email.lower().strip()

    result = await items_col.insert_one(data)
    return {"message": "Item added successfully", "id": str(result.inserted_id)}


# ------------------------------------------------------------------------------
# 8. GOOGLE SHEET GENERATION LOGIC
# ------------------------------------------------------------------------------
def duplicate_and_populate_sheet(
    req: VoucherRequest,
    sender_details: dict,
    beneficiary_details: Optional[dict],
    dc_no: str,
    sheet_file_name: str,
) -> str:
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        raise ValueError("APPS_SCRIPT_URL / APPS_SCRIPT_SECRET env vars are not set.")

    items_payload = [
        {"sr_no": i + 1, **item.model_dump()}
        for i, item in enumerate(req.items)
    ]

    full_address = sender_details.get("address_line_1", "")
    if sender_details.get("address_line_2"):
        full_address += f", {sender_details.get('address_line_2')}"

    payload = {
        "secret": APPS_SCRIPT_SECRET,
        "dc_no": dc_no,
        "dc_date": datetime.now().strftime("%d/%m/%Y"),
        "sheet_file_name": sheet_file_name,
        "sender_company_name": sender_details.get("company_name", ""),
        "sender_address": full_address,
        "sender_gstin": sender_details.get("gstin", ""),
        "sender_mobile": sender_details.get("mobile_no", ""),
        "beneficiary_name": req.beneficiary_name,
        "recipient_email": req.recipient_email,
        "approx_value": req.approx_value,
        "mode_of_transport": req.mode_of_transport,
        "note": req.note,
        "beneficiary_details": beneficiary_details,
        "items": items_payload,
    }

    resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise HTTPException(status_code=500, detail=f"Apps Script error: {data['error']}")

    return data["sheet_url"]


@app.post("/api/documents/generate")
async def generate_voucher_document(req: VoucherRequest):
    email_clean = req.user_email.lower().strip()
    user_doc = await users_col.find_one({"email": email_clean})

    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found. Please register first.")

    sender_details = {
        "company_name": user_doc.get("company_name", ""),
        "address_line_1": user_doc.get("address_line_1", ""),
        "address_line_2": user_doc.get("address_line_2", ""),
        "gstin": user_doc.get("gstin", ""),
        "mobile_no": user_doc.get("mobile_no", "")
    }

    beneficiary_doc = await beneficiaries_col.find_one({
        "name": req.beneficiary_name,
        "user_email": email_clean
    })
    
    # Fallback search without user_email if specific match isn't found
    if not beneficiary_doc:
        beneficiary_doc = await beneficiaries_col.find_one({"name": req.beneficiary_name})

    if beneficiary_doc:
        beneficiary_doc.pop("_id", None)

    # 1. Fetch user-isolated DC Number (e.g. DC-0001 per user)
    dc_no = await get_next_dc_no(email_clean)

    # 2. Derive user prefix for distinguishing Excel/Google Sheet copies (e.g. 'john_DC-0001')
    user_handle = email_clean.split("@")[0]
    sheet_file_name = f"{user_handle}_{dc_no}"

    record_data = req.model_dump()
    record_data["user_email"] = email_clean
    record_data["dc_no"] = dc_no
    record_data["created_at"] = datetime.now(timezone.utc)
    result = await vouchers_col.insert_one(record_data)

    sheet_url = duplicate_and_populate_sheet(
        req, 
        sender_details, 
        beneficiary_doc, 
        dc_no, 
        sheet_file_name
    )

    await vouchers_col.update_one(
        {"_id": result.inserted_id},
        {"$set": {"generated_sheet_url": sheet_url}}
    )

    return {
        "message": "Voucher generated successfully",
        "dc_no": dc_no,
        "sheet_url": sheet_url,
        "record_id": str(result.inserted_id)
    }