"""Backend he thong Radar IoT - FastAPI + SQLite.

Chay:  uvicorn main:app --host 0.0.0.0 --port 8000
Docs:  http://localhost:8000/docs
Push thong bao: can phuc vu dashboard qua HTTPS; khoa VAPID duoc tao va luu trong vapid_private.pem.
Dat RADAR_VAPID_SUBJECT thanh mailto lien he cua quan tri truoc khi trien khai.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

DB_PATH = "radar.db"
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
VAPID_PRIVATE_KEY_PATH = os.path.join(BACKEND_DIR, "vapid_private.pem")
STATES = ["DISARMED", "ARMED", "DETECTED", "ALARMING", "ACKNOWLEDGED"]
ONLINE_TIMEOUT_S = 30  # qua 30 giay khong thay thiet bi => offline
TOKEN_TTL_S = 12 * 3600  # phien dang nhap 12 gio
CAMERA_STREAM_URL = os.environ.get("RADAR_CAMERA_STREAM_URL", "http://127.0.0.1:8001/stream")
logger = logging.getLogger("radar.push")

# Khoa cho thiet bi (ESP32, simulator, ai_service) gui du lieu. DOI TRUOC KHI TRIEN KHAI THAT.
DEVICE_KEY = os.environ.get("RADAR_DEVICE_KEY", "radar-device-key")
ADMIN_DEFAULT_PASSWORD = os.environ.get("RADAR_ADMIN_PASSWORD", "admin123")


def load_secret() -> bytes:
    """Khoa ky token: tao ngau nhien lan dau, luu vao secret.key (dung commit len git)."""
    if os.path.exists("secret.key"):
        return open("secret.key", "rb").read()
    key = secrets.token_bytes(32)
    open("secret.key", "wb").write(key)
    return key


SECRET = load_secret()


def load_vapid_keys() -> str:
    if os.path.exists(VAPID_PRIVATE_KEY_PATH):
        with open(VAPID_PRIVATE_KEY_PATH, "rb") as key_file:
            private_key = serialization.load_pem_private_key(key_file.read(), password=None)
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with open(VAPID_PRIVATE_KEY_PATH, "wb") as key_file:
            key_file.write(pem)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise RuntimeError("VAPID private key must use the P-256 curve")
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    public_key = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
    return public_key


VAPID_PUBLIC_KEY = load_vapid_keys()
VAPID_SUBJECT = os.environ.get("RADAR_VAPID_SUBJECT", "mailto:admin@example.com")

app = FastAPI(title="Radar IoT Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse("dashboard.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        os.path.join(BACKEND_DIR, "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        os.path.join(BACKEND_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/icon.svg")
def app_icon():
    return FileResponse(os.path.join(BACKEND_DIR, "icon.svg"), media_type="image/svg+xml")


# ---------- Database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices(
                id TEXT PRIMARY KEY, name TEXT, zone TEXT,
                state TEXT, moving INTEGER, stationary INTEGER, last_seen REAL,
                radar_ok INTEGER, source TEXT, moving_distance_cm INTEGER,
                stationary_distance_cm INTEGER, light_on INTEGER, buzzer_on INTEGER);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, state TEXT,
                moving INTEGER, stationary INTEGER, ts REAL,
                radar_ok INTEGER, source TEXT, moving_distance_cm INTEGER,
                stationary_distance_cm INTEGER, light_on INTEGER, buzzer_on INTEGER);
            CREATE TABLE IF NOT EXISTS alerts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
                started_at REAL, acked_at REAL, cleared_at REAL, status TEXT);
            CREATE TABLE IF NOT EXISTS users(
                username TEXT PRIMARY KEY, pw TEXT, role TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS camera_settings(
                device_id TEXT PRIMARY KEY, enabled INTEGER);
            CREATE TABLE IF NOT EXISTS camera(
                device_id TEXT PRIMARY KEY, persons INTEGER, last_seen REAL);
            CREATE TABLE IF NOT EXISTS commands(
                id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
                cmd TEXT, created REAL, delivered INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS push_subscriptions(
                endpoint TEXT PRIMARY KEY, username TEXT NOT NULL,
                subscription_json TEXT NOT NULL, created REAL NOT NULL);
            """
        )
        for table, columns in {
            "devices": {
                "radar_ok": "INTEGER", "source": "TEXT",
                "moving_distance_cm": "INTEGER", "stationary_distance_cm": "INTEGER",
                "light_on": "INTEGER", "buzzer_on": "INTEGER",
            },
            "events": {
                "radar_ok": "INTEGER", "source": "TEXT",
                "moving_distance_cm": "INTEGER", "stationary_distance_cm": "INTEGER",
                "light_on": "INTEGER", "buzzer_on": "INTEGER",
            },
        }.items():
            existing = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
            for name, data_type in columns.items():
                if name not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {data_type}")
        c.commit()


# ---------- Mat khau + token ----------
def hash_pw(pw: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split("$")
    return hmac.compare_digest(hash_pw(pw, salt).split("$")[1], h)


def make_token(username: str) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time()) + TOKEN_TTL_S}, separators=(",", ":"))
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def make_camera_stream_ticket(username: str) -> str:
    payload = json.dumps(
        {"u": username, "exp": int(time.time()) + 60, "scope": "camera_stream"},
        separators=(",", ":"),
    )
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def valid_camera_stream_ticket(ticket: str) -> bool:
    try:
        body, sig = ticket.rsplit(".", 1)
        expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data.get("scope") != "camera_stream" or data.get("exp", 0) < time.time():
            return False
        with closing(db()) as c:
            return c.execute(
                "SELECT 1 FROM users WHERE username=?", (data.get("u"),)
            ).fetchone() is not None
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def user_from_token(token: Optional[str]):
    """Tra ve {'username','role'} neu token hop le va tai khoan con ton tai, nguoc lai None."""
    try:
        body, sig = (token or "").rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data["exp"] < time.time():
            return None
        with closing(db()) as c:
            r = c.execute("SELECT username, role FROM users WHERE username=?", (data["u"],)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def seed_admin():
    with closing(db()) as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            c.execute("INSERT INTO users VALUES(?,?,?,?)",
                      ("admin", hash_pw(ADMIN_DEFAULT_PASSWORD), "admin", time.time()))
            c.commit()
            print(f"[!] Da tao tai khoan admin / {ADMIN_DEFAULT_PASSWORD} - hay doi mat khau sau khi dang nhap.")


init_db()
seed_admin()

bearer = HTTPBearer(auto_error=False)


def current_user(cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    user = user_from_token(cred.credentials if cred else None)
    if user is None:
        raise HTTPException(401, "Chua dang nhap hoac phien da het han")
    return user


def admin_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Can quyen admin")
    return user


@app.get("/api/camera/stream-ticket")
def camera_stream_ticket(user: dict = Depends(current_user)):
    return {"ticket": make_camera_stream_ticket(user["username"])}


@app.get("/api/camera/stream")
def camera_stream(ticket: str = Query(..., min_length=1)):
    if not valid_camera_stream_ticket(ticket):
        raise HTTPException(401, "Ve xem camera khong hop le hoac da het han")
    try:
        upstream = urllib.request.urlopen(CAMERA_STREAM_URL, timeout=5)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        logger.warning("Khong ket noi duoc luong camera tai %s: %s", CAMERA_STREAM_URL, error)
        raise HTTPException(503, "Luong camera laptop chua san sang") from error

    def frames():
        try:
            with upstream:
                while True:
                    chunk = upstream.read1(8192)
                    if not chunk:
                        break
                    yield chunk
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            logger.warning("Mat ket noi luong camera laptop: %s", error)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def device_auth(x_device_key: Optional[str] = Header(None)):
    if not x_device_key or not hmac.compare_digest(x_device_key, DEVICE_KEY):
        raise HTTPException(401, "Sai khoa thiet bi (X-Device-Key)")


login_fails: dict = {}   # username -> [so lan sai, thoi diem sai gan nhat]


# ---------- WebSocket broadcast ----------
class Hub:
    def __init__(self):
        self.clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


hub = Hub()


# ---------- Schemas ----------
class EventIn(BaseModel):
    device_id: str
    state: Literal["DISARMED", "ARMED", "DETECTED", "ALARMING", "ACKNOWLEDGED"]
    moving_energy: int = 0
    stationary_energy: int = 0
    radar_ok: Optional[bool] = None
    source: Literal["radar", "simulation", "simulator", "unavailable"] = "unavailable"
    moving_distance_cm: Optional[int] = None
    stationary_distance_cm: Optional[int] = None
    light_on: Optional[bool] = None
    buzzer_on: Optional[bool] = None
    ts: Optional[float] = None  # giay (epoch); thieu thi lay gio server


class HeartbeatIn(BaseModel):
    device_id: str
    rssi: Optional[int] = None


class CameraIn(BaseModel):
    device_id: str
    persons: int


class LoginIn(BaseModel):
    username: str
    password: str


class NewUserIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=100)
    role: Literal["admin", "user"] = "user"


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=100)


class CameraEnableIn(BaseModel):
    enabled: bool


class PushKeysIn(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushKeysIn
    expirationTime: Optional[int] = None


class PushUnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)


class CommandIn(BaseModel):
    cmd: Literal[
        "arm", "disarm", "ack", "light_on", "light_off",
        "buzzer_on", "buzzer_off", "simulate_on", "simulate_off",
    ]


# ---------- Helpers ----------
def ensure_device(c, device_id: str):
    c.execute(
        "INSERT OR IGNORE INTO devices(id, name, zone, state, moving, stationary, last_seen)"
        " VALUES(?,?,?,?,?,?,?)",
        (device_id, device_id, "Chua gan khu vuc", "ARMED", 0, 0, time.time()),
    )


def update_alerts(c, device_id: str, state: str, ts: float):
    open_alert = c.execute(
        "SELECT * FROM alerts WHERE device_id=? AND status IN ('OPEN','ACKED') ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    if state == "ALARMING" and open_alert is None:
        c.execute(
            "INSERT INTO alerts(device_id, started_at, status) VALUES(?,?,'OPEN')", (device_id, ts)
        )
    elif state == "ACKNOWLEDGED" and open_alert is not None:
        c.execute("UPDATE alerts SET status='ACKED', acked_at=? WHERE id=?", (ts, open_alert["id"]))
    elif state in ("ARMED", "DISARMED") and open_alert is not None:
        c.execute("UPDATE alerts SET status='CLEARED', cleared_at=? WHERE id=?", (ts, open_alert["id"]))


def device_row(r) -> dict:
    d = dict(r)
    d["online"] = (time.time() - (d["last_seen"] or 0)) < ONLINE_TIMEOUT_S
    return d


def camera_enabled(c, device_id: str) -> bool:
    r = c.execute("SELECT enabled FROM camera_settings WHERE device_id=?", (device_id,)).fetchone()
    return True if r is None else bool(r["enabled"])


def camera_info(c, device_id: str) -> dict:
    enabled = camera_enabled(c, device_id)
    r = c.execute("SELECT persons, last_seen FROM camera WHERE device_id=?", (device_id,)).fetchone()
    if not enabled or r is None or time.time() - r["last_seen"] > 5:
        return {"camera_persons": None, "camera_enabled": enabled}   # tat / chua ket noi / mat tin hieu
    return {"camera_persons": r["persons"], "camera_enabled": enabled}


def send_push_notifications(event: dict):
    if event["source"] in ("simulation", "simulator") or event["radar_ok"] is False:
        title = f"THỬ NGHIỆM - {event['device_id']}"
        body = "Mô phỏng phát hiện người; radar LD2410C chưa kết nối."
    else:
        title = f"CẢNH BÁO XÂM NHẬP - {event['device_id']}"
        body = (
            f"Phát hiện người. Tín hiệu chuyển động: {event['moving_energy']}%, "
            f"đứng yên: {event['stationary_energy']}%."
        )
    payload = json.dumps(
        {"title": title, "body": body, "url": "/", "tag": f"radar-{event['device_id']}"}
    )
    with closing(db()) as c:
        subscriptions = c.execute(
            "SELECT endpoint, subscription_json FROM push_subscriptions"
        ).fetchall()
    expired = []
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=json.loads(subscription["subscription_json"]),
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY_PATH,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=60,
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("Push delivery failed (HTTP %s): %s", status_code, exc)
            if status_code in (404, 410):
                expired.append(subscription["endpoint"])
        except (OSError, ValueError) as exc:
            logger.warning("Push delivery failed: %s", exc)
    if expired:
        with closing(db()) as c:
            c.executemany("DELETE FROM push_subscriptions WHERE endpoint=?", [(e,) for e in expired])
            c.commit()


# ---------- API: thiet bi gui len ----------
@app.post("/api/events", dependencies=[Depends(device_auth)])
async def post_event(ev: EventIn, background_tasks: BackgroundTasks):
    ts = ev.ts or time.time()
    with closing(db()) as c:
        ensure_device(c, ev.device_id)
        previous = c.execute(
            "SELECT state, last_seen FROM devices WHERE id=?", (ev.device_id,)
        ).fetchone()
        new_alarm = ev.state == "ALARMING" and (
            previous is None
            or previous["state"] != "ALARMING"
            or time.time() - (previous["last_seen"] or 0) >= ONLINE_TIMEOUT_S
        )
        c.execute(
            "INSERT INTO events(device_id, state, moving, stationary, ts, radar_ok, source, "
            "moving_distance_cm, stationary_distance_cm, light_on, buzzer_on) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ev.device_id, ev.state, ev.moving_energy, ev.stationary_energy, ts,
             None if ev.radar_ok is None else int(ev.radar_ok), ev.source,
             ev.moving_distance_cm, ev.stationary_distance_cm,
             None if ev.light_on is None else int(ev.light_on),
             None if ev.buzzer_on is None else int(ev.buzzer_on)),
        )
        c.execute(
            "UPDATE devices SET state=?, moving=?, stationary=?, last_seen=?, radar_ok=?, source=?, "
            "moving_distance_cm=COALESCE(?,moving_distance_cm), "
            "stationary_distance_cm=COALESCE(?,stationary_distance_cm), "
            "light_on=COALESCE(?,light_on), buzzer_on=COALESCE(?,buzzer_on) "
            "WHERE id=?",
            (ev.state, ev.moving_energy, ev.stationary_energy, time.time(),
             None if ev.radar_ok is None else int(ev.radar_ok), ev.source,
             ev.moving_distance_cm, ev.stationary_distance_cm,
             None if ev.light_on is None else int(ev.light_on),
             None if ev.buzzer_on is None else int(ev.buzzer_on), ev.device_id),
        )
        update_alerts(c, ev.device_id, ev.state, ts)
        c.commit()
    await hub.broadcast({"type": "event", **ev.model_dump(), "ts": ts})
    if new_alarm:
        background_tasks.add_task(send_push_notifications, ev.model_dump())
    return {"ok": True}


@app.post("/api/heartbeat", dependencies=[Depends(device_auth)])
async def heartbeat(hb: HeartbeatIn):
    with closing(db()) as c:
        ensure_device(c, hb.device_id)
        c.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), hb.device_id))
        c.commit()
    return {"ok": True}


@app.post("/api/camera", dependencies=[Depends(device_auth)])
async def post_camera(cam: CameraIn):
    with closing(db()) as c:
        ensure_device(c, cam.device_id)
        c.execute(
            "INSERT INTO camera(device_id, persons, last_seen) VALUES(?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET persons=excluded.persons, last_seen=excluded.last_seen",
            (cam.device_id, cam.persons, time.time()),
        )
        c.commit()
    await hub.broadcast({"type": "camera", "device_id": cam.device_id, "persons": cam.persons})
    return {"ok": True}


@app.get("/api/camera/{device_id}/config", dependencies=[Depends(device_auth)])
def camera_config(device_id: str):
    """ai_service hoi dinh ky: camera dang duoc bat hay tat tu web."""
    with closing(db()) as c:
        return {"enabled": camera_enabled(c, device_id)}


@app.post("/api/camera/{device_id}/enable", dependencies=[Depends(admin_only)])
async def set_camera_enabled(device_id: str, body: CameraEnableIn):
    with closing(db()) as c:
        c.execute(
            "INSERT INTO camera_settings(device_id, enabled) VALUES(?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET enabled=excluded.enabled",
            (device_id, int(body.enabled)),
        )
        c.commit()
    await hub.broadcast({"type": "camera_config", "device_id": device_id, "enabled": body.enabled})
    return {"ok": True, "enabled": body.enabled}


@app.get("/api/push/config", dependencies=[Depends(current_user)])
def push_config():
    return {"public_key": VAPID_PUBLIC_KEY, "subject": VAPID_SUBJECT}


@app.get("/api/push/status", dependencies=[Depends(current_user)])
def push_status(user: dict = Depends(current_user)):
    with closing(db()) as c:
        endpoints = [
            row["endpoint"]
            for row in c.execute(
                "SELECT endpoint FROM push_subscriptions WHERE username=?", (user["username"],)
            )
        ]
    return {"enabled": bool(endpoints), "count": len(endpoints), "endpoints": endpoints}


@app.post("/api/push/subscribe", dependencies=[Depends(current_user)])
def push_subscribe(body: PushSubscriptionIn, user: dict = Depends(current_user)):
    subscription = body.model_dump(exclude_none=True)
    with closing(db()) as c:
        c.execute(
            "INSERT INTO push_subscriptions(endpoint, username, subscription_json, created) "
            "VALUES(?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
            "username=excluded.username, subscription_json=excluded.subscription_json",
            (body.endpoint, user["username"], json.dumps(subscription), time.time()),
        )
        c.commit()
    return {"ok": True}


@app.delete("/api/push/subscribe")
def push_unsubscribe(body: PushUnsubscribeIn, user: dict = Depends(current_user)):
    with closing(db()) as c:
        c.execute(
            "DELETE FROM push_subscriptions WHERE endpoint=? AND username=?",
            (body.endpoint, user["username"]),
        )
        c.commit()
    return {"ok": True}


@app.get("/api/devices/{device_id}/command", dependencies=[Depends(device_auth)])
def poll_command(device_id: str):
    """Thiet bi hoi lenh dang cho (lay 1 lenh cu nhat, danh dau da gui)."""
    with closing(db()) as c:
        now = time.time()
        c.execute("DELETE FROM commands WHERE delivered=0 AND created<?", (now - 30,))
        device = c.execute("SELECT last_seen FROM devices WHERE id=?", (device_id,)).fetchone()
        if device is None or now - (device["last_seen"] or 0) >= ONLINE_TIMEOUT_S:
            c.commit()
            return {"cmd": None}
        row = c.execute(
            "SELECT * FROM commands WHERE device_id=? AND delivered=0 ORDER BY id LIMIT 1",
            (device_id,),
        ).fetchone()
        if row is None:
            return {"cmd": None}
        c.execute("UPDATE commands SET delivered=1 WHERE id=?", (row["id"],))
        c.commit()
        return {"cmd": row["cmd"]}


# ---------- API: web/app goi ----------
@app.get("/api/devices", dependencies=[Depends(current_user)])
def list_devices():
    with closing(db()) as c:
        rows = c.execute(
            "SELECT * FROM devices ORDER BY CASE WHEN last_seen>=? THEN 0 ELSE 1 END, "
            "last_seen DESC, id",
            (time.time() - ONLINE_TIMEOUT_S,),
        ).fetchall()
        return [{**device_row(r), **camera_info(c, r["id"])} for r in rows]


@app.get("/api/events", dependencies=[Depends(current_user)])
def list_events(limit: int = 50, device_id: Optional[str] = None):
    q, args = "SELECT * FROM events", []
    if device_id:
        q += " WHERE device_id=?"
        args.append(device_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 500))
    with closing(db()) as c:
        return [dict(r) for r in c.execute(q, args)]


@app.get("/api/alerts", dependencies=[Depends(current_user)])
def list_alerts(status: Optional[str] = None, limit: int = 50):
    q, args = "SELECT * FROM alerts", []
    if status:
        q += " WHERE status=?"
        args.append(status.upper())
    q += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 500))
    with closing(db()) as c:
        return [dict(r) for r in c.execute(q, args)]


@app.post("/api/devices/{device_id}/command")
async def send_command(device_id: str, body: CommandIn, user: dict = Depends(current_user)):
    admin_commands = {
        "arm", "disarm", "light_on", "light_off", "buzzer_on", "buzzer_off",
        "simulate_on", "simulate_off",
    }
    if body.cmd in admin_commands and user["role"] != "admin":
        raise HTTPException(403, "Chi admin moi duoc dieu khien thiet bi")
    with closing(db()) as c:
        device = c.execute("SELECT last_seen FROM devices WHERE id=?", (device_id,)).fetchone()
        if device is None:
            raise HTTPException(404, "Khong tim thay thiet bi")
        if time.time() - (device["last_seen"] or 0) >= ONLINE_TIMEOUT_S:
            raise HTTPException(409, "ESP32 dang offline; khong the gui lenh dieu khien")

        command_groups = {
            "arm": "arming", "disarm": "arming",
            "light_on": "light", "light_off": "light",
            "buzzer_on": "buzzer", "buzzer_off": "buzzer",
            "simulate_on": "simulation", "simulate_off": "simulation",
        }
        group = command_groups.get(body.cmd)
        if group:
            group_commands = {
                "arming": ("arm", "disarm"),
                "light": ("light_on", "light_off"),
                "buzzer": ("buzzer_on", "buzzer_off"),
                "simulation": ("simulate_on", "simulate_off"),
            }[group]
            c.execute(
                "DELETE FROM commands WHERE device_id=? AND delivered=0 AND cmd IN (?,?)",
                (device_id, *group_commands),
            )
        c.execute("DELETE FROM commands WHERE delivered=0 AND created<?", (time.time() - 30,))
        c.execute(
            "INSERT INTO commands(device_id, cmd, created) VALUES(?,?,?)",
            (device_id, body.cmd, time.time()),
        )
        c.commit()
    await hub.broadcast({"type": "command", "device_id": device_id, "cmd": body.cmd})
    return {"ok": True}


# ---------- API: tai khoan ----------
@app.post("/api/login")
def login(body: LoginIn):
    name = body.username.strip()
    f = login_fails.get(name)
    if f and f[0] >= 5 and time.time() - f[1] < 60:
        raise HTTPException(429, "Sai qua nhieu lan, thu lai sau 1 phut")
    with closing(db()) as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    if r is None or not check_pw(body.password, r["pw"]):
        n = (f[0] if f and time.time() - f[1] < 60 else 0) + 1
        login_fails[name] = [n, time.time()]
        raise HTTPException(401, "Sai ten dang nhap hoac mat khau")
    login_fails.pop(name, None)
    return {"token": make_token(name), "username": name, "role": r["role"]}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return user


@app.post("/api/me/password")
def change_password(body: PasswordIn, user: dict = Depends(current_user)):
    with closing(db()) as c:
        r = c.execute("SELECT pw FROM users WHERE username=?", (user["username"],)).fetchone()
        if not check_pw(body.old_password, r["pw"]):
            raise HTTPException(400, "Mat khau cu khong dung")
        c.execute("UPDATE users SET pw=? WHERE username=?", (hash_pw(body.new_password), user["username"]))
        c.commit()
    return {"ok": True}


@app.get("/api/users", dependencies=[Depends(admin_only)])
def list_users():
    with closing(db()) as c:
        return [dict(r) for r in c.execute("SELECT username, role, created FROM users ORDER BY created")]


@app.post("/api/users", dependencies=[Depends(admin_only)])
def create_user(body: NewUserIn):
    with closing(db()) as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
            raise HTTPException(409, "Ten dang nhap da ton tai")
        c.execute("INSERT INTO users VALUES(?,?,?,?)",
                  (body.username, hash_pw(body.password), body.role, time.time()))
        c.commit()
    return {"ok": True}


@app.delete("/api/users/{username}")
def delete_user(username: str, admin: dict = Depends(admin_only)):
    if username == admin["username"]:
        raise HTTPException(400, "Khong the tu xoa tai khoan dang dung")
    with closing(db()) as c:
        c.execute("DELETE FROM users WHERE username=?", (username,))
        c.commit()
    return {"ok": True}


@app.get("/api/stats", dependencies=[Depends(current_user)])
def stats():
    with closing(db()) as c:
        total = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        open_ = c.execute("SELECT COUNT(*) FROM alerts WHERE status IN ('OPEN','ACKED')").fetchone()[0]
        devs = [device_row(r) for r in c.execute("SELECT * FROM devices")]
    return {
        "devices": len(devs),
        "online": sum(1 for d in devs if d["online"]),
        "alerts_total": total,
        "alerts_active": open_,
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if user_from_token(ws.query_params.get("token")) is None:
        await ws.close(code=4401)
        return
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # giu ket noi
    except WebSocketDisconnect:
        hub.disconnect(ws)