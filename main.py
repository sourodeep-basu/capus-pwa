"""Campus Life server: serves the PWA, sends Web Push reminders, and provides accounts,
data sync, friends/leaderboard and calendar import."""
import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush

import core

load_dotenv()
BASE = Path(__file__).parent
STATIC = BASE / "static"
DB_PATH = os.environ.get("DB_PATH", str(BASE / "campus.db"))
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
CHECK_EVERY = 30  # seconds
TASK_ACTIONS = [{"action": "done", "title": "Mark done"}, {"action": "snooze", "title": "Snooze 10 min"}]


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices(
                device_id TEXT PRIMARY KEY, subscription TEXT NOT NULL,
                lead INTEGER DEFAULT 30, rt TEXT DEFAULT '20:00', tz INTEGER DEFAULT 0,
                tasks TEXT DEFAULT '[]', sync_date TEXT DEFAULT '',
                habits_total INTEGER DEFAULT 0, habits_done INTEGER DEFAULT 0,
                courses_total INTEGER DEFAULT 0, courses_marked INTEGER DEFAULT 0,
                journal_today INTEGER DEFAULT 0, updated REAL DEFAULT 0, slots TEXT DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS sent(device_id TEXT, key TEXT, ts REAL, PRIMARY KEY(device_id, key));
            """
        )
        # upgrade databases created by earlier versions
        for col, ddl in [("slots", "TEXT DEFAULT '[]'"), ("st", "TEXT DEFAULT '21:30'"),
                         ("qs", "TEXT DEFAULT ''"), ("qe", "TEXT DEFAULT ''"), ("streaks", "TEXT DEFAULT '[]'")]:
            try:
                con.execute(f"ALTER TABLE devices ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
        con.commit()
        core.init(con)


# ---------- push scheduler ----------
def push(con, dev, title, body, extra=None):
    payload = {"title": title, "body": body}
    payload.update(extra or {})
    try:
        webpush(subscription_info=json.loads(dev["subscription"]), data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE, vapid_claims={"sub": VAPID_SUBJECT})
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):  # subscription expired or revoked
            con.execute("DELETE FROM devices WHERE device_id=?", (dev["device_id"],))
            con.commit()
        else:
            print("push failed:", e)
    except Exception as e:  # one bad device must not stop the loop
        print("push error:", e)


def send_once(con, dev, key, title, body, extra=None):
    """Record the key first so a reminder is never sent twice, then push it."""
    cur = con.execute("INSERT OR IGNORE INTO sent(device_id,key,ts) VALUES(?,?,?)", (dev["device_id"], key, time.time()))
    con.commit()
    if cur.rowcount:
        push(con, dev, title, body, dict(extra or {}, tag=key))


def run_checks():
    now_ms = time.time() * 1000
    with closing(db()) as con:
        con.execute("DELETE FROM sent WHERE ts < ?", (time.time() - 3 * 86400,))
        # snoozed reminders that are now due
        for sn in con.execute("SELECT * FROM snoozes WHERE send_at <= ?", (time.time(),)).fetchall():
            con.execute("DELETE FROM snoozes WHERE id=?", (sn["id"],))
            con.commit()
            dev = con.execute("SELECT * FROM devices WHERE device_id=?", (sn["device_id"],)).fetchone()
            if dev:
                push(con, dev, sn["title"], sn["body"], {"tag": f"snooze:{sn['id']}"})
        for dev in con.execute("SELECT * FROM devices").fetchall():
            local = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=dev["tz"])
            hm = local.strftime("%H:%M")
            if core.in_quiet(hm, dev["qs"], dev["qe"]):
                continue  # nothing is marked as sent, so reminders resume after quiet hours
            today = local.strftime("%Y-%m-%d")
            lead_ms = dev["lead"] * 60000
            for t in json.loads(dev["tasks"]):
                d = t["due"] - now_ms
                ex = {"task_id": t["id"], "actions": TASK_ACTIONS}
                if 0 < d <= lead_ms:
                    send_once(con, dev, f"d:{t['id']}:{int(t['due'])}", "Task due soon", f"Due soon: {t['t']}", ex)
                elif -86400000 < d <= 0:
                    send_once(con, dev, f"o:{t['id']}:{int(t['due'])}", "Task overdue", f"Overdue: {t['t']}", ex)
            nowmin = local.hour * 60 + local.minute
            jsdow = (local.weekday() + 1) % 7  # JS getDay(): Sunday is 0
            for sl in json.loads(dev["slots"] or "[]"):
                gap = sl["s"] - nowmin
                if sl["dow"] == jsdow and 0 < gap <= 15:
                    send_once(con, dev, f"c:{sl['id']}:{today}", "Class soon", f"{sl['n']} starts in {gap} minutes")
            streaks = json.loads(dev["streaks"] or "[]")
            if streaks and hm >= (dev["st"] or "21:30"):
                names = ", ".join(f"{x['n']} ({x['s']} days)" for x in streaks[:4])
                send_once(con, dev, f"k:{today}", "Streak dies soon", f"{names} - finish before midnight to keep it")
            if hm < dev["rt"]:
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


# ---------- models ----------
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
    s: int = Field(ge=0, le=1439)


class StreakIn(BaseModel):
    n: str = Field(max_length=100)
    s: int = Field(ge=0, le=100000)


class SyncIn(BaseModel):
    device_id: str = Field(min_length=20, max_length=128)
    lead: int = Field(default=30, ge=1, le=10080)
    rt: str = Field(default="20:00", pattern=r"^\d{2}:\d{2}$")
    st: str = Field(default="21:30", pattern=r"^\d{2}:\d{2}$")
    qs: str = Field(default="", pattern=r"^(\d{2}:\d{2})?$")
    qe: str = Field(default="", pattern=r"^(\d{2}:\d{2})?$")
    tz: int = Field(default=0, ge=-840, le=840)  # JS getTimezoneOffset(): UTC minus local
    tasks: list[TaskIn] = Field(default_factory=list, max_length=500)
    date: str = Field(default="", max_length=10)
    habits_total: int = Field(default=0, ge=0)
    habits_done: int = Field(default=0, ge=0)
    courses_total: int = Field(default=0, ge=0)
    courses_marked: int = Field(default=0, ge=0)
    journal_today: bool = False
    slots: list[SlotIn] = Field(default_factory=list, max_length=100)
    streaks: list[StreakIn] = Field(default_factory=list, max_length=50)


class DevIn(BaseModel):
    device_id: str = Field(min_length=20, max_length=128)


class SnoozeIn(DevIn):
    title: str = Field(max_length=200)
    body: str = Field(max_length=400)
    minutes: int = Field(default=10, ge=1, le=240)


class AuthIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class PushStateIn(BaseModel):
    ts: float = Field(gt=0)
    state: dict
    stats: Optional[dict] = None


class HandleIn(BaseModel):
    handle: str = Field(max_length=40)
    name: str = Field(max_length=80)


class RequestIn(BaseModel):
    handle: str = Field(max_length=40)


class RespondIn(BaseModel):
    from_id: int
    accept: bool


class UnfriendIn(BaseModel):
    user_id: int


class UrlIn(BaseModel):
    url: str = Field(max_length=2000)


def uid_of(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not signed in")
    with closing(db()) as con:
        uid = core.user_for(con, authorization[7:])
    if not uid:
        raise HTTPException(401, "Session expired. Please sign in again")
    return uid


def bad(e):
    return HTTPException(400, str(e))


# ---------- push API ----------
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
            (b.device_id, json.dumps(b.subscription), time.time()))
        con.commit()
    return {"ok": True}


@app.post("/api/sync")
def sync(b: SyncIn):
    with closing(db()) as con:
        cur = con.execute(
            """UPDATE devices SET lead=?, rt=?, st=?, qs=?, qe=?, tz=?, tasks=?, sync_date=?, habits_total=?,
               habits_done=?, courses_total=?, courses_marked=?, journal_today=?, updated=?, slots=?, streaks=?
               WHERE device_id=?""",
            (b.lead, b.rt, b.st, b.qs, b.qe, b.tz, json.dumps([t.model_dump() for t in b.tasks]), b.date,
             b.habits_total, b.habits_done, b.courses_total, b.courses_marked, int(b.journal_today), time.time(),
             json.dumps([x.model_dump() for x in b.slots]), json.dumps([x.model_dump() for x in b.streaks]),
             b.device_id))
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "unknown device")  # client re-subscribes on 404
    return {"ok": True}


@app.post("/api/unsubscribe")
def unsubscribe(b: DevIn):
    with closing(db()) as con:
        con.execute("DELETE FROM devices WHERE device_id=?", (b.device_id,))
        con.execute("DELETE FROM sent WHERE device_id=?", (b.device_id,))
        con.execute("DELETE FROM snoozes WHERE device_id=?", (b.device_id,))
        con.commit()
    return {"ok": True}


@app.post("/api/snooze")
def snooze(b: SnoozeIn):
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM devices WHERE device_id=?", (b.device_id,)).fetchone():
            raise HTTPException(404, "unknown device")
        con.execute("INSERT INTO snoozes(device_id,title,body,send_at) VALUES(?,?,?,?)",
                    (b.device_id, b.title, b.body, time.time() + b.minutes * 60))
        con.commit()
    return {"ok": True}


# ---------- accounts and data sync ----------
@app.post("/api/auth/register")
def register(b: AuthIn):
    with closing(db()) as con:
        try:
            token, has = core.register(con, b.email, b.password)
        except ValueError as e:
            raise bad(e)
    return {"token": token, "has_data": has}


@app.post("/api/auth/login")
def login(b: AuthIn):
    with closing(db()) as con:
        try:
            token, has = core.login(con, b.email, b.password)
        except ValueError as e:
            raise bad(e)
    return {"token": token, "has_data": has}


@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    uid_of(authorization)
    with closing(db()) as con:
        core.logout(con, authorization[7:])
    return {"ok": True}


@app.post("/api/data/push")
def data_push(b: PushStateIn, authorization: Optional[str] = Header(default=None)):
    uid = uid_of(authorization)
    with closing(db()) as con:
        try:
            return core.save_state(con, uid, b.ts, b.state, b.stats)
        except ValueError as e:
            raise bad(e)


@app.get("/api/data/pull")
def data_pull(authorization: Optional[str] = Header(default=None)):
    uid = uid_of(authorization)
    with closing(db()) as con:
        return core.load_state(con, uid)


# ---------- friends ----------
@app.post("/api/social/handle")
def social_handle(b: HandleIn, authorization: Optional[str] = Header(default=None)):
    uid = uid_of(authorization)
    with closing(db()) as con:
        try:
            core.set_handle(con, uid, b.handle, b.name)
        except ValueError as e:
            raise bad(e)
    return {"ok": True}


@app.post("/api/social/request")
def social_request(b: RequestIn, authorization: Optional[str] = Header(default=None)):
    uid = uid_of(authorization)
    with closing(db()) as con:
        try:
            return {"result": core.friend_request(con, uid, b.handle)}
        except ValueError as e:
            raise bad(e)


@app.post("/api/social/respond")
def social_respond(b: RespondIn, authorization: Optional[str] = Header(default=None)):
    uid = uid_of(authorization)
    with closing(db()) as con:
        core.respond(con, uid, b.from_id, b.accept)
    return {"ok": True}


@app.post("/api/social/unfriend")
def social_unfriend(b: UnfriendIn, authorization: Optional[str] = Header(default=None)):
    uid = uid_of(authorization)
    with closing(db()) as con:
        core.unfriend(con, uid, b.user_id)
    return {"ok": True}


@app.get("/api/social/overview")
def social_overview(authorization: Optional[str] = Header(default=None)):
    uid = uid_of(authorization)
    with closing(db()) as con:
        return core.overview(con, uid)


# ---------- calendar import ----------
@app.post("/api/calendar/fetch")
def calendar_fetch(b: UrlIn, authorization: Optional[str] = Header(default=None)):
    uid_of(authorization)  # signed-in users only, so the server is not an open proxy
    try:
        return {"text": core.fetch_ics(b.url)}
    except ValueError as e:
        raise bad(e)


@app.get("/api/health")
def health():
    return {"ok": True, "vapid": bool(VAPID_PRIVATE and VAPID_PUBLIC)}


# ---------- static app ----------
@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
