from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "Smart Trolley Backend Running"}

@app.post("/create-order")
async def create_payment_order(data: dict):
    amount = data.get("amount")
    return {"message": "Backend ready", "amount": amount}
