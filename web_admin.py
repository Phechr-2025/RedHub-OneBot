"""Secure web control panel for Bio-shop Telegram Bot.

The web panel and Telegram bot deliberately share database.py/SQLite so every
change is visible immediately from both surfaces without a cache or restart.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
import tempfile
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

import database as db

logger = logging.getLogger(__name__)
app = FastAPI(title="Bio-shop Control", docs_url=None, redoc_url=None)

COOKIE = "bioshop_admin"
SESSION_TTL = 60 * 60 * 12
_started = False
_start_lock = threading.Lock()

SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    "addclient_enabled": {"label": "ระบบขาย VPN", "type": "bool", "default": "1", "group": "ระบบหลัก"},
    "freeclient_enabled": {"label": "ทดลองใช้ฟรี", "type": "bool", "default": "1", "group": "ระบบหลัก"},
    "credit_code_enabled": {"label": "กรอกโค้ดเครดิต", "type": "bool", "default": "1", "group": "ระบบหลัก"},
    "truemoney_enabled": {"label": "เติมเงิน TrueMoney", "type": "bool", "default": "1", "group": "ระบบหลัก"},
    "cancel_button_enabled": {"label": "ปุ่มยกเลิก", "type": "bool", "default": "1", "group": "การแสดงผล"},
    "run_start_finish_enabled": {"label": "แสดงเมนูหลังจบ", "type": "bool", "default": "1", "group": "การแสดงผล"},
    "buy_dm_enabled": {"label": "อนุญาตซื้อใน DM", "type": "bool", "default": "0", "group": "ช่องทาง"},
    "freeclient_hours": {"label": "ระยะเวลาทดลองใช้ (ชั่วโมง)", "type": "number", "default": "1", "min": 0.01, "group": "ทดลองใช้ฟรี"},
    "freeclient_label": {"label": "คำที่แสดงแทน FREE ในชื่อไฟล์", "type": "text", "default": "FREE", "group": "ทดลองใช้ฟรี"},
    "freeclient_daily_limit": {"label": "สิทธิ์ทดลอง/รอบ", "type": "integer", "default": "1", "min": 0, "group": "ทดลองใช้ฟรี"},
    "run_start_finish_delay_seconds": {"label": "หน่วงเมนู /start (วินาที)", "type": "number", "default": "2", "min": 0.1, "group": "การแสดงผล"},
    "cancel_button_delay_seconds": {"label": "หน่วงปุ่มยกเลิก (วินาที)", "type": "number", "default": "1", "min": 0, "group": "การแสดงผล"},
    "run_start_finish_commands": {"label": "คำสั่งที่ใช้แล้วให้เด้งเมนู /start", "type": "text", "default": "addclient", "group": "การแสดงผล"},
    "truemoney_wallet_phone": {"label": "เบอร์รับซอง", "type": "text", "default": "", "group": "TrueMoney"},
    "truemoney_credit_rate": {"label": "เครดิตต่อ 1 บาท", "type": "number", "default": "1", "min": 0.01, "group": "TrueMoney"},
    "buy_group_ids": {"label": "กลุ่มที่ซื้อได้ (คั่นด้วย ,)", "type": "text", "default": "", "group": "ช่องทาง"},
    "freeclient_group_ids": {"label": "กลุ่มทดลองใช้ฟรี (คั่นด้วย ,)", "type": "text", "default": "", "group": "ช่องทาง"},
}


def _secret() -> bytes:
    value = os.getenv("WEB_ADMIN_SECRET", "").strip()
    if not value:
        # Stable fallback derived from bot token; production README requires a dedicated secret.
        value = hashlib.sha256((os.getenv("BOT_TOKEN", "development-only") + ":web").encode()).hexdigest()
    return value.encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def _new_session() -> str:
    expiry = str(int(time.time()) + SESSION_TTL)
    nonce = secrets.token_urlsafe(18)
    payload = f"{expiry}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2_sha256$200000${salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _web_credentials() -> tuple[str, str, str]:
    """Return username, password/hash, and mode. DB values override environment."""
    username = str(db.get_setting("web_admin_username", "admin") or "admin").strip() or "admin"
    stored_hash = str(db.get_setting("web_admin_password_hash", "") or "").strip()
    if stored_hash:
        return username, stored_hash, "hash"
    env_user = os.getenv("WEB_ADMIN_USERNAME", "").strip()
    env_password = os.getenv("WEB_ADMIN_PASSWORD", "")
    if env_user or env_password:
        return env_user or username, env_password or "123", "plain"
    return username, "123", "plain"


def _valid_session(value: str | None) -> bool:
    try:
        expiry, nonce, signature = (value or "").split(".", 2)
        payload = f"{expiry}.{nonce}"
        return int(expiry) >= int(time.time()) and hmac.compare_digest(signature, _sign(payload))
    except (ValueError, TypeError):
        return False


def _require(request: Request) -> None:
    if not _valid_session(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบใหม่")


def _clean_setting(key: str, value: Any) -> str:
    spec = SETTING_SCHEMA.get(key)
    if not spec:
        raise HTTPException(400, "ไม่อนุญาตให้แก้ไขค่านี้")
    kind = spec["type"]
    if kind == "bool":
        return "1" if str(value).lower() in {"1", "true", "on", "yes"} else "0"
    if kind in {"number", "integer"}:
        try:
            number = float(value)
            if number < float(spec.get("min", -1e99)):
                raise ValueError
            return str(int(number)) if kind == "integer" else f"{number:g}"
        except (TypeError, ValueError):
            raise HTTPException(400, f"ค่า {spec['label']} ไม่ถูกต้อง")
    text = str(value).strip()[:500]
    if key == "run_start_finish_commands":
        text = ",".join(part.strip().lstrip("/") for part in text.replace("\n", ",").split(",") if part.strip())
        if not text:
            raise HTTPException(400, "กรุณาระบุอย่างน้อย 1 คำสั่ง")
    if key == "truemoney_wallet_phone" and text and (not text.isdigit() or len(text) not in {9, 10}):
        raise HTTPException(400, "เบอร์ TrueMoney ไม่ถูกต้อง")
    if key.endswith("group_ids") and text:
        try:
            text = ",".join(str(int(x.strip())) for x in text.split(",") if x.strip())
        except ValueError:
            raise HTTPException(400, "Group ID ต้องเป็นตัวเลขคั่นด้วย comma")
    return text


class LoginBody(BaseModel):
    username: str
    password: str


class SettingBody(BaseModel):
    value: Any


class NetworkBody(BaseModel):
    code: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=9999)


class PackageBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    inbound_id: int = Field(ge=1, le=2_147_483_647)
    price_per_day: float = Field(gt=0, le=1_000_000)
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=9999)


class CreditBody(BaseModel):
    amount: float = Field(ge=-1_000_000, le=1_000_000)


class WebAccountBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(default="", max_length=200)


class CreditCodeBody(BaseModel):
    code_name: str = Field(min_length=1, max_length=64)
    mode: str = Field(pattern=r"^(fixed|random|free_reset)$")
    fixed_credit: float = Field(default=0, ge=0, le=1_000_000)
    total_credit: float = Field(default=0, ge=0, le=1_000_000)
    max_uses: int = Field(default=0, ge=0, le=1_000_000)
    expires_at: str = Field(min_length=1, max_length=80)
    active: bool = True


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "bio-shop-control", "time": int(time.time())}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    if not _valid_session(request.cookies.get(COOKIE)):
        return LOGIN_HTML
    return DASHBOARD_HTML


@app.post("/api/login")
def login(body: LoginBody, response: Response) -> dict[str, bool]:
    expected_user, password_value, password_mode = _web_credentials()
    password_ok = _verify_password(body.password, password_value) if password_mode == "hash" else hmac.compare_digest(body.password, password_value)
    if not (hmac.compare_digest(body.username, expected_user) and password_ok):
        time.sleep(0.35)
        raise HTTPException(401, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
    response.set_cookie(COOKIE, _new_session(), max_age=SESSION_TTL, httponly=True, secure=os.getenv("WEB_COOKIE_SECURE", "1") == "1", samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/overview")
def overview(request: Request) -> dict[str, Any]:
    _require(request)
    return db.get_dashboard_overview()


@app.get("/api/web-account")
def get_web_account(request: Request) -> dict[str, Any]:
    _require(request)
    username, _, _ = _web_credentials()
    return {"username": username}


@app.put("/api/web-account")
def update_web_account(body: WebAccountBody, request: Request, response: Response) -> dict[str, Any]:
    _require(request)
    username = body.username.strip()
    if not username:
        raise HTTPException(400, "กรุณากรอกชื่อผู้ใช้")
    db.set_setting("web_admin_username", username)
    if body.password:
        if len(body.password) < 3:
            raise HTTPException(400, "รหัสผ่านต้องมีอย่างน้อย 3 ตัวอักษร")
        db.set_setting("web_admin_password_hash", _hash_password(body.password))
    db.add_audit_log("web", "web-account.update", username, "", "password-changed" if body.password else "username-only")
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True, "message": "บันทึกแล้ว กรุณาเข้าสู่ระบบใหม่"}


@app.get("/api/settings")
def settings(request: Request) -> dict[str, Any]:
    _require(request)
    result = []
    for key, spec in SETTING_SCHEMA.items():
        result.append({"key": key, **spec, "value": db.get_setting_text(key, spec["default"])})
    return {"items": result, "updated_at": int(time.time())}


@app.put("/api/settings/{key}")
def update_setting(key: str, body: SettingBody, request: Request) -> dict[str, Any]:
    _require(request)
    value = _clean_setting(key, body.value)
    old = db.get_setting_text(key, SETTING_SCHEMA[key]["default"])
    db.set_setting(key, value)
    db.add_audit_log("web", "setting.update", key, old, value)
    return {"ok": True, "key": key, "value": value}


@app.get("/api/networks")
def networks(request: Request) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_networks(active_only=False)}


@app.post("/api/networks")
def create_network(body: NetworkBody, request: Request) -> dict[str, Any]:
    _require(request)
    try:
        data = body.model_dump()
        data["code"] = data["code"].strip().upper()
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HTTPException(400, "กรุณากรอกชื่อเครือข่าย")
        network_id = db.create_network(**data)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, "รหัสเครือข่ายนี้มีอยู่แล้ว")
        raise
    db.add_audit_log("web", "network.create", str(network_id), "", body.model_dump_json())
    return {"ok": True, "id": network_id}


@app.put("/api/networks/{network_id}")
def update_network(network_id: int, body: NetworkBody, request: Request) -> dict[str, Any]:
    _require(request)
    try:
        data = body.model_dump()
        data["code"] = data["code"].strip().upper()
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HTTPException(400, "กรุณากรอกชื่อเครือข่าย")
        changed = db.update_network(network_id, **data)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, "รหัสเครือข่ายนี้มีอยู่แล้ว")
        raise
    if not changed:
        raise HTTPException(404, "ไม่พบเครือข่าย")
    db.add_audit_log("web", "network.update", str(network_id), "", body.model_dump_json())
    return {"ok": True}


@app.delete("/api/networks/{network_id}")
def delete_network(network_id: int, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.delete_network(network_id):
        raise HTTPException(404, "ไม่พบเครือข่าย")
    db.add_audit_log("web", "network.delete", str(network_id), "active", "deleted")
    return {"ok": True}


@app.get("/api/networks/{network_id}/packages")
def packages(network_id: int, request: Request) -> dict[str, Any]:
    _require(request)
    network = db.get_network(network_id, active_only=False)
    if not network: raise HTTPException(404, "ไม่พบเครือข่าย")
    return {"network": network, "items": db.get_packages(network_id, active_only=False)}


@app.post("/api/networks/{network_id}/packages")
def create_package(network_id: int, body: PackageBody, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.get_network(network_id, active_only=False): raise HTTPException(404, "ไม่พบเครือข่าย")
    package_id = db.create_package(network_id=network_id, **body.model_dump())
    db.add_audit_log("web", "package.create", str(package_id), "", body.model_dump_json())
    return {"ok": True, "id": package_id}


@app.put("/api/packages/{package_id}")
def update_package(package_id: int, body: PackageBody, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.update_package(package_id, **body.model_dump()): raise HTTPException(404, "ไม่พบแพ็กเกจ")
    db.add_audit_log("web", "package.update", str(package_id), "", body.model_dump_json())
    return {"ok": True}


@app.delete("/api/packages/{package_id}")
def delete_package(package_id: int, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.delete_package(package_id): raise HTTPException(404, "ไม่พบแพ็กเกจ")
    db.add_audit_log("web", "package.delete", str(package_id), "active", "deleted")
    return {"ok": True}


@app.get("/api/backup")
def download_backup(request: Request):
    _require(request)
    path = db.create_database_backup()
    filename = time.strftime("bioshop-backup-%Y%m%d-%H%M%S.db")
    db.add_audit_log("web", "database.backup", filename)
    return FileResponse(
        path, media_type="application/x-sqlite3", filename=filename,
        background=BackgroundTask(lambda: os.path.exists(path) and os.remove(path)),
    )


@app.post("/api/restore")
async def restore_backup(request: Request, mode: str = "replace") -> dict[str, Any]:
    _require(request)
    payload = await request.body()
    if not payload:
        raise HTTPException(400, "กรุณาเลือกไฟล์สำรอง")
    if len(payload) > 100 * 1024 * 1024:
        raise HTTPException(413, "ไฟล์ใหญ่เกิน 100 MB")
    fd, path = tempfile.mkstemp(prefix="bioshop-restore-", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        if mode == "replace":
            db.restore_database_from_file(path)
            result = {"mode": "replace"}
        elif mode == "merge":
            result = {"mode": "merge", "added": db.merge_database_from_file(path)}
        else:
            raise HTTPException(400, "โหมดคืนค่าไม่ถูกต้อง")
        db.add_audit_log("web", f"database.restore.{mode}", "uploaded-file")
        return {"ok": True, "message": "นำเข้าฐานข้อมูลสำเร็จ", **result}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        if os.path.exists(path):
            os.remove(path)


@app.get("/api/users")
def users(request: Request, q: str = "", limit: int = 100) -> dict[str, Any]:
    _require(request)
    return {"items": db.search_users(q[:80], min(max(limit, 1), 200))}


@app.post("/api/users/{user_id}/credit")
def user_credit(user_id: int, body: CreditBody, request: Request) -> dict[str, Any]:
    _require(request)
    db.ensure_user(user_id, None)
    before = db.get_credit(user_id)
    if body.amount >= 0:
        db.add_credit(user_id, body.amount)
    else:
        db.deduct_credit(user_id, abs(body.amount))
    after = db.get_credit(user_id)
    db.add_audit_log("web", "credit.adjust", str(user_id), str(before), str(after))
    return {"ok": True, "balance": after}


@app.get("/api/credit-codes")
def credit_codes(request: Request) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_all_credit_codes()}


@app.post("/api/credit-codes")
def create_credit_code_web(body: CreditCodeBody, request: Request) -> dict[str, Any]:
    _require(request)
    name = body.code_name.strip()
    key = name.lower()
    if db.credit_code_exists(key):
        raise HTTPException(409, "มีโค้ดชื่อนี้อยู่แล้ว")
    if body.mode == "fixed" and body.fixed_credit <= 0:
        raise HTTPException(400, "เครดิตต่อคนต้องมากกว่า 0")
    if body.mode == "random" and body.total_credit <= 0:
        raise HTTPException(400, "เครดิตรวมต้องมากกว่า 0")
    if body.mode == "free_reset":
        body.fixed_credit = 0; body.total_credit = 0
    if not db.create_credit_code(key, name, body.mode, body.fixed_credit, body.total_credit,
                                 body.max_uses, body.expires_at, 0, db._now_th_str()):
        raise HTTPException(409, "สร้างโค้ดไม่สำเร็จ")
    return {"ok": True}


@app.put("/api/credit-codes/{code_key}")
def update_credit_code_web(code_key: str, body: CreditCodeBody, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.get_credit_code(code_key):
        raise HTTPException(404, "ไม่พบโค้ด")
    if body.mode == "fixed" and body.fixed_credit <= 0:
        raise HTTPException(400, "เครดิตต่อคนต้องมากกว่า 0")
    if body.mode == "random" and body.total_credit <= 0:
        raise HTTPException(400, "เครดิตรวมต้องมากกว่า 0")
    ok = db.update_credit_code(code_key, body.code_name.strip(), body.mode,
                               body.fixed_credit if body.mode == "fixed" else 0,
                               body.total_credit if body.mode == "random" else 0,
                               body.max_uses, body.expires_at, body.active)
    if not ok: raise HTTPException(404, "ไม่พบโค้ด")
    return {"ok": True}


@app.delete("/api/credit-codes/{code_key}")
def delete_credit_code_web(code_key: str, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.delete_credit_code(code_key): raise HTTPException(404, "ไม่พบโค้ด")
    return {"ok": True}


@app.get("/api/orders")
def orders(request: Request, limit: int = 100) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_recent_activity(min(max(limit, 1), 200))}


@app.get("/api/audit")
def audit(request: Request, limit: int = 100) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_audit_logs(min(max(limit, 1), 200))}


def start_web_server() -> None:
    global _started
    with _start_lock:
        if _started or os.getenv("WEB_ADMIN_ENABLED", "1") != "1":
            return
        _started = True
        host = os.getenv("WEB_HOST", "0.0.0.0")
        port = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
        thread = threading.Thread(target=lambda: uvicorn.run(app, host=host, port=port, log_level="info", access_log=False), daemon=True, name="web-admin")
        thread.start()
        logger.info("Web control panel started on %s:%s", host, port)


LOGIN_HTML = r'''<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bio-shop Control</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#202124;background:#f6f8fb}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px}.login{width:min(420px,100%);background:white;border:1px solid #e2e7ef;border-radius:16px;padding:32px;box-shadow:0 16px 50px #17325b12}.brand{display:flex;align-items:center;gap:12px;margin-bottom:28px}.logo{width:46px;height:46px;display:grid;place-items:center;background:#e8f1ff;color:#1769d2;border-radius:12px;font-size:24px}.brand h1{font-size:20px;margin:0}.brand p{margin:3px 0 0;color:#6b7280;font-size:14px}label{display:block;font-size:14px;font-weight:650;margin:16px 0 7px}input{width:100%;height:46px;border:1px solid #d8dee9;border-radius:9px;padding:0 13px;font-size:16px;outline:none}input:focus{border-color:#2783de;box-shadow:0 0 0 3px #2783de20}button{width:100%;height:46px;border:0;border-radius:9px;margin-top:22px;background:#2783de;color:white;font-weight:700;font-size:15px;cursor:pointer}button:disabled{opacity:.6}.error{min-height:20px;color:#d14343;font-size:14px;margin-top:12px}</style></head><body><main class="login"><div class="brand"><div class="logo">◈</div><div><h1>Bio-shop Control</h1><p>ศูนย์ควบคุมบอท VPN</p></div></div><form id="f"><label>ชื่อผู้ใช้</label><input id="u" autocomplete="username" required><label>รหัสผ่าน</label><input id="p" type="password" autocomplete="current-password" required><button>เข้าสู่ระบบ</button><div class="error" id="e"></div></form></main><script>f.onsubmit=async x=>{x.preventDefault();let b=f.querySelector('button');b.disabled=true;e.textContent='';let r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});if(r.ok)location.reload();else{let j=await r.json();e.textContent=j.detail||'เข้าสู่ระบบไม่สำเร็จ';b.disabled=false}}</script></body></html>'''

DASHBOARD_HTML = r'''<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bio-shop Control</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#202124;background:#f6f8fb;--blue:#2783de;--green:#2f9e6f;--red:#dc5a52;--muted:#6b7280;--border:#e1e6ee}*{box-sizing:border-box}body{margin:0;overflow-x:hidden}.app{display:grid;grid-template-columns:244px 1fr;min-height:100vh;transition:grid-template-columns .2s ease}.app.sidebar-collapsed{grid-template-columns:76px 1fr}.app.sidebar-collapsed .side{padding-left:10px;padding-right:10px}.app.sidebar-collapsed .brand-full,.app.sidebar-collapsed .brand small,.app.sidebar-collapsed .nav-label{display:none}.app.sidebar-collapsed .brand-mini{display:inline}.app.sidebar-collapsed .brand{text-align:center}.app.sidebar-collapsed .nav button{justify-content:center;padding-left:0;padding-right:0}.app.sidebar-collapsed .nav-icon{font-size:20px}.app.sidebar-collapsed .logout{font-size:0}.app.sidebar-collapsed .logout:after{content:"↪";font-size:18px}.side{background:#111827;color:#e5e7eb;padding:22px 14px;position:sticky;top:0;height:100vh}.brand{font-weight:800;font-size:18px;padding:4px 12px 12px;display:flex;align-items:center;gap:7px}.brand-mini{display:none}.side-toggle{width:100%;height:36px;margin:0 0 12px;border:1px solid #344258;background:#1b2638;color:#cbd5e1;border-radius:8px;cursor:pointer;font-size:18px}.side-toggle:hover{background:#253247;color:white}.brand small{display:block;color:#8fa0b8;font-weight:500;font-size:12px;margin-top:4px}.nav button{display:flex;align-items:center;gap:11px;width:100%;height:44px;padding:0 12px;border:0;background:transparent;color:#aeb9c9;border-radius:8px;font-size:15px;text-align:left;cursor:pointer;margin:3px 0}.nav button.active,.nav button:hover{background:#253247;color:white}.logout{position:absolute;bottom:20px;left:14px;right:14px;height:42px;border:1px solid #344258;background:#1b2638;color:#cbd5e1;border-radius:8px;cursor:pointer;font:inherit}.logout:hover{background:#253247;color:white}.main{padding:30px 36px;min-width:0}.app.focus-mode .top{display:none}.app.focus-mode .main{width:100%;max-width:1100px;margin:0 auto;padding:28px 22px}.focus-head{display:flex;align-items:center;gap:12px;margin-bottom:18px}.focus-head h1{margin:0;font-size:24px}.focus-card{background:#fff;border:1px solid var(--border);border-radius:12px;padding:20px}.focus-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:18px}.settings-action{align-items:center}.settings-action .btn{min-width:92px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}.top h1{font-size:25px;margin:0}.top p{color:var(--muted);margin:5px 0 0;font-size:14px}.status{font-size:13px;color:var(--green);background:#e9f6f0;border:1px solid #ccebdd;padding:7px 10px;border-radius:8px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.backup-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.card,.panel{background:#fff;border:1px solid var(--border);border-radius:12px}.card{padding:18px}.card .label{font-size:13px;color:var(--muted)}.card .value{font-size:27px;font-weight:800;margin-top:8px}.panel{margin-top:18px;overflow:hidden}.panel-head{padding:17px 19px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}.panel-head h2{font-size:16px;margin:0}.content{padding:18px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.setting{border:1px solid var(--border);border-radius:10px;padding:14px;display:flex;align-items:center;justify-content:space-between;gap:12px}.setting b{font-size:14px}.setting small{display:block;color:var(--muted);margin-top:3px}.switch{width:48px;height:28px;border:0;border-radius:16px;background:#cfd6df;position:relative;cursor:pointer;flex:none}.switch:after{content:"";position:absolute;width:22px;height:22px;background:white;border-radius:50%;left:3px;top:3px;transition:.18s}.switch.on{background:var(--green)}.switch.on:after{left:23px}.btn{border:1px solid var(--border);background:white;border-radius:8px;padding:9px 13px;cursor:pointer;font-weight:650}.btn.primary{background:var(--blue);border-color:var(--blue);color:white}.btn.danger{color:var(--red)}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid #edf0f4;white-space:nowrap}th{font-size:12px;color:var(--muted);background:#fafbfc}.table-wrap{overflow:auto}.tag{font-size:12px;padding:4px 7px;border-radius:6px;background:#eef3f8}.tag.on{background:#e7f5ee;color:#16764e}.purchase-tag{background:#e5f2fc;color:#1769d2}.trial-tag{background:#fbebde;color:#a85c20}.empty{padding:40px;text-align:center;color:var(--muted)}.hidden{display:none!important}.modal-bg{position:fixed;inset:0;background:#10182870;display:grid;place-items:center;padding:18px;z-index:5}.modal{width:min(560px,100%);max-height:92vh;overflow:auto;background:white;border-radius:14px;padding:24px}.modal h2{margin:0 0 18px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}.field.full{grid-column:1/-1}.field label{display:block;font-size:13px;font-weight:650;margin-bottom:6px}.field input,.field select,.field textarea{width:100%;border:1px solid #d8dee9;border-radius:8px;padding:10px;font:inherit}.actions{display:flex;justify-content:flex-end;gap:9px;margin-top:20px}.toast{position:fixed;right:20px;bottom:20px;background:#172033;color:white;padding:12px 16px;border-radius:9px;z-index:10;box-shadow:0 8px 30px #0003}.search{height:38px;border:1px solid var(--border);border-radius:8px;padding:0 11px;width:230px}.action-card{display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:start}.action-card p{color:var(--muted);line-height:1.55}.value.compact{font-size:20px}.action-icon{width:42px;height:42px;border-radius:10px;display:grid;place-items:center;font-size:22px;font-weight:800}.action-icon.blue{background:#e5f2fc;color:#2783de}.action-icon.orange{background:#fbebde;color:#d5803b}.restore-file{border:1px solid var(--border);background:#f9fafb;border-radius:10px;padding:14px}.restore-file small,.restore-option small{display:block;color:var(--muted);margin-top:4px}.modal-note{color:var(--muted)}.restore-options{display:grid;gap:10px}.restore-option{border:1px solid var(--border);background:white;border-radius:10px;padding:14px;text-align:left;cursor:pointer;font:inherit}.restore-option:hover{border-color:var(--blue);background:#f7fbff}.restore-option.danger-zone{border-color:#f0c8c4}.restore-option.danger-zone:hover{border-color:var(--red);background:#fff8f7}@media(max-width:900px){.app,.app.sidebar-collapsed{display:block;min-height:100vh}.side,.nav,.main,.panel{min-width:0;max-width:100vw}.side{height:auto;position:sticky;top:0;z-index:4;padding:8px 10px;display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:8px}.brand{padding:0;font-size:15px;white-space:nowrap}.brand small{display:none}.nav{display:flex;overflow-x:auto;scrollbar-width:none}.nav::-webkit-scrollbar{display:none}.nav button{min-width:max-content;height:40px;padding:0 10px;font-size:13px}.logout{position:static;width:auto;height:40px;padding:0 10px;font-size:13px}.main{padding:18px 14px}.top{margin-bottom:16px}.cards{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.panel-head{gap:10px;flex-wrap:wrap}}@media(max-width:900px){.app.sidebar-collapsed .side{display:flex;justify-content:space-between}.app.sidebar-collapsed .nav{display:none}.app.sidebar-collapsed .side-toggle{width:42px;margin:0}.app.sidebar-collapsed .logout{font-size:13px}.app.sidebar-collapsed .logout:after{content:none}}@media(max-width:520px){.cards,.backup-grid{grid-template-columns:1fr}.top{align-items:flex-start}.top h1{font-size:22px}.top p{font-size:13px;line-height:1.45}.status{display:none}.card{padding:16px}.action-card{grid-template-columns:1fr}.action-icon{width:38px;height:38px}.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}th,td{padding:11px 12px}.modal-bg{align-items:end;padding:0}.modal{width:100%;max-height:94vh;border-radius:16px 16px 0 0;padding:20px}.actions{position:sticky;bottom:-20px;background:white;padding-top:12px}}
</style></head><body><div class="app"><aside class="side"><div class="brand"><span class="brand-mini">◈</span><span class="brand-full">Bio-shop</span><small>CONTROL CENTER</small></div><button class="side-toggle" id="sideToggle" type="button" aria-label="พับหรือกางเมนูด้านข้าง" onclick="toggleSidebar()">☰</button><nav class="nav"><button data-page="overview" class="active"><span class="nav-icon">▦</span><span class="nav-label">ภาพรวม</span></button><button data-page="settings"><span class="nav-icon">⚙</span><span class="nav-label">ตั้งค่าบอท</span></button><button data-page="networks"><span class="nav-icon">◫</span><span class="nav-label">เครือข่าย</span></button><button data-page="users"><span class="nav-icon">◎</span><span class="nav-label">ผู้ใช้และเครดิต</span></button><button data-page="orders"><span class="nav-icon">≡</span><span class="nav-label">รายการสั่งซื้อ</span></button><button data-page="audit"><span class="nav-icon">⌁</span><span class="nav-label">ประวัติการแก้ไข</span></button><button data-page="backup"><span class="nav-icon">▣</span><span class="nav-label">สำรอง/คืนค่า</span></button></nav><button class="logout" onclick="logout()">ออกจากระบบ</button></aside><main class="main"><div class="top"><div><h1 id="title">ภาพรวมระบบ</h1><p id="subtitle">ข้อมูลจากบอทและเว็บไซต์ใช้ฐานข้อมูลเดียวกันแบบเรียลไทม์</p></div><div class="status">● ระบบออนไลน์</div></div><section id="page"></section></main></div><div id="modal" class="hidden"></div><div id="toast" class="toast hidden"></div><script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let networks=[],packages=[],activeNetwork=null;
async function api(url,opt={}){let r=await fetch(url,{headers:{'content-type':'application/json',...(opt.headers||{})},...opt});if(r.status===401){location.reload();throw Error('กรุณาเข้าสู่ระบบใหม่')}let j=await r.json().catch(()=>({}));if(!r.ok){let d=j.detail;if(Array.isArray(d))d=d.map(x=>x.msg||String(x)).join(', ');throw Error(d||'เกิดข้อผิดพลาด')}return j}window.addEventListener('unhandledrejection',e=>{e.preventDefault();toast(e.reason?.message||'เกิดข้อผิดพลาด')});function money(n){return Number(n||0).toLocaleString('th-TH',{maximumFractionDigits:2})}function toast(t){let x=$('#toast');x.textContent=t;x.classList.remove('hidden');setTimeout(()=>x.classList.add('hidden'),2200)}
async function overview(){let d=await api('/api/overview');$('#page').innerHTML=`<div class="cards"><div class="card"><div class="label">ผู้ใช้ทั้งหมด</div><div class="value">${money(d.users)}</div></div><div class="card"><div class="label">ยอดขายรวม (เครดิต)</div><div class="value">${money(d.revenue)}</div></div><div class="card"><div class="label">ออเดอร์ทั้งหมด</div><div class="value">${money(d.orders)}</div></div><div class="card"><div class="label">เครือข่ายที่เปิดใช้</div><div class="value">${money(d.active_networks)}</div></div></div><div class="panel"><div class="panel-head"><h2>รายการสั่งซื้อล่าสุด</h2></div>${ordersTable(d.recent_orders)}</div>`}
function ordersTable(a){if(!a?.length)return '<div class="empty">ยังไม่มีรายการ</div>';return `<div class="table-wrap"><table><thead><tr><th>ประเภท</th><th>เวลา</th><th>ผู้ใช้</th><th>ชื่อ</th><th>เครือข่าย</th><th>จำนวนเวลาที่เช่า</th><th>GB</th><th>ราคา</th></tr></thead><tbody>${a.map(x=>`<tr><td><span class="tag ${x.activity_type==='trial'?'trial-tag':'purchase-tag'}">${x.activity_type==='trial'?'ทดลองใช้งาน':'ซื้อ'}</span></td><td>${esc(x.created_at)}</td><td>${esc(x.username?'@'+x.username:x.user_id)}</td><td>${esc(x.code_name)}</td><td><span class="tag">${esc(x.network)}</span></td><td>${esc(x.duration_text||((x.days||0)+' วัน'))}</td><td>${x.gb||'∞'}</td><td>${x.activity_type==='trial'?'ฟรี':money(x.cost)}</td></tr>`).join('')}</tbody></table></div>`}async function settings(){leaveFocus();let d=await api('/api/settings'),groups={};d.items.forEach(x=>(groups[x.group]??=[]).push(x));let actionKeys=new Set(['run_start_finish_commands','freeclient_group_ids']);$('#page').innerHTML=Object.entries(groups).map(([g,a])=>`<div class="panel"><div class="panel-head"><h2>${esc(g)}</h2></div><div class="content grid">${a.filter(x=>!actionKeys.has(x.key)).map(settingRow).join('')}</div></div>`).join('')+`<div class="panel"><div class="panel-head"><h2>เครื่องมือและการเข้าถึง</h2></div><div class="content grid"><div class="setting settings-action"><div><b>คำสั่งที่ใช้แล้วให้เด้งเมนู /start</b><small>ตั้งค่าในหน้าเฉพาะแบบเต็มจอ</small></div><button class="btn" onclick="commandSettingsPage()">ตั้งค่า</button></div><div class="setting settings-action"><div><b>จัดการโค้ดรับเครดิต</b><small>สร้าง ดู แก้ไข และลบโค้ด</small></div><button class="btn" onclick="creditCodes()">ตั้งค่า</button></div><div class="setting settings-action"><div><b>กลุ่มทดลองใช้ฟรี</b><small>กำหนด Group ID ทีละบรรทัดหรือคั่นด้วย comma</small></div><button class="btn" onclick="freeTrialGroupsPage()">ตั้งค่า</button></div><div class="setting settings-action"><div><b>บัญชีเว็บไซต์จัดการ</b><small>เปลี่ยนชื่อผู้ใช้และรหัสผ่านเข้าสู่ระบบ</small></div><button class="btn" onclick="webAccountPage()">ตั้งค่า</button></div></div></div>`}
function settingRow(x){if(x.type==='bool')return `<div class="setting"><div><b>${esc(x.label)}</b><small>${esc(x.key)}</small></div><button class="switch ${x.value==='1'?'on':''}" aria-label="${esc(x.label)}" onclick="setSetting('${x.key}',${x.value!=='1'})"></button></div>`;if(x.key==='run_start_finish_commands')return `<div class="setting"><div><b>${esc(x.label)}</b><small>ใส่ทีละบรรทัด หรือคั่นด้วย comma เช่น test, fast</small></div><button class="btn" onclick="editCommandSetting('${x.key}','${esc(x.label)}','${esc(x.value)}')">แก้ไขรายการ</button></div>`;return `<div class="setting"><div><b>${esc(x.label)}</b><small>${esc(x.key)}</small></div><button class="btn" onclick="editSetting('${x.key}','${esc(x.label)}','${esc(x.value)}')">${esc(x.value)||'ตั้งค่า'}</button></div>`}
async function setSetting(k,v){await api('/api/settings/'+k,{method:'PUT',body:JSON.stringify({value:v})});toast('บันทึกแล้ว — บอทใช้ค่าใหม่ทันที');settings()}function editSetting(k,l,v){showModal(`<h2>${l}</h2><div class="field"><label>ค่าใหม่</label><input id="sv" value="${v}"></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveSetting('${k}')">บันทึก</button></div>`)}async function saveSetting(k){await api('/api/settings/'+k,{method:'PUT',body:JSON.stringify({value:$('#sv').value})});closeModal();toast('บันทึกแล้ว');settings()}
function enterFocus(title,content){document.querySelector('.app').classList.add('focus-mode');$('#title').textContent=title;$('#page').innerHTML=`<div class="focus-head"><button class="btn" onclick="settings()">← กลับไปตั้งค่าบอท</button><h1>${esc(title)}</h1></div>${content}`;window.scrollTo(0,0)}function leaveFocus(){document.querySelector('.app').classList.remove('focus-mode')}
async function commandSettingsPage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='run_start_finish_commands'),lines=String(x?.value||'').replaceAll(',',"\n").split(/\n+/).map(a=>a.trim()).filter(Boolean).join('\n');enterFocus('คำสั่งที่ใช้แล้วให้เด้งเมนู /start',`<div class="focus-card"><p class="modal-note">ใส่คำสั่งทีละบรรทัด หรือคั่นด้วย comma ได้</p><div class="field"><label>รายการคำสั่ง</label><textarea id="commandList" rows="16" placeholder="test\nfast">${esc(lines)}</textarea></div><div class="focus-actions"><button class="btn" onclick="settings()">ยกเลิก</button><button class="btn primary" onclick="saveCommandSettings()">บันทึก</button></div></div>`)}async function saveCommandSettings(){await api('/api/settings/run_start_finish_commands',{method:'PUT',body:JSON.stringify({value:$('#commandList').value})});toast('บันทึกรายการคำสั่งแล้ว');settings()}
async function freeTrialGroupsPage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='freeclient_group_ids'),lines=String(x?.value||'').replaceAll(',',"\n").split(/\n+/).map(a=>a.trim()).filter(Boolean).join('\n');enterFocus('กลุ่มทดลองใช้ฟรี',`<div class="focus-card"><p class="modal-note">ใส่ Group ID ทีละบรรทัด หรือคั่นด้วย comma ได้ หากไม่กำหนด ระบบจะใช้ตามโหมดช่องทางที่ตั้งไว้</p><div class="field"><label>Group ID ที่อนุญาต</label><textarea id="freeGroupList" rows="16" inputmode="numeric" placeholder="-1001234567890\n-1009876543210">${esc(lines)}</textarea></div><div class="focus-actions"><button class="btn" onclick="settings()">ยกเลิก</button><button class="btn primary" onclick="saveFreeTrialGroups()">บันทึก</button></div></div>`)}async function saveFreeTrialGroups(){await api('/api/settings/freeclient_group_ids',{method:'PUT',body:JSON.stringify({value:$('#freeGroupList').value})});toast('บันทึกกลุ่มทดลองใช้ฟรีแล้ว');settings()}
async function webAccountPage(){let d=await api('/api/web-account');enterFocus('บัญชีเว็บไซต์จัดการ',`<div class="focus-card"><p class="modal-note">หากไม่เคยตั้งค่า ค่าเริ่มต้นคือ admin / 123 แนะนำให้เปลี่ยนทันที</p><div class="form-grid"><div class="field full"><label>ชื่อผู้ใช้เว็บไซต์</label><input id="webUser" value="${esc(d.username||'admin')}" autocomplete="username"></div><div class="field full"><label>รหัสผ่านใหม่</label><input id="webPass" type="password" placeholder="เว้นว่างไว้หากไม่ต้องการเปลี่ยน" autocomplete="new-password"></div><div class="field full"><label>ยืนยันรหัสผ่านใหม่</label><input id="webPassConfirm" type="password" placeholder="กรอกรหัสผ่านเดิมอีกครั้ง" autocomplete="new-password"></div></div><div class="focus-actions"><button class="btn" onclick="settings()">ยกเลิก</button><button class="btn primary" onclick="saveWebAccount()">บันทึก</button></div></div>`)}async function saveWebAccount(){let username=$('#webUser').value.trim(),password=$('#webPass').value,confirmPassword=$('#webPassConfirm').value;if(!username)return toast('กรุณากรอกชื่อผู้ใช้');if(password!==confirmPassword)return toast('ยืนยันรหัสผ่านไม่ตรงกัน');let result=await api('/api/web-account',{method:'PUT',body:JSON.stringify({username,password})});alert(result.message||'บันทึกแล้ว');location.reload()}
async function networkPage(){let d=await api('/api/networks');networks=d.items;activeNetwork=null;$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>เครือข่าย VPN</h2><button class="btn primary" onclick="networkModal()">+ เพิ่มเครือข่าย</button></div>${networkTable(networks)}</div>`}function networkTable(a){if(!a.length)return '<div class="empty">ยังไม่มีเครือข่าย</div>';return `<div class="table-wrap"><table><thead><tr><th>ID เครือข่าย</th><th>เครือข่าย</th><th>แพ็กเกจ</th><th>ลำดับ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>`<tr><td>${x.id}</td><td><button class="btn" onclick="packagePage(${x.id})"><b>${esc(x.name)}</b></button></td><td>${x.active_package_count||0} เปิด / ${x.package_count||0} ทั้งหมด</td><td>${x.sort_order}</td><td><span class="tag ${x.active?'on':''}">${x.active?'เปิด':'ปิด'}</span></td><td><button class="btn primary" onclick="packagePage(${x.id})">จัดการแพ็กเกจ</button> <button class="btn" onclick="networkModal(${x.id})">แก้ไข</button> <button class="btn danger" onclick="removeNetwork(${x.id})">ลบ</button></td></tr>`).join('')}</tbody></table></div>`}
function networkModal(id){let x=networks.find(n=>n.id===id)||{code:'',name:'',active:true,sort_order:0};showModal(`<h2>${id?'แก้ไข':'เพิ่ม'}เครือข่าย</h2><div class="form-grid"><div class="field"><label>รหัส เช่น AIS</label><input id="nc" maxlength="24" value="${esc(x.code)}" autocomplete="off"></div><div class="field"><label>ชื่อที่แสดง</label><input id="nn" value="${esc(x.name)}" autocomplete="off"></div><div class="field"><label>ลำดับ (0 = อัตโนมัติ)</label><input id="ns" type="number" min="0" value="${x.sort_order}"></div><div class="field"><label>สถานะ</label><select id="na"><option value="1" ${x.active?'selected':''}>เปิด</option><option value="0" ${!x.active?'selected':''}>ปิด</option></select></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button id="networkSave" class="btn primary" onclick="saveNetwork(${id||0})">บันทึก</button></div>`)}async function saveNetwork(id){let button=$('#networkSave');try{let code=$('#nc').value.trim().toUpperCase(),name=$('#nn').value.trim(),order=Number($('#ns').value||0);if(!/^[A-Z0-9_-]{1,24}$/.test(code))throw Error('กรุณากรอกรหัสเครือข่าย เช่น AIS');if(!name)throw Error('กรุณากรอกชื่อที่แสดง');if(!Number.isInteger(order)||order<0)throw Error('ลำดับต้องเป็นเลข 0 ขึ้นไป');button.disabled=true;await api('/api/networks'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify({code,name,active:$('#na').value==='1',sort_order:order})});closeModal();toast(id?'แก้ไขเครือข่ายแล้ว':'เพิ่มเครือข่ายแล้ว');await networkPage()}catch(e){toast(e.message||'บันทึกเครือข่ายไม่สำเร็จ')}finally{if(button)button.disabled=false}}async function removeNetwork(id){try{if(!confirm('ลบเครือข่ายและแพ็กเกจทั้งหมดภายใน? ระบบจะจัดลำดับใหม่อัตโนมัติ'))return;await api('/api/networks/'+id,{method:'DELETE'});toast('ลบและจัดลำดับใหม่แล้ว');await networkPage()}catch(e){toast(e.message||'ลบเครือข่ายไม่สำเร็จ')}}
async function packagePage(networkId){let d=await api(`/api/networks/${networkId}/packages`);activeNetwork=d.network;packages=d.items;$('#title').textContent=`แพ็กเกจ • ${activeNetwork.name}`;$('#page').innerHTML=`<div class="panel"><div class="panel-head"><div><button class="btn" onclick="networkPage();$('#title').textContent='จัดการเครือข่าย'">← เครือข่าย</button> <b style="margin-left:10px">${esc(activeNetwork.name)}</b></div><button class="btn primary" onclick="packageModal()">+ เพิ่มแพ็กเกจ</button></div>${packageTable(packages)}</div>`}function packageTable(a){if(!a.length)return '<div class="empty"><b>ยังไม่มีรายการแพ็กเกจ</b><br><small>เพิ่มแพ็กเกจแรกเพื่อให้ผู้ใช้เลือกในบอท</small></div>';return `<div class="table-wrap"><table><thead><tr><th>แพ็กเกจ</th><th>Inbound ID (3x-ui)</th><th>ราคา/วัน</th><th>ลำดับ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>`<tr><td><b>${esc(x.name)}</b><br><small>${esc(x.description)||'—'}</small></td><td>${x.inbound_id}</td><td>${money(x.price_per_day)} เครดิต</td><td>${x.sort_order}</td><td><span class="tag ${x.active?'on':''}">${x.active?'เปิด':'ปิด'}</span></td><td><button class="btn" onclick="packageModal(${x.id})">แก้ไข</button> <button class="btn danger" onclick="removePackage(${x.id})">ลบ</button></td></tr>`).join('')}</tbody></table></div>`}
function packageModal(id){let x=packages.find(p=>p.id===id)||{name:'',description:'',inbound_id:'',price_per_day:'',active:true,sort_order:0};showModal(`<h2>${id?'แก้ไข':'เพิ่ม'}แพ็กเกจ • ${esc(activeNetwork.name)}</h2><div class="form-grid"><div class="field full"><label>ชื่อแพ็กเกจ</label><input id="pkname" value="${esc(x.name)}" placeholder="เช่น โปรกันรั่ว SSH"></div><div class="field full"><label>รายละเอียด</label><textarea id="pkdesc" placeholder="รายละเอียดแพ็กเกจ">${esc(x.description)}</textarea></div><div class="field"><label>Inbound ID ใน 3x-ui</label><input id="pkinbound" type="number" min="1" value="${x.inbound_id}" placeholder="เช่น 1"></div><div class="field"><label>ราคาต่อวัน (เครดิต)</label><input id="pkprice" type="number" min="0.01" step="0.01" value="${x.price_per_day}"></div><div class="field"><label>ลำดับ (0 = อัตโนมัติ)</label><input id="pkorder" type="number" min="0" value="${x.sort_order}"></div><div class="field"><label>สถานะ</label><select id="pkactive"><option value="1" ${x.active?'selected':''}>เปิด</option><option value="0" ${!x.active?'selected':''}>ปิด</option></select></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button id="packageSave" class="btn primary" onclick="savePackage(${id||0})">บันทึก</button></div>`)}async function savePackage(id){let button=$('#packageSave');try{let name=$('#pkname').value.trim(),description=$('#pkdesc').value.trim(),inbound=Number($('#pkinbound').value),price=Number($('#pkprice').value),order=Number($('#pkorder').value||0);if(!name)throw Error('กรุณากรอกชื่อแพ็กเกจ');if(!Number.isInteger(inbound)||inbound<1)throw Error('Inbound ID ต้องเป็นเลขตั้งแต่ 1');if(!(price>0))throw Error('ราคาต่อวันต้องมากกว่า 0');if(!Number.isInteger(order)||order<0)throw Error('ลำดับต้องเป็นเลข 0 ขึ้นไป');button.disabled=true;await api(id?`/api/packages/${id}`:`/api/networks/${activeNetwork.id}/packages`,{method:id?'PUT':'POST',body:JSON.stringify({name,description,inbound_id:inbound,price_per_day:price,active:$('#pkactive').value==='1',sort_order:order})});closeModal();toast('บันทึกแพ็กเกจแล้ว');await packagePage(activeNetwork.id)}catch(e){toast(e.message||'บันทึกแพ็กเกจไม่สำเร็จ')}finally{if(button)button.disabled=false}}async function removePackage(id){try{if(!confirm('ลบแพ็กเกจนี้? ระบบจะจัดลำดับใหม่อัตโนมัติ'))return;await api(`/api/packages/${id}`,{method:'DELETE'});toast('ลบและจัดลำดับใหม่แล้ว');await packagePage(activeNetwork.id)}catch(e){toast(e.message||'ลบแพ็กเกจไม่สำเร็จ')}}
async function users(){let query=($('#uq')?.value||'').trim();let d=await api('/api/users?q='+encodeURIComponent(query));$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>ผู้ใช้</h2><input class="search" id="uq" value="${esc(query)}" placeholder="ค้นหา ID หรือ username" onkeydown="if(event.key==='Enter')users()"></div><div class="table-wrap"><table><thead><tr><th>User ID</th><th>Username</th><th>เครดิต</th><th>เริ่ม DM</th><th></th></tr></thead><tbody>${d.items.map(x=>`<tr><td>${x.user_id}</td><td>${esc(x.username?'@'+x.username:'—')}</td><td><b>${money(x.credit)}</b></td><td>${x.dm_started?'ใช่':'ไม่'}</td><td><button class="btn" onclick="credit(${x.user_id})">ปรับเครดิต</button></td></tr>`).join('')}</tbody></table></div></div>`}function credit(id){showModal(`<h2>ปรับเครดิต User ${id}</h2><div class="field"><label>จำนวน (+ เพิ่ม / - ลด)</label><input id="ca" type="number" step=".01" value="0"></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveCredit(${id})">บันทึก</button></div>`)}async function saveCredit(id){await api(`/api/users/${id}/credit`,{method:'POST',body:JSON.stringify({amount:+ca.value})});closeModal();toast('ปรับเครดิตแล้ว');users()}
async function orderPage(){let d=await api('/api/orders');$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>รายการสั่งซื้อ</h2></div>${ordersTable(d.items)}</div>`}function creditCodePanel(a){return `<div class="panel"><div class="panel-head"><h2>จัดการโค้ดรับเครดิต</h2><button class="btn primary" onclick="creditCodeModal()">+ สร้างโค้ดรับเครดิต</button></div>${creditCodeTable(a)}</div>`}async function creditCodes(){let d=await api('/api/credit-codes');creditCodeItems=d.items;enterFocus('จัดการโค้ดรับเครดิต',creditCodePanel(d.items))}function creditCodeTable(a){if(!a.length)return '<div class="empty">ยังไม่มีโค้ดในระบบ</div>';return `<div class="table-wrap"><table><thead><tr><th>ชื่อโค้ด</th><th>ประเภท</th><th>รายละเอียด</th><th>หมดอายุ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>`<tr><td><b>${esc(x.code_name)}</b></td><td>${x.mode==='free_reset'?'รีเซ็ตทดลอง':x.mode==='random'?'สุ่มเครดิต':'เครดิตเท่ากัน'}</td><td>${x.mode==='fixed'?money(x.fixed_credit)+' / คน':x.mode==='random'?money(x.total_credit)+' รวม':'รีเซ็ตสิทธิ์'}</td><td>${esc(x.expires_at)}</td><td>${x.active?'เปิด':'ปิด'}</td><td><button class="btn" onclick="creditCodeModal('${esc(x.code_key)}')">แก้ไข</button> <button class="btn danger" onclick="removeCreditCode('${esc(x.code_key)}')">ลบ</button></td></tr>`).join('')}</tbody></table></div>`}function creditCodeModal(key){let x=(creditCodeItems||[]).find(a=>a.code_key===key)||{code_name:'',mode:'fixed',fixed_credit:1,total_credit:0,max_uses:0,expires_at:'',active:true};showModal(`<h2>${key?'แก้ไข':'สร้าง'}โค้ดรับเครดิต</h2><div class="form-grid"><div class="field full"><label>ชื่อโค้ด</label><input id="ccname" value="${esc(x.code_name)}" ${key?'readonly':''}></div><div class="field"><label>ประเภท</label><select id="ccmode"><option value="fixed" ${x.mode==='fixed'?'selected':''}>เครดิตเท่ากันทุกคน</option><option value="random" ${x.mode==='random'?'selected':''}>สุ่มเครดิต</option><option value="free_reset" ${x.mode==='free_reset'?'selected':''}>รีเซ็ตทดลอง</option></select></div><div class="field"><label>เครดิตต่อคน</label><input id="ccfixed" type="number" min="0" step="0.01" value="${x.fixed_credit||0}"></div><div class="field"><label>เครดิตรวม (สุ่ม)</label><input id="cctotal" type="number" min="0" step="0.01" value="${x.total_credit||0}"></div><div class="field"><label>จำนวนผู้ใช้สูงสุด (0=ไม่จำกัด)</label><input id="ccmax" type="number" min="0" value="${x.max_uses||0}"></div><div class="field full"><label>วันหมดอายุ ISO เช่น 2026-12-31T23:59:59+07:00</label><input id="ccexp" value="${esc(x.expires_at||'')}"></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveCreditCode('${key||''}')">บันทึก</button></div>`)}async function saveCreditCode(key){let body={code_name:ccname.value.trim(),mode:ccmode.value,fixed_credit:+ccfixed.value,total_credit:+cctotal.value,max_uses:+ccmax.value,expires_at:ccexp.value.trim(),active:true};try{await api(key?`/api/credit-codes/${encodeURIComponent(key)}`:'/api/credit-codes',{method:key?'PUT':'POST',body:JSON.stringify(body)});closeModal();toast('บันทึกโค้ดแล้ว');creditCodes()}catch(e){toast(e.message||'บันทึกโค้ดไม่สำเร็จ')}}async function removeCreditCode(key){if(!confirm('ยืนยันลบโค้ดนี้?'))return;try{await api('/api/credit-codes/'+encodeURIComponent(key),{method:'DELETE'});toast('ลบโค้ดแล้ว');creditCodes()}catch(e){toast(e.message||'ลบไม่สำเร็จ')}}async function audit(){let d=await api('/api/audit');$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>ประวัติการแก้ไข</h2></div><div class="table-wrap"><table><thead><tr><th>เวลา</th><th>ช่องทาง</th><th>การทำงาน</th><th>เป้าหมาย</th><th>ค่าใหม่</th></tr></thead><tbody>${d.items.map(x=>`<tr><td>${esc(x.created_at)}</td><td><span class="tag">${esc(x.actor)}</span></td><td>${esc(x.action)}</td><td>${esc(x.target)}</td><td>${esc(x.new_value).slice(0,80)}</td></tr>`).join('')}</tbody></table></div></div>`}
function backupPage(){$('#page').innerHTML=`<div class="cards backup-grid"><div class="card action-card"><div class="action-icon blue">↓</div><div><div class="label">สำรองข้อมูล</div><div class="value compact">ดาวน์โหลดฐานข้อมูล</div><p>เก็บผู้ใช้ เครดิต การตั้งค่า เครือข่าย แพ็กเกจ และรายการซื้อเป็นไฟล์ .db</p><button class="btn primary" onclick="downloadBackup()">ดาวน์โหลดไฟล์สำรอง</button></div></div><div class="card action-card"><div class="action-icon orange">↑</div><div><div class="label">นำเข้าข้อมูล</div><div class="value compact">อัปโหลดไฟล์สำรอง</div><p>หลังเลือกไฟล์ สามารถเลือกแทนที่ทั้งหมดหรือรวมกับข้อมูลปัจจุบัน</p><input id="restoreFile" type="file" accept=".db,.sqlite,.sqlite3,application/x-sqlite3" class="hidden" onchange="selectRestoreFile(this.files[0])"><button class="btn" onclick="$('#restoreFile').click()">เลือกไฟล์ฐานข้อมูล</button></div></div></div>`}
function downloadBackup(){location.href='/api/backup'}function selectRestoreFile(file){if(!file)return;window.pendingRestoreFile=file;showModal(`<h2>นำเข้าฐานข้อมูล</h2><div class="restore-file"><b>${esc(file.name)}</b><small>${(file.size/1024/1024).toFixed(2)} MB</small></div><p class="modal-note">กรุณาเลือกวิธีนำเข้าข้อมูล</p><div class="restore-options"><button class="restore-option danger-zone" onclick="runRestore('replace')"><b>แทนที่ข้อมูลปัจจุบัน</b><small>ลบข้อมูลปัจจุบันและใช้ข้อมูลในไฟล์ทั้งหมด</small></button><button class="restore-option" onclick="runRestore('merge')"><b>รวมกับข้อมูลปัจจุบัน</b><small>เพิ่มเฉพาะข้อมูลที่ยังไม่มี โดยเก็บข้อมูลปัจจุบันไว้</small></button></div><div class="actions"><button class="btn" onclick="cancelRestore()">ยกเลิก</button></div>`)}function cancelRestore(){window.pendingRestoreFile=null;closeModal();let f=$('#restoreFile');if(f)f.value=''}async function runRestore(mode){let file=window.pendingRestoreFile;if(!file)return;let warning=mode==='replace'?'ยืนยันแทนที่ข้อมูลปัจจุบันทั้งหมด?':'ยืนยันรวมข้อมูลจากไฟล์กับข้อมูลปัจจุบัน?';if(!confirm(warning))return;try{let buttons=document.querySelectorAll('.restore-option');buttons.forEach(x=>x.disabled=true);let result=await api('/api/restore?mode='+mode,{method:'POST',headers:{'content-type':'application/octet-stream'},body:file});toast(mode==='replace'?'แทนที่ข้อมูลสำเร็จ':'รวมข้อมูลสำเร็จ');setTimeout(()=>location.reload(),900)}catch(e){toast(e.message||'นำเข้าข้อมูลไม่สำเร็จ');document.querySelectorAll('.restore-option').forEach(x=>x.disabled=false)}}
function toggleSidebar(){let app=document.querySelector('.app');let collapsed=app.classList.toggle('sidebar-collapsed');localStorage.setItem('bioshop_sidebar_collapsed',collapsed?'1':'0');let b=document.querySelector('#sideToggle');if(b)b.textContent=collapsed?'☰':'‹';}if(localStorage.getItem('bioshop_sidebar_collapsed')==='1'){document.querySelector('.app').classList.add('sidebar-collapsed');document.querySelector('#sideToggle').textContent='☰'}
const pages={overview:["ภาพรวมระบบ",overview],settings:["ตั้งค่าบอท",settings],networks:["จัดการเครือข่าย",networkPage],users:["ผู้ใช้และเครดิต",users],orders:["รายการสั่งซื้อ",orderPage],audit:["ประวัติการแก้ไข",audit],backup:["สำรอง/คืนค่าข้อมูล",backupPage]};document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-page]').forEach(x=>x.classList.remove('active'));b.classList.add('active');leaveFocus();$('#title').textContent=pages[b.dataset.page][0];pages[b.dataset.page][1]().catch(e=>toast(e.message))});function showModal(c){$('#modal').innerHTML=`<div class="modal-bg" onclick="if(event.target===this)closeModal()"><div class="modal">${c}</div></div>`;$('#modal').classList.remove('hidden')}function closeModal(){$('#modal').classList.add('hidden')}async function logout(){await api('/api/logout',{method:'POST'});location.reload()}overview().catch(e=>toast(e.message));
</script></body></html>'''
