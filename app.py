# ════════════════════════════════════════════════════════════════
# Smart Trolley Backend — app.py
# ════════════════════════════════════════════════════════════════

from supabase import create_client
import os
import numpy as np

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

from fastapi import FastAPI, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi import Query
from sqlalchemy import text
from database import engine
from payment import create_order
from datetime import datetime
import qrcode
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import hashlib
import time
import razorpay



# ── Constants ────────────────────────────────────────────────────
client = razorpay.Client(auth=(
    os.getenv("RAZORPAY_KEY_ID"),
    os.getenv("RAZORPAY_KEY_SECRET")
))

SECRET_KEY = os.getenv("QR_SECRET_KEY")

# AI Dynamic Threshold constants
Z_SCORE     = 2.5
ALPHA       = 0.1
MIN_STD     = 2.0
MAX_STD     = 50.0
FRAUD_LIMIT = 70

# ── In-memory live weight store ──────────────────────────────────
# Stores latest ESP32 load cell reading per trolley
# No DB needed — ephemeral real-time data
live_weights: dict = {}


app = FastAPI()

# ── CORS ─────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cres-st.netlify.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ════════════════════════════════════════════════════════════════
# ROOT
# ════════════════════════════════════════════════════════════════
@app.get("/")
def root():
    return {"status": "Smart Trolley Backend Running"}


# ════════════════════════════════════════════════════════════════
# LIVE WEIGHT — ESP32 pushes raw load cell reading every 500ms
# React polls this to show real-time LIVE LOAD display
# ════════════════════════════════════════════════════════════════

@app.post("/live-weight")
def post_live_weight(data: dict):

    trolley_code = data.get("trolley_code")
    weight = float(data.get("weight", 0))
    if weight < 0 or weight > 50000:
        raise HTTPException(status_code=400, detail="Invalid weight reading")

    if not trolley_code or weight is None:
        raise HTTPException(
            status_code=400,
            detail="Missing trolley_code or weight"
        )

    live_weights[trolley_code] = {
        "weight": round(float(weight), 1),
        "timestamp": datetime.now().isoformat()
    }

    # ── Remove stale entries older than 60 seconds ──
    now = time.time()
    for code in list(live_weights.keys()):
        ts = datetime.fromisoformat(live_weights[code]["timestamp"]).timestamp()
        if now - ts > 60:
            del live_weights[code]

    return {"status": "ok"}


@app.get("/live-weight")
def get_live_weight(trolley_code: str):
    """
    React polls this every 500ms to update LIVE LOAD display.
    Returns 0 if no reading received yet.
    """
    data = live_weights.get(trolley_code)

    if not data:
        return {"weight": 0.0, "timestamp": None}

    return data


# ════════════════════════════════════════════════════════════════
# VALIDATE — Main ESP32 intelligence endpoint
# Replaces /esp32-data as the primary scan entry point.
# Runs: AI threshold + fraud detection + cart update
# ════════════════════════════════════════════════════════════════

@app.post("/validate")
def validate_item(data: dict):
    """
    Called by ESP32 after barcode scan + weight capture.
    Runs full intelligence pipeline and updates cart if ACCEPTED.

    Payload:
        barcode      : str   — scanned product barcode
        trolley_code : str   — trolley identifier
        delta_weight : float — weight change in grams (W_after - W_before)
        weight_curve : list  — 15 weight readings during placement window
    """
    barcode      = data.get("barcode")
    trolley_code = data.get("trolley_code")
    delta_weight = float(data.get("delta_weight", 0))
    weight_curve = data.get("weight_curve", [])

    if not barcode or not trolley_code:
        raise HTTPException(
            status_code=400,
            detail="Missing barcode or trolley_code"
        )

    with engine.begin() as conn:

        # ── Step 1: Product lookup ───────────────────────────────
        # Uses existing products table columns: id, name, price, weight
        product = conn.execute(
            text("""
                SELECT id, name, price, weight AS base_weight
                FROM products
                WHERE barcode = :barcode
            """),
            {"barcode": barcode}
        ).fetchone()

        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        # ── Step 2: Trolley lookup ───────────────────────────────
        # Uses existing trolleys table + new columns added via ALTER
        trolley = conn.execute(
            text("""
                SELECT id,
                       COALESCE(total_scans, 0)    AS total_scans,
                       COALESCE(rejected_scans, 0) AS rejected_scans,
                       COALESCE(fraud_score, 0)    AS fraud_score
                FROM trolleys
                WHERE trolley_code = :code
            """),
            {"code": trolley_code}
        ).fetchone()

        if not trolley:
            raise HTTPException(status_code=404, detail="Trolley not found")

        # ── Step 3: Load AI learned model ───────────────────────
        # Falls back to products.weight if no model yet
        model = conn.execute(
            text("""
                SELECT mean, stddev, scan_count
                FROM learned_models
                WHERE product_id = :pid
            """),
            {"pid": product.id}
        ).fetchone()

        if model:
            mean       = float(model.mean)
            stddev     = float(model.stddev)
            scan_count = int(model.scan_count)
        else:
            # First scan — seed from products.weight
            mean       = float(product.base_weight or 500.0)
            stddev     = 15.0
            scan_count = 0

        # ── Step 4: Compute dynamic threshold ───────────────────
        lower = mean - (Z_SCORE * stddev)
        upper = mean + (Z_SCORE * stddev)

        # ── Step 5: Fraud detection engine ──────────────────────
        fraud_score = int(trolley.fraud_score)
        ok          = True
        reasons     = []

        # Pattern 1 — Weight substitution attack
        # User scans cheap item, places heavier item
        if delta_weight > upper * 1.5:
            fraud_score += 40
            ok = False
            reasons.append("weight_substitution")

        elif 0 < delta_weight < lower * 0.5:
            fraud_score += 20
            ok = False
            reasons.append("weight_too_low")

        # Pattern 2 — Scan without place attack
        # User scans barcode but never puts item in trolley
        if abs(delta_weight) < max(lower * 0.3, 10.0):
            fraud_score += 35
            ok = False
            reasons.append("item_not_placed")

        # Pattern 3 — Incremental load attack
        # User places item slowly in small steps to confuse delta
        if len(weight_curve) >= 2:
            steps = sum(
                1 for i in range(1, len(weight_curve))
                if 5.0 < (weight_curve[i] - weight_curve[i - 1]) < 50.0
            )
            if steps > 5:
                fraud_score += 30
                ok = False
                reasons.append("incremental_load")

        # Pattern 4 — Signal oscillation attack
        # User shakes trolley to confuse sensor readings
        if len(weight_curve) >= 2:
            variance = float(np.var(weight_curve))
            if variance > 30.0:
                fraud_score += 25
                ok = False
                reasons.append("signal_unstable")

        # Pattern 5 — Session anomaly
        # Too many rejections = probing / testing the system
        total_scans    = int(trolley.total_scans)
        rejected_scans = int(trolley.rejected_scans)

        if total_scans > 3:
            rejection_rate = rejected_scans / total_scans
            if rejection_rate > 0.3:
                fraud_score += 20
                reasons.append("high_rejection_rate")

        # ── Step 6: Final verdict ────────────────────────────────
        if fraud_score >= FRAUD_LIMIT:
            verdict = "FLAGGED"
        elif not ok:
            verdict = "REJECTED"
        else:
            verdict = "ACCEPTED"

        accepted = (verdict == "ACCEPTED")

        # ── Step 7: Update trolley session counters ──────────────
        conn.execute(
            text("""
                UPDATE trolleys SET
                    total_scans    = COALESCE(total_scans, 0) + 1,
                    rejected_scans = COALESCE(rejected_scans, 0) +
                        CASE WHEN :accepted THEN 0 ELSE 1 END,
                    fraud_score    = :fscore
                WHERE id = :tid
            """),
            {
                "tid":      trolley.id,
                "accepted": accepted,
                "fscore":   fraud_score
            }
        )

        # ── Step 8: If ACCEPTED → update cart + learned model ────
        if accepted:

            # Check if product already in this trolley's cart
            existing = conn.execute(
                text("""
                    SELECT id, quantity
                    FROM cart
                    WHERE trolley_id = :tid
                    AND   product_id = :pid
                """),
                {"tid": trolley.id, "pid": product.id}
            ).fetchone()

            if existing:
                # Product already in cart — increment quantity
                conn.execute(
                    text("""
                        UPDATE cart SET
                            quantity        = quantity + 1,
                            expected_weight = :weight,
                            status          = 'SCANNED'
                        WHERE id = :id
                    """),
                    {"id": existing.id, "weight": delta_weight}
                )
            else:
                # New product — insert into cart
                # Uses existing cart columns: trolley_id, product_id,
                # quantity, expected_weight, status
                conn.execute(
                    text("""
                        INSERT INTO cart
                            (trolley_id, product_id, quantity,
                             expected_weight, status)
                        VALUES
                            (:tid, :pid, 1, :weight, 'SCANNED')
                    """),
                    {
                        "tid":    trolley.id,
                        "pid":    product.id,
                        "weight": delta_weight
                    }
                )

            # Update products.detected_weight with actual measured value
            # Uses existing detected_weight column in products table
            conn.execute(
                text("""
                    UPDATE products
                    SET detected_weight = :dw
                    WHERE id = :pid
                """),
                {"dw": delta_weight, "pid": product.id}
            )

            # Update AI learned model — online exponential weighted update
            new_mean   = (1 - ALPHA) * mean + ALPHA * delta_weight
            deviation  = abs(delta_weight - new_mean)
            new_stddev = float(np.clip(
                (1 - ALPHA) * stddev + ALPHA * deviation,
                MIN_STD,
                MAX_STD
            ))

            conn.execute(
                text("""
                    INSERT INTO learned_models
                        (product_id, mean, stddev, scan_count)
                    VALUES
                        (:pid, :mean, :std, 1)
                    ON CONFLICT (product_id) DO UPDATE SET
                        mean       = :mean,
                        stddev     = :std,
                        scan_count = learned_models.scan_count + 1,
                        updated_at = now()
                """),
                {
                    "pid":  product.id,
                    "mean": new_mean,
                    "std":  new_stddev
                }
            )

        # ── Step 9: Log fraud events ─────────────────────────────
        if reasons:
            conn.execute(
                text("""
                    INSERT INTO fraud_logs
                        (trolley_id, product_id, pattern,
                         fraud_score, delta_weight)
                    VALUES
                        (:tid, :pid, :pattern, :score, :delta)
                """),
                {
                    "tid":     trolley.id,
                    "pid":     product.id,
                    "pattern": ", ".join(reasons),
                    "score":   fraud_score,
                    "delta":   delta_weight
                }
            )

        return {
            "verdict":     verdict,
            "product":     product.name,
            "price":       product.price,
            "fraud_score": fraud_score,
            "reasons":     reasons,
            "threshold": {
                "lower": round(lower, 2),
                "upper": round(upper, 2)
            }
        }


# ════════════════════════════════════════════════════════════════
# PAYMENT
# ════════════════════════════════════════════════════════════════

@app.post("/create-order")
async def create_payment_order(data: dict):
    amount = int(data.get("amount", 0))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")
    order = create_order(amount)
    return order


# ════════════════════════════════════════════════════════════════
# SCAN PRODUCT (legacy — kept for backward compatibility)
# New ESP32 code should use /validate instead
# ════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════
# VIEW CART
# ════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════
# CHECKOUT
# ════════════════════════════════════════════════════════════════

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

        # Reset trolley session counters after checkout
        conn.execute(
            text("""
                UPDATE trolleys SET
                    total_scans    = 0,
                    rejected_scans = 0,
                    fraud_score    = 0
                WHERE id = :tid
            """),
            {"tid": trolley_id}
        )

        return {
            "message": "Order created successfully",
            "order_id": str(order_id),
            "total_amount": total
        }


# ════════════════════════════════════════════════════════════════
# PAYMENT SUCCESS
# ════════════════════════════════════════════════════════════════
@app.post("/payment-success")
def payment_success(data: dict):

    order_id   = data.get("order_id")
    payment_id = data.get("payment_id")
    signature  = data.get("signature")

    if not order_id or not payment_id or not signature:
        raise HTTPException(status_code=400, detail="Missing payment fields")

    # ── Razorpay Signature Verification ─────────────────────────
    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature
        })
    except Exception:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    # ── Update order after verification ─────────────────────────
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
                SET payment_status = 'SUCCESS',
                    payment_id = :pid
                WHERE id = :order_id
            """),
            {
                "order_id": order_id,
                "pid": payment_id
            }
        )

    return {"message": "Payment verified successfully"}


# ════════════════════════════════════════════════════════════════
# ESP32 DATA (legacy — kept for backward compatibility)
# New ESP32 firmware should use /validate instead
# ════════════════════════════════════════════════════════════════

@app.post("/esp32-data")
def receive_esp32_data(data: dict):

    barcode      = data.get("barcode")
    trolley_code = data.get("trolley_code")

    if not barcode or not trolley_code:
        raise HTTPException(
            status_code=400,
            detail="Missing barcode or trolley_code"
        )

    return scan_product(barcode=barcode, trolley_code=trolley_code)


# ════════════════════════════════════════════════════════════════
# GENERATE RECEIPT
# ════════════════════════════════════════════════════════════════

@app.get("/generate-receipt")
def generate_receipt(order_id: str):

    timestamp = int(time.time())
    token = hashlib.sha256(
        f"{order_id}{SECRET_KEY}{timestamp}".encode()
    ).hexdigest()

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

    doc      = SimpleDocTemplate(filename)
    elements = []
    styles   = getSampleStyleSheet()

    elements.append(Paragraph("<b>SMART TROLLEY STORE</b>", styles["Title"]))
    elements.append(Paragraph("Chennai, India", styles["Normal"]))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(f"Invoice No: {order_id}", styles["Normal"]))
    elements.append(Paragraph(
        f"Date: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}",
        styles["Normal"]
    ))
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
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1,  0), colors.lightgrey),
    ])
    elements.append(table)
    elements.append(Spacer(1, 20))

    verification_url = (
        f"https://smart-trolley-backend-5x17.onrender.com"
        f"/verify-invoice/{order_id}?token={token}&ts={timestamp}"
    )

    qr      = qrcode.make(verification_url)
    qr_path = f"receipts/qr_{order_id}.png"
    qr.save(qr_path)

    elements.append(Paragraph("Scan QR for Payment Verification", styles["Normal"]))
    elements.append(Spacer(1, 10))
    elements.append(Image(qr_path, width=120, height=120))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph("Thank you for shopping!", styles["Normal"]))

    doc.build(elements)
    os.remove(qr_path)

    with open(filename, "rb") as f:
        supabase.storage.from_("receipts").upload(
            f"receipt_{order_id}.pdf",
            f.read(),
            {
                "content-type": "application/pdf",
                "upsert": "true"
            }
        )

    receipt_url = supabase.storage.from_("receipts").get_public_url(
        f"receipt_{order_id}.pdf"
    )

    return {
        "message":     "Professional receipt generated",
        "file_path":   filename,
        "receipt_url": receipt_url
    }


# ════════════════════════════════════════════════════════════════
# VERIFY PAGE
# ════════════════════════════════════════════════════════════════

@app.get("/verify-invoice/{order_id}", response_class=HTMLResponse)
def verify_page(order_id: str, token: str = Query(...), ts: int = Query(...)):

    current_time = int(time.time())

    if current_time - ts > 600:
        return """
        <div style="background:red;color:white;padding:50px;text-align:center;font-size:30px;">
            ❌ QR CODE EXPIRED
        </div>
        """

    expected_token = hashlib.sha256(
        f"{order_id}{SECRET_KEY}{ts}".encode()
    ).hexdigest()

    if token != expected_token:
        return """
        <div style="background:red;color:white;padding:50px;text-align:center;font-size:30px;">
            ❌ INVALID OR TAMPERED INVOICE
        </div>
        """

    return f"""
    <html>
    <head><title>Invoice Verification</title></head>
    <body style="text-align:center;font-family:Arial;background:#f4f4f4;padding-top:100px;">
        <div style="background:white;padding:40px;width:400px;margin:auto;border-radius:10px;box-shadow:0 0 10px gray;">
            <h2>Security Verification</h2>
            <p>Invoice ID:</p>
            <b>{order_id}</b>
            <br><br>
            <form action="/verify-submit/{order_id}" method="post">
                <label><b>Enter Actual Trolley Weight (kg):</b></label><br><br>
                <input type="number" step="0.01" name="actual_weight" required
                       style="padding:8px;width:200px;">
                <br><br>
                <button type="submit"
                        style="padding:10px 20px;background:blue;color:white;border:none;border-radius:5px;">
                    Verify
                </button>
            </form>
        </div>
    </body>
    </html>
    """


# ════════════════════════════════════════════════════════════════
# VERIFY SUBMIT
# ════════════════════════════════════════════════════════════════

@app.post("/verify-submit/{order_id}", response_class=HTMLResponse)
def verify_submit(order_id: str, actual_weight: float = Form(...)):

    try:

        with engine.connect() as conn:

            order = conn.execute(
                text("""
                    SELECT total_amount, payment_status
                    FROM orders WHERE id = :id
                """),
                {"id": order_id}
            ).fetchone()

            if not order:
                return """
                <div style="background:red;color:white;padding:50px;text-align:center;font-size:30px;">
                    ❌ INVALID INVOICE
                </div>
                """

            total_amount   = order._mapping["total_amount"]
            payment_status = order._mapping["payment_status"]

            expected_weight = conn.execute(
                text("""
                    SELECT COALESCE(SUM(oi.quantity * COALESCE(p.weight, 0)), 0)
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
                    Expected Weight: {round(expected_weight, 2)} kg <br>
                    Actual Weight: {round(actual_weight, 2)} kg
                </div>
                """
            else:
                return f"""
                <div style="background:#dc3545;color:white;padding:60px;text-align:center;font-size:35px;">
                    ❌ WEIGHT MISMATCH <br><br>
                    Expected: {round(expected_weight, 2)} kg <br>
                    Actual: {round(actual_weight, 2)} kg
                </div>
                """

    except Exception as e:
        return f"""
        <div style="background:black;color:white;padding:40px;text-align:center;">
            SERVER ERROR <br><br> {str(e)}
        </div>
        """

# ════════════════════════════════════════════════
# CLEAR CART — call on app load / new session
# ════════════════════════════════════════════════
@app.post("/clear-cart")
def clear_cart(data: dict):
    trolley_code = data.get("trolley_code")

    if not trolley_code:
        raise HTTPException(status_code=400, detail="Missing trolley_code")

    with engine.begin() as conn:
        trolley = conn.execute(
            text("SELECT id FROM trolleys WHERE trolley_code = :code"),
            {"code": trolley_code}
        ).fetchone()

        if not trolley:
            raise HTTPException(status_code=404, detail="Trolley not found")

        conn.execute(
            text("DELETE FROM cart WHERE trolley_id = :tid"),
            {"tid": trolley.id}
        )

        # Also reset fraud counters for clean session
        conn.execute(
            text("""
                UPDATE trolleys SET
                    total_scans    = 0,
                    rejected_scans = 0,
                    fraud_score    = 0
                WHERE id = :tid
            """),
            {"tid": trolley.id}
        )

    return {"message": "Cart cleared for new session"}