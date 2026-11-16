"""Campus Life push server: stores push subscriptions and reminder data, and sends
Web Push notifications when reminders are due, even if the app is closed."""
import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush

load_dotenv()
BASE = Path(__file__).parent
STATIC = BASE / "static"
DB_PATH = os.environ.get("DB_PATH", str(BASE / "campus.db"))
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
CHECK_EVERY = 30  # seconds


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices(
                device_id TEXT PRIMARY KEY,
                subscription TEXT NOT NULL,
                lead INTEGER DEFAULT 30,
                rt TEXT DEFAULT '20:00',
                tz INTEGER DEFAULT 0,
                tasks TEXT DEFAULT '[]',
                sync_date TEXT DEFAULT '',
                habits_total INTEGER DEFAULT 0,
                habits_done INTEGER DEFAULT 0,
                courses_total INTEGER DEFAULT 0,
                courses_marked INTEGER DEFAULT 0,
                journal_today INTEGER DEFAULT 0,
                updated REAL DEFAULT 0,
                slots TEXT DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS sent(
                device_id TEXT, key TEXT, ts REAL,
                PRIMARY KEY(device_id, key)
            );
            """
        )
        try:  # upgrade databases created before timetable support
            con.execute("ALTER TABLE devices ADD COLUMN slots TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        con.commit()


# ---------- scheduler ----------
def send_once(con, dev, key, title, body):
    """Record the key first so a reminder is never sent twice, then push it."""
    cur = con.execute(
        "INSERT OR IGNORE INTO sent(device_id,key,ts) VALUES(?,?,?)",
        (dev["device_id"], key, time.time()),
    )
    con.commit()
    if cur.rowcount == 0:
        return
    try:
        webpush(
            subscription_info=json.loads(dev["subscription"]),
            data=json.dumps({"title": title, "body": body, "tag": key}),
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):  # subscription expired or revoked
            con.execute("DELETE FROM devices WHERE device_id=?", (dev["device_id"],))
            con.commit()
        else:
            print("push failed:", e)
    except Exception as e:  # never let one device stop the loop
        print("push error:", e)


def run_checks():
    now_ms = time.time() * 1000
    with closing(db()) as con:
        con.execute("DELETE FROM sent WHERE ts < ?", (time.time() - 3 * 86400,))
        for dev in con.execute("SELECT * FROM devices").fetchall():
            lead_ms = dev["lead"] * 60000
            for t in json.loads(dev["tasks"]):
                d = t["due"] - now_ms
                if 0 < d <= lead_ms:
                    send_once(con, dev, f"d:{t['id']}:{int(t['due'])}", "Task due soon", f"{t['t']} is due soon")
                elif -86400000 < d <= 0:
                    send_once(con, dev, f"o:{t['id']}:{int(t['due'])}", "Task overdue", f"{t['t']} is past its deadline")

            local = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=dev["tz"])
            today = local.strftime("%Y-%m-%d")
            nowmin = local.hour * 60 + local.minute
            jsdow = (local.weekday() + 1) % 7  # JS getDay(): Sunday is 0
            for sl in json.loads(dev["slots"] or "[]"):
                gap = sl["s"] - nowmin
                if sl["dow"] == jsdow and 0 < gap <= 15:
                    send_once(con, dev, f"c:{sl['id']}:{today}", "Class soon", f"{sl['n']} starts in {gap} minutes")
            if local.strftime("%H:%M") < dev["rt"]:
                continue
            fresh = dev["sync_date"] == today  # has the app reported today's progress?
            hp = dev["habits_total"] - (dev["habits_done"] if fresh else 0)
            cp = dev["courses_total"] - (dev["courses_marked"] if fresh else 0)
            if hp > 0:
                send_once(con, dev, f"h:{today}", "Habit check-in", f"{hp} habit(s) not done today")
            if cp > 0:
                send_once(con, dev, f"a:{today}", "Attendance", f"{cp} class(es) not marked today")
            if not (fresh and dev["journal_today"]):
                send_once(con, dev, f"j:{today}", "Daily reflection", "You have not written a journal entry today")


async def scheduler():
    while True:
        try:
            await asyncio.to_thread(run_checks)
        except Exception as e:
            print("scheduler error:", e)
        await asyncio.sleep(CHECK_EVERY)


@asynccontextmanager
async def lifespan(app):
    if not (VAPID_PRIVATE and VAPID_PUBLIC):
        print("WARNING: VAPID keys missing. Run gen_keys.py and fill in .env")
    init_db()
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(title="Campus Life", lifespan=lifespan)


# ---------- API ----------
class SubIn(BaseModel):
    device_id: str = Field(min_length=20, max_length=128)
    subscription: dict


class TaskIn(BaseModel):
    id: str = Field(max_length=64)
    t: str = Field(max_length=300)
    due: float


class SlotIn(BaseModel):
    id: str = Field(max_length=100)
    n: str = Field(max_length=200)
    dow: int = Field(ge=0, le=6)
    s: int = Field(ge=0, le=1439)  # start, minutes after local midnight


class SyncIn(BaseModel):
    device_id: str = Field(min_length=20, max_length=128)
    lead: int = Field(default=30, ge=1, le=10080)
    rt: str = Field(default="20:00", pattern=r"^\d{2}:\d{2}$")
    tz: int = Field(default=0, ge=-840, le=840)  # JS getTimezoneOffset(): UTC minus local
    tasks: list[TaskIn] = Field(default_factory=list, max_length=500)
    date: str = Field(default="", max_length=10)
    habits_total: int = Field(default=0, ge=0)
    habits_done: int = Field(default=0, ge=0)
    courses_total: int = Field(default=0, ge=0)
    courses_marked: int = Field(default=0, ge=0)
    journal_today: bool = False
    slots: list[SlotIn] = Field(default_factory=list, max_length=100)


class DevIn(BaseModel):
    device_id: str = Field(min_length=20, max_length=128)


@app.get("/api/vapid-public-key")
def public_key():
    if not VAPID_PUBLIC:
        raise HTTPException(503, "VAPID keys not configured")
    return {"key": VAPID_PUBLIC}


@app.post("/api/subscribe")
def subscribe(b: SubIn):
    if "endpoint" not in b.subscription:
        raise HTTPException(422, "invalid subscription")
    with closing(db()) as con:
        con.execute(
            """INSERT INTO devices(device_id, subscription, updated) VALUES(?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET subscription=excluded.subscription, updated=excluded.updated""",
            (b.device_id, json.dumps(b.subscription), time.time()),
        )
        con.commit()
    return {"ok": True}


@app.post("/api/sync")
def sync(b: SyncIn):
    with closing(db()) as con:
        cur = con.execute(
            """UPDATE devices SET lead=?, rt=?, tz=?, tasks=?, sync_date=?, habits_total=?,
               habits_done=?, courses_total=?, courses_marked=?, journal_today=?, updated=?, slots=?
               WHERE device_id=?""",
            (b.lead, b.rt, b.tz, json.dumps([t.model_dump() for t in b.tasks]), b.date,
             b.habits_total, b.habits_done, b.courses_total, b.courses_marked,
             int(b.journal_today), time.time(), json.dumps([x.model_dump() for x in b.slots]), b.device_id),
        )
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "unknown device")  # client re-subscribes on 404
    return {"ok": True}


@app.post("/api/unsubscribe")
def unsubscribe(b: DevIn):
    with closing(db()) as con:
        con.execute("DELETE FROM devices WHERE device_id=?", (b.device_id,))
        con.execute("DELETE FROM sent WHERE device_id=?", (b.device_id,))
        con.commit()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "vapid": bool(VAPID_PRIVATE and VAPID_PUBLIC)}


# ---------- static app ----------
@app.get("/sw.js")
def service_worker():
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")