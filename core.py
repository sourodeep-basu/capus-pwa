"""Pure logic (sqlite + stdlib only) for accounts, data sync, friends and calendar fetching.
Kept free of web-framework imports so it can be unit tested on its own."""
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

import database

SESSION_SECONDS = 90 * 86400
MAX_STATE_BYTES = 2_000_000
MAX_ICS_BYTES = 1_000_000
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
HANDLE_RE = re.compile(r"^[a-z0-9_]{3,20}$")
_fails = {}  # email -> recent failed login timestamps (in memory throttle)


def init(con):
    con.script(
        """
        CREATE TABLE IF NOT EXISTS users(
            id {PK}, email TEXT UNIQUE NOT NULL,
            salt TEXT NOT NULL, pw TEXT NOT NULL, created DOUBLE PRECISION);
        CREATE TABLE IF NOT EXISTS sessions(
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created DOUBLE PRECISION);
        CREATE TABLE IF NOT EXISTS userdata(
            user_id INTEGER PRIMARY KEY, state TEXT NOT NULL, ts DOUBLE PRECISION NOT NULL);
        CREATE TABLE IF NOT EXISTS profiles(
            user_id INTEGER PRIMARY KEY, handle TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS friendships(
            a INTEGER NOT NULL, b INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created DOUBLE PRECISION, PRIMARY KEY(a, b));
        CREATE TABLE IF NOT EXISTS user_stats(
            user_id INTEGER PRIMARY KEY, focus_week INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, updated DOUBLE PRECISION);
        CREATE TABLE IF NOT EXISTS snoozes(
            id {PK}, device_id TEXT, title TEXT, body TEXT, send_at DOUBLE PRECISION);
        """
    )
    con.commit()


# ---------- accounts ----------
def _hash(pw, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200_000).hex()


def _new_session(con, uid):
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sessions(token_hash,user_id,created) VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), uid, time.time()))
    con.commit()
    return token


def register(con, email, password):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address")
    if not (8 <= len(password or "") <= 200):
        raise ValueError("Password must be at least 8 characters")
    salt = secrets.token_hex(16)
    try:
        con.execute("INSERT INTO users(email,salt,pw,created) VALUES(?,?,?,?)",
                    (email, salt, _hash(password, salt), time.time()))
    except database.IntegrityError:
        con.rollback()
        raise ValueError("That email is already registered")
    con.commit()
    uid = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
    return _new_session(con, uid), False


def login(con, email, password):
    email = (email or "").strip().lower()
    now = time.time()
    recent = [t for t in _fails.get(email, []) if now - t < 600]
    if len(recent) >= 5:
        raise ValueError("Too many attempts. Try again in a few minutes")
    row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    ok = row is not None and hmac.compare_digest(_hash(password or "", row["salt"]), row["pw"])
    if not ok:
        _fails[email] = recent + [now]
        raise ValueError("Incorrect email or password")
    _fails.pop(email, None)
    has = con.execute("SELECT 1 FROM userdata WHERE user_id=?", (row["id"],)).fetchone() is not None
    return _new_session(con, row["id"]), has


def user_for(con, token):
    if not token:
        return None
    row = con.execute("SELECT user_id, created FROM sessions WHERE token_hash=?",
                      (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row or time.time() - row["created"] > SESSION_SECONDS:
        return None
    return row["user_id"]


def logout(con, token):
    con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    con.commit()


# ---------- data sync (whole-document, last write wins) ----------
def _clamp(n, hi=10_000_000):
    try:
        return max(0, min(int(n), hi))
    except (TypeError, ValueError):
        return 0


def save_state(con, uid, ts, state, stats=None):
    """Store the client's snapshot unless the server copy is newer; then return the server copy."""
    blob = json.dumps(state)
    if len(blob) > MAX_STATE_BYTES:
        raise ValueError("Data is too large to sync")
    row = con.execute("SELECT state, ts FROM userdata WHERE user_id=?", (uid,)).fetchone()
    if stats:
        con.execute(
            """INSERT INTO user_stats(user_id,focus_week,streak,xp,updated) VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET focus_week=excluded.focus_week,
               streak=excluded.streak, xp=excluded.xp, updated=excluded.updated""",
            (uid, _clamp(stats.get("focus_week")), _clamp(stats.get("streak")), _clamp(stats.get("xp")), time.time()))
    if row and row["ts"] > ts:
        con.commit()
        return {"ok": True, "ts": row["ts"], "state": json.loads(row["state"])}
    con.execute("""INSERT INTO userdata(user_id,state,ts) VALUES(?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, ts=excluded.ts""", (uid, blob, ts))
    con.commit()
    return {"ok": True, "ts": ts}


def load_state(con, uid):
    row = con.execute("SELECT state, ts FROM userdata WHERE user_id=?", (uid,)).fetchone()
    return {"ts": row["ts"], "state": json.loads(row["state"])} if row else {"ts": 0, "state": None}


# ---------- friends and leaderboard ----------
def set_handle(con, uid, handle, name):
    handle = (handle or "").strip().lower().lstrip("@")
    name = (name or "").strip()[:40]
    if not HANDLE_RE.match(handle):
        raise ValueError("Handle must be 3-20 letters, numbers or underscores")
    if not name:
        raise ValueError("Enter a display name")
    try:
        con.execute("""INSERT INTO profiles(user_id,handle,name) VALUES(?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET handle=excluded.handle, name=excluded.name""",
                    (uid, handle, name))
    except database.IntegrityError:
        con.rollback()
        raise ValueError("That handle is taken")
    con.commit()


def friend_request(con, uid, handle):
    if not con.execute("SELECT 1 FROM profiles WHERE user_id=?", (uid,)).fetchone():
        raise ValueError("Set your handle first")
    t = con.execute("SELECT user_id FROM profiles WHERE handle=?", ((handle or "").strip().lower().lstrip("@"),)).fetchone()
    if not t:
        raise ValueError("No user with that handle")
    t = t["user_id"]
    if t == uid:
        raise ValueError("That is your own handle")
    if con.execute("SELECT 1 FROM friendships WHERE a=? AND b=? AND status='pending'", (t, uid)).fetchone():
        con.execute("UPDATE friendships SET status='accepted' WHERE a=? AND b=?", (t, uid))
        con.commit()
        return "accepted"
    if con.execute("SELECT 1 FROM friendships WHERE (a=? AND b=?) OR (a=? AND b=?)", (uid, t, t, uid)).fetchone():
        raise ValueError("Already requested or already friends")
    con.execute("INSERT INTO friendships(a,b,status,created) VALUES(?,?,'pending',?)", (uid, t, time.time()))
    con.commit()
    return "sent"


def respond(con, uid, from_id, accept):
    if accept:
        con.execute("UPDATE friendships SET status='accepted' WHERE a=? AND b=? AND status='pending'", (from_id, uid))
    else:
        con.execute("DELETE FROM friendships WHERE a=? AND b=? AND status='pending'", (from_id, uid))
    con.commit()


def unfriend(con, uid, other):
    con.execute("DELETE FROM friendships WHERE (a=? AND b=?) OR (a=? AND b=?)", (uid, other, other, uid))
    con.commit()


def overview(con, uid):
    """Only totals are shared: handle, display name, focus minutes (7 days), best streak and XP."""
    def stat(u):
        r = con.execute(
            """SELECT p.user_id AS id, p.handle, p.name, COALESCE(s.focus_week,0) AS focus_week,
               COALESCE(s.streak,0) AS streak, COALESCE(s.xp,0) AS xp
               FROM profiles p LEFT JOIN user_stats s ON s.user_id=p.user_id WHERE p.user_id=?""", (u,)).fetchone()
        return dict(r) if r else None
    me = stat(uid)
    fids = [r["fid"] for r in con.execute(
        """SELECT CASE WHEN a=? THEN b ELSE a END AS fid FROM friendships
           WHERE status='accepted' AND (a=? OR b=?)""", (uid, uid, uid))]
    incoming = [dict(r) for r in con.execute(
        """SELECT p.user_id AS id, p.handle, p.name FROM friendships f JOIN profiles p ON p.user_id=f.a
           WHERE f.b=? AND f.status='pending'""", (uid,))]
    outgoing = [r["handle"] for r in con.execute(
        """SELECT p.handle FROM friendships f JOIN profiles p ON p.user_id=f.b
           WHERE f.a=? AND f.status='pending'""", (uid,))]
    friends = [x for x in (stat(f) for f in fids) if x]
    return {"me": me, "friends": friends, "incoming": incoming, "outgoing": outgoing}


# ---------- reminders helper ----------
def in_quiet(hm, qs, qe):
    """True if 'HH:MM' falls inside the quiet window (which may wrap past midnight)."""
    if not qs or not qe or qs == qe:
        return False
    return qs <= hm < qe if qs < qe else (hm >= qs or hm < qe)


# ---------- safe calendar download ----------
def _check_url(url):
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("Use an https:// calendar link")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("Could not find that host")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("That address is not allowed")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch_ics(url, hops=3):
    """Download an iCal feed. https only, public hosts only, size limited, redirects re-checked."""
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(hops + 1):
        _check_url(url)
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "CampusLife/1"}), timeout=10) as r:
                data = r.read(MAX_ICS_BYTES + 1)
                if len(data) > MAX_ICS_BYTES:
                    raise ValueError("Calendar file is too large")
                text = data.decode("utf-8", "replace")
                if "BEGIN:VCALENDAR" not in text:
                    raise ValueError("That link is not an iCal calendar")
                return text
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urljoin(url, e.headers["Location"])
                continue
            raise ValueError(f"Calendar server returned {e.code}")
        except urllib.error.URLError:
            raise ValueError("Could not reach the calendar")
    raise ValueError("Too many redirects")
