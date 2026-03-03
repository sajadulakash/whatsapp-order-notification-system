# WhatsApp Order Notification Bot

An automated order-tracking and notification system that sends real-time WhatsApp messages to team members via the **Meta WhatsApp Cloud API**, with timed reminders, escalating CEO alerts, and a web dashboard.

---

## Features

- **Meta WhatsApp Cloud API** — sends real WhatsApp messages directly (no third-party providers)
- **Webhook Integration** — receives incoming WhatsApp messages via Meta webhooks
- **24-Hour Session Tracking** — employees send a message first to open a free-text window; falls back to templates if no active session
- **Sequential Workflow** — orders move through a fixed pipeline: OPS → SCM → SCM-Analyst → SCM-Finalist → CEO → Completed
- **Automated Reminders** — 3-hour and 6-hour reminders sent to the responsible team member
- **CEO Escalation** — if a step isn't completed within 9 hours, the CEO is automatically notified
- **CEO Escalating Reminders** — CEO receives up to 8 unique, increasingly urgent messages over 2 days; the system stops after the final reminder
- **Web Dashboard** — create orders, view status, trigger actions, and see notifications from the browser
- **ngrok Auto-Tunnel** — a public HTTPS URL is generated automatically on startup for the webhook

---

## Workflow

```
New Order → OPS Pending  →  SCM Pending  →  SCM-Analyst Pending
                                                    ↓
          Completed  ←  CEO Pending  ←  SCM-Finalist Pending
```

### Reminder Timeline (for OPS / SCM / Analyst / Finalist)

| Elapsed | Test Mode | Action |
|---------|-----------|--------|
| 3 hours | 20 sec | 🔔 Reminder — "Complete your task before 7 PM" |
| 6 hours | 40 sec | ⚠️ Warning — "CEO will be notified if not done" |
| 9 hours | 60 sec | 🚨 Escalation — CEO notified with the delayed person's name |

### CEO Reminder Timeline (8 messages over 2 days, then stops)

| # | Elapsed | Test Mode | Message |
|---|---------|-----------|---------|
| 1 | 3h | 20s | Gentle first nudge |
| 2 | 6h | 40s | Follow-up, still pending |
| 3 | 9h | 60s | End of day 1 warning |
| 4 | 12h | 80s | "Already 1 day has passed" |
| 5 | 15h | 100s | Overdue — more than 1 day |
| 6 | 18h | 120s | Urgent — approaching 2 days |
| 7 | 21h | 140s | Critical delay — act immediately |
| 8 | 24h | 160s | 🛑 FINAL reminder — system stops |

> Timings are configurable in `reminder_scheduler.py`. Test mode: 1 minute ≈ 1 working day.

---

## How to Run

### 1. Prerequisites

- Python 3.11+
- PostgreSQL
- A [Meta Developer](https://developers.facebook.com/) account with WhatsApp Business API configured
- An [ngrok](https://ngrok.com/) account (free tier works)

### 2. Clone & Install

```bash
git clone https://github.com/sajadulakash/whatsapp-order-notification-system.git
cd whatsapp-order-notification-system
pip install -r requirements.txt
```

### 3. Configure `.env`

Create a `.env` file in the project root:

```env
# Database
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=wpbot
DB_USER=your_db_user
DB_PASSWORD=your_db_password

# WhatsApp Business API (Meta Cloud API)
WHATSAPP_API_URL=https://graph.facebook.com/v22.0
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_ACCESS_TOKEN=your_access_token

# Webhook
WEBHOOK_VERIFY_TOKEN=pick_any_secret_string

# ngrok
NGROK_AUTH_TOKEN=your_ngrok_auth_token
```

**How to get these values:**
- **Phone Number ID & Access Token:** Meta Developer Dashboard → Your App → WhatsApp → API Setup
- **ngrok Auth Token:** [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken)

### 4. Set Up the Database

Create the database and tables:

```sql
CREATE DATABASE wpbot;

-- Connect to wpbot, then run:

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
    reminder_type   VARCHAR(80) NOT NULL,
    sent_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(order_id, reminder_type)
);
```

Insert your team members:

```sql
INSERT INTO users (full_name, user_type, phone_number) VALUES
('Mr Islam',       'OPS',          '+8801XXXXXXXXX'),
('Mr Sharif',      'SCM',          '+8801XXXXXXXXX'),
('Mr Zam',         'SCM-ANALYST',  '+8801XXXXXXXXX'),
('Mr Zam',         'SCM-FINALEST', '+8801XXXXXXXXX'),
('Sajadul Akash',  'CEO',          '+8801XXXXXXXXX');
```

### 5. Start the Server

```bash
python main.py
```

On startup you'll see:
- The **ngrok public URL** printed in the console
- The **reminder scheduler** starts automatically in the background
- The server runs on `http://localhost:9753`

### 6. Configure Meta Webhook

Go to **Meta Developer Dashboard → Your App → WhatsApp → Configuration**:

1. **Callback URL:** paste the ngrok URL + `/webhook` (e.g. `https://your-subdomain.ngrok-free.dev/webhook`)
2. **Verify Token:** the value you set for `WEBHOOK_VERIFY_TOKEN` in `.env`
3. **Subscribe to:** `messages`

### 7. Open the Dashboard

Open `http://localhost:9753` in your browser to create orders and manage the workflow.

---

## Integrating Into Another System

This bot exposes a **REST API** that any external system (ERP, CRM, e-commerce platform, etc.) can call to create orders and trigger notifications.

### Creating an Order Programmatically

Send a POST request to create a new order and start the notification pipeline:

```bash
curl -X POST http://localhost:9753/action/start
```

This creates an order with status `OPS Pending` and immediately notifies the OPS team on WhatsApp.

### Moving an Order Forward

Call the appropriate action endpoint to advance the order to the next step:

```bash
# After OPS completes their work
curl -X POST http://localhost:9753/action/ops_done?order_id=1

# After SCM completes
curl -X POST http://localhost:9753/action/scm_done?order_id=1

# After SCM-Analyst completes
curl -X POST http://localhost:9753/action/analyst_done?order_id=1

# After SCM-Finalist completes
curl -X POST http://localhost:9753/action/finalist_done?order_id=1

# After CEO approves
curl -X POST http://localhost:9753/action/ceo_done?order_id=1
```

Each action triggers WhatsApp notifications to the next responsible person and resets the reminder timer.

### Reading Order Data

```bash
# Get all pending orders
curl http://localhost:9753/api/orders/pending

# Get a specific order
curl http://localhost:9753/api/orders/1

# Get who's responsible for the current step
curl http://localhost:9753/api/orders/1/responsible-users

# Get notification history for an order
curl http://localhost:9753/api/notifications/1
```

### Webhook — Receiving WhatsApp Replies

When a team member replies on WhatsApp, the bot receives it via the `/webhook` endpoint and registers a 24-hour session for that phone number. Your system can check active sessions:

```bash
curl http://localhost:9753/api/whatsapp/sessions
```

### Integration Example

In your existing application (e.g., an ERP), you would:

1. **When a new purchase order is created** → call `POST /action/start` to create an order in the bot
2. **When a department completes their review** → call `POST /action/{step}_done?order_id=X`
3. **To check status** → call `GET /api/orders/{id}` and read the `current_status` field
4. The bot handles all WhatsApp messaging, reminders, and escalations automatically

---

## Project Structure

```
WhatsAppBot/
├── main.py                 # FastAPI app — routes, dashboard, webhook, ngrok
├── whatsapp_client.py      # Meta Cloud API client & session tracking
├── notification_service.py # DB ↔ WhatsApp notification bridge
├── reminder_scheduler.py   # Background reminder & escalation scheduler
├── requirements.txt        # Python dependencies
├── .env                    # Credentials (not committed)
├── .gitignore
├── templates/
│   └── index.html          # Web dashboard
└── README.md
```

---

## Switching to Production

1. **Update timing constants** in `reminder_scheduler.py`:
   ```python
   REMINDER_3H_SECONDS = 10800    # 3 hours
   WARNING_6H_SECONDS  = 21600    # 6 hours
   DEADLINE_9H_SECONDS = 32400    # 9 hours
   CEO_REMINDER_INTERVAL = 10800  # 3 hours between CEO reminders
   CHECK_INTERVAL = 60            # check every minute
   ```

2. **Use a permanent access token** — generate a System User token from Meta Business Manager instead of the temporary 24-hour token.

3. **Use a static ngrok domain** or deploy behind a reverse proxy (Nginx/Caddy) with a proper domain and SSL instead of ngrok.

4. **Set up the database** on a production PostgreSQL server with proper backups.

---

## License

MIT
