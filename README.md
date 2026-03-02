# WhatsApp Order Notification Bot

An automated order-tracking and notification system that sends real-time WhatsApp messages to team members via the **Meta WhatsApp Cloud API**, with timed reminders and CEO escalation.

## 🎯 Features

- **Meta WhatsApp Cloud API** — sends real WhatsApp messages (no Twilio)
- **Webhook Integration** — receives incoming messages from WhatsApp via Meta webhooks
- **24-Hour Session Tracking** — employees message first to open a free-text window; falls back to templates otherwise
- **Sequential Workflow** — orders flow through OPS → SCM → SCM-Analyst → SCM-Finalist → CEO → Completed
- **Automated Reminders** — 3-hour and 6-hour reminders to the responsible team
- **CEO Escalation** — if a step isn't completed by the 9-hour deadline, the CEO is notified automatically
- **Web Dashboard** — view orders, notifications, sessions, and trigger actions from the browser
- **ngrok Auto-Tunnel** — public HTTPS URL generated automatically on startup

## 📋 Workflow

```
New Order → OPS Pending  (notify OPS team)
                ↓
           OPS Done → SCM Pending  (notify SCM team)
                ↓
           SCM Done → SCM-Analyst Pending
                ↓
      SCM-Analyst Done → SCM-Finalist Pending
                ↓
      SCM-Finalist Done → CEO Pending
                ↓
           CEO Done → Completed 🎉
```

### Reminder & Escalation Timeline (per step)

| Elapsed   | Test Mode | Action                                                          |
| --------- | --------- | --------------------------------------------------------------- |
| 3 hours   | 20 sec    | 🔔 Reminder — "Complete your task before 7 PM"                  |
| 6 hours   | 40 sec    | ⚠️ Warning — "CEO will be notified if not done by 7 PM"         |
| 9 hours   | 60 sec    | 🚨 Escalation — CEO notified with name & role of delayed person |

> **Test mode:** 1 minute = 1 working day. Timings are configurable in `reminder_scheduler.py`.

## 🚀 Quick Start

### 1. Prerequisites

- Python 3.11+
- PostgreSQL (running on port 5433)
- A Meta Developer account with WhatsApp Business API set up

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

Create a `.env` file:

```env
# Database
DB_HOST=127.0.0.1
DB_PORT=5433
DB_NAME=wpbot
DB_USER=your_db_user
DB_PASSWORD=your_db_password

# WhatsApp Business API (Meta Cloud API)
WHATSAPP_API_URL=https://graph.facebook.com/v22.0
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_ACCESS_TOKEN=your_access_token

# Webhook
WEBHOOK_VERIFY_TOKEN=your_verify_token

# ngrok
NGROK_AUTH_TOKEN=your_ngrok_auth_token
```

### 4. Set Up the Database

```sql
CREATE TABLE orders (
    order_id        SERIAL PRIMARY KEY,
    current_status  VARCHAR(50) DEFAULT 'OPS Pending',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    final_deadline  DATE,
    status_changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE users (
    user_id      SERIAL PRIMARY KEY,
    full_name    VARCHAR(100),
    user_type    VARCHAR(50),
    phone_number VARCHAR(20)
);

CREATE TABLE notifications (
    notification_id SERIAL PRIMARY KEY,
    order_id        INTEGER REFERENCES orders(order_id),
    user_id         BIGINT REFERENCES users(user_id),
    status_at_send  VARCHAR(50),
    message         TEXT,
    sent_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered       BOOLEAN DEFAULT FALSE,
    confirmed_at    TIMESTAMP
);

CREATE TABLE whatsapp_sessions (
    phone_number    VARCHAR(20) PRIMARY KEY,
    last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE reminders_sent (
    id              SERIAL PRIMARY KEY,
    order_id        INTEGER REFERENCES orders(order_id),
    reminder_type   VARCHAR(20) NOT NULL,
    sent_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(order_id, reminder_type)
);
```

### 5. Run the Server

```bash
python main.py
```

The server starts on **port 9753** with:
- ngrok tunnel auto-created
- Reminder scheduler running in the background
- Webhook URL printed to the console

### 6. Configure Meta Webhook

Go to **Meta Developer Dashboard → WhatsApp → Configuration** and set:
- **Callback URL:** the ngrok URL printed at startup + `/webhook`
- **Verify Token:** the value of `WEBHOOK_VERIFY_TOKEN` in your `.env`
- **Subscribe to:** `messages`

## 📡 Endpoints

### Web Dashboard

| Method | Path | Description                        |
| ------ | ---- | ---------------------------------- |
| GET    | `/`  | Order dashboard with action buttons |

### Order Actions (form POSTs from dashboard)

| Method | Path                    | Description                                |
| ------ | ----------------------- | ------------------------------------------ |
| POST   | `/action/start`         | Create a new order (→ OPS Pending)         |
| POST   | `/action/ops_done`      | Mark OPS complete (→ SCM Pending)          |
| POST   | `/action/scm_done`      | Mark SCM complete (→ SCM-Analyst Pending)  |
| POST   | `/action/analyst_done`  | Mark Analyst complete (→ SCM-Finalist Pending) |
| POST   | `/action/finalist_done` | Mark Finalist complete (→ CEO Pending)     |
| POST   | `/action/ceo_done`      | Mark CEO complete (→ Completed)            |

### JSON API

| Method | Path                              | Description                          |
| ------ | --------------------------------- | ------------------------------------ |
| GET    | `/api/orders/pending`             | List all non-completed orders        |
| GET    | `/api/orders/{id}`                | Get a single order                   |
| GET    | `/api/orders/{id}/responsible-users` | Users responsible for current step |
| POST   | `/api/notifications/send/{id}`    | Trigger notifications for an order   |
| POST   | `/api/notifications/confirm/{id}` | Mark notification as confirmed       |
| GET    | `/api/notifications/{id}`         | Notification history for an order    |
| GET    | `/api/whatsapp/log`               | WhatsApp message log                 |
| GET    | `/api/whatsapp/sessions`          | Active 24-hour sessions              |

### Webhook (Meta WhatsApp)

| Method | Path       | Description                         |
| ------ | ---------- | ----------------------------------- |
| GET    | `/webhook` | Meta verification handshake         |
| POST   | `/webhook` | Receive incoming WhatsApp messages  |

## 📁 Project Structure

```
WhatsAppBot/
├── main.py                  # FastAPI app, routes, dashboard, webhook, ngrok
├── whatsapp_client.py       # Meta Cloud API client, session tracking
├── notification_service.py  # DB ↔ WhatsApp notification bridge
├── reminder_scheduler.py   # Background reminder & escalation scheduler
├── requirements.txt         # Python dependencies
├── .env                     # Credentials (not committed)
├── .gitignore
├── templates/
│   └── index.html           # Web dashboard template
└── README.md
```

## 🔧 User Roles

| Role           | User Type    | Receives notifications for              |
| -------------- | ------------ | --------------------------------------- |
| OPS            | OPS          | OPS Pending orders                      |
| SCM            | SCM          | SCM Pending orders                      |
| SCM Analyst    | SCM-ANALYST  | SCM-Analyst Pending orders              |
| SCM Finalist   | SCM-FINALEST | SCM-Finalist Pending orders             |
| CEO            | CEO          | CEO Pending orders + escalation alerts  |

## License

MIT
