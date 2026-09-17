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
    "cancel_button_enabled": {"label": "ปุ่มยกเลิก", "type": "bool", "default": "0", "group": "การแสดงผล"},
    "run_start_finish_enabled": {"label": "แสดงเมนูหลังจบ", "type": "bool", "default": "1", "group": "การแสดงผล"},
    "buy_dm_enabled": {"label": "อนุญาตซื้อใน DM", "type": "bool", "default": "0", "group": "ช่องทาง"},
    "freeclient_hours": {"label": "ชั่วโมงทดลองใช้ฟรี", "type": "number", "default": "1", "min": 0.1, "group": "ทดลองใช้ฟรี"},
    "freeclient_daily_limit": {"label": "สิทธิ์ทดลอง/รอบ", "type": "integer", "default": "1", "min": 0, "group": "ทดลองใช้ฟรี"},
    "run_start_finish_delay_seconds": {"label": "หน่วงเมนู (วินาที)", "type": "number", "default": "5", "min": 0.1, "group": "การแสดงผล"},
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
    expected_user = os.getenv("WEB_ADMIN_USERNAME", "admin")
    expected_pass = os.getenv("WEB_ADMIN_PASSWORD", "")
    if not expected_pass:
        raise HTTPException(503, "ยังไม่ได้ตั้ง WEB_ADMIN_PASSWORD")
    if not (hmac.compare_digest(body.username, expected_user) and hmac.compare_digest(body.password, expected_pass)):
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
        network_id = db.create_network(**body.model_dump())
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
        changed = db.update_network(network_id, **body.model_dump())
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
async def restore_backup(request: Request) -> dict[str, Any]:
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
        db.restore_database_from_file(path)
        db.add_audit_log("web", "database.restore", "uploaded-file")
        return {"ok": True, "message": "คืนค่าฐานข้อมูลสำเร็จ"}
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


@app.get("/api/orders")
def orders(request: Request, limit: int = 100) -> dict[str, Any]:
    _require(request)
    return {"items": list(reversed(db.get_buy_log_all(min(max(limit, 1), 200))))}


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
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#202124;background:#f6f8fb;--blue:#2783de;--green:#2f9e6f;--red:#dc5a52;--muted:#6b7280;--border:#e1e6ee}*{box-sizing:border-box}body{margin:0;overflow-x:hidden}.app{display:grid;grid-template-columns:244px 1fr;min-height:100vh}.side{background:#111827;color:#e5e7eb;padding:22px 14px;position:sticky;top:0;height:100vh}.brand{font-weight:800;font-size:18px;padding:4px 12px 22px}.brand small{display:block;color:#8fa0b8;font-weight:500;font-size:12px;margin-top:4px}.nav button{display:flex;align-items:center;gap:11px;width:100%;height:44px;padding:0 12px;border:0;background:transparent;color:#aeb9c9;border-radius:8px;font-size:15px;text-align:left;cursor:pointer;margin:3px 0}.nav button.active,.nav button:hover{background:#253247;color:white}.logout{position:absolute;bottom:20px;left:14px;right:14px;height:42px;border:1px solid #344258;background:#1b2638;color:#cbd5e1;border-radius:8px;cursor:pointer;font:inherit}.logout:hover{background:#253247;color:white}.main{padding:30px 36px;min-width:0}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}.top h1{font-size:25px;margin:0}.top p{color:var(--muted);margin:5px 0 0;font-size:14px}.status{font-size:13px;color:var(--green);background:#e9f6f0;border:1px solid #ccebdd;padding:7px 10px;border-radius:8px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.backup-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.card,.panel{background:#fff;border:1px solid var(--border);border-radius:12px}.card{padding:18px}.card .label{font-size:13px;color:var(--muted)}.card .value{font-size:27px;font-weight:800;margin-top:8px}.panel{margin-top:18px;overflow:hidden}.panel-head{padding:17px 19px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}.panel-head h2{font-size:16px;margin:0}.content{padding:18px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.setting{border:1px solid var(--border);border-radius:10px;padding:14px;display:flex;align-items:center;justify-content:space-between;gap:12px}.setting b{font-size:14px}.setting small{display:block;color:var(--muted);margin-top:3px}.switch{width:48px;height:28px;border:0;border-radius:16px;background:#cfd6df;position:relative;cursor:pointer;flex:none}.switch:after{content:"";position:absolute;width:22px;height:22px;background:white;border-radius:50%;left:3px;top:3px;transition:.18s}.switch.on{background:var(--green)}.switch.on:after{left:23px}.btn{border:1px solid var(--border);background:white;border-radius:8px;padding:9px 13px;cursor:pointer;font-weight:650}.btn.primary{background:var(--blue);border-color:var(--blue);color:white}.btn.danger{color:var(--red)}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid #edf0f4;white-space:nowrap}th{font-size:12px;color:var(--muted);background:#fafbfc}.table-wrap{overflow:auto}.tag{font-size:12px;padding:4px 7px;border-radius:6px;background:#eef3f8}.tag.on{background:#e7f5ee;color:#16764e}.empty{padding:40px;text-align:center;color:var(--muted)}.hidden{display:none!important}.modal-bg{position:fixed;inset:0;background:#10182870;display:grid;place-items:center;padding:18px;z-index:5}.modal{width:min(560px,100%);max-height:92vh;overflow:auto;background:white;border-radius:14px;padding:24px}.modal h2{margin:0 0 18px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}.field.full{grid-column:1/-1}.field label{display:block;font-size:13px;font-weight:650;margin-bottom:6px}.field input,.field select,.field textarea{width:100%;border:1px solid #d8dee9;border-radius:8px;padding:10px;font:inherit}.actions{display:flex;justify-content:flex-end;gap:9px;margin-top:20px}.toast{position:fixed;right:20px;bottom:20px;background:#172033;color:white;padding:12px 16px;border-radius:9px;z-index:10;box-shadow:0 8px 30px #0003}.search{height:38px;border:1px solid var(--border);border-radius:8px;padding:0 11px;width:230px}@media(max-width:900px){.app{grid-template-columns:minmax(0,1fr)}.side,.nav,.main,.panel{min-width:0;max-width:100vw}.side{height:auto;position:static;padding:12px}.brand{padding:6px 8px 10px}.nav{display:flex;overflow:auto}.nav button{min-width:max-content}.logout{position:static}.main{padding:22px 16px}.cards{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}}@media(max-width:520px){.cards,.backup-grid{grid-template-columns:1fr}.top{align-items:flex-start}.status{display:none}.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}}
</style></head><body><div class="app"><aside class="side"><div class="brand">◈ Bio-shop<small>CONTROL CENTER</small></div><nav class="nav"><button data-page="overview" class="active">▦ ภาพรวม</button><button data-page="settings">⚙ ตั้งค่าบอท</button><button data-page="networks">◫ เครือข่าย</button><button data-page="users">◎ ผู้ใช้และเครดิต</button><button data-page="orders">≡ รายการสั่งซื้อ</button><button data-page="audit">⌁ ประวัติการแก้ไข</button><button data-page="backup">▣ สำรอง/คืนค่า</button></nav><button class="logout" onclick="logout()">ออกจากระบบ</button></aside><main class="main"><div class="top"><div><h1 id="title">ภาพรวมระบบ</h1><p id="subtitle">ข้อมูลจากบอทและเว็บไซต์ใช้ฐานข้อมูลเดียวกันแบบเรียลไทม์</p></div><div class="status">● ระบบออนไลน์</div></div><section id="page"></section></main></div><div id="modal" class="hidden"></div><div id="toast" class="toast hidden"></div><script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let networks=[],packages=[],activeNetwork=null;
async function api(url,opt={}){let r=await fetch(url,{headers:{'content-type':'application/json',...(opt.headers||{})},...opt});if(r.status===401){location.reload();throw Error('unauthorized')}let j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.detail||'เกิดข้อผิดพลาด');return j}function money(n){return Number(n||0).toLocaleString('th-TH',{maximumFractionDigits:2})}function toast(t){let x=$('#toast');x.textContent=t;x.classList.remove('hidden');setTimeout(()=>x.classList.add('hidden'),2200)}
async function overview(){let d=await api('/api/overview');$('#page').innerHTML=`<div class="cards"><div class="card"><div class="label">ผู้ใช้ทั้งหมด</div><div class="value">${money(d.users)}</div></div><div class="card"><div class="label">ยอดขายรวม (เครดิต)</div><div class="value">${money(d.revenue)}</div></div><div class="card"><div class="label">ออเดอร์ทั้งหมด</div><div class="value">${money(d.orders)}</div></div><div class="card"><div class="label">เครือข่ายที่เปิดใช้</div><div class="value">${money(d.active_networks)}</div></div></div><div class="panel"><div class="panel-head"><h2>รายการสั่งซื้อล่าสุด</h2></div>${ordersTable(d.recent_orders)}</div>`}
function ordersTable(a){if(!a?.length)return '<div class="empty">ยังไม่มีรายการ</div>';return `<div class="table-wrap"><table><thead><tr><th>เวลา</th><th>ผู้ใช้</th><th>ชื่อ</th><th>เครือข่าย</th><th>วัน</th><th>GB</th><th>ราคา</th></tr></thead><tbody>${a.map(x=>`<tr><td>${esc(x.created_at)}</td><td>${esc(x.username?'@'+x.username:x.user_id)}</td><td>${esc(x.code_name)}</td><td><span class="tag">${esc(x.network)}</span></td><td>${x.days}</td><td>${x.gb||'∞'}</td><td>${money(x.cost)}</td></tr>`).join('')}</tbody></table></div>`}
async function settings(){let d=await api('/api/settings'), groups={};d.items.forEach(x=>(groups[x.group]??=[]).push(x));$('#page').innerHTML=Object.entries(groups).map(([g,a])=>`<div class="panel"><div class="panel-head"><h2>${esc(g)}</h2></div><div class="content grid">${a.map(settingRow).join('')}</div></div>`).join('')}
function settingRow(x){if(x.type==='bool')return `<div class="setting"><div><b>${esc(x.label)}</b><small>${esc(x.key)}</small></div><button class="switch ${x.value==='1'?'on':''}" aria-label="${esc(x.label)}" onclick="setSetting('${x.key}',${x.value!=='1'})"></button></div>`;return `<div class="setting"><div><b>${esc(x.label)}</b><small>${esc(x.key)}</small></div><button class="btn" onclick="editSetting('${x.key}','${esc(x.label)}','${esc(x.value)}')">${esc(x.value)||'ตั้งค่า'}</button></div>`}
async function setSetting(k,v){await api('/api/settings/'+k,{method:'PUT',body:JSON.stringify({value:v})});toast('บันทึกแล้ว — บอทใช้ค่าใหม่ทันที');settings()}function editSetting(k,l,v){showModal(`<h2>${l}</h2><div class="field"><label>ค่าใหม่</label><input id="sv" value="${v}"></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveSetting('${k}')">บันทึก</button></div>`)}async function saveSetting(k){await api('/api/settings/'+k,{method:'PUT',body:JSON.stringify({value:$('#sv').value})});closeModal();toast('บันทึกแล้ว');settings()}
async function networkPage(){let d=await api('/api/networks');networks=d.items;activeNetwork=null;$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>เครือข่าย VPN</h2><button class="btn primary" onclick="networkModal()">+ เพิ่มเครือข่าย</button></div>${networkTable(networks)}</div>`}function networkTable(a){if(!a.length)return '<div class="empty">ยังไม่มีเครือข่าย</div>';return `<div class="table-wrap"><table><thead><tr><th>ID เครือข่าย</th><th>เครือข่าย</th><th>แพ็กเกจ</th><th>ลำดับ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>`<tr><td>${x.id}</td><td><button class="btn" onclick="packagePage(${x.id})"><b>${esc(x.name)}</b></button></td><td>${x.active_package_count||0} เปิด / ${x.package_count||0} ทั้งหมด</td><td>${x.sort_order}</td><td><span class="tag ${x.active?'on':''}">${x.active?'เปิด':'ปิด'}</span></td><td><button class="btn primary" onclick="packagePage(${x.id})">จัดการแพ็กเกจ</button> <button class="btn" onclick="networkModal(${x.id})">แก้ไข</button> <button class="btn danger" onclick="removeNetwork(${x.id})">ลบ</button></td></tr>`).join('')}</tbody></table></div>`}
function networkModal(id){let x=networks.find(n=>n.id===id)||{code:'',name:'',active:true,sort_order:0};showModal(`<h2>${id?'แก้ไข':'เพิ่ม'}เครือข่าย</h2><div class="form-grid"><div class="field"><label>รหัส เช่น AIS</label><input id="nc" maxlength="24" value="${esc(x.code)}"></div><div class="field"><label>ชื่อที่แสดง</label><input id="nn" value="${esc(x.name)}"></div><div class="field"><label>ลำดับ (0 = อัตโนมัติ)</label><input id="ns" type="number" min="0" value="${x.sort_order}"></div><div class="field"><label>สถานะ</label><select id="na"><option value="1" ${x.active?'selected':''}>เปิด</option><option value="0" ${!x.active?'selected':''}>ปิด</option></select></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveNetwork(${id||0})">บันทึก</button></div>`)}async function saveNetwork(id){let b={code:nc.value.trim().toUpperCase(),name:nn.value.trim(),active:na.value==='1',sort_order:+ns.value};await api('/api/networks'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(b)});closeModal();toast('บันทึกเครือข่ายและจัดลำดับใหม่แล้ว');networkPage()}async function removeNetwork(id){if(!confirm('ลบเครือข่ายและแพ็กเกจทั้งหมดภายใน? ระบบจะจัดลำดับใหม่อัตโนมัติ'))return;await api('/api/networks/'+id,{method:'DELETE'});toast('ลบและจัดลำดับใหม่แล้ว');networkPage()}
async function packagePage(networkId){let d=await api(`/api/networks/${networkId}/packages`);activeNetwork=d.network;packages=d.items;$('#title').textContent=`แพ็กเกจ • ${activeNetwork.name}`;$('#page').innerHTML=`<div class="panel"><div class="panel-head"><div><button class="btn" onclick="networkPage();$('#title').textContent='จัดการเครือข่าย'">← เครือข่าย</button> <b style="margin-left:10px">${esc(activeNetwork.name)}</b></div><button class="btn primary" onclick="packageModal()">+ เพิ่มแพ็กเกจ</button></div>${packageTable(packages)}</div>`}function packageTable(a){if(!a.length)return '<div class="empty"><b>ยังไม่มีรายการแพ็กเกจ</b><br><small>เพิ่มแพ็กเกจแรกเพื่อให้ผู้ใช้เลือกในบอท</small></div>';return `<div class="table-wrap"><table><thead><tr><th>แพ็กเกจ</th><th>Inbound ID (3x-ui)</th><th>ราคา/วัน</th><th>ลำดับ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>`<tr><td><b>${esc(x.name)}</b><br><small>${esc(x.description)||'—'}</small></td><td>${x.inbound_id}</td><td>${money(x.price_per_day)} เครดิต</td><td>${x.sort_order}</td><td><span class="tag ${x.active?'on':''}">${x.active?'เปิด':'ปิด'}</span></td><td><button class="btn" onclick="packageModal(${x.id})">แก้ไข</button> <button class="btn danger" onclick="removePackage(${x.id})">ลบ</button></td></tr>`).join('')}</tbody></table></div>`}
function packageModal(id){let x=packages.find(p=>p.id===id)||{name:'',description:'',inbound_id:'',price_per_day:'',active:true,sort_order:0};showModal(`<h2>${id?'แก้ไข':'เพิ่ม'}แพ็กเกจ • ${esc(activeNetwork.name)}</h2><div class="form-grid"><div class="field full"><label>ชื่อแพ็กเกจ</label><input id="pkname" value="${esc(x.name)}" placeholder="เช่น โปรกันรั่ว SSH"></div><div class="field full"><label>รายละเอียด</label><textarea id="pkdesc" placeholder="รายละเอียดแพ็กเกจ">${esc(x.description)}</textarea></div><div class="field"><label>Inbound ID ใน 3x-ui</label><input id="pkinbound" type="number" min="1" value="${x.inbound_id}" placeholder="เช่น 1"></div><div class="field"><label>ราคาต่อวัน (เครดิต)</label><input id="pkprice" type="number" min="0.01" step="0.01" value="${x.price_per_day}"></div><div class="field"><label>ลำดับ (0 = อัตโนมัติ)</label><input id="pkorder" type="number" min="0" value="${x.sort_order}"></div><div class="field"><label>สถานะ</label><select id="pkactive"><option value="1" ${x.active?'selected':''}>เปิด</option><option value="0" ${!x.active?'selected':''}>ปิด</option></select></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="savePackage(${id||0})">บันทึก</button></div>`)}async function savePackage(id){let b={name:pkname.value.trim(),description:pkdesc.value.trim(),inbound_id:+pkinbound.value,price_per_day:+pkprice.value,active:pkactive.value==='1',sort_order:+pkorder.value};await api(id?`/api/packages/${id}`:`/api/networks/${activeNetwork.id}/packages`,{method:id?'PUT':'POST',body:JSON.stringify(b)});closeModal();toast('บันทึกแพ็กเกจและจัดลำดับใหม่แล้ว');packagePage(activeNetwork.id)}async function removePackage(id){if(!confirm('ลบแพ็กเกจนี้? ระบบจะจัดลำดับใหม่อัตโนมัติ'))return;await api(`/api/packages/${id}`,{method:'DELETE'});toast('ลบและจัดลำดับใหม่แล้ว');packagePage(activeNetwork.id)}
async function users(){let d=await api('/api/users?q='+encodeURIComponent($('#uq')?.value||''));$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>ผู้ใช้</h2><input class="search" id="uq" placeholder="ค้นหา ID หรือ username" onkeydown="if(event.key==='Enter')users()"></div><div class="table-wrap"><table><thead><tr><th>User ID</th><th>Username</th><th>เครดิต</th><th>เริ่ม DM</th><th></th></tr></thead><tbody>${d.items.map(x=>`<tr><td>${x.user_id}</td><td>${esc(x.username?'@'+x.username:'—')}</td><td><b>${money(x.credit)}</b></td><td>${x.dm_started?'ใช่':'ไม่'}</td><td><button class="btn" onclick="credit(${x.user_id})">ปรับเครดิต</button></td></tr>`).join('')}</tbody></table></div></div>`}function credit(id){showModal(`<h2>ปรับเครดิต User ${id}</h2><div class="field"><label>จำนวน (+ เพิ่ม / - ลด)</label><input id="ca" type="number" step=".01" value="0"></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveCredit(${id})">บันทึก</button></div>`)}async function saveCredit(id){await api(`/api/users/${id}/credit`,{method:'POST',body:JSON.stringify({amount:+ca.value})});closeModal();toast('ปรับเครดิตแล้ว');users()}
async function orderPage(){let d=await api('/api/orders');$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>รายการสั่งซื้อ</h2></div>${ordersTable(d.items)}</div>`}async function audit(){let d=await api('/api/audit');$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>ประวัติการแก้ไข</h2></div><div class="table-wrap"><table><thead><tr><th>เวลา</th><th>ช่องทาง</th><th>การทำงาน</th><th>เป้าหมาย</th><th>ค่าใหม่</th></tr></thead><tbody>${d.items.map(x=>`<tr><td>${esc(x.created_at)}</td><td><span class="tag">${esc(x.actor)}</span></td><td>${esc(x.action)}</td><td>${esc(x.target)}</td><td>${esc(x.new_value).slice(0,80)}</td></tr>`).join('')}</tbody></table></div></div>`}
function backupPage(){$('#page').innerHTML=`<div class="cards backup-grid"><div class="card"><div class="label">สำรองข้อมูล</div><div class="value" style="font-size:20px">ดาวน์โหลดฐานข้อมูล</div><p style="color:var(--muted)">รวมผู้ใช้ เครดิต การตั้งค่า เครือข่าย แพ็กเกจ และรายการซื้อ</p><button class="btn primary" onclick="downloadBackup()">ดาวน์โหลดไฟล์ .db</button></div><div class="card"><div class="label">คืนค่าข้อมูล</div><div class="value" style="font-size:20px">อัปโหลดไฟล์สำรอง</div><p style="color:var(--muted)">ข้อมูลปัจจุบันจะถูกแทนที่ กรุณาสำรองก่อนคืนค่า</p><input id="restoreFile" type="file" accept=".db,.sqlite,.sqlite3,application/x-sqlite3" style="display:none" onchange="restoreBackup(this.files[0])"><button class="btn danger" onclick="$('#restoreFile').click()">เลือกไฟล์และคืนค่า</button></div></div>`}
function downloadBackup(){location.href='/api/backup'}async function restoreBackup(file){if(!file)return;if(!confirm('ยืนยันคืนค่าจากไฟล์นี้? ข้อมูลปัจจุบันจะถูกแทนที่')){$('#restoreFile').value='';return}let r=await fetch('/api/restore',{method:'POST',headers:{'content-type':'application/octet-stream'},body:file});let j=await r.json().catch(()=>({}));if(!r.ok){toast(j.detail||'คืนค่าไม่สำเร็จ');return}toast('คืนค่าข้อมูลสำเร็จ');setTimeout(()=>location.reload(),900)}
const pages={overview:["ภาพรวมระบบ",overview],settings:["ตั้งค่าบอท",settings],networks:["จัดการเครือข่าย",networkPage],users:["ผู้ใช้และเครดิต",users],orders:["รายการสั่งซื้อ",orderPage],audit:["ประวัติการแก้ไข",audit],backup:["สำรอง/คืนค่าข้อมูล",backupPage]};document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-page]').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('#title').textContent=pages[b.dataset.page][0];pages[b.dataset.page][1]().catch(e=>toast(e.message))});function showModal(c){$('#modal').innerHTML=`<div class="modal-bg" onclick="if(event.target===this)closeModal()"><div class="modal">${c}</div></div>`;$('#modal').classList.remove('hidden')}function closeModal(){$('#modal').classList.add('hidden')}async function logout(){await api('/api/logout',{method:'POST'});location.reload()}overview().catch(e=>toast(e.message));
</script></body></html>'''
