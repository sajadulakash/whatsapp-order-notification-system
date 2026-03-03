"""
Reminder & Escalation Scheduler
================================
Background thread that periodically checks all in-progress orders
and sends timed reminders / warnings / CEO escalations.

TIME MAPPING (test mode):
  1 minute  = 1 working day  (10 am → 7 pm = 9 office hours)
  20 seconds = 3 hours
  40 seconds = 6 hours
  60 seconds = 9 hours  (end of day / deadline)

REMINDER FLOW per step:
  +20 s  →  3-hour reminder : "You haven't completed your task yet …"
  +40 s  →  6-hour warning  : "CEO will be notified if not done by 7 pm …"
  +60 s  →  9-hour deadline : CEO is notified about the delay
"""

import logging
import threading
import time
from datetime import datetime

import psycopg2

from whatsapp_client import send_whatsapp_message

logger = logging.getLogger("reminder_scheduler")
logger.setLevel(logging.INFO)

# ── Timing constants (TEST MODE) ──
# In production, change these to real seconds:
#   3 hours = 10800s,  6 hours = 21600s,  9 hours = 32400s
REMINDER_3H_SECONDS = 20     # 3-hour reminder
WARNING_6H_SECONDS  = 40     # 6-hour warning
DEADLINE_9H_SECONDS = 60     # end-of-day / CEO escalation

CHECK_INTERVAL = 5           # how often the scheduler checks (seconds)

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 5433,
    "database": "wpbot",
    "user": "sajadulakash",
    "password": "fringe_core",
}

# Status → user_type (same mapping as notification_service)
STATUS_TO_USER_TYPE = {
    "OPS Pending":           "OPS",
    "SCM Pending":           "SCM",
    "SCM-Analyst Pending":   "SCM-ANALYST",
    "SCM-Finalist Pending":  "SCM-FINALEST",
    "CEO Pending":           "CEO",
}

# Statuses that get reminder → warning → escalation
ESCALATION_STATUSES = [
    "OPS Pending",
    "SCM Pending",
    "SCM-Analyst Pending",
    "SCM-Finalist Pending",
]

# ALL statuses that should be monitored (including CEO)
MONITORED_STATUSES = ESCALATION_STATUSES + ["CEO Pending"]

# CEO gets repeated reminders at this interval (no deadline)
CEO_REMINDER_INTERVAL = REMINDER_3H_SECONDS   # every 20s in test / 3h in prod
CEO_MAX_REMINDERS = 8                          # stop after 8 reminders (≈ 2 days)


def _conn():
    return psycopg2.connect(**DB_CONFIG)


# ═══════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════

def _has_reminder_been_sent(order_id: int, reminder_type: str) -> bool:
    """Check if a specific reminder has already been sent for this order+status."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM reminders_sent WHERE order_id = %s AND reminder_type = %s",
        (order_id, reminder_type),
    )
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists


def _record_reminder(order_id: int, reminder_type: str):
    """Mark a reminder as sent so we don't send it again."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders_sent (order_id, reminder_type) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (order_id, reminder_type),
    )
    conn.commit()
    cur.close()
    conn.close()


def _get_users_for_status(status: str) -> list[dict]:
    """Get responsible users for the given order status."""
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
        {"user_id": r[0], "full_name": r[1], "user_type": r[2], "phone_number": r[3]}
        for r in rows
    ]


def _get_ceo_users() -> list[dict]:
    """Get CEO users (for escalation notifications)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, full_name, user_type, phone_number "
        "FROM users WHERE user_type = 'CEO'"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"user_id": r[0], "full_name": r[1], "user_type": r[2], "phone_number": r[3]}
        for r in rows
    ]


def clear_reminders_for_order(order_id: int):
    """
    Clear all sent reminders for an order when its status changes.
    Called from main.py when an action moves the order forward.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders_sent WHERE order_id = %s", (order_id,))
    conn.commit()
    cur.close()
    conn.close()
    logger.info("🧹 Cleared reminders for order #%s", order_id)


# ═══════════════════════════════════════════════
#  Reminder / Warning / Escalation messages
# ═══════════════════════════════════════════════

def _send_3h_reminder(order_id: int, status: str):
    """Send the 3-hour reminder to responsible users."""
    reminder_key = f"3h_{status}"
    if _has_reminder_been_sent(order_id, reminder_key):
        return

    users = _get_users_for_status(status)
    for user in users:
        message = (
            f"🔔 *Reminder* — Order #{order_id}\n\n"
            f"Hi {user['full_name']}, you haven't completed your task yet.\n"
            f"Current status: *{status}*\n\n"
            f"⏰ You must complete your task before *7:00 PM* today."
        )
        send_whatsapp_message(user["phone_number"], message)
        logger.info(
            "⏰ 3h reminder sent to %s (%s) for order #%s [%s]",
            user["full_name"], user["phone_number"], order_id, status,
        )

    _record_reminder(order_id, reminder_key)


def _send_6h_warning(order_id: int, status: str):
    """Send the 6-hour warning with CEO threat."""
    warning_key = f"6h_{status}"
    if _has_reminder_been_sent(order_id, warning_key):
        return

    users = _get_users_for_status(status)
    for user in users:
        message = (
            f"⚠️ *WARNING* — Order #{order_id}\n\n"
            f"Hi {user['full_name']}, this is your final warning.\n"
            f"Current status: *{status}*\n\n"
            f"🚨 If you don't complete your task before *7:00 PM*, "
            f"the *CEO will be notified* that you are not completing your work.\n\n"
            f"⏳ Time is running out!"
        )
        send_whatsapp_message(user["phone_number"], message)
        logger.info(
            "⚠️  6h warning sent to %s (%s) for order #%s [%s]",
            user["full_name"], user["phone_number"], order_id, status,
        )

    _record_reminder(order_id, warning_key)


def _send_ceo_escalation(order_id: int, status: str):
    """Notify CEO that the responsible team failed to complete on time."""
    escalation_key = f"9h_{status}"
    if _has_reminder_been_sent(order_id, escalation_key):
        return

    # Find the users who failed
    delayed_users = _get_users_for_status(status)
    user_type = STATUS_TO_USER_TYPE.get(status, "Unknown")
    delayed_names = ", ".join(u["full_name"] for u in delayed_users) or "Unknown"

    # Notify the CEO
    ceo_users = _get_ceo_users()
    for ceo in ceo_users:
        message = (
            f"🚨 *ESCALATION ALERT* — Order #{order_id}\n\n"
            f"The *{user_type}* team has *failed* to complete their task "
            f"by the 7:00 PM deadline.\n\n"
            f"👤 Responsible: *{delayed_names}*\n"
            f"📋 Status stuck at: *{status}*\n\n"
            f"This requires your immediate attention."
        )
        send_whatsapp_message(ceo["phone_number"], message)
        logger.info(
            "🚨 CEO escalation sent to %s for order #%s — %s team (%s) delayed",
            ceo["full_name"], order_id, user_type, delayed_names,
        )

    # Also notify the delayed users that CEO has been informed
    for user in delayed_users:
        message = (
            f"🚨 *DEADLINE MISSED* — Order #{order_id}\n\n"
            f"Hi {user['full_name']}, you failed to complete your task by 7:00 PM.\n\n"
            f"❗ The *CEO has been notified* about this delay.\n"
            f"Please complete your task immediately."
        )
        send_whatsapp_message(user["phone_number"], message)

    _record_reminder(order_id, escalation_key)


def _send_ceo_periodic_reminder(order_id: int, status: str, elapsed: float):
    """
    Send up to CEO_MAX_REMINDERS (8) reminders to CEO, each with a unique
    escalating message.  After the 8th reminder (~2 days) the system stops.

    Reminder schedule (test → production):
      #1  20s →  3h     Gentle first nudge
      #2  40s →  6h     Half-day check
      #3  60s →  9h     End of day 1
      #4  80s → 12h     Day 1 done – new day starts
      #5 100s → 15h     More than a day passed
      #6 120s → 18h     Approaching 2 days
      #7 140s → 21h     Almost 2 days – urgent
      #8 160s → 24h     FINAL reminder – system stops
    """
    # Which reminder number are we on?
    reminder_num = int(elapsed // CEO_REMINDER_INTERVAL)
    if reminder_num < 1:
        return

    # Cap at CEO_MAX_REMINDERS — stop sending after that
    if reminder_num > CEO_MAX_REMINDERS:
        return

    reminder_key = f"ceo_reminder_{reminder_num}_{status}"
    if _has_reminder_been_sent(order_id, reminder_key):
        return

    users = _get_users_for_status(status)

    # ── Build a unique message for each reminder ──
    CEO_MESSAGES = {
        1: (
            f"🔔 *Reminder* — Order #{order_id}\n\n"
            f"Hi {{name}}, this order is waiting for your approval.\n"
            f"Current status: *{status}*\n\n"
            f"Please review and take action when you get a chance."
        ),
        2: (
            f"🔔 *Reminder* — Order #{order_id}\n\n"
            f"Hi {{name}}, just a follow-up — this order is still pending.\n"
            f"Current status: *{status}*\n\n"
            f"⏳ It has been pending for a while now. "
            f"Please review at your earliest convenience."
        ),
        3: (
            f"⚠️ *End of Day Reminder* — Order #{order_id}\n\n"
            f"Hi {{name}}, the first working day is ending and this "
            f"order is still pending your approval.\n"
            f"Current status: *{status}*\n\n"
            f"📋 Please try to complete it before the day ends."
        ),
        4: (
            f"⚠️ *Day 2 — Still Pending* — Order #{order_id}\n\n"
            f"Hi {{name}}, already *1 day has passed* and you haven't "
            f"completed your task yet.\n"
            f"Current status: *{status}*\n\n"
            f"⏰ A new working day has started. Please prioritize this order."
        ),
        5: (
            f"⚠️ *Overdue* — Order #{order_id}\n\n"
            f"Hi {{name}}, *more than 1 day has passed* since this order "
            f"reached your desk and it's still not done.\n"
            f"Current status: *{status}*\n\n"
            f"🚨 This is getting delayed. Please take action now."
        ),
        6: (
            f"🚨 *Urgent* — Order #{order_id}\n\n"
            f"Hi {{name}}, this order has been pending for almost *2 days* "
            f"now.\n"
            f"Current status: *{status}*\n\n"
            f"The team is waiting on your approval to move forward. "
            f"Please complete your task as soon as possible."
        ),
        7: (
            f"🚨 *Critical Delay* — Order #{order_id}\n\n"
            f"Hi {{name}}, *almost 2 full days have passed* and this order "
            f"is still stuck at your step.\n"
            f"Current status: *{status}*\n\n"
            f"❗ This is causing significant delays downstream. "
            f"Please act immediately."
        ),
        8: (
            f"🛑 *FINAL REMINDER* — Order #{order_id}\n\n"
            f"Hi {{name}}, this is your *last reminder*.\n\n"
            f"⏳ *2 days have passed* and you still haven't completed "
            f"your task.\n"
            f"Current status: *{status}*\n\n"
            f"The system will *stop sending reminders* after this message.\n"
            f"Please complete your task immediately."
        ),
    }

    message_template = CEO_MESSAGES[reminder_num]

    for user in users:
        message = message_template.format(name=user["full_name"])
        send_whatsapp_message(user["phone_number"], message)
        logger.info(
            "🔔 CEO reminder #%d/%d sent to %s for order #%s",
            reminder_num, CEO_MAX_REMINDERS, user["full_name"], order_id,
        )

    _record_reminder(order_id, reminder_key)


# ═══════════════════════════════════════════════
#  Main Scheduler Loop
# ═══════════════════════════════════════════════

def _check_all_orders():
    """
    Check all in-progress orders and send reminders/warnings/escalations
    based on how long the current status has been active.
    """
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT order_id, current_status, status_changed_at "
            "FROM orders "
            "WHERE current_status IN %s "
            "AND status_changed_at IS NOT NULL",
            (tuple(MONITORED_STATUSES),),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        now = datetime.now()

        for order_id, status, changed_at in rows:
            elapsed = (now - changed_at).total_seconds()

            if status == "CEO Pending":
                # CEO gets repeated reminders, no deadline/escalation
                _send_ceo_periodic_reminder(order_id, status, elapsed)
            else:
                # Other roles: reminder → warning → CEO escalation
                # Check escalation first (highest priority)
                if elapsed >= DEADLINE_9H_SECONDS:
                    _send_ceo_escalation(order_id, status)

                # Check 6-hour warning
                if elapsed >= WARNING_6H_SECONDS:
                    _send_6h_warning(order_id, status)

                # Check 3-hour reminder
                if elapsed >= REMINDER_3H_SECONDS:
                    _send_3h_reminder(order_id, status)

    except Exception as e:
        logger.error("❌ Scheduler error: %s", e)


def _scheduler_loop():
    """Infinite loop that checks orders every CHECK_INTERVAL seconds."""
    logger.info(
        "🕐 Reminder scheduler started (check every %ds | "
        "3h=%ds, 6h=%ds, deadline=%ds)",
        CHECK_INTERVAL, REMINDER_3H_SECONDS,
        WARNING_6H_SECONDS, DEADLINE_9H_SECONDS,
    )
    while True:
        _check_all_orders()
        time.sleep(CHECK_INTERVAL)


# ── Public API ──

_scheduler_thread = None


def start_scheduler():
    """Start the reminder scheduler in a background daemon thread."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        logger.info("⏭️  Scheduler already running")
        return

    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        daemon=True,
        name="ReminderScheduler",
    )
    _scheduler_thread.start()
    logger.info("✅ Reminder scheduler thread started")
