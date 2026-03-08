from supabase import create_client
import os

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)
from fastapi import FastAPI, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from database import engine
from payment import create_order
from datetime import datetime
import qrcode

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cres-smart-trolley.netlify.app/"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "Smart Trolley Backend Running"}


# ---------------- PAYMENT ----------------

@app.post("/create-order")
async def create_payment_order(data: dict):
    amount = int(data.get("amount"))
    order = create_order(amount)
    return order


# ---------------- SCAN PRODUCT ----------------

@app.post("/scan")
def scan_product(barcode: str, trolley_code: str):

    with engine.begin() as conn:

        product = conn.execute(
            text("SELECT id, price FROM products WHERE barcode = :barcode"),
            {"barcode": barcode}
        ).fetchone()

        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        product_id = product.id

        trolley = conn.execute(
            text("SELECT id FROM trolleys WHERE trolley_code = :code"),
            {"code": trolley_code}
        ).fetchone()

        if not trolley:
            raise HTTPException(status_code=404, detail="Trolley not found")

        trolley_id = trolley.id

        cart_item = conn.execute(
            text("""
                SELECT id, quantity
                FROM cart
                WHERE trolley_id = :trolley_id
                AND product_id = :product_id
            """),
            {"trolley_id": trolley_id, "product_id": product_id}
        ).fetchone()

        if cart_item:

            conn.execute(
                text("""
                    UPDATE cart
                    SET quantity = quantity + 1
                    WHERE id = :id
                """),
                {"id": cart_item.id}
            )

        else:

            conn.execute(
                text("""
                    INSERT INTO cart (trolley_id, product_id, quantity)
                    VALUES (:trolley_id, :product_id, 1)
                """),
                {"trolley_id": trolley_id, "product_id": product_id}
            )

        return {"message": "Product added to cart"}


# ---------------- VIEW CART ----------------

@app.get("/cart")
def view_cart(trolley_code: str):

    with engine.connect() as conn:

        trolley = conn.execute(
            text("SELECT id FROM trolleys WHERE trolley_code = :code"),
            {"code": trolley_code}
        ).fetchone()

        if not trolley:
            raise HTTPException(status_code=404, detail="Trolley not found")

        trolley_id = trolley.id

        items = conn.execute(
            text("""
                SELECT p.name, c.quantity, p.price,
                       (c.quantity * p.price) AS subtotal
                FROM cart c
                JOIN products p ON c.product_id = p.id
                WHERE c.trolley_id = :trolley_id
            """),
            {"trolley_id": trolley_id}
        ).fetchall()

        total = conn.execute(
            text("""
                SELECT COALESCE(SUM(c.quantity * p.price), 0)
                FROM cart c
                JOIN products p ON c.product_id = p.id
                WHERE c.trolley_id = :trolley_id
            """),
            {"trolley_id": trolley_id}
        ).scalar()

        return {
            "items": [dict(row._mapping) for row in items],
            "total_amount": total
        }


# ---------------- CHECKOUT ----------------

@app.post("/checkout")
def checkout(trolley_code: str):

    with engine.begin() as conn:

        trolley = conn.execute(
            text("SELECT id FROM trolleys WHERE trolley_code = :code"),
            {"code": trolley_code}
        ).fetchone()

        if not trolley:
            raise HTTPException(status_code=404, detail="Trolley not found")

        trolley_id = trolley.id

        total = conn.execute(
            text("""
                SELECT COALESCE(SUM(c.quantity * p.price), 0)
                FROM cart c
                JOIN products p ON c.product_id = p.id
                WHERE c.trolley_id = :trolley_id
            """),
            {"trolley_id": trolley_id}
        ).scalar()

        if total == 0:
            raise HTTPException(status_code=400, detail="Cart is empty")

        order = conn.execute(
            text("""
                INSERT INTO orders (trolley_id, total_amount, payment_status)
                VALUES (:trolley_id, :total, 'PENDING')
                RETURNING id
            """),
            {"trolley_id": trolley_id, "total": total}
        ).fetchone()

        order_id = order.id

        conn.execute(
            text("""
                INSERT INTO order_items (order_id, product_id, quantity, price)
                SELECT :order_id, c.product_id, c.quantity, p.price
                FROM cart c
                JOIN products p ON c.product_id = p.id
                WHERE c.trolley_id = :trolley_id
            """),
            {"order_id": order_id, "trolley_id": trolley_id}
        )

        conn.execute(
            text("DELETE FROM cart WHERE trolley_id = :trolley_id"),
            {"trolley_id": trolley_id}
        )

        return {
            "message": "Order created successfully",
            "order_id": str(order_id),
            "total_amount": total
        }


# ---------------- PAYMENT SUCCESS ----------------

@app.post("/payment-success")
def payment_success(order_id: str):

    with engine.begin() as conn:

        order = conn.execute(
            text("SELECT id FROM orders WHERE id = :order_id"),
            {"order_id": order_id}
        ).fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        conn.execute(
            text("""
                UPDATE orders
                SET payment_status = 'SUCCESS'
                WHERE id = :order_id
            """),
            {"order_id": order_id}
        )

        return {"message": "Payment successful"}


# ---------------- ESP32 DATA ----------------

@app.post("/esp32-data")
def receive_esp32_data(data: dict):

    barcode = data.get("barcode")
    trolley_code = data.get("trolley_code")

    if not barcode or not trolley_code:
        raise HTTPException(status_code=400, detail="Missing barcode or trolley_code")

    return scan_product(barcode=barcode, trolley_code=trolley_code)


# ---------------- GENERATE RECEIPT ----------------

@app.get("/generate-receipt")
def generate_receipt(order_id: str):

    with engine.connect() as conn:

        order = conn.execute(
            text("SELECT total_amount FROM orders WHERE id = :id"),
            {"id": order_id}
        ).fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        items = conn.execute(
            text("""
                SELECT p.name, oi.quantity, oi.price,
                       (oi.quantity * oi.price) AS subtotal
                FROM order_items oi
                JOIN products p ON oi.product_id = p.id
                WHERE oi.order_id = :id
            """),
            {"id": order_id}
        ).fetchall()

    os.makedirs("receipts", exist_ok=True)
    filename = f"receipts/receipt_{order_id}.pdf"

    doc = SimpleDocTemplate(filename)
    elements = []
    styles = getSampleStyleSheet()

    elements.append(Paragraph("<b>SMART TROLLEY STORE</b>", styles["Title"]))
    elements.append(Paragraph("Chennai, India", styles["Normal"]))
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(f"Invoice No: {order_id}", styles["Normal"]))
    elements.append(Paragraph(f"Date: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}", styles["Normal"]))
    elements.append(Spacer(1, 10))

    data = [["Item", "Qty", "Price", "Total"]]

    for item in items:
        data.append([
            item.name,
            str(item.quantity),
            f"{item.price}",
            f"{item.subtotal}"
        ])

    data.append(["", "", "Grand Total", f"{order.total_amount}"])

    table = Table(data, colWidths=[150, 40, 60, 60])
    table.setStyle([
        ("GRID", (0,0), (-1,-1), 0.5, colors.black),
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
    ])

    elements.append(table)
    elements.append(Spacer(1, 20))

    # QR verification URL (Render backend)
    verification_url = f"https://smart-trolley-backend-5x17.onrender.com/verify-invoice/{order_id}"

    qr = qrcode.make(verification_url)

    qr_path = f"receipts/qr_{order_id}.png"
    qr.save(qr_path)

    elements.append(Paragraph("Scan QR for Payment Verification", styles["Normal"]))
    elements.append(Spacer(1, 10))
    elements.append(Image(qr_path, width=120, height=120))
    elements.append(Spacer(1, 10))

    elements.append(Paragraph("Thank you for shopping!", styles["Normal"]))

    doc.build(elements)

    # ---------------- Upload to Supabase Storage ----------------

    with open(filename, "rb") as f:
        supabase.storage.from_("receipts").upload(
            f"receipt_{order_id}.pdf",
            f,
            {"content-type": "application/pdf"}
        )

    # Get public URL
    receipt_url = supabase.storage.from_("receipts").get_public_url(
        f"receipt_{order_id}.pdf"
    )

    return {
        "message": "Professional receipt generated",
        "file_path": filename,
        "receipt_url": receipt_url
    }

# ---------------- VERIFY PAGE ----------------

@app.get("/verify-invoice/{order_id}", response_class=HTMLResponse)
def verify_page(order_id: str):
    return f"""
    <html>
    <head>
        <title>Invoice Verification</title>
    </head>
    <body style="text-align:center; font-family:Arial; background-color:#f4f4f4; padding-top:100px;">
        <div style="background:white; padding:40px; width:400px; margin:auto; border-radius:10px; box-shadow:0 0 10px gray;">
            <h2>Security Verification</h2>
            <p>Invoice ID:</p>
            <b>{order_id}</b>
            <br><br>
            <form action="/verify-submit/{order_id}" method="post">
                <label><b>Enter Actual Trolley Weight (kg):</b></label><br><br>
                <input type="number" step="0.01" name="actual_weight" required style="padding:8px; width:200px;">
                <br><br>
                <button type="submit" style="padding:10px 20px; background:blue; color:white; border:none; border-radius:5px;">
                    Verify
                </button>
            </form>
        </div>
    </body>
    </html>
    """


# ---------------- VERIFY SUBMIT ----------------

@app.post("/verify-submit/{order_id}", response_class=HTMLResponse)
def verify_submit(order_id: str, actual_weight: float = Form(...)):

    try:

        with engine.connect() as conn:

            order = conn.execute(
                text("""
                    SELECT total_amount, payment_status
                    FROM orders
                    WHERE id = :id
                """),
                {"id": order_id}
            ).fetchone()

            if not order:
                return """
                <div style="background:red;color:white;padding:50px;text-align:center;font-size:30px;">
                    ❌ INVALID INVOICE
                </div>
                """

            total_amount = order._mapping["total_amount"]
            payment_status = order._mapping["payment_status"]

            expected_weight = conn.execute(
                text("""
                    SELECT COALESCE(SUM(oi.quantity * COALESCE(p.weight,0)), 0)
                    FROM order_items oi
                    JOIN products p ON oi.product_id = p.id
                    WHERE oi.order_id = :id
                """),
                {"id": order_id}
            ).scalar()

            tolerance = 0.05

            if payment_status != "SUCCESS":
                return """
                <div style="background:orange;color:white;padding:50px;text-align:center;font-size:30px;">
                    ⚠ PAYMENT NOT COMPLETED
                </div>
                """

            if abs(float(actual_weight) - float(expected_weight)) <= tolerance:

                return f"""
                <div style="background:#28a745;color:white;padding:60px;text-align:center;font-size:35px;">
                    ✅ ALL CLEAR <br><br>
                    Amount Paid: ₹{total_amount} <br><br>
                    Expected Weight: {round(expected_weight,2)} kg <br>
                    Actual Weight: {round(actual_weight,2)} kg
                </div>
                """

            else:

                return f"""
                <div style="background:#dc3545;color:white;padding:60px;text-align:center;font-size:35px;">
                    ❌ WEIGHT MISMATCH <br><br>
                    Expected: {round(expected_weight,2)} kg <br>
                    Actual: {round(actual_weight,2)} kg
                </div>
                """

    except Exception as e:

        return f"""
        <div style="background:black;color:white;padding:40px;text-align:center;">
            SERVER ERROR <br><br>
            {str(e)}
        </div>
        """