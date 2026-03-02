"""
Notification service — the brain that connects DB ↔ API ↔ WhatsApp.

Call `notify_for_order(order_id)` to:
  1. Look up the order's current_status
  2. Map the status to the responsible user_type
  3. Fetch all users of that type
  4. Send each user a WhatsApp message (dummy)
  5. Record the notification in the DB
"""

import logging
import psycopg2
from whatsapp_client import send_whatsapp_message

logger = logging.getLogger("notification_service")
logger.setLevel(logging.INFO)

# ── Status → user_type mapping ──
STATUS_TO_USER_TYPE: dict[str, str] = {
    "OPS Pending":           "OPS",
    "SCM Pending":           "SCM",
    "SCM-Analyst Pending":   "SCM-ANALYST",
    "SCM-Finalist Pending":  "SCM-FINALEST",
    "CEO Pending":           "CEO",
}

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 5433,
    "database": "wpbot",
    "user": "sajadulakash",
    "password": "fringe_core",
}


def _conn():
    return psycopg2.connect(**DB_CONFIG)


def get_responsible_users(status: str) -> list[dict]:
    """Return all users whose user_type matches the given order status."""
    user_type = STATUS_TO_USER_TYPE.get(status)
    if not user_type:
        return []

    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, full_name, user_type, phone_number "
        "FROM users WHERE user_type = %s",
        (user_type,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "user_id": r[0],
            "full_name": r[1],
            "user_type": r[2],
            "phone_number": r[3],
        }
        for r in rows
    ]


def _record_notification(order_id: int, user_id: int, status: str, message: str):
    """Insert a row into the notifications table."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notifications (order_id, user_id, status_at_send, message, delivered) "
        "VALUES (%s, %s, %s, %s, %s)",
        (order_id, user_id, status, message, True),
    )
    conn.commit()
    cur.close()
    conn.close()


def confirm_notification(notification_id: int):
    """Mark a notification as confirmed (e.g., user acknowledged)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE notifications SET confirmed_at = CURRENT_TIMESTAMP WHERE notification_id = %s",
        (notification_id,),
    )
    conn.commit()
    cur.close()
    conn.close()


def notify_for_order(order_id: int, current_status: str) -> list[dict]:
    """
    Main entry point: send WhatsApp reminders to every user responsible
    for the current stage of the given order.

    Returns a list of results (one per user notified).
    """
    users = get_responsible_users(current_status)
    if not users:
        logger.warning(
            "No responsible users found for order #%s status '%s'",
            order_id,
            current_status,
        )
        return []

    user_type = STATUS_TO_USER_TYPE.get(current_status, "?")
    results = []

    for user in users:
        message = (
            f"🔔 Reminder: Order #{order_id} is awaiting your action.\n"
            f"Current status: {current_status}\n"
            f"Responsible role: {user_type}\n"
            f"Hi {user['full_name']}, please complete your step."
        )

        wa_response = send_whatsapp_message(user["phone_number"], message)

        # Record in DB
        _record_notification(order_id, user["user_id"], current_status, message)

        results.append(
            {
                "user": user["full_name"],
                "phone": user["phone_number"],
                "wa_message_id": wa_response.message_id,
                "success": wa_response.success,
            }
        )
        logger.info(
            "✅ Notified %s (%s) for order #%s",
            user["full_name"],
            user["phone_number"],
            order_id,
        )

    return results


def get_notifications_for_order(order_id: int) -> list[dict]:
    """Fetch all notification records for a given order."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT n.notification_id, n.order_id, u.full_name, u.phone_number, "
        "       n.status_at_send, n.message, n.sent_at, n.delivered, n.confirmed_at "
        "FROM notifications n "
        "JOIN users u ON n.user_id = u.user_id "
        "WHERE n.order_id = %s "
        "ORDER BY n.sent_at DESC",
        (order_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "notification_id": r[0],
            "order_id": r[1],
            "full_name": r[2],
            "phone_number": r[3],
            "status_at_send": r[4],
            "message": r[5],
            "sent_at": r[6],
            "delivered": r[7],
            "confirmed_at": r[8],
        }
        for r in rows
    ]


def get_all_notifications() -> list[dict]:
    """Fetch all notification records (newest first)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT n.notification_id, n.order_id, u.full_name, u.phone_number, "
        "       n.status_at_send, n.sent_at, n.delivered, n.confirmed_at "
        "FROM notifications n "
        "JOIN users u ON n.user_id = u.user_id "
        "ORDER BY n.sent_at DESC"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            "notification_id": r[0],
            "order_id": r[1],
            "full_name": r[2],
            "phone_number": r[3],
            "status_at_send": r[4],
            "sent_at": r[5],
            "delivered": r[6],
            "confirmed_at": r[7],
        }
        for r in rows
    ]
