"""
WhatsApp client using Meta WhatsApp Cloud API.
Sends real WhatsApp messages via Meta's Graph API.

Strategy:
  - If a phone number has an active 24-hour session (user messaged us),
    send FREE-TEXT messages directly (no template needed).
  - If no active session, fall back to template messages.
"""

import logging
import os
from datetime import datetime, timedelta

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("whatsapp_meta")
logger.setLevel(logging.INFO)

# ── Meta WhatsApp Cloud API Credentials ──
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL", "https://graph.facebook.com/v22.0")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "5433")),
    "database": os.getenv("DB_NAME", "wpbot"),
    "user": os.getenv("DB_USER", "sajadulakash"),
    "password": os.getenv("DB_PASSWORD", "fringe_core"),
}

# ── In-memory log of all sent messages (for dashboard display) ──
message_log: list[dict] = []


def _db():
    return psycopg2.connect(**DB_CONFIG)


# ═══════════════════════════════════════════════
#  Session Tracking (24-hour conversation window)
# ═══════════════════════════════════════════════

def register_session(phone_number: str):
    """
    Record that a user sent us a message, opening a 24-hour window.
    Called by the webhook when we receive an incoming message.
    """
    phone_clean = phone_number.lstrip("+")
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO whatsapp_sessions (phone_number, last_message_at)
           VALUES (%s, NOW())
           ON CONFLICT (phone_number)
           DO UPDATE SET last_message_at = NOW()""",
        (phone_clean,),
    )
    conn.commit()
    cur.close()
    conn.close()
    logger.info("🟢 Session opened/refreshed for %s", phone_number)


def has_active_session(phone_number: str) -> bool:
    """Check if a phone number has an active 24-hour session."""
    phone_clean = phone_number.lstrip("+")
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """SELECT last_message_at FROM whatsapp_sessions
           WHERE phone_number = %s AND last_message_at > NOW() - INTERVAL '24 hours'""",
        (phone_clean,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def get_all_sessions() -> list[dict]:
    """Return all sessions for dashboard display."""
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """SELECT phone_number, last_message_at,
                  CASE WHEN last_message_at > NOW() - INTERVAL '24 hours'
                       THEN 'ACTIVE' ELSE 'EXPIRED' END as status
           FROM whatsapp_sessions ORDER BY last_message_at DESC"""
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"phone": r[0], "last_message_at": str(r[1]), "status": r[2]}
        for r in rows
    ]


# ═══════════════════════════
#  WhatsApp Message Sending
# ═══════════════════════════

class WhatsAppResponse:
    """Wraps the Meta API message response."""

    def __init__(self, phone: str, message: str, msg_id: str = "", success: bool = True, error: str = ""):
        self.success = success
        self.message_id = msg_id or f"wa_err_{datetime.now().strftime('%Y%m%d%H%M%S')}_{phone[-4:]}"
        self.timestamp = datetime.now().isoformat()
        self.phone = phone
        self.message = message
        self.error = error

    def to_dict(self):
        return {
            "success": self.success,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "phone": self.phone,
            "error": self.error,
        }


def _send_free_text(phone_clean: str, message: str) -> tuple[dict, int]:
    """Send a free-text message (only works within 24-hour window)."""
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone_clean,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    return resp.json(), resp.status_code


def _send_template(phone_clean: str, template_name: str = "hello_world") -> tuple[dict, int]:
    """Send a template message (works anytime, no session needed)."""
    url = f"{WHATSAPP_API_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_clean,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "en_US"},
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    return resp.json(), resp.status_code


def send_whatsapp_message(phone_number: str, message: str) -> WhatsAppResponse:
    """
    Send a WhatsApp message via Meta Cloud API.

    Strategy:
      1. If the phone has an active session → send FREE TEXT (your custom message)
      2. If no session → send hello_world template as fallback
    """
    phone_clean = phone_number.lstrip("+")

    try:
        # Check if this number has an active 24-hour session
        if has_active_session(phone_number):
            logger.info("📨 [META] Active session for %s — sending free text", phone_number)
            data, status_code = _send_free_text(phone_clean, message)
            msg_type = "free_text"
        else:
            logger.info("📨 [META] No session for %s — sending hello_world template", phone_number)
            data, status_code = _send_template(phone_clean, "hello_world")
            msg_type = "template"

        if status_code == 200 and "messages" in data:
            msg_id = data["messages"][0]["id"]
            logger.info("📱 [META] Sent %s to %s | ID: %s", msg_type, phone_number, msg_id)

            response = WhatsAppResponse(
                phone=phone_number, message=message, msg_id=msg_id, success=True,
            )
            message_log.append({
                "message_id": msg_id, "phone": phone_number,
                "message": message, "sent_at": response.timestamp,
                "status": f"sent ({msg_type})",
            })
            return response
        else:
            error_msg = data.get("error", {}).get("message", str(data))
            logger.error("❌ [META] Failed to send %s to %s: %s", msg_type, phone_number, error_msg)

            response = WhatsAppResponse(
                phone=phone_number, message=message, success=False, error=error_msg,
            )
            message_log.append({
                "message_id": response.message_id, "phone": phone_number,
                "message": message, "sent_at": response.timestamp,
                "status": f"FAILED: {error_msg}",
            })
            return response

    except requests.RequestException as e:
        logger.error("❌ [META] Request error sending to %s: %s", phone_number, str(e))

        response = WhatsAppResponse(
            phone=phone_number, message=message, success=False, error=str(e),
        )
        message_log.append({
            "message_id": response.message_id, "phone": phone_number,
            "message": message, "sent_at": response.timestamp,
            "status": f"FAILED: {str(e)}",
        })
        return response


def send_reply(phone_number: str, message: str) -> WhatsAppResponse:
    """
    Send a reply to a user (always free text since they just messaged us).
    Used by the webhook handler.
    """
    phone_clean = phone_number.lstrip("+")

    try:
        data, status_code = _send_free_text(phone_clean, message)

        if status_code == 200 and "messages" in data:
            msg_id = data["messages"][0]["id"]
            logger.info("💬 [META] Reply to %s | ID: %s", phone_number, msg_id)
            return WhatsAppResponse(phone=phone_number, message=message, msg_id=msg_id, success=True)
        else:
            error_msg = data.get("error", {}).get("message", str(data))
            logger.error("❌ [META] Reply failed to %s: %s", phone_number, error_msg)
            return WhatsAppResponse(phone=phone_number, message=message, success=False, error=error_msg)

    except requests.RequestException as e:
        logger.error("❌ [META] Reply error to %s: %s", phone_number, str(e))
        return WhatsAppResponse(phone=phone_number, message=message, success=False, error=str(e))


def get_message_log() -> list[dict]:
    """Return the in-memory log of all messages sent (newest first)."""
    return list(reversed(message_log))
