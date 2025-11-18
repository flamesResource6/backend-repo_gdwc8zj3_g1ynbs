"""
Database Schemas for Fuel Credit App

Each Pydantic model corresponds to a MongoDB collection (lowercased class name).
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


class Customer(BaseModel):
    name: str = Field(..., description="Full name")
    phone: str = Field(..., description="Phone number used as login")
    email: Optional[str] = Field(None, description="Email address")
    pin: str = Field(..., min_length=4, max_length=8, description="Numeric PIN for simple auth")
    balance: float = Field(0.0, ge=0, description="Current credit balance")
    active: bool = Field(True, description="Whether the account is active")


class CreditEvent(BaseModel):
    customer_id: str = Field(..., description="Customer ObjectId as string")
    amount: float = Field(..., gt=0, description="Credit added amount")
    note: Optional[str] = Field(None, description="Optional top-up note or reference")


class FuelTransaction(BaseModel):
    customer_id: str = Field(..., description="Customer ObjectId as string")
    grade: Literal["G91", "G95", "Diesel"] = Field(..., description="Fuel grade")
    liters: float = Field(..., gt=0, description="Liters dispensed")
    price_per_liter: float = Field(..., gt=0, description="Price per liter at time of transaction")
    total: float = Field(..., gt=0, description="Total deducted = liters * price_per_liter")
    pump_id: str = Field(..., description="Dispenser/pump identifier")
    status: Literal["confirmed", "cancelled"] = Field("confirmed", description="Transaction status")
    receipt_no: Optional[str] = Field(None, description="Human readable receipt number")


class PumpSession(BaseModel):
    token: str = Field(..., description="Short-lived token for QR/pump verification")
    customer_id: str = Field(..., description="Customer ObjectId as string")
    pump_id: str = Field(..., description="Pump identifier")
    expires_at: datetime = Field(..., description="Expiry timestamp")
    used: bool = Field(False, description="Whether session has been used")
