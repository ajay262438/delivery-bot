import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from twilio.rest import Client
import sqlalchemy

# ---------------------------
# Config
# ---------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")

app = FastAPI(title="Delivery Bot Backend", version="1.5.0") # Final Version
engine = sqlalchemy.create_engine(DATABASE_URL)

# ---------------------------
# DB helpers
# ---------------------------
def _ensure_db():
    """Use SQLAlchemy to create the table if it doesn't exist."""
    metadata = sqlalchemy.MetaData()
    sqlalchemy.Table('deliveries', metadata,
        sqlalchemy.Column('id', sqlalchemy.Integer, primary_key=True),
        sqlalchemy.Column('order_id', sqlalchemy.String, unique=True, index=True),
        sqlalchemy.Column('pickup_location', sqlalchemy.String),
        sqlalchemy.Column('drop_location', sqlalchemy.String),
        sqlalchemy.Column('customer_contact', sqlalchemy.String),
        sqlalchemy.Column('status', sqlalchemy.String),
        sqlalchemy.Column('target_lat', sqlalchemy.Float),
        sqlalchemy.Column('target_lon', sqlalchemy.Float),
        sqlalchemy.Column('created_at', sqlalchemy.String),
        sqlalchemy.Column('updated_at', sqlalchemy.String),
    )
    metadata.create_all(engine)

def _conn():
    """Get a connection from the SQLAlchemy engine pool."""
    return engine.connect()

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
    if not all([TWILIO_SID, TWILIO_AUTH, TWILIO_NUMBER]):
        print("❌ SMS Error: Twilio credentials are not fully configured.")
        return False
    try:
        client = Client(TWILIO_SID, TWILIO_AUTH)
        msg = client.messages.create(body=message, from_=TWILIO_NUMBER, to=to_number)
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
    """A debug endpoint to check the status of environment variables."""
    db_url_status = "✅ SET" if os.getenv("DATABASE_URL") else "❌ MISSING"
    twilio_sid_status = "✅ SET" if os.getenv("TWILIO_SID") else "❌ MISSING"
    twilio_auth_status = "✅ SET" if os.getenv("TWILIO_AUTH") else "❌ MISSING"
    twilio_number_status = "✅ SET" if os.getenv("TWILIO_NUMBER") else "❌ MISSING"
    server_url_status = "✅ SET" if os.getenv("SERVER_URL") else "❌ MISSING"

    return {
        "service_status": "Running",
        "message": "Checking environment variable configuration...",
        "database_url_is_set": db_url_status,
        "server_url_is_set": server_url_status,
        "twilio_sid_is_set": twilio_sid_status,
        "twilio_auth_is_set": twilio_auth_status,
        "twilio_number_is_set": twilio_number_status
    }
@app.post("/create_delivery")
def create_delivery(payload: DeliveryCreate):
    now = datetime.utcnow().isoformat()
    order_data = {}

    with _conn() as c:
        # PostgreSQL's ON CONFLICT requires specifying the constraint column
        query = sqlalchemy.text("""
            INSERT INTO deliveries (order_id, pickup_location, drop_location, customer_contact, status, created_at, updated_at)
            VALUES (:order_id, :pickup_location, :drop_location, :customer_contact, 'created', :now, :now)
            ON CONFLICT (order_id) DO UPDATE SET
                pickup_location=excluded.pickup_location,
                drop_location=excluded.drop_location,
                customer_contact=excluded.customer_contact,
                updated_at=excluded.updated_at
        """)
        c.execute(query, {
            "order_id": payload.order_id, "pickup_location": payload.pickup_location,
            "drop_location": payload.drop_location, "customer_contact": payload.customer_contact,
            "now": now
        })
        
        result_proxy = c.execute(sqlalchemy.text("SELECT * FROM deliveries WHERE order_id = :order_id"), {"order_id": payload.order_id})
        row = result_proxy.fetchone()
        
        if row:
            order_data = dict(row._mapping)
        c.commit()

    if not order_data:
        raise HTTPException(status_code=500, detail="Failed to retrieve order after creation.")

    SERVER_URL = os.getenv("SERVER_URL")
    message = f"Your parcel has been received!\nOrder ID: {order_data['order_id']}\nPlease share your location: {SERVER_URL}/share/{order_data['order_id']}"
    send_sms(order_data["customer_contact"], message)

    return { "status": "success", "message": "Delivery task created ✅ & SMS sent", **order_data }

@app.get("/deliveries")
def list_deliveries():
    with _conn() as c:
        query = sqlalchemy.text("SELECT * FROM deliveries ORDER BY id DESC")
        rows = c.execute(query).fetchall()
    return [dict(r._mapping) for r in rows]

@app.get("/deliveries/{order_id}")
def get_delivery(order_id: str):
    with _conn() as c:
        query = sqlalchemy.text("SELECT * FROM deliveries WHERE order_id = :order_id")
        row = c.execute(query, {"order_id": order_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="order not found")
    return dict(row._mapping)

@app.post("/deliveries/{order_id}/location")
def set_target_location(order_id: str, loc: LocationUpdate):
    with _conn() as c:
        get_query = sqlalchemy.text("SELECT customer_contact FROM deliveries WHERE order_id = :order_id")
        row = c.execute(get_query, {"order_id": order_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="order not found")
        customer_contact = row._mapping["customer_contact"]

        update_query = sqlalchemy.text("""
            UPDATE deliveries SET target_lat=:lat, target_lon=:lon, status='location_received', updated_at=:now
            WHERE order_id=:order_id
        """)
        c.execute(update_query, {"lat": loc.lat, "lon": loc.lon, "now": datetime.utcnow().isoformat(), "order_id": order_id})
        c.commit()

    message = f"✅ Delivery Bot received your location! Order ID: {order_id}"
    send_sms(customer_contact, message)
    return {"status": "ok", "order_id": order_id, "lat": loc.lat, "lon": loc.lon, "current_status": "location_received"}

@app.post("/deliveries/{order_id}/status/{new_status}")
def update_status(order_id: str, new_status: str):
    with _conn() as c:
        get_query = sqlalchemy.text("SELECT customer_contact FROM deliveries WHERE order_id = :order_id")
        row = c.execute(get_query, {"order_id": order_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="order not found")
        customer_contact = row._mapping["customer_contact"]

        update_query = sqlalchemy.text("UPDATE deliveries SET status=:status, updated_at=:now WHERE order_id=:order_id")
        c.execute(update_query, {"status": new_status, "now": datetime.utcnow().isoformat(), "order_id": order_id})
        c.commit()

    if new_status == "completed":
        send_sms(customer_contact, f"✅ Your parcel (Order {order_id}) has been delivered!")
    elif new_status == "failed":
        send_sms(customer_contact, f"⚠️ Delivery failed for Order {order_id}. Please contact support.")
    return {"status": "ok", "order_id": order_id, "current_status": new_status}

# --- HTML pages (No changes needed below this line) ---
@app.get("/share/{order_id}", response_class=HTMLResponse)
def share_page(order_id: str):
    return f"""
    <html><head><title>Share Location</title><meta name="viewport" content="width=device-width, initial-scale=1.0"></head><body>
        <h2>📦 Sharing Location for Order {order_id}...</h2><p id="status">⏳ Requesting your location...</p>
        <script>
        window.onload = function() {{
            if (!navigator.geolocation) {{
                document.getElementById('status').innerText = "❌ Geolocation not supported by this browser.";
                return;
            }}
            navigator.geolocation.getCurrentPosition(function(pos) {{
                fetch('/deliveries/{order_id}/location', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ lat: pos.coords.latitude, lon: pos.coords.longitude }})
                }}).then(res => {{
                    if (res.ok) {{ window.location.href = "/thankyou/{order_id}"; }}
                    else {{ document.getElementById('status').innerText = "❌ Error: Could not save location."; }}
                }}).catch(err => {{ document.getElementById('status').innerText = "❌ Error: " + err; }});
            }}, function(error) {{
                document.getElementById('status').innerText = "❌ Permission denied or error: " + error.message;
            }});
        }}
        </script>
    </body></html>
    """

@app.get("/thankyou/{order_id}", response_class=HTMLResponse)
def thank_you(order_id: str):
    return f"""
    <html><head><title>Location Received!</title><meta name="viewport" content="width=device-width, initial-scale=1.0"><style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; text-align: center; padding: 40px 20px; background-color: #f4f7f6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }}
        h2 {{ color: #28a745; }} p {{ font-size: 1.1em; line-height: 1.6; }}
    </style></head><body><div class="container">
        <h2>✅ Location Received!</h2>
        <p>The delivery bot has your location for Order <b>{order_id}</b> and is on the way to deliver your goods.</p>
        <p>Thank you!</p>
    </div></body></html>
    """

