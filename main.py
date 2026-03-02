import logging
import os
import psycopg2
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

from notification_service import (
    STATUS_TO_USER_TYPE,
    confirm_notification,
    get_all_notifications,
    get_notifications_for_order,
    get_responsible_users,
    notify_for_order,
)
from whatsapp_client import (
    get_message_log,
    register_session,
    send_reply,
    get_all_sessions,
)
from reminder_scheduler import start_scheduler, clear_reminders_for_order

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(message)s")

app = FastAPI(title="WhatsApp Order Notification System")
templates = Jinja2Templates(directory="templates")

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 5433,
    "database": "wpbot",
    "user": "sajadulakash",
    "password": "fringe_core",
}

# The ordered workflow steps
STEPS = [
    {"action": "start",           "label": "Start",              "sets_status": "OPS Pending"},
    {"action": "ops_done",        "label": "Ops Done",           "sets_status": "SCM Pending"},
    {"action": "scm_done",        "label": "SCM Done",           "sets_status": "SCM-Analyst Pending"},
    {"action": "analyst_done",    "label": "SCM-Analyst Done",   "sets_status": "SCM-Finalist Pending"},
    {"action": "finalist_done",   "label": "SCM-Finalist Done",  "sets_status": "CEO Pending"},
    {"action": "ceo_done",        "label": "CEO Done",           "sets_status": "Completed"},
]

# Map: which status must exist BEFORE each action can be taken
REQUIRED_STATUS = {
    "start":          None,                   # no order needed
    "ops_done":       "OPS Pending",
    "scm_done":       "SCM Pending",
    "analyst_done":   "SCM-Analyst Pending",
    "finalist_done":  "SCM-Finalist Pending",
    "ceo_done":       "CEO Pending",
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def get_latest_order():
    """Return the latest order (by order_id desc), or None."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT order_id, current_status, created_at, final_deadline "
        "FROM orders ORDER BY order_id DESC LIMIT 1"
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {
            "order_id": row[0],
            "current_status": row[1],
            "created_at": row[2],
            "final_deadline": row[3],
        }
    return None


def get_all_orders():
    """Return all orders newest first."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT order_id, current_status, created_at, final_deadline "
        "FROM orders ORDER BY order_id DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "order_id": r[0],
            "current_status": r[1],
            "created_at": r[2],
            "final_deadline": r[3],
        }
        for r in rows
    ]


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, msg: str = "", error: str = ""):
    latest = get_latest_order()
    orders = get_all_orders()
    notifications = get_all_notifications()
    wa_log = get_message_log()

    current_status = latest["current_status"] if latest else None

    # Figure out which step index we're at
    active_index = 0  # default: "Start" is available
    if current_status:
        for i, step in enumerate(STEPS):
            if step["sets_status"] == current_status:
                active_index = i + 1  # next step is available
                break
        # If status is "Completed", reset to allow new order
        if current_status == "Completed":
            active_index = 0  # "Start" button available again

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "steps": STEPS,
            "active_index": active_index,
            "current_status": current_status,
            "latest": latest,
            "orders": orders,
            "notifications": notifications,
            "wa_log": wa_log,
            "msg": msg,
            "error": error,
        },
    )


@app.post("/action/{action}")
async def perform_action(action: str):
    required = REQUIRED_STATUS.get(action)
    latest = get_latest_order()
    current_status = latest["current_status"] if latest else None

    # --- Validation ---
    if action == "start":
        # Can only start if there's no active order or last order is Completed
        if latest and current_status != "Completed":
            return RedirectResponse(
                url=f"/?error=Cannot start a new order. Current order (#{latest['order_id']}) is still in progress: {current_status}",
                status_code=303,
            )
        # Create new order
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (current_status, status_changed_at) VALUES (%s, CURRENT_TIMESTAMP) RETURNING order_id",
            ("OPS Pending",),
        )
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        # ── Auto-notify OPS users ──
        notify_for_order(order_id, "OPS Pending")

        return RedirectResponse(
            url=f"/?msg=Order #{order_id} created! Status: OPS Pending — notifications sent to OPS team.",
            status_code=303,
        )

    # For all other actions, an active order must exist
    if not latest or current_status == "Completed":
        return RedirectResponse(
            url="/?error=No active order. Click Start first.",
            status_code=303,
        )

    if current_status != required:
        return RedirectResponse(
            url=f"/?error=Cannot perform '{action}'. Current status is '{current_status}', expected '{required}'.",
            status_code=303,
        )

    # Find what status this action sets
    new_status = None
    for step in STEPS:
        if step["action"] == action:
            new_status = step["sets_status"]
            break

    if not new_status:
        return RedirectResponse(url="/?error=Unknown action.", status_code=303)

    # Update order
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE orders SET current_status = %s, status_changed_at = CURRENT_TIMESTAMP WHERE order_id = %s",
        (new_status, latest["order_id"]),
    )
    conn.commit()
    cur.close()
    conn.close()

    # Clear old reminders so the new step starts fresh
    clear_reminders_for_order(latest["order_id"])

    # ── Auto-notify the next responsible team ──
    notified = []
    if new_status != "Completed":
        results = notify_for_order(latest["order_id"], new_status)
        notified = [r["user"] for r in results]

    notify_msg = ""
    if notified:
        notify_msg = f" — notified: {', '.join(notified)}"
    elif new_status == "Completed":
        notify_msg = " 🎉 Order complete!"

    return RedirectResponse(
        url=f"/?msg=Order #{latest['order_id']} updated to: {new_status}{notify_msg}",
        status_code=303,
    )


# ═══════════════════════════════════════════════════════════════
#  JSON API ENDPOINTS  (for the WhatsApp bot to consume)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/orders/pending", response_class=JSONResponse)
async def api_pending_orders():
    """Return all orders that are NOT 'Completed'."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT order_id, current_status, created_at, final_deadline "
        "FROM orders WHERE current_status != 'Completed' ORDER BY order_id DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "order_id": r[0],
            "current_status": r[1],
            "created_at": str(r[2]) if r[2] else None,
            "final_deadline": str(r[3]) if r[3] else None,
        }
        for r in rows
    ]


@app.get("/api/orders/{order_id}", response_class=JSONResponse)
async def api_get_order(order_id: int):
    """Return a single order by ID."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT order_id, current_status, created_at, final_deadline "
        "FROM orders WHERE order_id = %s",
        (order_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return JSONResponse({"error": "Order not found"}, status_code=404)
    return {
        "order_id": row[0],
        "current_status": row[1],
        "created_at": str(row[2]) if row[2] else None,
        "final_deadline": str(row[3]) if row[3] else None,
    }


@app.get("/api/orders/{order_id}/responsible-users", response_class=JSONResponse)
async def api_responsible_users(order_id: int):
    """
    Look up the order's current_status, map it to a user_type,
    and return the list of users responsible for that stage.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT current_status FROM orders WHERE order_id = %s", (order_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return JSONResponse({"error": "Order not found"}, status_code=404)

    status = row[0]
    users = get_responsible_users(status)
    return {
        "order_id": order_id,
        "current_status": status,
        "responsible_user_type": STATUS_TO_USER_TYPE.get(status),
        "users": users,
    }


@app.post("/api/notifications/send/{order_id}", response_class=JSONResponse)
async def api_send_notifications(order_id: int):
    """
    The WhatsApp bot calls this to trigger notifications for an order.
    Looks up current status, finds responsible users, sends WhatsApp messages.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT current_status FROM orders WHERE order_id = %s", (order_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return JSONResponse({"error": "Order not found"}, status_code=404)

    status = row[0]
    if status == "Completed":
        return {"order_id": order_id, "message": "Order already completed", "notified": []}

    results = notify_for_order(order_id, status)
    return {
        "order_id": order_id,
        "current_status": status,
        "notifications_sent": len(results),
        "notified": results,
    }


@app.post("/api/notifications/confirm/{notification_id}", response_class=JSONResponse)
async def api_confirm_notification(notification_id: int):
    """Mark a notification as confirmed (acknowledged by user)."""
    confirm_notification(notification_id)
    return {"notification_id": notification_id, "confirmed": True}


@app.get("/api/notifications/{order_id}", response_class=JSONResponse)
async def api_get_notifications(order_id: int):
    """Get all notifications for a specific order."""
    notifs = get_notifications_for_order(order_id)
    for n in notifs:
        n["sent_at"] = str(n["sent_at"]) if n["sent_at"] else None
        n["confirmed_at"] = str(n["confirmed_at"]) if n["confirmed_at"] else None
    return {"order_id": order_id, "notifications": notifs}


@app.get("/api/whatsapp/log", response_class=JSONResponse)
async def api_wa_log():
    """Return the dummy WhatsApp message log (for debugging)."""
    return {"messages": get_message_log()}


@app.get("/api/whatsapp/sessions", response_class=JSONResponse)
async def api_wa_sessions():
    """Return all WhatsApp session statuses."""
    return {"sessions": get_all_sessions()}


# ═══════════════════════════════════════════════════════════════
#  WEBHOOK — receive incoming WhatsApp messages from Meta
# ═══════════════════════════════════════════════════════════════

WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "wpbot_verify_token_2026")


@app.get("/webhook")
async def webhook_verify(
    request: Request,
):
    """
    Meta sends a GET request to verify the webhook URL.
    We must respond with the challenge value if the token matches.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        logging.info("✅ Webhook verified!")
        return PlainTextResponse(content=challenge, status_code=200)

    logging.warning("❌ Webhook verification failed — token mismatch")
    return PlainTextResponse(content="Forbidden", status_code=403)


@app.post("/webhook")
async def webhook_receive(request: Request):
    """
    Meta sends a POST request when a user sends a WhatsApp message.
    We:
      1. Extract the sender's phone number and message text
      2. Register/refresh their 24-hour session
      3. Send an auto-reply confirming they're registered
    """
    body = await request.json()

    try:
        # Navigate the Meta webhook payload structure
        entry = body.get("entry", [])
        for e in entry:
            changes = e.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])

                for msg in messages:
                    sender_phone = msg.get("from", "")  # e.g. "8801601790298"
                    msg_type = msg.get("type", "")
                    msg_text = ""

                    if msg_type == "text":
                        msg_text = msg.get("text", {}).get("body", "").strip()

                    logging.info(
                        "📩 Incoming WhatsApp from +%s: '%s'",
                        sender_phone, msg_text,
                    )

                    # Register/refresh the 24-hour session
                    register_session(sender_phone)

                    # Send auto-reply
                    reply_text = (
                        f"✅ Hi! You're now registered for order notifications.\n\n"
                        f"You'll receive real-time updates about your orders "
                        f"for the next 24 hours.\n\n"
                        f"💡 Send any message to refresh your session."
                    )
                    send_reply(sender_phone, reply_text)

    except Exception as exc:
        logging.error("❌ Error processing webhook: %s", exc)

    # Always return 200 to Meta (otherwise they'll retry)
    return JSONResponse({"status": "ok"}, status_code=200)


if __name__ == "__main__":
    import uvicorn
    from pyngrok import ngrok

    # Start the reminder/escalation scheduler
    start_scheduler()

    # Start ngrok tunnel
    public_url = ngrok.connect(9753, "http").public_url
    logging.info("=" * 60)
    logging.info("🌐 ngrok tunnel: %s", public_url)
    logging.info("📋 Webhook URL:  %s/webhook", public_url)
    logging.info("=" * 60)
    logging.info("")
    logging.info("👉 Go to Meta Developer Dashboard → WhatsApp → Configuration")
    logging.info("   Set Callback URL: %s/webhook", public_url)
    logging.info("   Set Verify Token: %s", WEBHOOK_VERIFY_TOKEN)
    logging.info("   Subscribe to: messages")
    logging.info("=" * 60)

    uvicorn.run(app, host="0.0.0.0", port=9753)
