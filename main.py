import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Customer, CreditEvent, FuelTransaction, PumpSession

# Constants
FUEL_PRICES = {
    "G91": 2.18,
    "G95": 2.33,
    "Diesel": 1.66,
}

app = FastAPI(title="Fuel Credit Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Utility helpers

def _oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id format")


def _customer_or_404(customer_id: str):
    cust = db["customer"].find_one({"_id": _oid(customer_id)})
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    return cust


# Public health
@app.get("/")
def root():
    return {"status": "ok", "service": "fuel-credit", "prices": FUEL_PRICES}


# Auth (simple PIN-based for demo; extend to biometrics on client side)
class LoginRequest(BaseModel):
    phone: str
    pin: str


@app.post("/api/auth/login")
def login(req: LoginRequest):
    user = db["customer"].find_one({"phone": req.phone, "pin": req.pin, "active": True})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {
        "customer_id": str(user["_id"]),
        "name": user.get("name"),
        "balance": user.get("balance", 0.0),
    }


# Owner endpoints
class CreateCustomer(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    pin: str


@app.post("/api/owner/customers")
def create_customer(req: CreateCustomer):
    exists = db["customer"].find_one({"phone": req.phone})
    if exists:
        raise HTTPException(status_code=409, detail="Customer with this phone already exists")
    model = Customer(name=req.name, phone=req.phone, email=req.email, pin=req.pin, balance=0.0, active=True)
    new_id = create_document("customer", model)
    return {"customer_id": new_id}


@app.get("/api/owner/customers")
def list_customers():
    docs = get_documents("customer")
    for d in docs:
        d["_id"] = str(d["_id"])  # serialize
    return docs


class TopUpRequest(BaseModel):
    amount: float
    note: Optional[str] = None


@app.post("/api/owner/customers/{customer_id}/topup")
def topup(customer_id: str, body: TopUpRequest):
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    cust = _customer_or_404(customer_id)
    new_balance = float(cust.get("balance", 0.0)) + float(body.amount)
    db["customer"].update_one({"_id": cust["_id"]}, {"$set": {"balance": new_balance, "updated_at": datetime.now(timezone.utc)}})

    # log credit event
    ce = CreditEvent(customer_id=customer_id, amount=body.amount, note=body.note)
    create_document("creditevent", ce)

    return {"customer_id": customer_id, "balance": new_balance}


@app.get("/api/owner/customers/{customer_id}/history")
def credit_history(customer_id: str):
    events = db["creditevent"].find({"customer_id": customer_id}).sort("created_at", -1)
    events = [{**e, "_id": str(e["_id"]) } for e in events]
    return events


@app.get("/api/owner/reports/transactions")
def report_transactions(limit: int = 100):
    txs = db["fueltransaction"].find().sort("created_at", -1).limit(limit)
    out = []
    for t in txs:
        t["_id"] = str(t["_id"])  # serialize
        out.append(t)
    return out


# Customer endpoints
@app.get("/api/customer/{customer_id}/balance")
def get_balance(customer_id: str):
    cust = _customer_or_404(customer_id)
    return {"balance": float(cust.get("balance", 0.0)), "name": cust.get("name")}


class StartPumpRequest(BaseModel):
    pump_id: str


@app.post("/api/customer/{customer_id}/start-session")
def start_pump_session(customer_id: str, body: StartPumpRequest):
    _customer_or_404(customer_id)
    token = os.urandom(6).hex()
    session = PumpSession(
        token=token,
        customer_id=customer_id,
        pump_id=body.pump_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        used=False,
    )
    create_document("pumpsession", session)
    return {"token": token, "expires_in_sec": 300}


class CalculateLitersQuery(BaseModel):
    grade: str


@app.post("/api/customer/{customer_id}/calc-liters")
def calc_liters(customer_id: str, body: CalculateLitersQuery):
    cust = _customer_or_404(customer_id)
    price = FUEL_PRICES.get(body.grade)
    if not price:
        raise HTTPException(status_code=400, detail="Invalid grade")
    balance = float(cust.get("balance", 0.0))
    liters = round(balance / price, 2)
    return {"grade": body.grade, "price": price, "max_liters": liters}


class ConfirmDispenseRequest(BaseModel):
    token: str
    liters: float
    grade: str


@app.post("/api/dispense/confirm")
def confirm_dispense(body: ConfirmDispenseRequest):
    # verify session
    ps = db["pumpsession"].find_one({"token": body.token})
    if not ps:
        raise HTTPException(status_code=404, detail="Session not found")
    if ps.get("used"):
        raise HTTPException(status_code=400, detail="Session already used")
    if ps.get("expires_at") < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Session expired")

    cust = _customer_or_404(ps["customer_id"])  # ps stores string id

    price = FUEL_PRICES.get(body.grade)
    if not price:
        raise HTTPException(status_code=400, detail="Invalid grade")

    total = round(float(body.liters) * float(price), 2)
    balance = float(cust.get("balance", 0.0))
    if total > balance:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    # deduct
    new_balance = round(balance - total, 2)
    db["customer"].update_one({"_id": cust["_id"]}, {"$set": {"balance": new_balance, "updated_at": datetime.now(timezone.utc)}})

    # create transaction
    tx = FuelTransaction(
        customer_id=ps["customer_id"],
        grade=body.grade,
        liters=float(body.liters),
        price_per_liter=price,
        total=total,
        pump_id=ps["pump_id"],
        status="confirmed",
        receipt_no=f"RX{int(datetime.now().timestamp())}"
    )
    create_document("fueltransaction", tx)

    # mark session used
    db["pumpsession"].update_one({"_id": ps["_id"]}, {"$set": {"used": True, "updated_at": datetime.now(timezone.utc)}})

    return {
        "customer_id": ps["customer_id"],
        "new_balance": new_balance,
        "total": total,
        "grade": body.grade,
        "liters": body.liters,
        "price_per_liter": price,
        "receipt_no": tx.receipt_no,
    }


# Utility endpoints for prices and grades
@app.get("/api/prices")
def prices():
    return FUEL_PRICES


# Schema discovery for database viewer
@app.get("/schema")
def get_schema_examples():
    return {
        "customer": Customer.model_json_schema(),
        "creditevent": CreditEvent.model_json_schema(),
        "fueltransaction": FuelTransaction.model_json_schema(),
        "pumpsession": PumpSession.model_json_schema(),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
