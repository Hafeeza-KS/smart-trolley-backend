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

def create_order(amount: int):
    """
    Create Razorpay order
    amount: in rupees
    """
    order = client.order.create({
        "amount": amount
        "currency": "INR",
        "payment_capture": 1
    })
    return order
