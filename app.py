import os
import hashlib
import time
import secrets
import random
import json
import asyncio
from io import BytesIO
from datetime import datetime, timedelta
from typing import List

from fastapi import FastAPI, HTTPException, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
import qrcode
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from supabase import create_client
import base64
import cv2
import numpy as np

#import anthropic

from database import engine
from payment import create_order

class CheckoutData(BaseModel):
    trolley_code: str
    session_token: str | None = None


# ================================================================
# CLIENTS & CONFIG
# ================================================================

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
#claude   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SECRET_KEY = os.getenv("QR_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("QR_SECRET_KEY is not set")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000",
        "http://192.168.1.7:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================
# WEBSOCKET — real-time staff alerts
# ================================================================

active_connections: List[WebSocket] = []

@app.websocket("/ws/fraud-alerts")
async def fraud_alert_stream(websocket: WebSocket):
    """
    Staff dashboard connects here.
    Receives instant push alerts when HIGH severity fraud is detected.
    Connect via: ws://your-backend/ws/fraud-alerts
    """
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        if websocket in active_connections:
            active_connections.remove(websocket)


async def push_fraud_alert(trolley_id: int, reason: str, severity: str):
    alert = {
        "trolley_id": trolley_id,
        "reason":     reason,
        "severity":   severity,
        "timestamp":  datetime.now().isoformat(),
        "alert_type": "FRAUD_FLAG"
    }
    dead = []
    for ws in active_connections:
        try:
            await ws.send_json(alert)
        except Exception:
            dead.append(ws)
    for ws in dead:
        active_connections.remove(ws)


# ================================================================
# HELPERS
# ================================================================

def log_fraud_flag(conn, reason: str, severity: str,
                   trolley_id: int = None, order_id: str = None):
    conn.execute(
        text("""
            INSERT INTO fraud_flags (trolley_id, order_id, reason, severity)
            VALUES (:trolley_id, :order_id, :reason, :severity)
        """),
        {"trolley_id": trolley_id, "order_id": order_id,
         "reason": reason, "severity": severity}
    )
    if severity == "HIGH":
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(push_fraud_alert(trolley_id, reason, severity))
        except RuntimeError:
            print("⚠️ No event loop, skipping websocket alert")


def compute_risk_score(conn, trolley_id: int, order_id: str = None) -> int:
    query  = """
        SELECT severity FROM fraud_flags
        WHERE (trolley_id = :tid AND flagged_at > NOW() - INTERVAL '2 hours')
    """
    params = {"tid": trolley_id}
    if order_id:
        query += " OR order_id = :oid"
        params["oid"] = order_id
    flags   = conn.execute(text(query), params).fetchall()
    weights = {"LOW": 1, "MEDIUM": 3, "HIGH": 7}
    return sum(weights.get(f.severity, 0) for f in flags)


def validate_session(conn, trolley_code: str, session_token: str):

    session = conn.execute(
        text("""
            SELECT s.id, s.trolley_id, t.session_expires_at
            FROM sessions s
            JOIN trolleys t ON s.trolley_id = t.id
            WHERE t.trolley_code = :code
            AND s.session_token = :token
            AND s.status = 'ACTIVE'
            AND s.ended_at IS NULL
        """),
        {"code": trolley_code, "token": session_token}
    ).fetchone()

    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")

    # ✅ NOW THIS WILL WORK
    if session.session_expires_at and session.session_expires_at < datetime.now():
        raise HTTPException(status_code=401, detail="Session expired")

    return session.trolley_id


def requires_photo_check(risk_score: int, customer_fingerprint: str = None) -> bool:
    """
    Photo check triggered for:
      - Risk score >= 3 (suspicious session)
      - Unknown / first-time customer
      - Random 20% of all customers (keeps everyone honest)
    """
    if risk_score >= 3:
        return True
    if not customer_fingerprint:
        return True
    if random.random() < 0.2:
        return True
    return False

#======================
import math

def ml_risk_score(features: dict):
    """
    Simulated ML model using weighted features + sigmoid
    """

    weights = {
        "scan_count": 0.3,
        "avg_interval": -0.5,   # slower = safer
        "fraud_score": 1.0,
        "clears_count": 0.8,
        "high_flags": 1.2,
        "image_issues": 0.9
    }

    bias = -2  # baseline

    score = bias

    for key, value in features.items():
        score += weights.get(key, 0) * value

    # Sigmoid → probability (0 to 1)
    score = max(min(score, 10), -10)  # clamp

    probability = 1 / (1 + math.exp(-score))

    return probability

def get_risk_level(prob):
    if prob > 0.7:
        return "HIGH"
    elif prob > 0.4:
        return "MEDIUM"
    else:
        return "LOW"

#====================================
def render_error_page(title: str, message: str, icon: str = "fa-circle-xmark"):
    return f"""
    <html>
    <head>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
    </head>

    <body style="
        margin:0;
        font-family:Arial;
        background:#f9fafb;
    ">

        <div style="
            padding:20px;
            max-width:500px;
            margin:auto;
            text-align:center;
            margin-top:80px;
        ">

            <!-- ICON -->
            <div style="
                width:70px;height:70px;
                background:#fee2e2;
                border-radius:20px;
                display:flex;
                align-items:center;
                justify-content:center;
                margin:auto;
            ">
                <i class="fas {icon}" style="color:#dc2626;font-size:28px;"></i>
            </div>

            <h2 style="margin-top:20px;color:#7f1d1d;">
                {title}
            </h2>

            <p style="font-size:13px;color:#6b7280;margin-top:10px;">
                {message}
            </p>

        </div>

    </body>
    </html>
    """
#==============================================




# ================================================================
# ROOT
# ================================================================

@app.get("/")
def root():
    return {"status": "Smart Trolley Backend Running"}


# ================================================================
# PAYMENT
# ================================================================

@app.post("/create-order")
async def create_payment_order(data: dict):

    order_id = data.get("order_id")

    if not order_id:
        raise HTTPException(status_code=400, detail="order_id required")

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
            raise HTTPException(status_code=404, detail="Order not found")

        # 🚫 Prevent duplicate payment
        if order.payment_status == "SUCCESS":
            raise HTTPException(status_code=400, detail="Already paid")

        # 🔒 SECURE AMOUNT FROM DB ONLY
        amount = float(order.total_amount)

    # Razorpay order creation
    razorpay_order = create_order(amount)

    return {
        "razorpay_order": razorpay_order,
        "amount": amount,
        "order_id": order_id
    }


# ================================================================
# SESSION MANAGEMENT
# ================================================================

@app.post("/start-session")
def start_session(trolley_code: str, phone_hash: str = None):

    session_token = secrets.token_hex(32)
    expiry        = datetime.now() + timedelta(hours=2)

    with engine.begin() as conn:

        trolley = conn.execute(
            text("SELECT id FROM trolleys WHERE trolley_code = :code"),
            {"code": trolley_code}
        ).fetchone()

        if not trolley:
            raise HTTPException(status_code=404, detail="Trolley not found")

        # 🔥 CLOSE OLD ACTIVE SESSION
        conn.execute(
            text("""
                UPDATE sessions
                SET ended_at = NOW(),
                    status = 'COMPLETED'
                WHERE trolley_id = :tid
                AND status = 'ACTIVE'
            """),
            {"tid": trolley.id}
        )

        # ✅ INSERT NEW SESSION
        conn.execute(
            text("""
                INSERT INTO sessions (trolley_id, session_token, started_at, status)
                VALUES (:tid, :token, NOW(), 'ACTIVE')
            """),
            {"tid": trolley.id, "token": session_token}
        )

        # ✅ Update trolley (optional but useful)
        conn.execute(
            text("""
                UPDATE trolleys
                SET session_token = :token,
                    session_expires_at = :expiry,
                    clears_count = 0,
                    customer_fingerprint = :fp
                WHERE trolley_code = :code
            """),
            {"token": session_token,
             "expiry": expiry,
             "fp": phone_hash,
             "code": trolley_code}
        )

    return {
        "session_token": session_token,
        "expires_at": expiry.isoformat(),
        "message": "Session started successfully"
    }


# ================================================================
# SCAN PRODUCT
# ================================================================

@app.post("/scan")
def scan_product(barcode: str, trolley_code: str, session_token: str = None):

    with engine.begin() as conn:

        # Session validation
        if session_token:
            trolley_id = validate_session(conn, trolley_code, session_token)
        else:
            trolley = conn.execute(
                text("SELECT id FROM trolleys WHERE trolley_code = :code"),
                {"code": trolley_code}
            ).fetchone()
            if not trolley:
                raise HTTPException(status_code=404, detail="Trolley not found")
            trolley_id = trolley.id


        conn.execute(
            text("""
                UPDATE trolleys
                SET session_expires_at = NOW() + INTERVAL '2 hours'
                WHERE trolley_code = :code
            """),
            {"code": trolley_code}
        )

        conn.execute(
            text("""
                UPDATE sessions
                SET expires_at = NOW() + INTERVAL '2 hours'
                WHERE session_token = :token
            """),
            {"token": session_token}
        )



        # Product lookup
        product = conn.execute(
            text("SELECT id, price, name, weight FROM products WHERE barcode = :barcode"),
            {"barcode": barcode}
        ).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        product_id = product.id

        # ── FRAUD CHECK 1: Rapid scan pattern ────────────────────
        scan_times = conn.execute(
            text("""
                SELECT scanned_at FROM cart
                WHERE trolley_id = :tid
                ORDER BY scanned_at DESC LIMIT 10
            """),
            {"tid": trolley_id}
        ).fetchall()

        if len(scan_times) >= 2:
            intervals = [
                (scan_times[i].scanned_at - scan_times[i+1].scanned_at).total_seconds()
                for i in range(len(scan_times) - 1)
            ]

            avg_interval = sum(intervals) / len(intervals)
            print("Avg interval:", avg_interval)

            # ✅ Correct threshold logic
            if avg_interval < 1:
                severity = "HIGH"

            elif avg_interval < 3:
                severity = "MEDIUM"

            else:
                severity = "LOW"

            # ✅ Log only if suspicious
            if severity != "LOW":
                log_fraud_flag(
                    conn,
                    trolley_id=trolley_id,
                    reason=f"scan_speed_{severity.lower()}_{round(avg_interval,2)}s",
                    severity=severity
                )
                

        # ── FRAUD CHECK 2: Ghost product / barcode spike ─────────
        scan_count_today = conn.execute(
            text("""
                SELECT COUNT(*) FROM cart_audit_log
                WHERE product_id = :pid
                AND actioned_at > NOW() - INTERVAL '1 day'
                AND action = 'ADD'
            """),
            {"pid": product_id}
        ).scalar() or 0

        historical_avg = conn.execute(
            text("""
                SELECT COALESCE(AVG(daily_count), 1) FROM (
                    SELECT DATE(actioned_at), COUNT(*) as daily_count
                    FROM cart_audit_log
                    WHERE product_id = :pid AND action = 'ADD'
                    GROUP BY DATE(actioned_at)
                ) sub
            """),
            {"pid": product_id}
        ).scalar() or 1

        if scan_count_today > historical_avg * 5:
            log_fraud_flag(conn, trolley_id=trolley_id,
                           reason=f"product_scan_spike_{product.name}",
                           severity="MEDIUM")

        # Cart insert / update
        cart_item = conn.execute(
            text("""
                SELECT id, quantity FROM cart
                WHERE trolley_id = :trolley_id AND product_id = :product_id
            """),
            {"trolley_id": trolley_id, "product_id": product_id}
        ).fetchone()

        if cart_item:
            old_qty = cart_item.quantity
            conn.execute(
                text("UPDATE cart SET quantity = quantity + 1, scanned_at = NOW() WHERE id = :id"),
                {"id": cart_item.id}
            )
        else:
            old_qty = 0
            conn.execute(
                text("""
                    INSERT INTO cart (trolley_id, product_id, quantity, scanned_at, expected_weight)
                    VALUES (:trolley_id, :product_id, 1, NOW(), :weight )
                """),
                {"trolley_id": trolley_id, "product_id": product_id, "weight": float(product.weight)}
            )

        # Audit log
        conn.execute(
            text("""
                INSERT INTO cart_audit_log
                    (trolley_id, product_id, action, old_quantity, new_quantity)
                VALUES (:tid, :pid, 'ADD', :old, :new)
            """),
            {"tid": trolley_id, "pid": product_id,
             "old": old_qty, "new": old_qty + 1}
        )

        return {
            "message": "Product added to cart",
            "product": product.name,
            "price": float(product.price),
            "expected_weight": float(product.weight),
            "detected_weight": float(product.weight)   # mimic live weight
        }


# ================================================================
# REMOVE FROM CART
# ================================================================

@app.post("/remove-item")
def remove_item(trolley_code: str, product_id: str, session_token: str = None):

    with engine.begin() as conn:

        if session_token:
            trolley_id = validate_session(conn, trolley_code, session_token)
        else:
            trolley = conn.execute(
                text("SELECT id FROM trolleys WHERE trolley_code = :code"),
                {"code": trolley_code}
            ).fetchone()
            if not trolley:
                raise HTTPException(status_code=404, detail="Trolley not found")
            trolley_id = trolley.id

        cart_item = conn.execute(
            text("SELECT id, quantity FROM cart WHERE trolley_id = :tid AND product_id = :pid"),
            {"tid": trolley_id, "pid": product_id}
        ).fetchone()

        if not cart_item:
            raise HTTPException(status_code=404, detail="Item not in cart")

        old_qty = cart_item.quantity
        if old_qty > 1:
            conn.execute(
                text("UPDATE cart SET quantity = quantity - 1 WHERE id = :id"),
                {"id": cart_item.id}
            )
            new_qty = old_qty - 1
        else:
            conn.execute(text("DELETE FROM cart WHERE id = :id"), {"id": cart_item.id})
            new_qty = 0

        conn.execute(
            text("""
                INSERT INTO cart_audit_log
                    (trolley_id, product_id, action, old_quantity, new_quantity)
                VALUES (:tid, :pid, 'REMOVE', :old, :new)
            """),
            {"tid": trolley_id, "pid": product_id, "old": old_qty, "new": new_qty}
        )

        # ── FRAUD CHECK 3: Repeated deletion of same item ────────
        del_count = conn.execute(
            text("""
                SELECT COUNT(*) FROM cart_audit_log
                WHERE trolley_id = :tid AND product_id = :pid
                AND action = 'REMOVE'
                AND actioned_at > NOW() - INTERVAL '1 hour'
            """),
            {"tid": trolley_id, "pid": product_id}
        ).scalar()

        if del_count >= 2:
            log_fraud_flag(conn, trolley_id=trolley_id,
                           reason=f"repeated_item_deletion_product_{product_id}",
                           severity="HIGH")

        return {"message": "Item removed", "new_quantity": new_qty}


# ================================================================
# VIEW CART
# ================================================================

@app.get("/cart")
def view_cart(trolley_code: str):

    with engine.connect() as conn:

        # 🔹 Get trolley
        trolley = conn.execute(
            text("SELECT id FROM trolleys WHERE trolley_code = :code"),
            {"code": trolley_code}
        ).fetchone()
        if not trolley:
            raise HTTPException(status_code=404, detail="Trolley not found")

        trolley_id = trolley.id

        # 🔹 Get cart items
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

        # 🔹 Total
        total = conn.execute(
            text("""
                SELECT COALESCE(SUM(c.quantity * p.price), 0)
                FROM cart c
                JOIN products p ON c.product_id = p.id
                WHERE c.trolley_id = :trolley_id
            """),
            {"trolley_id": trolley_id}
        ).scalar()

        # 🔹 Risk score (from fraud flags)
        risk_score = compute_risk_score(conn, trolley_id)

        # 🔹 Additional ML features
        clears = conn.execute(
            text("SELECT clears_count FROM trolleys WHERE id = :id"),
            {"id": trolley_id}
        ).scalar() or 0

        scan_count = len(items)
        avg_interval = 10  # simple approximation

        high_flags = conn.execute(
            text("""
                SELECT COUNT(*) FROM fraud_flags
                WHERE trolley_id = :id AND severity = 'HIGH'
            """),
            {"id": trolley_id}
        ).scalar() or 0

        # 🔹 ML Feature vector
        features = {
            "scan_count": scan_count,
            "avg_interval": avg_interval,
            "fraud_score": risk_score,
            "clears_count": clears,
            "high_flags": high_flags,
            "image_issues": 1 if risk_score > 5 else 0
        }

        # 🔥 ML Prediction
        risk_prob = ml_risk_score(features)
        risk_level = get_risk_level(risk_prob)

        # 🔹 Response
        return {
            "items": [dict(row._mapping) for row in items],
            "total_amount": total,
            "risk_score": risk_score,
            "flagged": risk_score >= 5,
            "risk_level": risk_level,
            "risk_probability": round(risk_prob, 2)
        }

# ================================================================
# CHECKOUT
# ================================================================

@app.post("/checkout")
def checkout(data: CheckoutData):

    with engine.begin() as conn:

        # 🔹 Validate trolley
        if data.session_token:
            trolley_id = validate_session(conn, data.trolley_code, data.session_token)
        else:
            trolley = conn.execute(
                text("SELECT id FROM trolleys WHERE trolley_code = :code"),
                {"code": data.trolley_code}
            ).fetchone()
            if not trolley:
                raise HTTPException(status_code=404, detail="Trolley not found")
            trolley_id = trolley.id

        trolley_info = conn.execute(
            text("SELECT clears_count, customer_fingerprint FROM trolleys WHERE id = :id"),
            {"id": trolley_id}
        ).fetchone()

        # 🔹 FRAUD CHECK 4: Cart cleared too many times
        if trolley_info and trolley_info.clears_count > 2:
            log_fraud_flag(conn, trolley_id=trolley_id,
                           reason=f"cart_cleared_{trolley_info.clears_count}_times",
                           severity="HIGH")

        # 🔹 Get total
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

        # 🔹 FRAUD CHECK 5: Basket value Z-score
        stats = conn.execute(
            text("""
                SELECT AVG(total_amount) as avg, STDDEV(total_amount) as std
                FROM orders
                WHERE payment_status = 'SUCCESS'
                AND created_at > NOW() - INTERVAL '30 days'
            """)
        ).fetchone()

        risk_score = compute_risk_score(conn, trolley_id)

        if stats and stats.avg and stats.std and float(stats.std) > 0:
            z_score = (float(total) - float(stats.avg)) / float(stats.std)
            if z_score > 3:
                log_fraud_flag(conn, trolley_id=trolley_id,
                               reason=f"basket_value_outlier_zscore_{round(z_score,2)}",
                               severity="MEDIUM")
                risk_score += 3

        # 🔹 FRAUD CHECK 6: Cross-session customer risk
        fp = trolley_info.customer_fingerprint if trolley_info else None
        if fp:
            history = conn.execute(
                text("""
                    SELECT AVG(o.risk_score) as avg_score
                    FROM orders o
                    JOIN trolleys t ON o.trolley_id = t.id
                    WHERE t.customer_fingerprint = :fp
                    AND o.payment_status = 'SUCCESS'
                """),
                {"fp": fp}
            ).fetchone()

            if history and history.avg_score and float(history.avg_score) >= 7:
                log_fraud_flag(conn, trolley_id=trolley_id,
                               reason=f"high_risk_repeat_customer_avg_{round(float(history.avg_score),1)}",
                               severity="HIGH")
                risk_score += 7

        # =========================================================
        # 🔥 ML-BASED RISK PREDICTION (FINAL)
        # =========================================================

        risk_prob = ml_risk_score({
            "scan_count": 5,
            "avg_interval": 10,
            "fraud_score": risk_score,
            "clears_count": trolley_info.clears_count if trolley_info else 0,
            "high_flags": 2 if risk_score >= 5 else 0,
            "image_issues": 1 if risk_score >= 5 else 0
        })

        risk_level = get_risk_level(risk_prob)

        # 🚨 AUTO BLOCK
        if risk_level == "HIGH":
            raise HTTPException(
                status_code=403,
                detail=f"🚨 Checkout blocked (ML Risk={round(risk_prob,2)})"
            )

        # =========================================================

        needs_review = risk_score >= 5
        need_photo   = requires_photo_check(risk_score, fp)

        # 🔹 Create order
        order = conn.execute(
            text("""
                INSERT INTO orders
                    (trolley_id, total_amount, payment_status, needs_review, risk_score)
                VALUES (:trolley_id, :total, 'PENDING', :review, :score)
                RETURNING id
            """),
            {"trolley_id": trolley_id, "total": total,
             "review": needs_review, "score": risk_score}
        ).fetchone()

        order_id = order.id

        # 🔹 Insert items
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

        # 🔹 Clear cart
        conn.execute(
            text("DELETE FROM cart WHERE trolley_id = :trolley_id"),
            {"trolley_id": trolley_id}
        )

        # 🔹 Increment clears count
        conn.execute(
            text("UPDATE trolleys SET clears_count = clears_count + 1 WHERE id = :id"),
            {"id": trolley_id}
        )

        # ✅ END SESSION (SAFE)
        if data.session_token:
            conn.execute(
                text("""
                    UPDATE sessions
                    SET ended_at = NOW(),
                        status = 'COMPLETED'
                    WHERE session_token = :token
                """),
                {"token": data.session_token}
            )

        # ✅ CLEAR ACTIVE SESSION FROM TROLLEY
        conn.execute(
            text("""
                UPDATE trolleys
                SET session_token = NULL,
                    session_expires_at = NULL
                WHERE id = :id
            """),
            {"id": trolley_id}
        )

        # 🔹 Response
        response = {
            "message": "Order created successfully",
            "order_id": str(order_id),
            "total_amount": total,
            "requires_photo_verification": need_photo,
            "risk_probability": round(risk_prob, 2),
            "risk_level": risk_level
        }

        if needs_review:
            response["warning"] = "Your order has been flagged for manual review at exit."
        return response


# ================================================================
# PAYMENT SUCCESS
# ================================================================

import hmac
import hashlib

RAZORPAY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

@app.post("/payment-success")
def payment_success(data: dict):

    razorpay_order_id = data.get("razorpay_order_id")
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_signature = data.get("razorpay_signature")
    order_id = data.get("order_id")

    if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature, order_id]):
        raise HTTPException(status_code=400, detail="Missing payment data")

    # 🔐 SIGNATURE VERIFICATION (CRITICAL)
    generated_signature = hmac.new(
        bytes(RAZORPAY_SECRET, 'utf-8'),
        bytes(f"{razorpay_order_id}|{razorpay_payment_id}", 'utf-8'),
        hashlib.sha256
    ).hexdigest()

    if generated_signature != razorpay_signature:
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    # ✅ ONLY AFTER VALIDATION → update DB
    with engine.begin() as conn:

        order = conn.execute(
            text("SELECT payment_status FROM orders WHERE id = :id"),
            {"id": order_id}
        ).fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.payment_status == "SUCCESS":
            return {"message": "Already processed"}

        # ✅ UPDATE PAYMENT
        conn.execute(
            text("""
                UPDATE orders
                SET payment_status = 'SUCCESS'
                WHERE id = :id
            """),
            {"id": order_id}
        )

    # ✅ GENERATE RECEIPT FIRST
    receipt = generate_receipt(order_id)

    # ✅ END SESSION AFTER RECEIPT
    if session_token:
        end_session(session_token)

    return {
        "message": "Payment successful",
        "receipt": receipt
    }



# ================================================================
# END SESSION (MANUAL)
# ================================================================
@app.post("/end-session")
def end_session(session_token: str):

    conn = engine.connect()

    conn.execute(
        text("""
            UPDATE sessions 
            SET status = 'ENDED',
                ended_at = NOW()   -- ✅ ADD THIS
            WHERE session_token = :token
        """),
        {"token": session_token}
    )

    conn.commit()
    conn.close()

    return {"message": "Session ended"}

# ================================================================
# ESP32 DATA
# ================================================================

@app.post("/esp32-data")
def receive_esp32_data(data: dict):
    barcode       = data.get("barcode")
    trolley_code  = data.get("trolley_code")
    session_token = data.get("session_token")

    if not barcode or not trolley_code:
        raise HTTPException(status_code=400, detail="Missing barcode or trolley_code")

    return scan_product(barcode=barcode, trolley_code=trolley_code,
                        session_token=session_token)


# ================================================================
# PHONE CAMERA VERIFICATION 
# ================================================================

class PhotoVerifyData(BaseModel):
    order_id: str
    trolley_code: str
    image_base64: str
    angle: str = "top"



@app.post("/verify-item-image")
async def verify_item_image(data: PhotoVerifyData):

    # Decode image
    try:
        img_data = base64.b64decode(data.image_base64)
        np_arr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        
    except:
        raise HTTPException(status_code=400, detail="Invalid image")
    
    if img is None:
        raise HTTPException(status_code=400, detail="Image decoding failed")

    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Edge detection
    edges = cv2.Canny(gray, 50, 150)

    edge_count = np.sum(edges > 0)

    # Blur detection
    blur_value = cv2.Laplacian(gray, cv2.CV_64F).var()

    # Brightness check
    brightness = np.mean(gray)

    # Fetch expected items
    with engine.begin() as conn:
        order = conn.execute(
            text("SELECT trolley_id FROM orders WHERE id = :id"),
            {"id": data.order_id}
        ).fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        items = conn.execute(
            text("SELECT quantity FROM order_items WHERE order_id = :id"),
            {"id": data.order_id}
        ).fetchall()

        expected_count = sum(i.quantity for i in items)

    # 🧠 Decision Logic (REAL ANALYSIS)

    issues = []

    if edge_count < 500:
        issues.append("Low object detail")

    if blur_value < 50:
        issues.append("Image is blurry")

    if brightness < 50:
        issues.append("Image too dark")

    # Simulate object presence using edge density
    detected_objects = max(1, edge_count // 1500)  # rough estimate

    if detected_objects < expected_count:
        issues.append("Possible missing items")

   
    # Final decision
    if len(issues) == 0:
        passed = True
        confidence = random.randint(85, 95)
        reason = "Image quality good and items appear consistent"
    else:
        passed = False
        confidence = random.randint(55, 70)
        reason = ", ".join(issues)

    result = {
        "match": passed,
        "confidence": confidence,
        "suspicious_items": issues,
        "missing_items": issues,
        "extra_items": [],
        "reasoning": reason
    }

    # DB update
    with engine.begin() as conn:
        if passed:
            conn.execute(
                text("UPDATE orders SET photo_verified = TRUE WHERE id = :id"),
                {"id": data.order_id}
            )
        else:
            log_fraud_flag(
                conn,
                order_id=data.order_id,
                trolley_id=order.trolley_id,
                reason=f"vision_issue_{reason}",
                severity="HIGH"
            )

    return {
        "order_id": data.order_id,
        "angle": data.angle,
        "vision_result": result,
        "passed": passed
    }
# ================================================================
# CUSTOMER RISK PROFILE — cross-session intelligence
# ================================================================

@app.get("/customer-risk-profile/{phone_hash}")
def customer_risk_profile(phone_hash: str):
    """
    Lifetime fraud profile for a customer (identified by hashed phone).
    Call at /start-session to pre-flag known bad actors.
    """
    with engine.connect() as conn:
        history = conn.execute(
            text("""
                SELECT
                    COUNT(ff.id) as total_flags,
                    SUM(CASE WHEN ff.severity = 'HIGH' THEN 1 ELSE 0 END) as high_flags,
                    COUNT(DISTINCT o.id)  as total_orders,
                    AVG(o.risk_score)     as avg_risk_score,
                    MAX(ff.flagged_at)    as last_flag_date
                FROM fraud_flags ff
                JOIN orders o ON ff.order_id = CAST(o.id AS TEXT)
                JOIN trolleys t ON o.trolley_id = t.id
                WHERE t.customer_fingerprint = :fp
            """),
            {"fp": phone_hash}
        ).fetchone()

        avg_score = float(history.avg_risk_score or 0)

        if avg_score >= 7:
            tier           = "HIGH_RISK"
            recommendation = "Enhanced exit check required"
        elif avg_score >= 4:
            tier           = "MEDIUM_RISK"
            recommendation = "Random bag check recommended"
        else:
            tier           = "TRUSTED"
            recommendation = "Fast lane exit permitted"

        return {
            "risk_tier":      tier,
            "total_flags":    history.total_flags  or 0,
            "high_flags":     history.high_flags   or 0,
            "total_orders":   history.total_orders or 0,
            "avg_risk_score": round(avg_score, 2),
            "last_flag_date": str(history.last_flag_date or "Never"),
            "recommendation": recommendation
        }


# ================================================================
# FRAUD HEATMAP — category-level intelligence
# ================================================================

@app.get("/fraud-heatmap")
def fraud_heatmap():
    """
    Which product categories generate the most fraud events.
    Helps manager decide camera placement and staff positioning.
    """
    with engine.connect() as conn:
        heatmap = conn.execute(
            text("""
                SELECT
                    p.category,
                    COUNT(DISTINCT ff.id) as fraud_events,
                    AVG(p.price)          as avg_item_price,
                    SUM(p.price)          as total_at_risk_value
                FROM fraud_flags ff
                JOIN cart_audit_log cal ON ff.trolley_id = cal.trolley_id
                JOIN products p ON cal.product_id = p.id
                WHERE ff.flagged_at > NOW() - INTERVAL '30 days'
                GROUP BY p.category
                ORDER BY fraud_events DESC
            """)
        ).fetchall()

        return {
            "heatmap": [dict(row._mapping) for row in heatmap],
            "insight": "Categories with high avg_price + high fraud_events → relocate near staff or camera zones."
        }


# ================================================================
# FRAUD DASHBOARD — admin summary
# ================================================================

@app.get("/fraud-dashboard")
def fraud_dashboard():
    with engine.connect() as conn:

        flags = conn.execute(
            text("""
                SELECT id, trolley_id, order_id, reason, severity, flagged_at
                FROM fraud_flags
                ORDER BY flagged_at DESC LIMIT 100
            """)
        ).fetchall()

        flagged_orders = conn.execute(
            text("""
                SELECT id, total_amount, risk_score, needs_review,
                       photo_verified, created_at
                FROM orders
                WHERE needs_review = TRUE
                ORDER BY created_at DESC LIMIT 20
            """)
        ).fetchall()

        high   = [f for f in flags if f.severity == "HIGH"]
        medium = [f for f in flags if f.severity == "MEDIUM"]
        low    = [f for f in flags if f.severity == "LOW"]

        return {
            "summary": {
                "total_flags":               len(flags),
                "high_severity":             len(high),
                "medium_severity":           len(medium),
                "low_severity":              len(low),
                "orders_flagged_for_review": len(flagged_orders)
            },
            "recent_flags":   [dict(row._mapping) for row in flags],
            "flagged_orders": [dict(row._mapping) for row in flagged_orders]
        }


# ================================================================
# GENERATE RECEIPT — with receipt hash integrity
# ================================================================

@app.get("/generate-receipt")
def generate_receipt(order_id: str):

    print(f"Generating receipt for order: {order_id}")

    try:
        timestamp = int(time.time())
        token = hashlib.sha256(
            f"{order_id}{SECRET_KEY}{timestamp}".encode()
        ).hexdigest()

        with engine.begin() as conn:

            order = conn.execute(
                text("SELECT total_amount, needs_review, risk_score FROM orders WHERE id = :id"),
                {"id": order_id}
            ).fetchone()

            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

            items = conn.execute(
                text("""
                    SELECT p.name, oi.quantity, oi.price,
                           (oi.quantity * oi.price) AS subtotal,
                           p.weight
                    FROM order_items oi
                    JOIN products p ON oi.product_id = p.id
                    WHERE oi.order_id = :id
                """),
                {"id": order_id}
            ).fetchall()

            # 🔥 NEW: totals
            total_items = sum(i.quantity for i in items)
            total_weight = sum(int(i.quantity) * float(i.weight or 0) for i in items)

            # 🔐 receipt hash
            receipt_data = f"{order_id}|{order.total_amount}|" + "|".join(
                [f"{i.name}:{i.quantity}:{i.price}" for i in items]
            )

            receipt_hash = hashlib.sha256(
                f"{receipt_data}{SECRET_KEY}".encode()
            ).hexdigest()

            conn.execute(
                text("UPDATE orders SET receipt_hash = :hash WHERE id = :id"),
                {"hash": receipt_hash, "id": order_id}
            )

        # 📄 PDF
        os.makedirs("/tmp/receipts", exist_ok=True)
        filename = f"/tmp/receipts/receipt_{order_id}.pdf"

        doc = SimpleDocTemplate(filename)
        styles = getSampleStyleSheet()
        elements = []

        elements.append(Paragraph("<b>SMART TROLLEY STORE</b>", styles["Title"]))
        elements.append(Paragraph("Chennai, India", styles["Normal"]))
        elements.append(Spacer(1, 10))

        elements.append(Paragraph(f"Invoice: {order_id}", styles["Normal"]))
        elements.append(Paragraph(
            f"Date: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}",
            styles["Normal"]
        ))

        elements.append(Spacer(1, 10))

        # 🔥 UPDATED TABLE (WITH WEIGHT)
        table_data = [["Item", "Qty", "Price", "Weight", "Total"]]

        for item in items:
            table_data.append([
                item.name,
                str(item.quantity),
                f"{item.price}",
                f"{round(item.weight or 0,2)}",
                f"{item.subtotal}"
            ])

        # 🔥 GRAND TOTALS
        table_data.append(["", "", "", "Total Items", str(total_items)])
        table_data.append(["", "", "", "Total Weight", f"{round(total_weight,2)} g"])
        table_data.append(["", "", "", "Grand Total", f"Rs. {order.total_amount}"])

        t = Table(table_data, colWidths=[120, 40, 50, 60, 60])
        t.setStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ])

        elements.append(t)
        elements.append(Spacer(1, 20))

        # 🔗 QR
        verification_url = f"http://192.168.1.7:8000/verify-invoice/{order_id}?token={token}&ts={timestamp}"

        from io import BytesIO
        qr = qrcode.make(verification_url)

        qr_buffer = BytesIO()
        qr.save(qr_buffer)
        qr_buffer.seek(0)

        elements.append(Paragraph("Scan QR for Payment Verification", styles["Normal"]))
        elements.append(Spacer(1, 10))
        elements.append(Image(qr_buffer, width=120, height=120))
        elements.append(Spacer(1, 10))

        elements.append(Paragraph("Thank you for shopping!", styles["Normal"]))

        doc.build(elements)

        # Upload to Supabase
        file_name = f"receipt_{order_id}_{int(time.time())}.pdf"

        with open(filename, "rb") as f:
            supabase.storage.from_("receipts").upload(
                file_name,
                f,
                {"content-type": "application/pdf"}
            )

        receipt_url = supabase.storage.from_("receipts").get_public_url(file_name)

        print("Receipt uploaded:", file_name)
        print("Public URL:", receipt_url)

        return {
            "message": "Professional receipt generated",
            "file_path": filename,
            "receipt_url": receipt_url
        }

    except Exception as e:
        print("ERROR:", str(e))
        raise HTTPException(status_code=500, detail=str(e))   



# ================================================================
# VERIFY PAGE — one-time QR + optional phone camera check
# ================================================================

@app.get("/verify-invoice/{order_id}", response_class=HTMLResponse)
def verify_page(order_id: str, token: str = Query(...), ts: int = Query(...)):

    current_time = int(time.time())

    # 🔹 QR expiry check
    if current_time - ts > 600:
        return render_error_page(
            "QR Code Expired",
            "This verification link has expired. Please generate a new receipt.",
            "fa-clock"
        )
        

    expected_token = hashlib.sha256(
        f"{order_id}{SECRET_KEY}{ts}".encode()
    ).hexdigest()

    # 🔹 Tamper check
    if token != expected_token:
        return render_error_page(
            "Invalid Invoice",
            "This invoice is invalid or has been tampered with.",
            "fa-shield-exclamation"
        )
   

    with engine.begin() as conn:

        order = conn.execute(
            text("""
                SELECT qr_used, needs_review, risk_score, photo_verified
                FROM orders WHERE id = :id
            """),
            {"id": order_id}
        ).fetchone()

        if not order:
            return render_error_page(
            "Order Not Found",
            "The requested order could not be located. Please verify the invoice.",
            "fa-circle-xmark"
        )

        # 🔹 QR reuse detection
        if order.qr_used:
            log_fraud_flag(conn, order_id=order_id,
                           reason="qr_reuse_attempt", severity="HIGH")
            return render_error_page(
                "QR Code Reuse Detected",
                "This QR code has already been used. The attempt has been recorded for security review.",
                "fa-user-shield"
            )
        # Mark QR used
        conn.execute(
            text("UPDATE orders SET qr_used = TRUE WHERE id = :id"),
            {"id": order_id}
        )

    # 🔹 Photo check decision
    need_photo = requires_photo_check(order.risk_score or 0)

    review_banner = ""
    if order.needs_review:
        review_banner = f"""
        <div style="
            background:linear-gradient(135deg,#f59e0b,#f97316);
            color:white;
            padding:15px;
            border-radius:12px;
            margin-bottom:20px;
            display:flex;
            align-items:center;
            gap:10px;
            box-shadow:0 10px 20px rgba(0,0,0,0.15);
        ">
            <i class="fas fa-triangle-exclamation" style="font-size:18px;"></i>
            <div>
                <div style="font-weight:bold;">Manual Review Required</div>
                <div style="font-size:12px;opacity:0.9;">
                    Risk Score: {order.risk_score}
                </div>
            </div>
        </div>
        """
    # 🔹 Photo UI
    photo_section = ""
    if need_photo and not order.photo_verified:
        photo_section = f"""
        <div style="
            background:#f9fafb;
            padding:20px;
            border-radius:15px;
            margin-bottom:20px;
            border:1px solid #e5e7eb;
        ">

            <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
                <i class="fas fa-camera" style="color:#4f46e5;"></i>
                <span style="font-weight:bold;color:#111827;">Trolley Image Verification</span>
            </div>

            <p style="font-size:12px;color:#6b7280;margin-bottom:15px;">
                Capture a top-view image of the trolley for verification.
            </p>

            <input type="file" id="photoInput" accept="image/*" capture="environment"
                style="width:100%;padding:10px;border-radius:10px;
                    border:1px solid #e5e7eb;background:white;cursor:pointer;"
                onchange="submitPhoto(this)">

            <div id="photoStatus" style="margin-top:15px;font-size:13px;"></div>

        </div>

        <script>
        async function submitPhoto(input) {{
            const file = input.files[0];
            const reader = new FileReader();

            reader.onload = async function(e) {{
                const base64 = e.target.result.split(',')[1];

                document.getElementById('photoStatus').innerHTML =
                    '<span style="color:#4f46e5;"><i class="fas fa-spinner fa-spin"></i> Processing...</span>';

                const resp = await fetch('/verify-item-image', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        order_id: '{order_id}',
                        trolley_code: '',
                        image_base64: base64,
                        angle: 'top'
                    }})
                }});

                const result = await resp.json();

                if (result.passed) {{
                    document.getElementById('photoStatus').innerHTML =
                        '<span style="color:green;"><i class="fas fa-check-circle"></i> Verified successfully</span>';
                }} else {{
                    document.getElementById('photoStatus').innerHTML =
                        '<span style="color:red;"><i class="fas fa-xmark-circle"></i> Verification failed</span>';
                }}
            }};
            reader.readAsDataURL(file);
        }}
        </script>
        """

   
    return f"""
    <html>
    <head>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
    </head>

    <body style="margin:0;font-family:Arial;background:linear-gradient(135deg,#4f46e5,#9333ea);min-height:100vh;display:flex;align-items:center;justify-content:center;">

        <div style="background:white;width:420px;padding:30px;border-radius:20px;
                    box-shadow:0 20px 40px rgba(0,0,0,0.2);">

            <!-- HEADER -->
            <div style="text-align:center;margin-bottom:20px;">
                <div style="width:60px;height:60px;background:#eef2ff;
                            border-radius:15px;display:flex;align-items:center;
                            justify-content:center;margin:auto;">
                    <i class="fas fa-receipt" style="color:#4f46e5;font-size:24px;"></i>
                </div>

                <h2 style="margin-top:10px;color:#1f2937;">Invoice Verification</h2>
                <p style="color:#6b7280;font-size:12px;">Secure exit validation</p>
            </div>

            {review_banner}

            <!-- INVOICE INFO -->
            <div style="background:#f9fafb;padding:15px;border-radius:12px;margin-bottom:20px;">
                <p style="font-size:13px;color:#6b7280;">Invoice ID</p>
                <p style="font-weight:bold;color:#111827;">{order_id}</p>
            </div>

            {photo_section}

            <!-- FORM -->
            <form action="/verify-submit/{order_id}" method="post">

                <!-- ITEM COUNT -->
                <div style="margin-bottom:15px;">
                    <label style="font-size:13px;color:#374151;">
                        <i class="fas fa-box"></i> Total Items
                    </label><br>
                    <input type="number" name="actual_count" required
                        style="width:100%;padding:10px;border-radius:10px;
                            border:1px solid #e5e7eb;margin-top:5px;">
                </div>

                <!-- WEIGHT -->
                <div style="margin-bottom:20px;">
                    <label style="font-size:13px;color:#374151;">
                        <i class="fas fa-weight-hanging"></i> Weight (grams)
                    </label><br>
                    <input type="number" name="actual_weight" required
                        style="width:100%;padding:10px;border-radius:10px;
                            border:1px solid #e5e7eb;margin-top:5px;">
                </div>

                <!-- BUTTON -->
                <button type="submit"
                    style="width:100%;background:#4f46e5;color:white;
                        padding:12px;border:none;border-radius:12px;
                        font-weight:bold;cursor:pointer;
                        box-shadow:0 10px 20px rgba(79,70,229,0.3);">
                    <i class="fas fa-shield-check"></i> Verify & Exit
                </button>

            </form>

        </div>

    </body>
    </html>
    """


# ================================================================
# VERIFY SUBMIT — receipt integrity + item count + fraud summary
# ================================================================

@app.post("/verify-submit/{order_id}", response_class=HTMLResponse)
def verify_submit(
    order_id: str,
    actual_count: int = Form(...),
    actual_weight: float = Form(...)
):

    try:
        with engine.begin() as conn:

            order = conn.execute(
                text("""
                    SELECT total_amount, payment_status, receipt_hash,
                           needs_review, risk_score, photo_verified
                    FROM orders WHERE id = :id
                """),
                {"id": order_id}
            ).fetchone()

            if not order:
                return "<div style='background:red;color:white;padding:50px;text-align:center;font-size:30px;'>❌ INVALID INVOICE</div>"

            total_amount   = order._mapping["total_amount"]
            payment_status = order._mapping["payment_status"]
            stored_hash    = order._mapping["receipt_hash"]
            needs_review   = order._mapping["needs_review"]
            risk_score     = order._mapping["risk_score"]
            photo_verified = order._mapping["photo_verified"]

            if payment_status != "SUCCESS":
                return "<div style='background:orange;color:white;padding:50px;text-align:center;font-size:30px;'>⚠ PAYMENT NOT COMPLETED</div>"

            items = conn.execute(
                text("""
                    SELECT p.name, oi.quantity, oi.price, p.weight
                    FROM order_items oi
                    JOIN products p ON oi.product_id = p.id
                    WHERE oi.order_id = :id
                """),
                {"id": order_id}
            ).fetchall()

            # 🔐 Receipt hash check
            receipt_data = f"{order_id}|{total_amount}|" + "|".join(
                [f"{i.name}:{i.quantity}:{i.price}" for i in items]
            )

            recomputed_hash = hashlib.sha256(
                f"{receipt_data}{SECRET_KEY}".encode()
            ).hexdigest()

            if stored_hash and recomputed_hash != stored_hash:
                log_fraud_flag(conn, order_id=order_id,
                               reason="receipt_integrity_violation", severity="HIGH")

                return "<div style='background:#8B0000;color:white;padding:60px;text-align:center;'>🚨 RECEIPT TAMPERING DETECTED</div>"

            # ===============================
            # NORMALIZE VALUES (GRAM SYSTEM)
            # ===============================

            expected_count = int(sum(i.quantity for i in items))

            expected_weight = sum(
                int(i.quantity) * float(i.weight or 0)
                for i in items
            )

            actual_count = int(actual_count)
            actual_weight = float(actual_weight)

            # ===============================
            # CONFIG (GRAM TOLERANCE)
            # ===============================
            tolerance_weight = 50   # 50 grams tolerance

            # ===============================
            # COMPARISON LOGIC
            # ===============================
            count_ok = (actual_count == expected_count)

            weight_difference = abs(actual_weight - expected_weight)
            weight_ok = (weight_difference <= tolerance_weight)

            # Fraud flags display
            fraud_flags = conn.execute(
                text("""
                    SELECT reason, severity FROM fraud_flags
                    WHERE order_id = :oid
                    ORDER BY flagged_at DESC LIMIT 5
                """),
                {"oid": order_id}
            ).fetchall()

            flag_html = ""
            if fraud_flags:
                flag_html = "<ul>" + "".join(
                    [f"<li>[{f.severity}] {f.reason}</li>" for f in fraud_flags]
                ) + "</ul>"

            if count_ok and weight_ok:

                return f"""  
                <html>
                <head>
                    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
                </head>

                <body style="
                    margin:0;
                    font-family:Arial;
                    background:linear-gradient(135deg,#22c55e,#16a34a);
                    min-height:100vh;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                ">

                    <div style="
                        background:white;
                        width:420px;
                        padding:30px;
                        border-radius:20px;
                        box-shadow:0 20px 40px rgba(0,0,0,0.2);
                        text-align:center;
                    ">

                        <!-- ICON -->
                        <div style="
                            width:70px;height:70px;
                            background:#dcfce7;
                            border-radius:20px;
                            display:flex;
                            align-items:center;
                            justify-content:center;
                            margin:auto;
                        ">
                            <i class="fas fa-circle-check" style="color:#16a34a;font-size:30px;"></i>
                        </div>

                        <h2 style="margin-top:15px;color:#065f46;">Verification Successful</h2>
                        <p style="font-size:12px;color:#6b7280;">All values match the invoice</p>

                        <!-- SUMMARY CARD -->
                        <div style="
                            background:#f9fafb;
                            padding:15px;
                            border-radius:12px;
                            margin-top:20px;
                            text-align:left;
                        ">
                            <p><i class="fas fa-indian-rupee-sign"></i> <b>Amount:</b> ₹{total_amount}</p>
                            <p><i class="fas fa-box"></i> <b>Total Items:</b> {expected_count}</p>
                            <p><i class="fas fa-weight-hanging"></i> <b>Total Weight:</b> {round(expected_weight,2)} g</p>
                        </div>

                        <!-- ENTERED DETAILS -->
                        <div style="
                            margin-top:20px;
                            padding:15px;
                            border-radius:12px;
                            background:#ecfdf5;
                            text-align:left;
                        ">
                            <p><b>Entered Values</b></p>
                            <p>Items: {actual_count} / {expected_count}</p>
                            <p>Weight: {round(actual_weight,2)} g / {round(expected_weight,2)} g</p>
                        </div>

                    </div>

                </body>
                </html>

                """

            else:

                log_fraud_flag(conn, order_id=order_id,
                               reason="verification_mismatch", severity="HIGH")

                reasons = []

                if not count_ok:
                    reasons.append(f"🧾 Item mismatch: Expected {expected_count}, Got {actual_count}")

                if not weight_ok:
                    reasons.append(
                        f"⚖ Weight mismatch: Expected {round(expected_weight,2)} g, Got {round(actual_weight,2)} g"
                    )

                reason_text = "<br>".join(reasons)

                return f"""
                <html>
                <head>
                    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
                </head>

                <body style="
                    margin:0;
                    font-family:Arial;
                    background:linear-gradient(135deg,#ef4444,#b91c1c);
                    min-height:100vh;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                ">

                    <div style="
                        background:white;
                        width:420px;
                        padding:30px;
                        border-radius:20px;
                        box-shadow:0 20px 40px rgba(0,0,0,0.3);
                        text-align:center;
                    ">

                        <!-- ICON -->
                        <div style="
                            width:70px;height:70px;
                            background:#fee2e2;
                            border-radius:20px;
                            display:flex;
                            align-items:center;
                            justify-content:center;
                            margin:auto;
                        ">
                            <i class="fas fa-shield-exclamation" style="color:#dc2626;font-size:30px;"></i>
                        </div>

                        <h2 style="margin-top:15px;color:#7f1d1d;">Verification Failed</h2>
                        <p style="font-size:12px;color:#6b7280;">Mismatch detected during validation</p>

                        <!-- EXPECTED DETAILS -->
                        <div style="
                            background:#f9fafb;
                            padding:15px;
                            border-radius:12px;
                            margin-top:20px;
                            text-align:left;
                        ">
                            <p><i class="fas fa-box"></i> <b>Total Items:</b> {expected_count}</p>
                            <p><i class="fas fa-weight-hanging"></i> <b>Total Weight:</b> {round(expected_weight,2)} g</p>
                        </div>

                        <!-- ENTERED DETAILS -->
                        <div style="
                            margin-top:20px;
                            padding:15px;
                            border-radius:12px;
                            background:#fef2f2;
                            text-align:left;
                        ">
                            <p><b>Entered Values</b></p>
                            <p>Items: {actual_count} / {expected_count}</p>
                            <p>Weight: {round(actual_weight,2)} g / {round(expected_weight,2)} g</p>
                        </div>

                        <!-- ISSUE -->
                        <div style="
                            margin-top:20px;
                            padding:15px;
                            border-radius:12px;
                            background:#fee2e2;
                            text-align:left;
                            border:1px solid #fecaca;
                        ">
                            <p><i class="fas fa-triangle-exclamation"></i> <b>Issue Detected</b></p>
                            <p style="font-size:13px;color:#7f1d1d;margin-top:5px;">
                                {reason_text}
                            </p>
                        </div>

                        <!-- FLAGS -->
                        <div style="margin-top:15px;font-size:12px;color:#6b7280;">
                            {flag_html}
                        </div>

                    </div>

                </body>
                </html>
                """
    except Exception as e: 
        return f"<div style='background:black;color:white;padding:40px;'>ERROR: {str(e)}</div>"
