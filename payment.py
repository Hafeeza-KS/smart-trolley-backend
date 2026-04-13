import razorpay
import os
from dotenv import load_dotenv

load_dotenv()

# Initialize Razorpay client (TEST MODE)
client = razorpay.Client(
    auth=(
        os.getenv("RAZORPAY_KEY_ID"),
        os.getenv("RAZORPAY_KEY_SECRET")
    )
)

def create_order(amount: float):
    amount_paise = int(amount * 100)  # convert to paise

    order = client.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "payment_capture": 1,
        "notes": {
            "system": "smart_trolley",
            "secure": "true"
        }
    })

    return order
