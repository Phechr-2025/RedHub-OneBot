"""
3x-ui Panel API wrapper
รองรับ 3x-ui v3.2.8 (และ backward-compatible กับ v3.1.x / v2.x)
รองรับทั้ง Cookie-session + CSRF และ API Token แบบ Authorization: Bearer ...

อัปเดตใน v32 (3x-ui v3.2.8 compatible):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. _add_client_v3_r2: ลอง payload {"client": {...}, "inboundId": N} (v3.2.x format) ก่อน
2. _add_client_v3:    fallback ด้วย {"client": {...}, "inboundIds": [N]} (v3.1.x format)
3. _add_client_v2:    fallback เพิ่มเติม POST /panel/api/inbounds/addClient (v2.x format)
4. แก้ bug _verify_api_token: fallback endpoint เดิมซ้ำกับ primary — เปลี่ยนเป็น /panel/api/inbounds/get/1
5. _is_missing_endpoint ขยายให้รับ HTTP 400 / 422 และ success:false ที่เกิดจาก payload ผิด format
6. เพิ่ม debug log สำหรับ API response เพื่อง่ายต่อการ diagnose ใน Railway logs
7. เพิ่ม _add_client_try_all: รวม 3 ขั้นตอนในฟังก์ชันเดียว พร้อม log ชัดเจนในแต่ละขั้น
"""

import json
import base64
import time
import uuid
import secrets
import urllib.parse
import logging
from typing import Optional, Dict, Any, List

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

SESSION_REFRESH_MINUTES = 50          # session cookie 3x-ui อยู่ 60 นาที, refresh ก่อน 10 นาที
_API_TOKEN_REFRESH_MINUTES = 1440     # API Token ไม่หมดอายุ — verify ใหม่แค่ทุก 24 ชั่วโมง

_NOT_FOUND_STATUSES = {404, 405}
_WILDCARD_LISTEN = {"", "0.0.0.0", "::", "::0", "[::]"}
_V3_CLIENT_ENDPOINT = "/panel/api/clients"

_AUTH_ERROR_KEYWORDS = (
    "unauthorized",
    "please login",
    "login first",
    "not logged in",
    "session",
    "forbidden",
    "permission denied",
    "access denied",
    "invalid token",
    "token expired",
    "token is",
)

# Browser-like User-Agent เพื่อผ่าน CSRF / bot-filtering ของ 3x-ui เวอร์ชันใหม่
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _generate_sub_id(length: int = 16) -> str:
    """
    สร้าง subId แบบ random สำหรับ 3x-ui v3.x
    ใช้ lowercase hex เพื่อความเข้ากันได้สูงสุด
    """
    return secrets.token_hex(length // 2)[:length]


def _uuid_secret(with_hyphens: bool = False) -> str:
    value = uuid.uuid4()
    return str(value) if with_hyphens else value.hex


class XUIApi:
    def __init__(self, base_url: str, username: str, password: str, api_token: str = ""):
        self.base_url    = base_url.rstrip("/")
        self.username    = username
        self.password    = password
        self.session     = requests.Session()
        self.session.verify = False
        self._logged_in  = False
        self._last_login = 0.0
        self._token      = ""
        self._csrf_token = ""

        # ── Static API Token mode (Panel Settings → Security → API Tokens) ──
        self._api_token_mode = bool(api_token.strip())
        if self._api_token_mode:
            self._token     = api_token.strip()
            self._logged_in = False
            self._last_login = 0.0
            logger.info("🔑 XUI API Token mode: ใช้ Static API Token — ข้ามขั้นตอน login")

        self._apply_browser_headers(self.session)

        if self._token:
            self.session.headers.update({"Authorization": f"Bearer {self._token}"})

    # ── Browser headers ────────────────────────────────────────────────────

    def _apply_browser_headers(self, sess: requests.Session) -> None:
        sess.headers.update({
            "User-Agent": _USER_AGENT,
            "Origin":     self.base_url,
            "Referer":    f"{self.base_url}/",
            "Accept":     "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
        })

    # ── Authentication ─────────────────────────────────────────────────────

    def _fetch_csrf_token(self, sess: requests.Session) -> str:
        """
        3x-ui v3 uses CSRF middleware for /login and session POST requests.
        2.x panels do not expose this endpoint, so 404/non-JSON is harmless.
        """
        try:
            resp = sess.get(f"{self.base_url}/csrf-token", timeout=10)
        except Exception as e:
            logger.debug(f"CSRF token fetch skipped: {e}")
            return ""

        data = self._safe_json(resp)
        if resp.status_code in _NOT_FOUND_STATUSES or not data:
            return ""

        token = ""
        if data.get("success") and isinstance(data.get("obj"), str):
            token = data["obj"]
        elif isinstance(data.get("csrf"), str):
            token = data["csrf"]

        if token:
            self._csrf_token = token
            sess.headers.update({"X-CSRF-Token": token})
            logger.debug("Fetched 3x-ui CSRF token")
        return token

    def _try_login_with(self, credentials: dict, use_json: bool) -> Optional[Dict]:
        sess = requests.Session()
        sess.verify = False
        self._apply_browser_headers(sess)
        self._fetch_csrf_token(sess)

        url    = f"{self.base_url}/login"
        method = "JSON body" if use_json else "form-data"

        logger.debug(f"🔑 Attempting login [{method}] → {url}")

        try:
            if use_json:
                resp = sess.post(url, json=credentials,
                                 headers={"Content-Type": "application/json"}, timeout=15)
            else:
                resp = sess.post(url, data=credentials, timeout=15)
        except Exception as e:
            logger.error(f"❌ login request error [{method}]: {e}")
            return None

        data = self._safe_json(resp)

        if data is None:
            try:
                body_preview = resp.text[:300].replace("\n", " ").strip()
            except Exception:
                body_preview = "(อ่าน body ไม่ได้)"

            if resp.status_code == 403:
                logger.warning(
                    f"⚠️  login [{method}] HTTP 403 — "
                    f"URL ที่ส่งไป: {url}\n"
                    f"   สาเหตุที่เป็นไปได้:\n"
                    f"   1) XUI_URL ขาด Sub-Path (เส้นทางลับ) ของ Panel\n"
                    f"      → ตัวอย่างที่ถูก: https://server.com:2053/yourpath\n"
                    f"   2) IP ถูก block โดย fail2ban หรือ firewall\n"
                    f"   3) browser headers ยังไม่ผ่าน security ของ panel version นี้\n"
                    f"   body: {body_preview or '(empty)'}"
                )
            elif resp.status_code == 404:
                logger.warning(
                    f"⚠️  login [{method}] HTTP 404 — "
                    f"URL ที่ส่งไป: {url}\n"
                    f"   → XUI_URL อาจผิด หรือ panel ย้าย port/path แล้ว\n"
                    f"   body: {body_preview or '(empty)'}"
                )
            else:
                logger.warning(
                    f"⚠️  login [{method}] HTTP {resp.status_code} — "
                    f"non-JSON response: {body_preview or '(empty)'}"
                )
            return None

        if data.get("success"):
            self.session = sess
        return data

    def _verify_api_token(self) -> bool:
        try:
            self.session.headers.update({"Authorization": f"Bearer {self._token}"})
            # Primary: ดึง inbound list
            resp = self.session.get(
                f"{self.base_url}/panel/api/inbounds/list",
                timeout=15,
            )
            data = self._safe_json(resp)
            self._logged_in = bool(data and data.get("success"))
            if self._logged_in:
                self._last_login = time.time()
                logger.info("✅ API Token verified via /panel/api/inbounds/list")
                return True
            # Fallback: ลอง server status endpoint (แตกต่างจาก primary — fix bug เดิมที่ใช้ endpoint เดิมซ้ำ)
            resp2 = self.session.get(
                f"{self.base_url}/panel/api/server/status",
                timeout=15,
            )
            data2 = self._safe_json(resp2)
            if data2 and data2.get("success"):
                self._logged_in = True
                self._last_login = time.time()
                logger.info("✅ API Token verified via /panel/api/server/status")
                return True
            logger.error(
                f"❌ API Token verify failed — HTTP {resp.status_code}\n"
                f"   msg: {(data or {}).get('msg', '(no msg)')}\n"
                f"   → ตรวจสอบ XUI_API_TOKEN และ XUI_URL"
            )
            return False
        except Exception as e:
            logger.error(f"API Token verify error: {e}")
            # ถ้าเคย verify สำเร็จมาแล้วและ network error ชั่วคราว ให้ถือว่ายังใช้งานได้
            if self._logged_in:
                logger.warning("⚠️  API Token verify: network error แต่ token เคยใช้ได้ — ใช้ค่าเดิมไปก่อน")
                return True
            self._logged_in = False
            return False

    def login(self) -> bool:
        """
        Login / re-login to 3x-ui panel
        รองรับ Cookie session + CSRF และ Static API Token แบบ Bearer
        """
        if self._api_token_mode:
            logger.debug("🔑 API Token mode: skip login")
            return self._verify_api_token()

        try:
            self._token = ""
            credentials = {"username": self.username, "password": self.password}

            data = self._try_login_with(credentials, use_json=False)

            if data is None:
                logger.info("🔄 ลอง login ด้วย JSON body...")
                data = self._try_login_with(credentials, use_json=True)

            if data is None:
                logger.error(
                    "❌ 3x-ui login ล้มเหลว\n"
                    f"   URL ที่ใช้: {self.base_url}/login\n"
                    "   ─────────────────────────────────────────────\n"
                    "   สิ่งที่ต้องตรวจสอบ:\n"
                    "   1) XUI_URL ต้องรวม Sub-Path ด้วย ถ้า panel ตั้งค่าไว้\n"
                    "      ตัวอย่าง: https://your-server.com:2053/secretpath\n"
                    "   2) ตรวจสอบ XUI_USERNAME และ XUI_PASSWORD ว่าถูกต้อง\n"
                    "   3) ตรวจสอบว่า IP ของ Railway ไม่ถูก block ใน panel\n"
                    "   4) ลอง ping/curl panel URL จากเครื่องอื่นก่อน"
                )
                self._logged_in = False
                return False

            self._logged_in = data.get("success", False)

            if self._logged_in:
                obj   = data.get("obj") or {}
                token = str(obj.get("token", "") or "") if isinstance(obj, dict) else ""
                if token:
                    self._token = token
                    self.session.headers.update({"Authorization": f"Bearer {token}"})
                    logger.info("✅ 3x-ui login สำเร็จ (JWT — 3x-ui 3.0.0+)")
                else:
                    logger.info("✅ 3x-ui login สำเร็จ (session cookie)")
                self._last_login = time.time()
            else:
                logger.error(
                    f"❌ 3x-ui login ล้มเหลว: {data.get('msg', 'ไม่ทราบสาเหตุ')}\n"
                    "   → ตรวจสอบ XUI_USERNAME / XUI_PASSWORD"
                )

            return self._logged_in

        except Exception as e:
            logger.error(f"❌ 3x-ui login exception: {e}")
            self._logged_in = False
            return False

    def _should_refresh(self) -> bool:
        if not self._logged_in:
            return True
        # API Token ไม่หมดอายุ — ใช้ refresh interval ยาวกว่า session มาก
        refresh_min = _API_TOKEN_REFRESH_MINUTES if self._api_token_mode else SESSION_REFRESH_MINUTES
        return (time.time() - self._last_login) > (refresh_min * 60)

    def is_available(self) -> bool:
        if self._should_refresh():
            self.login()
        return self._logged_in

    def keep_alive(self) -> bool:
        logger.info("🔄 3x-ui session keep-alive...")
        return self.login()

    # ── Diagnostics ────────────────────────────────────────────────────────

    def probe_base_path(self, sub_path: str = "") -> bool:
        test_url = f"{self.base_url.rstrip('/')}"
        if sub_path:
            test_url = f"{test_url}/{sub_path.strip('/')}"
        test_login = f"{test_url}/login"
        try:
            resp = requests.get(test_url, verify=False, timeout=10,
                                headers={"User-Agent": _USER_AGENT})
            logger.info(
                f"🔍 probe {test_login} → HTTP {resp.status_code} "
                f"({'JSON' if 'json' in resp.headers.get('Content-Type','') else 'non-JSON'})"
            )
            return resp.status_code in (200, 301, 302)
        except Exception as e:
            logger.warning(f"🔍 probe {test_login} → error: {e}")
            return False

    # ── JSON helpers ───────────────────────────────────────────────────────

    def _safe_json(self, resp: requests.Response) -> Optional[Dict]:
        try:
            if not resp.content:
                return None
            return resp.json()
        except Exception:
            return None

    def _json_obj(self, value: Any, default: Optional[Any] = None) -> Any:
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except Exception:
                return default
        return default

    def _is_auth_error(self, data: Dict) -> bool:
        if data.get("success", True):
            return False
        msg = str(data.get("msg", "")).lower()
        if not msg:
            return True
        return any(kw in msg for kw in _AUTH_ERROR_KEYWORDS)

    # ── HTTP request with auto re-login ────────────────────────────────────

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 2,
        **kwargs,
    ) -> Optional[Dict]:
        for attempt in range(1, max_retries + 1):
            try:
                if self._api_token_mode and self._token:
                    self.session.headers.update({"Authorization": f"Bearer {self._token}"})

                resp = self.session.request(method, url, **kwargs)
                data = self._safe_json(resp)

                if data is None:
                    if resp.status_code in _NOT_FOUND_STATUSES:
                        return {
                            "success": False,
                            "msg": f"HTTP {resp.status_code} non-JSON response",
                            "_status_code": resp.status_code,
                            "_non_json": True,
                        }
                    if attempt < max_retries:
                        logger.warning(
                            f"⚠️  empty/non-JSON HTTP {resp.status_code} "
                            f"[{url}] (attempt {attempt}) → re-login..."
                        )
                        if not self.login():
                            logger.error("❌ Re-login ล้มเหลว")
                            return None
                        continue
                    logger.error(
                        f"❌ API ไม่ตอบกลับ JSON หลังลองซ้ำ {max_retries} ครั้ง\n"
                        f"   URL: {url}\n"
                        f"   → ตรวจสอบว่า XUI_URL ถูกต้องและ panel ยัง online อยู่"
                    )
                    return None

                if self._is_auth_error(data):
                    if attempt < max_retries:
                        logger.warning(
                            f"⚠️  auth error '{data.get('msg','')}' "
                            f"(attempt {attempt}) → re-login..."
                        )
                        if not self.login():
                            logger.error("❌ Re-login ล้มเหลว")
                            return None
                        continue
                    logger.error(f"❌ Auth error หลังลอง re-login: {data.get('msg','')}")
                    return data

                return data

            except requests.exceptions.ConnectionError as e:
                logger.error(f"❌ Connection error (attempt {attempt}): {e}")
                if attempt < max_retries:
                    self.login()
            except requests.exceptions.Timeout:
                logger.error(f"❌ Timeout (attempt {attempt}): {url}")
                if attempt < max_retries:
                    time.sleep(2)
            except Exception as e:
                logger.error(f"❌ Request error (attempt {attempt}): {e}")
                if attempt < max_retries:
                    self.login()

        return None

    # ── Inbound operations ─────────────────────────────────────────────────

    def get_inbound(self, inbound_id: int) -> Optional[Dict]:
        if not self.is_available():
            logger.error("❌ get_inbound ล้มเหลว: ไม่สามารถ login เข้า 3x-ui ได้")
            return None
        data = self._request_with_retry(
            "GET",
            f"{self.base_url}/panel/api/inbounds/get/{inbound_id}",
            timeout=15,
        )
        if data and data.get("success"):
            return data.get("obj")
        if data:
            logger.error(f"get_inbound failed: {data.get('msg')}")
        return None

    def get_inbounds(self) -> Optional[List[Dict]]:
        """ดึงรายการ inbound ทั้งหมด (ใช้สำหรับ v3.1.0 ที่ต้องการ list)"""
        if not self.is_available():
            return None
        data = self._request_with_retry(
            "GET",
            f"{self.base_url}/panel/api/inbounds/list",
            timeout=15,
        )
        if data and data.get("success"):
            obj = data.get("obj")
            if isinstance(obj, list):
                return obj
        return None

    def get_client_by_email(self, email: str) -> Optional[Dict]:
        """
        ดึงข้อมูล client จาก email (ชื่อ) — รองรับ 3x-ui v3.x และ v2.x

        ลำดับการลอง endpoint:
        1. /panel/api/clients/get/{email}                (3x-ui v3.1.0)
        2. /panel/api/inbounds/getClientTraffics/{email} (legacy/v2)
        """
        if not self.is_available():
            return None
        encoded = urllib.parse.quote(email, safe="")
        endpoints = [
            f"{self.base_url}{_V3_CLIENT_ENDPOINT}/get/{encoded}",
            f"{self.base_url}/panel/api/inbounds/getClientTraffics/{encoded}",
        ]
        for endpoint in endpoints:
            data = self._request_with_retry("GET", endpoint, timeout=15, max_retries=1)
            if data and not self._is_missing_endpoint(data) and data.get("success"):
                return data.get("obj")
        return None

    # ── Client payload ────────────────────────────────────────────────────

    def _client_payload(
        self,
        client_uuid: str,
        name: str,
        expire_ms: int,
        total_bytes: int,
        flow: str = "",
        sub_id: str = "",
        protocol: str = "",
        password: str = "",
        auth: str = "",
    ) -> Dict[str, Any]:
        """
        สร้าง client payload สำหรับ 3x-ui v3.1.0

        model.Client ของ v3.1.0 ต้องการ tgId เป็น int64 ไม่ใช่ string
        และรองรับ secret แยกตาม protocol:
        - VLESS/VMess ใช้ id
        - Trojan/Shadowsocks ใช้ password
        - Hysteria/Hysteria2 ใช้ auth
        - subId: สร้างอัตโนมัติถ้าไม่ระบุ (ใช้สำหรับ subscription URL)
        """
        protocol = (protocol or "").lower()
        client: Dict[str, Any] = {
            "email":      name,
            "limitIp":    0,
            "totalGB":    total_bytes,
            "expiryTime": expire_ms,
            "enable":     True,
            "tgId":       0,
            "subId":      sub_id or _generate_sub_id(),
            "reset":      0,
            "comment":    "",
        }

        if protocol in ("", "vless", "vmess"):
            client["id"] = client_uuid
            client["alterId"] = 0
            client["security"] = "auto"
            if flow:
                client["flow"] = flow
        elif protocol == "trojan":
            client["password"] = password or _uuid_secret()
        elif protocol == "shadowsocks":
            client["password"] = password or _uuid_secret()
        elif protocol in ("hysteria", "hysteria2"):
            client["auth"] = auth or _uuid_secret()
        else:
            # ให้ 3x-ui เติม default protocol-specific secret เองสำหรับ protocol ใหม่
            client["id"] = client_uuid
            if flow:
                client["flow"] = flow

        return client

    def _infer_client_flow(self, inbound: Optional[Dict]) -> str:
        """ดึง flow value จาก inbound settings (สำหรับ VLESS+Reality)"""
        if not inbound or str(inbound.get("protocol", "")).lower() != "vless":
            return ""
        settings = self._json_obj(inbound.get("settings"), {}) or {}
        clients = settings.get("clients", [])
        if not isinstance(clients, list):
            return ""
        for client in clients:
            if isinstance(client, dict):
                flow = str(client.get("flow", "") or "").strip()
                if flow:
                    return flow
        # ตรวจสอบจาก streamSettings → realitySettings ว่าใช้ Reality หรือไม่
        stream = self._json_obj(inbound.get("streamSettings"), {}) or {}
        if stream.get("security") == "reality":
            return "xtls-rprx-vision"  # default flow สำหรับ Reality
        return ""

    @staticmethod
    def _shadowsocks_key_bytes(method: str) -> int:
        method = (method or "").lower()
        if method == "2022-blake3-aes-128-gcm":
            return 16
        if method in ("2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305"):
            return 32
        return 0

    def _random_shadowsocks_password(self, inbound: Optional[Dict]) -> str:
        method, _ = self._get_shadowsocks_params(inbound or {})
        key_bytes = self._shadowsocks_key_bytes(method)
        if key_bytes:
            return base64.b64encode(secrets.token_bytes(key_bytes)).decode()
        return _uuid_secret()

    @staticmethod
    def _unwrap_client_obj(obj: Any) -> Dict[str, Any]:
        if isinstance(obj, dict) and isinstance(obj.get("client"), dict):
            return obj["client"]
        if isinstance(obj, dict):
            return obj
        return {}

    @staticmethod
    def _primary_secret_for_protocol(protocol: str, client: Dict[str, Any], fallback: str) -> str:
        protocol = (protocol or "").lower()
        if protocol in ("vless", "vmess"):
            return str(client.get("uuid") or client.get("id") or fallback)
        if protocol in ("trojan", "shadowsocks"):
            return str(client.get("password") or fallback)
        if protocol in ("hysteria", "hysteria2"):
            return str(client.get("auth") or fallback)
        return str(client.get("uuid") or client.get("id") or client.get("password") or client.get("auth") or fallback)

    # HTTP status codes ที่บ่งชี้ว่า endpoint ไม่มีอยู่ หรือ payload ผิด format → ลอง fallback
    _FALLBACK_TRIGGER_STATUSES = {400, 404, 405, 422}

    def _is_missing_endpoint(self, data: Optional[Dict]) -> bool:
        """
        คืน True เมื่อควรลอง fallback endpoint/payload:
        - HTTP 404/405: endpoint ไม่มี
        - HTTP 400/422: payload format ผิด (เช่น inboundIds vs inboundId ใน v3.2.x)
        - msg มีคำ not found / no route / invalid / bad request
        """
        if data is None:
            return False
        if data.get("_status_code") in self._FALLBACK_TRIGGER_STATUSES:
            return True
        if data.get("_non_json"):
            return True
        msg = str(data.get("msg", "")).lower()
        return any(kw in msg for kw in (
            "404", "not found", "no route",
            "invalid", "bad request", "cannot unmarshal",
            "inboundid", "field", "required",
        ))

    def _add_client_v3_r2(
        self,
        inbound_id: int,
        client: Dict[str, Any],
    ) -> Optional[Dict]:
        """
        3x-ui v3.2.x format: inboundId เป็น int เดี่ยว (ไม่ใช่ array)
        POST /panel/api/clients/add  {"client": {...}, "inboundId": N}
        """
        payload = {
            "client": client,
            "inboundId": inbound_id,          # ← singular int (v3.2.x)
        }
        logger.debug(f"[v3.2] POST {_V3_CLIENT_ENDPOINT}/add inboundId={inbound_id}")
        data = self._request_with_retry(
            "POST",
            f"{self.base_url}{_V3_CLIENT_ENDPOINT}/add",
            json=payload,
            timeout=20,
            max_retries=1,
        )
        if data and not data.get("success"):
            logger.debug(
                f"[v3.2] add failed — HTTP {data.get('_status_code','?')} "
                f"msg: {data.get('msg','')}"
            )
        return data

    def _add_client_v3(
        self,
        inbound_id: int,
        client: Dict[str, Any],
    ) -> Optional[Dict]:
        """
        3x-ui v3.1.x format: inboundIds เป็น array
        POST /panel/api/clients/add  {"client": {...}, "inboundIds": [N]}
        """
        payload = {
            "client": client,
            "inboundIds": [inbound_id],       # ← array (v3.1.x)
        }
        logger.debug(f"[v3.1] POST {_V3_CLIENT_ENDPOINT}/add inboundIds=[{inbound_id}]")
        data = self._request_with_retry(
            "POST",
            f"{self.base_url}{_V3_CLIENT_ENDPOINT}/add",
            json=payload,
            timeout=20,
            max_retries=1,
        )
        if data and not data.get("success"):
            logger.debug(
                f"[v3.1] add failed — HTTP {data.get('_status_code','?')} "
                f"msg: {data.get('msg','')}"
            )
        return data

    def _add_client_v2(
        self,
        inbound_id: int,
        client: Dict[str, Any],
    ) -> Optional[Dict]:
        """
        3x-ui v2.x fallback endpoint
        POST /panel/api/inbounds/addClient
        """
        # v2 payload ไม่รองรับ comment และ subId — ตัดออกเพื่อ compatibility
        # (v2.x จะ reject หรือ error ถ้ามี field ที่ไม่รู้จัก)
        v2_client = {k: v for k, v in client.items()
                     if k not in ("comment", "subId")}
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [v2_client]}),
        }
        return self._request_with_retry(
            "POST",
            f"{self.base_url}/panel/api/inbounds/addClient",
            json=payload,
            timeout=20,
        )

    def add_client(
        self,
        inbound_id: int,
        name: str,
        days: float,
        gb_limit: float,
    ) -> Optional[Dict[str, str]]:
        """สร้าง client ใหม่ใน inbound ที่กำหนด"""
        if not self.is_available():
            logger.error("❌ add_client ล้มเหลว: ไม่สามารถ login เข้า 3x-ui ได้")
            return None

        expire_ms   = int((time.time() + days * 86_400) * 1000) if days > 0 else 0
        total_bytes = int(gb_limit * 1024 ** 3) if gb_limit > 0 else 0
        inbound     = self.get_inbound(inbound_id)
        protocol    = str((inbound or {}).get("protocol", "") or "").lower()
        flow        = self._infer_client_flow(inbound)
        sub_id      = _generate_sub_id()
        client_uuid = str(uuid.uuid4())
        password    = ""
        auth        = ""

        if protocol == "trojan":
            password = _uuid_secret()
        elif protocol == "shadowsocks":
            password = self._random_shadowsocks_password(inbound)
        elif protocol in ("hysteria", "hysteria2"):
            auth = _uuid_secret()

        client = self._client_payload(
            client_uuid, name, expire_ms, total_bytes,
            flow=flow, sub_id=sub_id, protocol=protocol,
            password=password, auth=auth,
        )

        # ── ขั้น 1: ลอง v3.2.x format (inboundId singular) ──────────────────────
        data = self._add_client_v3_r2(inbound_id, client)

        # ── ขั้น 2: fallback → v3.1.x format (inboundIds array) ──────────────
        if not (data and data.get("success")):
            reason = f"HTTP {data.get('_status_code','?')} msg={data.get('msg','')!r}" if data else "None"
            logger.info(f"[v3.2] add ไม่สำเร็จ ({reason}) → ลอง v3.1 format (inboundIds array)")
            data = self._add_client_v3(inbound_id, client)

        # ── ขั้น 3: fallback → v2 endpoint (inbounds/addClient) ───────────────
        if not (data and data.get("success")) and self._is_missing_endpoint(data):
            reason = f"HTTP {data.get('_status_code','?')} msg={data.get('msg','')!r}" if data else "None"
            logger.info(f"[v3.1] add ไม่สำเร็จ ({reason}) → ลอง v2 endpoint (inbounds/addClient)")
            data = self._add_client_v2(inbound_id, client)

        if data and data.get("success"):
            created_obj = self.get_client_by_email(name)
            created = self._unwrap_client_obj(created_obj)
            actual_sub_id = str(created.get("subId") or sub_id)
            fallback_secret = (
                password or auth or client.get("id") or client_uuid
            )
            primary_secret = self._primary_secret_for_protocol(
                protocol, created, str(fallback_secret)
            )
            actual_flow = str(created.get("flow") or flow or "")
            actual_password = str(created.get("password") or password or "")
            actual_auth = str(created.get("auth") or auth or "")

            logger.info(
                f"✅ สร้าง client '{name}' สำเร็จ "
                f"(protocol={protocol or 'unknown'}, flow={actual_flow!r}, subId={actual_sub_id!r})"
            )
            return {
                "uuid": primary_secret,
                "name": name,
                "sub_id": actual_sub_id,
                "flow": actual_flow,
                "protocol": protocol,
                "password": actual_password,
                "auth": actual_auth,
            }
        if data:
            logger.error(
                f"❌ addClient failed — HTTP {data.get('_status_code','?')} "
                f"msg: {data.get('msg')}\n"
                f"   inbound_id={inbound_id} name={name!r} protocol={protocol!r}\n"
                f"   → ตรวจสอบ inbound ID ว่าถูกต้องใน 3x-ui panel"
            )
        else:
            logger.error(
                f"❌ addClient: API คืนค่า None — ไม่ได้รับ response จาก 3x-ui\n"
                f"   inbound_id={inbound_id} name={name!r}\n"
                f"   → ตรวจสอบ XUI_URL, network, และ panel status"
            )
        return None

    def delete_client(self, email: str, keep_traffic: bool = False) -> bool:
        """
        ลบ client ด้วย email ผ่าน API ใหม่ของ 3x-ui v3.x
        ใช้เป็น rollback เมื่อ bot สร้าง client ได้แล้วแต่สร้าง/ดึง link ไม่สำเร็จ
        """
        if not self.is_available():
            return False
        encoded = urllib.parse.quote(email, safe="")
        suffix = "?keepTraffic=1" if keep_traffic else ""
        data = self._request_with_retry(
            "POST",
            f"{self.base_url}{_V3_CLIENT_ENDPOINT}/del/{encoded}{suffix}",
            timeout=20,
            max_retries=1,
        )
        ok = bool(data and data.get("success"))
        if not ok and data:
            logger.warning(f"delete_client failed for {email!r}: {data.get('msg')}")
        return ok

    # ── Link generation ────────────────────────────────────────────────────

    _VPN_SCHEMES = (
        "vless://", "vmess://", "trojan://", "ss://", "ssr://",
        "hysteria://", "hysteria2://", "hy2://", "tuic://", "wireguard://",
    )

    def _get_client_links(self, client_name: str) -> Optional[str]:
        """
        ดึง link จาก /panel/api/clients/links/{email}
        v3.1.0 คืน list ของ links — เลือก VPN config link โดยตรง ไม่ใช่ subscription URL

        ลำดับการลอง endpoint:
        1. /panel/api/clients/links/{email}        (primary, 3x-ui v3.x)
        2. /panel/api/clients/getClientLinks/{email} (fallback, บาง build ของ v3.x)
        """
        encoded = urllib.parse.quote(client_name, safe="")
        endpoints = [
            f"{self.base_url}{_V3_CLIENT_ENDPOINT}/links/{encoded}",
            f"{self.base_url}{_V3_CLIENT_ENDPOINT}/getClientLinks/{encoded}",
        ]

        data = None
        for endpoint in endpoints:
            data = self._request_with_retry("GET", endpoint, timeout=15, max_retries=1)
            if data and not self._is_missing_endpoint(data) and data.get("success"):
                break
            data = None

        if not data or not data.get("success"):
            return None

        links = data.get("obj")

        def _is_vpn_link(link_str: str) -> bool:
            """คืน True ถ้า link เป็น VPN config (ไม่ใช่ subscription URL)"""
            s = str(link_str).strip()
            return any(s.startswith(scheme) for scheme in self._VPN_SCHEMES)

        # Handle list of links (v3.1.0 — อาจมีทั้ง VPN link และ subscription URL)
        if isinstance(links, list) and links:
            # ลำดับ 1: เลือก VPN config link
            for link in links:
                link_str = str(link).strip()
                if link_str and _is_vpn_link(link_str):
                    return link_str
            # ลำดับ 2: fallback ถ้าไม่มี VPN link ให้ใช้ตัวแรก (อาจเป็น sub URL)
            first = str(links[0]).strip()
            return first if first else None

        if isinstance(links, str) and links.strip():
            # อาจเป็น newline-separated หลาย links
            for line in links.split("\n"):
                line = line.strip()
                if line and _is_vpn_link(line):
                    return line
            # fallback: ใช้บรรทัดแรกที่ไม่ว่าง
            for line in links.split("\n"):
                line = line.strip()
                if line:
                    return line

        return None

    @staticmethod
    def _first_value(value: Any, default: str = "") -> str:
        if isinstance(value, list):
            return str(value[0]) if value else default
        if value is None:
            return default
        return str(value)

    def generate_link(
        self,
        inbound_id: int,
        client_uuid: str,
        client_name: str,
        flow: str = "",
    ) -> Optional[str]:
        """
        สร้าง VLESS, VMESS, Trojan หรือ Shadowsocks URI จาก inbound settings

        เพิ่มใน v29:
        - รับ flow parameter จาก add_client() โดยตรง
        - รองรับ Trojan protocol
        - รองรับ Shadowsocks protocol
        - ส่ง flow ใน VLESS URL สำหรับ Reality
        """
        # ลองดึง link จาก panel API ก่อน (เร็วและถูกต้องที่สุด)
        panel_link = self._get_client_links(client_name)
        if panel_link:
            return panel_link

        inbound = self.get_inbound(inbound_id)
        if not inbound:
            return None

        protocol = inbound.get("protocol", "").lower()
        port     = inbound.get("port", 443)

        stream   = self._json_obj(inbound.get("streamSettings"), {}) or {}
        network  = stream.get("network", "tcp")
        security = stream.get("security", "none")

        listen = str(inbound.get("listen", "") or "").strip()
        server = "" if listen in _WILDCARD_LISTEN else listen
        if not server:
            server = urllib.parse.urlparse(self.base_url).hostname or ""

        sni         = ""
        fingerprint = "chrome"
        public_key  = ""
        short_id    = ""
        is_reality  = security == "reality"

        if security == "tls":
            tls = stream.get("tlsSettings", {})
            sni = tls.get("serverName", "")
        elif is_reality:
            reality    = stream.get("realitySettings", {})
            sni        = self._first_value(reality.get("serverNames"))
            public_key = reality.get("settings", {}).get("publicKey", "")
            short_id   = self._first_value(reality.get("shortIds"))

        path         = "/"
        host         = ""
        service_name = ""
        header_type  = "none"

        if network == "ws":
            ws   = stream.get("wsSettings", {})
            path = ws.get("path", "/")
            host = ws.get("headers", {}).get("Host", "")
        elif network == "grpc":
            grpc         = stream.get("grpcSettings", {})
            service_name = grpc.get("serviceName", "")
        elif network == "tcp":
            tcp         = stream.get("tcpSettings", {})
            header      = tcp.get("header", {})
            header_type = header.get("type", "none")
            if header_type == "http":
                req  = header.get("request", {})
                path = self._first_value(req.get("path"), "/")
                host = self._first_value(req.get("headers", {}).get("Host"))
        elif network == "httpupgrade":
            hup  = stream.get("httpupgradeSettings", {})
            path = hup.get("path", "/")
            host = hup.get("host", "")
        elif network == "splithttp":
            sh   = stream.get("splithttpSettings", {})
            path = sh.get("path", "/")
            host = sh.get("host", "")

        encoded_name = urllib.parse.quote(client_name, safe="")

        # ── ใช้ flow จาก parameter ก่อน ถ้าไม่มีค่อยดึงจาก inbound ──────
        effective_flow = flow or self._infer_client_flow(inbound)

        if protocol == "vless":
            return self._build_vless(
                uuid=client_uuid, server=server, port=port,
                network=network, security=security, sni=sni,
                fingerprint=fingerprint, path=path, host=host,
                service_name=service_name, header_type=header_type,
                public_key=public_key, short_id=short_id,
                is_reality=is_reality, name=encoded_name,
                flow=effective_flow,
            )
        elif protocol == "vmess":
            return self._build_vmess(
                uuid=client_uuid, server=server, port=port,
                network=network, security=security, sni=sni,
                path=path, host=host, service_name=service_name,
                name=client_name,
            )
        elif protocol == "trojan":
            # Trojan ใช้ password แทน UUID; add_client() ส่ง primary secret เข้ามาในพารามิเตอร์นี้
            password = self._get_trojan_password(inbound, client_uuid)
            return self._build_trojan(
                password=password, server=server, port=port,
                network=network, security=security, sni=sni,
                fingerprint=fingerprint, path=path, host=host,
                service_name=service_name, name=encoded_name,
            )
        elif protocol == "shadowsocks":
            method, _ = self._get_shadowsocks_params(inbound)
            return self._build_shadowsocks(
                method=method, password=client_uuid,
                server=server, port=port, name=encoded_name,
            )

        logger.warning(f"ไม่รองรับ protocol: {protocol}")
        return None

    # ── Protocol builders ──────────────────────────────────────────────────

    @staticmethod
    def _build_vless(
        uuid, server, port, network, security,
        sni, fingerprint, path, host, service_name,
        header_type, public_key, short_id, is_reality, name,
        flow: str = "",
    ) -> str:
        """
        สร้าง VLESS URI — v29: เพิ่ม flow parameter สำหรับ Reality/XTLS
        """
        params: dict = {"type": network, "security": security}
        if security in ("tls", "reality"):
            params["sni"] = sni
            params["fp"]  = fingerprint
        if is_reality:
            params["pbk"] = public_key
            params["sid"] = short_id
        # ── flow: ส่งเฉพาะเมื่อมีค่า (xtls-rprx-vision สำหรับ Reality) ───
        if flow:
            params["flow"] = flow
        if network == "ws":
            params["path"] = path
            if host:
                params["host"] = host
        elif network == "grpc":
            params["serviceName"] = service_name
            params["mode"]        = "gun"
        elif network in ("httpupgrade", "splithttp"):
            params["path"] = path
            if host:
                params["host"] = host
        elif network == "tcp" and header_type == "http":
            params["headerType"] = "http"
            params["path"]       = path
            if host:
                params["host"] = host
        query = urllib.parse.urlencode(params)
        return f"vless://{uuid}@{server}:{port}?{query}#{name}"

    @staticmethod
    def _build_vmess(
        uuid, server, port, network, security,
        sni, path, host, service_name, name,
    ) -> str:
        obj = {
            "v": "2", "ps": name, "add": server, "port": str(port),
            "id": uuid, "aid": "0", "scy": "auto", "net": network,
            "type": "none", "host": host,
            "path": path if network != "grpc" else service_name,
            "tls": "tls" if security == "tls" else "",
            "sni": sni, "alpn": "", "fp": "",
        }
        encoded = base64.b64encode(
            json.dumps(obj, ensure_ascii=False).encode()
        ).decode()
        return f"vmess://{encoded}"

    @staticmethod
    def _build_trojan(
        password, server, port, network, security,
        sni, fingerprint, path, host, service_name, name,
    ) -> str:
        """สร้าง Trojan URI (รองรับใน 3x-ui v3.x)"""
        params: dict = {"security": security}
        if security in ("tls", "reality"):
            params["sni"] = sni
            params["fp"]  = fingerprint
        if network != "tcp":
            params["type"] = network
        if network == "ws":
            params["path"] = path
            if host:
                params["host"] = host
        elif network == "grpc":
            params["serviceName"] = service_name
            params["mode"]        = "gun"
        query = urllib.parse.urlencode(params)
        return f"trojan://{password}@{server}:{port}?{query}#{name}"

    @staticmethod
    def _build_shadowsocks(
        method, password, server, port, name,
    ) -> str:
        """สร้าง Shadowsocks URI (รองรับใน 3x-ui v3.x)"""
        userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
        return f"ss://{userinfo}@{server}:{port}#{name}"

    # ── Protocol helper extractors ──────────────────────────────────────────

    def _get_trojan_password(self, inbound: Dict, fallback_uuid: str) -> str:
        """
        คืน Trojan password สำหรับ manual fallback
        add_client() ส่ง primary secret ของ protocol เข้ามาผ่าน fallback_uuid แล้ว
        """
        return fallback_uuid

    def _get_shadowsocks_params(self, inbound: Dict):
        """ดึง method และ password จาก Shadowsocks inbound settings"""
        settings = self._json_obj(inbound.get("settings"), {}) or {}
        method   = str(settings.get("method", "aes-256-gcm") or "aes-256-gcm")
        password = str(settings.get("password", "") or "").strip()
        return method, password
