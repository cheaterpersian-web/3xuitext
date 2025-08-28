## Telegram 3X-UI Bot

Features:
- Admin login to 3X-UI (`POST /login`)
- List inbounds (`GET /inbounds/list`)
- Create client (`POST /client/add`) with inbound, volume, days, username
- View own configs (`GET /client/traffics`, `GET /client/options` optional)
- Admin limit per numeric ID stored in SQLite

### Setup
1) Copy `.env.example` to `.env` and fill values
2) Python 3.11+
3) Install deps:
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Run
```bash
python main.py
```

### Environment
- `TELEGRAM_BOT_TOKEN`: bot token
- `BOT_ADMIN_IDS`: comma-separated numeric IDs who are admins (optional)
- `PER_USER_LIMIT`: default per-user config limit
- `PANEL_BASE_URL`, `PANEL_USERNAME`, `PANEL_PASSWORD`, `PANEL_INSECURE`
- `SUBSCRIPTION_BASE_URL` optional fallback

