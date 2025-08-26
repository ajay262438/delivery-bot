import os
import sqlite3
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from twilio.rest import Client

# ---------------------------
# Config
# ---------------------------
DB_PATH = os.getenv("DB_PATH", "database/deliveries.db")

# Twilio Credentials (use env vars for safety, hardcoded here for testing)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")  # your Twilio phone number

app = FastAPI(title="Delivery Bot Backend", version="1.1.0")


# ---------------------------
# DB helpers
# ---------------------------
def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE,
            pickup_location TEXT,
            drop_location TEXT,
            customer_contact TEXT,
            status TEXT,
            target_lat REAL,
            target_lon REAL,
            created_at TEXT,
            updated_at TEXT
        )
        """)
        conn.commit()

def _conn():
    _ensure_db()
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

@app.on_event("startup")
def on_startup():
    _ensure_db()


# ---------------------------
# Schemas
# ---------------------------
class DeliveryCreate(BaseModel):
    order_id: str = Field(..., min_length=1)
    pickup_location: str = Field(..., min_length=1)
    drop_location: str = Field(..., min_length=1)
    customer_contact: str = Field(..., min_length=6)

class LocationUpdate(BaseModel):
    lat: float
    lon: float


# ---------------------------
# SMS Helper (Twilio)
# ---------------------------
def send_sms(to_number: str, message: str):
    """Send SMS using Twilio API."""
    try:
        client = Client(TWILIO_SID, TWILIO_AUTH)
        msg = client.messages.create(
            body=message,
            from_=TWILIO_NUMBER,
            to=to_number
        )
        print(f"✅ SMS sent to {to_number}, SID: {msg.sid}")
        return True
    except Exception as e:
        print(f"❌ SMS Error: {e}")
        return False


# ---------------------------
# Routes
# ---------------------------
@app.get("/")
def root():
    return {"message": "Delivery Bot Backend + SQLite + Twilio SMS Running 🚀"}


@app.post("/create_delivery")
def create_delivery(payload: DeliveryCreate):
    """Create (or upsert) a delivery row when QR is scanned."""
    now = datetime.utcnow().isoformat()

    with _conn() as c:
        c.execute("""
            INSERT INTO deliveries (order_id, pickup_location, drop_location, customer_contact, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'created', ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                pickup_location=excluded.pickup_location,
                drop_location=excluded.drop_location,
                customer_contact=excluded.customer_contact,
                updated_at=excluded.updated_at
        """, (payload.order_id, payload.pickup_location, payload.drop_location, payload.customer_contact, now, now))
        c.commit()

        row = c.execute("SELECT * FROM deliveries WHERE order_id=?", (payload.order_id,)).fetchone()

    # 🔔 Send SMS to customer with location-sharing link
    SERVER_URL = os.getenv("SERVER_URL")  # update
    message = f"Your parcel has been received!\nOrder ID: {row['order_id']}\nPlease share your location: {SERVER_URL}/share/{row['order_id']}"
    send_sms(row["customer_contact"], message)

    return {
        "status": "success",
        "message": "Delivery task created ✅ & SMS sent",
        "order_id": row["order_id"],
        "pickup": row["pickup_location"],
        "drop": row["drop_location"],
        "customer_contact": row["customer_contact"],
        "db_id": row["id"],
        "current_status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/deliveries")
def list_deliveries():
    """List all deliveries (latest first)."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/deliveries/{order_id}")
def get_delivery(order_id: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM deliveries WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="order not found")
    return dict(row)


@app.post("/deliveries/{order_id}/location")
def set_target_location(order_id: str, loc: LocationUpdate):
    """Save customer's target GPS once they share it."""
    with _conn() as c:
        row = c.execute("SELECT * FROM deliveries WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="order not found")

        c.execute("""
            UPDATE deliveries
            SET target_lat=?, target_lon=?, status='location_received', updated_at=?
            WHERE order_id=?
        """, (loc.lat, loc.lon, datetime.utcnow().isoformat(), order_id))
        c.commit()

    # Confirmation SMS
    message = f"✅ Delivery Bot received your location! Order ID: {order_id}"
    send_sms(row["customer_contact"], message)

    return {"status": "ok", "order_id": order_id, "lat": loc.lat, "lon": loc.lon, "current_status": "location_received"}


@app.post("/deliveries/{order_id}/status/{new_status}")
def update_status(order_id: str, new_status: str):
    """Update status manually (created/in_progress/completed/failed/etc.)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM deliveries WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="order not found")

        c.execute("""
            UPDATE deliveries SET status=?, updated_at=? WHERE order_id=?
        """, (new_status, datetime.utcnow().isoformat(), order_id))
        c.commit()

    # 🔔 Status SMS
    if new_status == "completed":
        send_sms(row["customer_contact"], f"✅ Your parcel (Order {order_id}) has been delivered!")
    elif new_status == "failed":
        send_sms(row["customer_contact"], f"⚠️ Delivery failed for Order {order_id}. Please contact support.")

    return {"status": "ok", "order_id": order_id, "current_status": new_status}


# ---------------------------
# Location Share Page
# ---------------------------
@app.get("/share/{order_id}", response_class=HTMLResponse)
def share_page(order_id: str):
    return f"""
    <html>
    <head><title>Share Location</title></head>
    <body>
        <h2>📦 Sharing Location for Order {order_id}...</h2>
        <p id="status">⏳ Requesting your location...</p>

        <script>
        window.onload = function() {{
            if (navigator.geolocation) {{
                navigator.geolocation.getCurrentPosition(function(pos) {{
                    fetch('/deliveries/{order_id}/location', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ lat: pos.coords.latitude, lon: pos.coords.longitude }})
                    }}).then(res => res.text())
                      .then(data => {{
                          window.location.href = "/thankyou/{order_id}";
                      }}).catch(err => {{
                          document.getElementById('status').innerText = "❌ Error: " + err;
                      }});
                }}, function(error) {{
                    document.getElementById('status').innerText = "❌ Permission denied or error: " + error.message;
                }});
            }} else {{
                document.getElementById('status').innerText = "❌ Geolocation not supported by this browser.";
            }}
        }}
        </script>
    </body>
    </html>
    """

@app.get("/thankyou/{order_id}", response_class=HTMLResponse)
def thank_you(order_id: str):
    return f"""
    <html>
    <head>
        <title>Location Received!</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                text-align: center; 
                padding: 40px 20px; 
                background-color: #f4f7f6;
                color: #333;
            }}
            h2 {{ 
                color: #28a745; /* A nice green color */
            }}
            p {{ 
                font-size: 1.1em; 
                line-height: 1.6;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                background-color: #ffffff;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 4px 8px rgba(0,0,0,0.1);
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>✅ Location Received!</h2>
            <p>The delivery bot has your location for Order <b>{order_id}</b> and is on the way to deliver your goods.</p>
            <p>Thank you!</p>
        </div>
    </body>
    </html>
    """




