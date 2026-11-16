# Campus Life PWA with push reminders

FastAPI serves the app and sends Web Push notifications, so reminders arrive even when the app is closed.

## Run locally
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python gen_keys.py                                   # prints 3 lines; paste into a new .env file
uvicorn main:app --reload
```
Open http://localhost:8000 (localhost counts as secure, so push works for testing).
Click "Enable Reminders" in the Planner view and allow notifications.

## Deploy (HTTPS is required)
Render, Railway or Fly.io all work. On Render:
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment variables: `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT`, `DB_PATH`
- Free tiers sleep when idle and may have no persistent disk. A sleeping server cannot send
  reminders. Use a paid instance, or a free uptime pinger on `/api/health`.
  If the database is wiped, the app silently re-subscribes the next time it is opened.
- Keep the same VAPID keys forever. Changing them invalidates every existing subscription.

## Install on a phone
- Android Chrome: menu, then "Install app".
- iPhone (iOS 16.4+): Safari, Share, "Add to Home Screen", then open it from the home screen and enable reminders.
  Push does not work from a normal Safari tab on iPhone.

## How it works
1. `static/index.html` registers `sw.js` and, on "Enable Reminders", creates a push subscription.
2. The app sends the subscription and a small reminder summary (task titles and due times, lead time,
   check-in time, today's habit/class/journal counts) to `/api/subscribe` and `/api/sync`
   whenever data changes. Everything else stays in the browser's localStorage.
3. `main.py` checks every 30 seconds and pushes: task due soon, task overdue, and a daily check-in
   (habits, classes, journal). Each reminder is sent once.
4. `sw.js` shows the notification and focuses the app when tapped.

## Notes
- The random device ID acts as the device's secret. There are no user accounts, so do not expose
  this as a multi-user public service without adding authentication and rate limiting.
- Data lives in localStorage per browser; it does not sync between devices.
