import os
import re
import json
import asyncio
import logging
import datetime
import warnings
from urllib.parse import urlparse, parse_qs
from telegram.warnings import PTBUserWarning

# ✅ FIX: suppress warning ที่ขึ้นทุกครั้งตอน start เกี่ยวกับ per_message=False
warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from dotenv import load_dotenv
import requests
import database as db
from xui_api import XUIApi

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# ลด log ของ HTTP client เพื่อไม่ให้ URL Telegram API (ซึ่งมี Bot Token) แสดงใน Console
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
BUILD_VERSION = "v40.0-live-dm-text-repair"

# ── Conversation states ──────────────────────────────────────────────────────
CHOOSE_NETWORK, ENTER_NAME, ENTER_DAYS, ENTER_GB = range(4)
FREE_CHOOSE_NETWORK, FREE_ENTER_NAME, FREE_ENTER_GB = range(4, 7)
ENTER_CODE_VALUE = 7
(
    ADD_CODE_TYPE,
    ADD_CODE_NAME,
    ADD_CODE_MODE,
    ADD_CODE_FIXED_AMOUNT,
    ADD_CODE_FIXED_MAX_USERS,
    ADD_CODE_FIXED_DURATION,
    ADD_CODE_RANDOM_TOTAL,
    ADD_CODE_RANDOM_DURATION,
    ADD_CODE_UNIT,
) = range(8, 17)

CFREE_ENTER_IDS      = 17   # state สำหรับรับ group IDs ใน /channelfreeclient
ADD_MYCREDIT_LINK    = 22   # state รอรับลิงก์ซองอั่งเปา TrueMoney
SMC_HUB              = 23   # state หน้า hub /Settingsmycredit
SMC_ENTER_PHONE      = 24   # state รอรับเบอร์โทรศัพท์
SMC_ENTER_RATE       = 25   # state รอรับอัตราเครดิต/บาท
SMC_ENTER_CHANNEL_IDS = 26  # state รอรับ group IDs ช่องทางเติมเครดิต
SELECT_PACKAGE = 27
CONFIRM_PURCHASE = 28

# ── Configuration ────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
RUN_START_FINISH_ENABLED_KEY = "run_start_finish_enabled"
RUN_START_FINISH_COMMANDS_KEY = "run_start_finish_commands"
RUN_START_FINISH_DELAY_KEY = "run_start_finish_delay_seconds"
DEFAULT_RUN_START_FINISH_DELAY_SECONDS = 5
CANCEL_BUTTON_ENABLED_KEY = "cancel_button_enabled"
BUY_DM_ENABLED_KEY = "buy_dm_enabled"
BUY_GROUP_IDS_KEY = "buy_group_ids"
FREECLIENT_ENABLED_KEY = "freeclient_enabled"
FREECLIENT_HOURS_KEY = "freeclient_hours"
FREECLIENT_DAILY_LIMIT_KEY = "freeclient_daily_limit"
FREECLIENT_RESET_MODE_KEY = "freeclient_reset_mode"
ADDCLIENT_ENABLED_KEY = "addclient_enabled"
CREDIT_CODE_ENABLED_KEY = "credit_code_enabled"
TRUEMONEY_WALLET_PHONE_KEY = "truemoney_wallet_phone"
TRUEMONEY_CREDIT_RATE_KEY = "truemoney_credit_rate"
TRUEMONEY_REDEEM_TIMEOUT_SECONDS = 20
TRUEMONEY_ENABLED_KEY = "truemoney_enabled"
TRUEMONEY_CHANNEL_MODE_KEY = "truemoney_channel_mode"
TRUEMONEY_GROUP_IDS_KEY = "truemoney_group_ids"
LOG_DISPLAY_LIMIT_BUY_KEY  = "log_display_limit_buy"
LOG_DISPLAY_LIMIT_FREE_KEY = "log_display_limit_free"
FREECLIENT_CHANNEL_MODE_KEY   = "freeclient_channel_mode"
FREECLIENT_GROUP_IDS_KEY      = "freeclient_group_ids"

# ── เวลาไทย (UTC+7) ───────────────────────────────────────────────────────────
_TZ_THAI = datetime.timezone(datetime.timedelta(hours=7))


def _thai_now_str() -> str:
    """วันเวลาปัจจุบันตามเวลาไทย รูปแบบ DD/MM/YYYY HH:MM:SS"""
    return _thai_now().strftime("%d/%m/%Y %H:%M:%S")


def _thai_now() -> datetime.datetime:
    return datetime.datetime.now(_TZ_THAI)


def _thai_now_iso() -> str:
    return _thai_now().isoformat(timespec="seconds")


def _thai_today_prefix() -> str:
    return _thai_now().strftime("%d/%m/%Y")


xui = XUIApi(
    base_url=os.getenv("XUI_URL", ""),
    username=os.getenv("XUI_USERNAME", ""),
    password=os.getenv("XUI_PASSWORD", ""),
    # ✅ Static API Token — ตั้งค่าใน Railway env เพื่อข้าม login/fail2ban
    # Panel Settings → Security → API Tokens → Create/Copy token มาใส่ที่นี่
    api_token=os.getenv("XUI_API_TOKEN", ""),
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _start_menu_text() -> str:
    return (
        "✨ *ยินดีต้อนรับสู่ร้าน Bio\\-shop* ✨\n\n"
        "คุณสามารถใช้คำสั่งต่อไปนี้:\n"
        "🚀 /addclient \\- สร้างโค้ดใหม่\n"
        "🧪 /freeclient \\- ทดลองใช้งานฟรี\n"
        "💰 /mycredit \\- ตรวจสอบเครดิตของคุณ\n"
        "💸 /addmycredit \\- เติมเครดิตด้วยซองอั่งเปา\n"
        "📄 /mycodes \\- ดูโค้ดที่คุณสร้างไว้\n"
        "🎁 /Enterthecode \\- กรอกโค๊ด\n"
        "💵 /checkprice \\- ดูราคาต่อวันปัจจุบัน\n\n"
        "🔮 *Tip:* /start เพื่อดูคำสั่งทั้งหมด\n\n"
        "สามารถสร้างโค้ดไปขายต่อได้\\!\\! \n"
        "แต่ถ้าหากกระทำความผิดใดๆทางเราจะไม่รับผิดชอบใดๆ ✅\n\n"
    )


async def _send_start_menu(
    bot,
    chat_id: int,
    chat_type: str,
    user_id: int,
    username: str | None,
):
    db.ensure_user(user_id, username)
    if chat_type == "private":
        db.set_dm_started(user_id)
    await bot.send_message(chat_id=chat_id, text=_start_menu_text(), parse_mode="MarkdownV2")


async def _can_receive_dm(bot, user_id: int, username: str | None = None) -> bool:
    """ตรวจจริงแบบไม่ส่งข้อความว่า Bot API ยังส่ง DM ถึงผู้ใช้ได้หรือไม่"""
    db.ensure_user(user_id, username)
    for attempt in range(2):
        try:
            await bot.send_chat_action(chat_id=user_id, action="typing")
            db.set_dm_started(user_id, True)
            return True
        except Exception as exc:
            if attempt == 0:
                await asyncio.sleep(0.25)
                continue
            db.set_dm_started(user_id, False)
            logger.info("ตรวจ DM ของ user_id=%s ไม่ผ่าน: %s", user_id, type(exc).__name__)
            return False
    return False


async def _require_dm_for_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """ส่งคำเตือนเฉพาะเมื่อทดสอบจริงแล้วพบว่าส่ง DM ไม่ได้"""
    user = update.effective_user
    if update.effective_chat.type == "private":
        db.set_dm_started(user.id, True)
        return True
    if await _can_receive_dm(context.bot, user.id, user.username):
        return True
    bot_username = (await context.bot.get_me()).username
    await update.message.reply_text(
        "⚠️ กรุณาเริ่มแชทกับบอทในส่วนตัวก่อนนะ!\n\n"
        "เพื่อให้บอทสามารถส่งไฟล์หรือโค้ดให้คุณทาง DM ได้\n"
        "กรุณากดปุ่มด้านล่าง พิมพ์ /start แล้วกลับมาใช้คำสั่งนี้ใหม่อีกครั้ง 👇",
        parse_mode=None,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💬 เปิดแชทส่วนตัว",
                url=f"https://t.me/{bot_username}?start=welcome",
            )
        ]]),
    )
    return False


def _normalize_command_name(command: str) -> str:
    return command.strip().lstrip("/").split("@", 1)[0].lower()


def _run_start_finish_enabled() -> bool:
    value = db.get_setting(RUN_START_FINISH_ENABLED_KEY, "1")
    if isinstance(value, (int, float)):
        return float(value) != 0
    return str(value).strip().lower() in {"1", "true", "on", "yes", "enabled", "open", "เปิด"}


def _setting_enabled(key: str, default: str = "0") -> bool:
    value = db.get_setting(key, default)
    if isinstance(value, (int, float)):
        return float(value) != 0
    return str(value).strip().lower() in {"1", "true", "on", "yes", "enabled", "open", "เปิด"}


def _cancel_button_enabled() -> bool:
    return _setting_enabled(CANCEL_BUTTON_ENABLED_KEY, "0")


def _buy_dm_enabled() -> bool:
    return _setting_enabled(BUY_DM_ENABLED_KEY, "0")


def _freeclient_enabled() -> bool:
    return _setting_enabled(FREECLIENT_ENABLED_KEY, "1")


def _addclient_enabled() -> bool:
    return _setting_enabled(ADDCLIENT_ENABLED_KEY, "1")


def _credit_code_enabled() -> bool:
    return _setting_enabled(CREDIT_CODE_ENABLED_KEY, "1")

def _format_interval(seconds: int) -> str:
    """แสดงความถี่ในหน่วยที่เหมาะสม"""
    if seconds < 60:
        return f"{seconds} วินาที"
    elif seconds < 3600:
        m = seconds // 60
        return f"{m} นาที"
    else:
        h = seconds / 3600
        return f"{h:.1f} ชั่วโมง".replace(".0 ", " ")


def _get_positive_float_setting(key: str, default: float) -> float:
    value = db.get_setting(key, str(default))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _get_run_start_finish_delay_seconds() -> float:
    return _get_positive_float_setting(
        RUN_START_FINISH_DELAY_KEY,
        float(DEFAULT_RUN_START_FINISH_DELAY_SECONDS),
    )


def _format_seconds(seconds: float) -> str:
    return str(int(seconds)) if float(seconds).is_integer() else f"{seconds:g}"


def _get_freeclient_hours() -> float:
    return _get_positive_float_setting(FREECLIENT_HOURS_KEY, 1.0)


def _get_freeclient_daily_limit() -> int:
    try:
        return max(0, int(float(str(db.get_setting(FREECLIENT_DAILY_LIMIT_KEY, 1)))))
    except (TypeError, ValueError):
        return 1


def _get_freeclient_reset_mode() -> str:
    mode = str(db.get_setting_text(FREECLIENT_RESET_MODE_KEY, "midnight") or "midnight").strip()
    return mode if mode in {"midnight", "rolling_24h"} else "midnight"


def _freeclient_reset_mode_label(mode: str | None = None) -> str:
    mode = mode or _get_freeclient_reset_mode()
    if mode == "rolling_24h":
        return "ตัดตามเวลาที่ผู้ใช้สร้างแต่ละคน (ครบ 24 ชั่วโมง)"
    return "ทุกคนรีเซ็ต 00:00 พร้อมกัน"


def _parse_free_log_datetime(raw: str) -> datetime.datetime | None:
    text = str(raw or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=_TZ_THAI)
        except ValueError:
            pass
    try:
        parsed = datetime.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_TZ_THAI)
    except ValueError:
        return None


def _parse_iso_thai(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_TZ_THAI)
    except ValueError:
        return None


def _freeclient_usage_cutoff(user_id: int) -> tuple[datetime.datetime, str]:
    now = _thai_now()
    mode = _get_freeclient_reset_mode()
    if mode == "rolling_24h":
        cutoff = now - datetime.timedelta(hours=24)
        label = "ช่วง 24 ชั่วโมงล่าสุด"
    else:
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "วันนี้"

    reset_at = _parse_iso_thai(db.get_freeclient_limit_reset(user_id))
    if reset_at and reset_at > cutoff:
        cutoff = reset_at
        label = "หลังการรีเซ็ตล่าสุด"
    return cutoff, label


def _count_freeclient_usage(user_id: int) -> tuple[int, str]:
    cutoff, label = _freeclient_usage_cutoff(user_id)
    count = 0
    for entry in db.get_free_log(user_id, db.LOG_MAX_ENTRIES):
        created_at = _parse_free_log_datetime(entry.get("created_at"))
        if created_at and created_at >= cutoff:
            count += 1
    return count, label


def _format_hours(hours: float) -> str:
    return str(int(hours)) if float(hours).is_integer() else f"{hours:g}"


# ── freeclient channel mode helpers ──────────────────────────────────────────

_CFREE_MODE_LABELS = {
    "dm_and_group":    "🌐 DM และกลุ่มทั้งหมด",
    "group_only":      "👥 กลุ่มเท่านั้น",
    "specified_and_dm":"🔒 กลุ่มที่กำหนด + DM",
    "specified_only":  "🔐 กลุ่มที่กำหนดเท่านั้น",
    "dm_only":         "💬 DM เท่านั้น",
}

def _get_freeclient_channel_mode() -> str:
    return str(db.get_setting(FREECLIENT_CHANNEL_MODE_KEY, "dm_and_group") or "dm_and_group")


def _get_freeclient_group_ids() -> set:
    raw = str(db.get_setting(FREECLIENT_GROUP_IDS_KEY, "") or "")
    ids: set = set()
    for part in raw.split(","):
        try:
            ids.add(int(part.strip()))
        except (ValueError, TypeError):
            pass
    return ids


def _set_freeclient_group_ids(ids: set):
    db.set_setting(FREECLIENT_GROUP_IDS_KEY, ",".join(str(i) for i in sorted(ids)))


def _check_freeclient_channel(chat) -> tuple:
    """ตรวจสอบว่าแชทนี้อนุญาตให้ใช้ /freeclient หรือไม่  คืน (allowed, error_msg)"""
    mode = _get_freeclient_channel_mode()
    in_group = chat.type != "private"
    if mode == "dm_and_group":
        return True, ""
    if mode == "dm_only":
        if in_group:
            return False, "❌ คำสั่ง /freeclient ใช้ได้เฉพาะทาง DM ส่วนตัวเท่านั้น"
        return True, ""
    if mode == "group_only":
        if not in_group:
            return False, "❌ คำสั่ง /freeclient ใช้ได้เฉพาะในกลุ่มเท่านั้น"
        return True, ""
    allowed_ids = _get_freeclient_group_ids()
    if mode == "specified_and_dm":
        if not in_group:
            return True, ""
        if allowed_ids and chat.id not in allowed_ids:
            return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาตให้ใช้คำสั่ง /freeclient"
        return True, ""
    if mode == "specified_only":
        if not in_group:
            return False, "❌ คำสั่ง /freeclient ใช้ได้เฉพาะในกลุ่มที่กำหนดเท่านั้น"
        if allowed_ids and chat.id not in allowed_ids:
            return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาตให้ใช้คำสั่ง /freeclient"
        return True, ""
    return True, ""

def _get_mycodes_sort_order() -> str:
    """คืน 'newest_bottom' หรือ 'newest_top' ตามที่ admin ตั้งไว้"""
    val = db.get_setting("mycodes_sort_order", "newest_bottom")
    return str(val) if val in ("newest_bottom", "newest_top") else "newest_bottom"


def _get_mycodes_store_limit() -> int:
    try:
        return max(1, int(float(str(db.get_setting("mycodes_store_limit", 100)))))
    except Exception:
        return 100


def _get_mycodes_display_limit() -> int:
    try:
        return max(1, int(float(str(db.get_setting("mycodes_display_limit", 100)))))
    except Exception:
        return 100


def _get_log_display_limit(key: str) -> int:
    """ดึงจำนวนรายการที่แสดงจาก settings (ค่าเริ่มต้น = LOG_MAX_ENTRIES ใน database.py)"""
    raw = db.get_setting(key, str(db.LOG_MAX_ENTRIES))
    try:
        n = int(float(raw))
        return max(1, n)
    except (TypeError, ValueError):
        return db.LOG_MAX_ENTRIES


def _get_log_display_limit_buy() -> int:
    return _get_log_display_limit(LOG_DISPLAY_LIMIT_BUY_KEY)


def _get_log_display_limit_free() -> int:
    return _get_log_display_limit(LOG_DISPLAY_LIMIT_FREE_KEY)


def _normalize_credit_code_name(raw: str) -> str:
    return str(raw or "").strip().lstrip("/").strip()


def _credit_code_key(name: str) -> str:
    return _normalize_credit_code_name(name).lower()


def _is_valid_credit_code_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name))


def _parse_positive_amount(raw: str) -> float | None:
    try:
        amount = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    rounded = round(amount, 2)
    return rounded if rounded > 0 else None


def _format_credit(amount: float) -> str:
    amount = float(amount)
    return str(int(amount)) if amount.is_integer() else f"{amount:.2f}".rstrip("0").rstrip(".")


def _refund_credit_after_failure(user_id: int, amount: float, reason: str) -> None:
    """คืนเครดิตเมื่อสร้าง VPN ไม่สำเร็จหลังจากหักเครดิตไปแล้ว"""
    if amount <= 0:
        return
    db.add_credit(user_id, amount)
    logger.warning("Refunded %.2f credits to user %s: %s", amount, user_id, reason)


def _normalize_truemoney_phone(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def _is_valid_truemoney_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"0\d{9}", phone))


def _get_truemoney_wallet_phone() -> str:
    phone = _normalize_truemoney_phone(db.get_setting_text(TRUEMONEY_WALLET_PHONE_KEY, ""))
    return phone if _is_valid_truemoney_phone(phone) else ""


def _get_truemoney_credit_rate() -> float:
    try:
        rate = float(db.get_setting(TRUEMONEY_CREDIT_RATE_KEY, "1"))
    except (TypeError, ValueError):
        return 1.0
    return rate if rate > 0 else 1.0


def _truemoney_enabled() -> bool:
    return _setting_enabled(TRUEMONEY_ENABLED_KEY, "1")


_SMC_CHANNEL_LABELS: dict[str, str] = {
    "dm_only":          "DM เท่านั้น",
    "dm_and_group":     "กลุ่มและ DM",
    "group_only":       "กลุ่มเท่านั้น",
    "specified_only":   "กลุ่มที่กำหนด ID เท่านั้น",
    "specified_and_dm": "กลุ่มที่กำหนด ID + DM",
}


def _get_truemoney_channel_mode() -> str:
    v = db.get_setting(TRUEMONEY_CHANNEL_MODE_KEY, "dm_only")
    return str(v).strip() if v else "dm_only"


def _get_truemoney_group_ids() -> set:
    raw = str(db.get_setting(TRUEMONEY_GROUP_IDS_KEY, "") or "")
    ids: set = set()
    for part in raw.split(","):
        try:
            ids.add(int(part.strip()))
        except (TypeError, ValueError):
            pass
    return ids


def _truemoney_allowed_in_chat(chat) -> tuple:
    """คืน (allowed, error_msg) ตาม truemoney_channel_mode"""
    mode = _get_truemoney_channel_mode()
    is_private = chat.type == "private"
    if mode == "dm_only":
        if is_private:
            return True, ""
        return False, "❌ คำสั่ง /addmycredit ใช้ได้เฉพาะใน DM ส่วนตัวกับบอทเท่านั้น"
    if mode == "dm_and_group":
        return True, ""
    if mode == "group_only":
        if not is_private:
            return True, ""
        return False, "❌ คำสั่ง /addmycredit ใช้ได้เฉพาะในกลุ่มเท่านั้น"
    allowed_ids = _get_truemoney_group_ids()
    if mode == "specified_only":
        if is_private:
            return False, "❌ คำสั่ง /addmycredit ใช้ได้เฉพาะในกลุ่มที่กำหนดเท่านั้น"
        if not allowed_ids or chat.id in allowed_ids:
            return True, ""
        return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาต กรุณาเติมเครดิตในกลุ่มที่แอดมินกำหนด"
    if mode == "specified_and_dm":
        if is_private:
            return True, ""
        if not allowed_ids or chat.id in allowed_ids:
            return True, ""
        return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาต กรุณาใช้ DM หรือกลุ่มที่แอดมินกำหนด"
    return True, ""


def _extract_truemoney_voucher_hash(raw: str) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None

    match = re.search(
        r"https://gift\.truemoney\.com/campaign(?:/voucher_detail)?/?\?v=([A-Za-z0-9]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)

    cleaned = text.strip("<>()[]{}'\"")
    try:
        parsed = urlparse(cleaned)
        if parsed.netloc.lower() == "gift.truemoney.com":
            values = parse_qs(parsed.query).get("v", [])
            if values and re.fullmatch(r"[A-Za-z0-9]{20,80}", values[0]):
                return values[0]
    except ValueError:
        pass

    direct_match = re.fullmatch(r"(?:v=)?([A-Za-z0-9]{20,80})", cleaned)
    return direct_match.group(1) if direct_match else None


def _parse_truemoney_amount(value) -> float | None:
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        match = re.search(r"\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", str(value or ""))
        if not match:
            return None
        amount = float(match.group(0).replace(",", ""))
    amount = round(amount, 2)
    return amount if amount > 0 else None


def _json_compact(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:5000]
    except (TypeError, ValueError):
        return ""


def _truemoney_error_message(code: str, message: str = "") -> str:
    code = str(code or "").strip()
    mapping = {
        "VOUCHER_NOT_FOUND": "ไม่พบซองอั่งเปานี้",
        "VOUCHER_EXPIRED": "ซองอั่งเปาหมดอายุแล้ว",
        "VOUCHER_OUT_OF_STOCK": "ซองอั่งเปานี้ถูกใช้ไปแล้ว",
        "CANNOT_GET_OWN_VOUCHER": "ไม่สามารถรับซองของตัวเองได้",
        "TARGET_USER_NOT_FOUND": "ไม่พบเบอร์รับซองในระบบ TrueMoney",
        "INTERNAL_ERROR": "ระบบ TrueMoney ไม่พร้อมให้บริการชั่วคราว",
        "INVALID_VOUCHER": "รูปแบบซองอั่งเปาไม่ถูกต้อง",
        "INVALID_PHONE": "เบอร์รับซองอั่งเปาไม่ถูกต้อง",
        "INVALID_AMOUNT": "ระบบไม่สามารถอ่านจำนวนเงินจากซองได้",
    }
    if code in mapping:
        return f"{mapping[code]} ({code})"
    if message:
        return f"{message} ({code})" if code else message
    return f"สถานะจาก TrueMoney: {code}" if code else "ไม่สามารถรับซองอั่งเปาได้"


def _redeem_truemoney_voucher_sync(phone: str, voucher_hash: str) -> dict:
    url = f"https://gift.truemoney.com/campaign/vouchers/{voucher_hash}/redeem"
    payload = {"mobile": phone, "voucher_hash": voucher_hash}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 BioShopBot/1.0",
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=TRUEMONEY_REDEEM_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return {
            "success": False,
            "status_code": "REQUEST_FAILED",
            "message": str(exc),
            "raw_response": {},
        }

    try:
        data = response.json()
    except ValueError:
        return {
            "success": False,
            "status_code": f"HTTP_{response.status_code}",
            "message": "TrueMoney ไม่ได้ส่งผลลัพธ์เป็น JSON",
            "raw_response": {},
        }

    status = data.get("status") if isinstance(data, dict) else {}
    status = status if isinstance(status, dict) else {}
    code = str(status.get("code") or f"HTTP_{response.status_code}")
    message = str(status.get("message") or "")

    if response.status_code != 200 or code != "SUCCESS":
        return {
            "success": False,
            "status_code": code,
            "message": message,
            "raw_response": data,
        }

    ticket = ((data.get("data") or {}).get("my_ticket") or {})
    amount = _parse_truemoney_amount(ticket.get("amount_baht"))
    if amount is None:
        return {
            "success": False,
            "status_code": "INVALID_AMOUNT",
            "message": "",
            "raw_response": data,
        }

    return {
        "success": True,
        "status_code": code,
        "amount_baht": amount,
        "raw_response": data,
    }


def _format_limit_count(max_uses: int) -> str:
    return "ไม่จำกัด" if int(max_uses) == 0 else f"{int(max_uses)} คน"


def _format_thai_datetime(iso_text: str) -> str:
    try:
        return datetime.datetime.fromisoformat(str(iso_text)).strftime("%d/%m/%Y %H:%M:%S")
    except (TypeError, ValueError):
        return str(iso_text)


def _duration_to_delta(amount: int, unit: str) -> datetime.timedelta:
    if unit == "s":
        return datetime.timedelta(seconds=amount)
    if unit == "m":
        return datetime.timedelta(minutes=amount)
    if unit == "h":
        return datetime.timedelta(hours=amount)
    if unit == "d":
        return datetime.timedelta(days=amount)
    if unit == "mo":
        return datetime.timedelta(days=amount * 30)
    if unit == "y":
        return datetime.timedelta(days=amount * 365)
    return datetime.timedelta(days=amount)


def _unit_label(unit: str) -> str:
    labels = {
        "s": "วินาที",
        "m": "นาที",
        "h": "ชั่วโมง",
        "d": "วัน",
        "mo": "เดือน",
        "y": "ปี",
    }
    return labels.get(unit, "วัน")


def _credit_code_mode_label(mode: str) -> str:
    if mode == "fixed":
        return "เครดิตเท่ากันทุกคน"
    if mode == "random":
        return "เครดิตสุ่ม"
    if mode == "free_reset":
        return "รีเซ็ตเวลาทดลอง"
    return str(mode)


def _credit_code_usage_text(code: dict) -> str:
    if code["mode"] in ("fixed", "free_reset"):
        return f"{int(code['used_count'])}/{_format_limit_count(int(code['max_uses']))}"
    return (
        f"{_format_credit(code['distributed_credit'])}/"
        f"{_format_credit(code['total_credit'])} เครดิต"
    )


def _credit_code_status_text(code: dict) -> str:
    if int(code.get("active", 1)) != 1:
        return "ปิดใช้งาน"
    if str(code["expires_at"]) <= _thai_now_iso():
        return "หมดอายุ"
    if code["mode"] in ("fixed", "free_reset") and int(code["max_uses"]) > 0 and int(code["used_count"]) >= int(code["max_uses"]):
        return "ครบจำนวนแล้ว"
    if code["mode"] == "random" and float(code["distributed_credit"]) >= float(code["total_credit"]):
        return "เครดิตเต็มแล้ว"
    return "ใช้งานได้"


def _build_credit_code_unit_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("วินาที", callback_data=f"addcode_unit_s_{user_id}"),
            InlineKeyboardButton("นาที", callback_data=f"addcode_unit_m_{user_id}"),
        ],
        [
            InlineKeyboardButton("ชั่วโมง", callback_data=f"addcode_unit_h_{user_id}"),
            InlineKeyboardButton("วัน", callback_data=f"addcode_unit_d_{user_id}"),
        ],
        [
            InlineKeyboardButton("เดือน", callback_data=f"addcode_unit_mo_{user_id}"),
            InlineKeyboardButton("ปี", callback_data=f"addcode_unit_y_{user_id}"),
        ],
    ])


async def _send_chunked_text(update: Update, header: str, entries: list[str]):
    chunk = header
    for entry in entries:
        block = entry + "\n"
        if len(chunk) + len(block) > 3900:
            await update.message.reply_text(chunk, disable_web_page_preview=True)
            chunk = block
        else:
            chunk += block
    if chunk.strip():
        await update.message.reply_text(chunk, disable_web_page_preview=True)


def _parse_int_setting_part(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _get_allowed_buy_group_ids() -> set[int]:
    raw_group_ids = db.get_setting(BUY_GROUP_IDS_KEY, "")
    group_ids: set[int] = set()
    for part in str(raw_group_ids).split(","):
        group_id = _parse_int_setting_part(part.strip())
        if group_id is not None:
            group_ids.add(group_id)
    return group_ids


def _set_allowed_buy_group_ids(group_ids: set[int]):
    db.set_setting(BUY_GROUP_IDS_KEY, ",".join(str(group_id) for group_id in sorted(group_ids)))


def _format_buy_policy() -> str:
    dm_status = "เปิด" if _buy_dm_enabled() else "ปิด"
    allowed_groups = sorted(_get_allowed_buy_group_ids())
    group_text = "ทุกกลุ่ม" if not allowed_groups else ", ".join(str(group_id) for group_id in allowed_groups)
    return f"ซื้อผ่าน DM: {dm_status}\nกลุ่มที่ซื้อได้: {group_text}"


def _addclient_allowed_in_chat(chat, user_id: int) -> tuple[bool, str | None]:
    if is_admin(user_id):
        return True, None

    if chat.type == "private":
        if _buy_dm_enabled():
            return True, None
        return False, "❌ ตอนนี้ปิดการซื้อผ่านแชทส่วนตัว กรุณาใช้ /addclient ในกลุ่มที่แอดมินกำหนด"

    allowed_group_ids = _get_allowed_buy_group_ids()
    if allowed_group_ids and chat.id not in allowed_group_ids:
        return False, "❌ กลุ่มนี้ยังไม่ได้รับอนุญาตให้ซื้อ กรุณาใช้ /addclient ในกลุ่มที่แอดมินกำหนด"

    return True, None


def _get_run_start_finish_commands() -> set[str]:
    raw_commands = db.get_setting(RUN_START_FINISH_COMMANDS_KEY, "addclient")
    return {
        command
        for command in (_normalize_command_name(part) for part in str(raw_commands).split(","))
        if command
    }


def _set_run_start_finish_commands(commands: set[str]):
    normalized = sorted({_normalize_command_name(command) for command in commands if command})
    db.set_setting(RUN_START_FINISH_COMMANDS_KEY, ",".join(normalized))


def _format_run_start_finish_commands() -> str:
    commands = sorted(_get_run_start_finish_commands())
    if not commands:
        return "ยังไม่มีคำสั่ง"
    return ", ".join(f"/{command}" for command in commands)


def _should_run_start_finish(command_name: str) -> bool:
    return _run_start_finish_enabled() and _normalize_command_name(command_name) in _get_run_start_finish_commands()


async def _delayed_start_menu(
    bot,
    chat_id: int,
    chat_type: str,
    user_id: int,
    username: str | None,
):
    await asyncio.sleep(_get_run_start_finish_delay_seconds())
    try:
        await _send_start_menu(bot, chat_id, chat_type, user_id, username)
    except Exception:
        logger.exception("Failed to send delayed /start menu")


def _schedule_start_menu_after_finish(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_name: str,
):
    if not _should_run_start_finish(command_name):
        return
    chat = update.effective_chat
    user = update.effective_user
    context.application.create_task(
        _delayed_start_menu(context.bot, chat.id, chat.type, user.id, user.username)
    )


def _cancel_keyboard(user_id: int, flow_name: str = "addclient") -> InlineKeyboardMarkup | None:
    if not _cancel_button_enabled():
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ /cancel", callback_data=f"cancel_{flow_name}_{user_id}")]
    ])


def _is_cancel_button_target(raw_target: str) -> bool:
    return _normalize_command_name(raw_target) == "cancel"


async def _send_cancel_button_before_question(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    flow_name: str = "addclient",
):
    if not _cancel_button_enabled():
        return
    text_by_flow = {
        "addclient": "หากต้องการยกเลิกการสร้างโค้ด กดปุ่มด้านล่าง",
        "freeclient": "หากต้องการยกเลิกการทดลองใช้ฟรี กดปุ่มด้านล่าง",
        "entercode": "หากต้องการยกเลิกการกรอกโค้ด กดปุ่มด้านล่าง",
        "addcode": "หากต้องการยกเลิกการสร้างโค้ด กดปุ่มด้านล่าง",
        "addmycredit": "หากต้องการยกเลิกการเติมเครดิต กดปุ่มด้านล่าง",
    }
    await context.bot.send_message(
        chat_id=chat_id,
        text=text_by_flow.get(flow_name, "หากต้องการยกเลิก กดปุ่มด้านล่าง"),
        reply_markup=_cancel_keyboard(user_id, flow_name),
    )


async def _send_force_reply_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    user_id: int,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
    flow_name: str = "addclient",
):
    chat_id = update.effective_chat.id
    await _send_cancel_button_before_question(context, chat_id, user_id, flow_name)
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_to_message_id=reply_to_message_id,
        reply_markup=ForceReply(selective=True),
    )
    context.user_data["waiting_msg_id"] = sent.message_id
    return sent


async def _send_plain_force_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    user_id: int,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
):
    sent = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode=parse_mode,
        reply_to_message_id=reply_to_message_id,
        reply_markup=ForceReply(selective=True),
    )
    context.user_data["waiting_msg_id"] = sent.message_id
    context.user_data["session_user_id"] = user_id
    return sent


# ────────────────────────────────────────────────────────────────────────────
# Session ownership helpers
#
# bot_data["active_sessions"] เก็บ mapping ของ message_id (ของปุ่ม inline keyboard)
# → user_id เจ้าของ session นั้น  ใช้ตรวจสอบว่าใครกดปุ่มได้บ้าง
#
# callback_data จะฝัง user_id ของเจ้าของไว้ท้าย เช่น "network_ais_123456"
# เพื่อให้ตรวจสอบได้ทั้งในและนอก ConversationHandler
# ────────────────────────────────────────────────────────────────────────────

def _parse_owner_id(callback_data: str) -> int | None:
    """แยก owner user_id จาก callback_data รูปแบบ 'prefix_value_userid'"""
    try:
        return int(callback_data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return None


async def _reject_foreign_interaction(
    update: Update,
    is_callback: bool = True,
):
    """ตอบกลับผู้ใช้ที่พยายามยุ่งกับ session ของคนอื่น"""
    msg = "⚠️ คุณไม่สามารถใช้งานของผู้อื่นได้"
    if is_callback and update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg)


# ────────────────────────────────────────────────────────────────────────────
# Helper: ตรวจสอบว่า user reply ตอบกลับข้อความคำถามของบอทหรือไม่
#   - Private chat  → รับทุกข้อความ (ไม่ต้องตรวจ reply)
#   - Group chat    → ต้องเป็น reply ต่อ message_id ที่บอทถามไว้เท่านั้น
# ────────────────────────────────────────────────────────────────────────────
def is_reply_to_bot_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    in_group = update.effective_chat.type != "private"
    if not in_group:
        return True
    waiting_id = context.user_data.get("waiting_msg_id")
    if not waiting_id:
        return True
    reply_to = update.message.reply_to_message
    return reply_to is not None and reply_to.message_id == waiting_id


def is_session_owner_for_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    ตรวจสอบว่าผู้ส่งข้อความคือเจ้าของ session ปัจจุบันหรือไม่
    ใช้สำหรับ Group chat เท่านั้น (Private chat 1-to-1 ไม่มีปัญหา)
    """
    in_group = update.effective_chat.type != "private"
    if not in_group:
        return True
    # context.user_data เป็น per-user → ถ้า user นี้มี session ของตัวเอง
    # ก็จะมี "session_user_id" ที่ตรงกับตัวเองอยู่แล้ว
    session_owner = context.user_data.get("session_user_id")
    if session_owner is None:
        return True
    return update.effective_user.id == session_owner


# ── /start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _send_start_menu(
        context.bot,
        update.effective_chat.id,
        update.effective_chat.type,
        user.id,
        user.username,
    )


# ── /mycredit ────────────────────────────────────────────────────────────────
async def mycredit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username)
    credit = db.get_credit(user.id)
    await update.message.reply_text(
        f"💰 เครดิตของคุณ: *{credit:.2f}* เครดิต",
        parse_mode="Markdown",
    )
    _schedule_start_menu_after_finish(update, context, "mycredit")


# ── /addmycredit ─────────────────────────────────────────────────────────────
async def addmycredit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    db.ensure_user(user.id, user.username)
    if chat.type == "private":
        db.set_dm_started(user.id)

    # ── ตรวจสอบว่าระบบเติมเครดิตเปิดอยู่ ─────────────────────────────────────
    if not _truemoney_enabled():
        await update.message.reply_text("❌ ระบบเติมเครดิตด้วยซองอั่งเปาถูกปิดไว้ชั่วคราว")
        return ConversationHandler.END

    # ── ตรวจสอบช่องทางที่อนุญาต ───────────────────────────────────────────────
    allowed, deny_msg = _truemoney_allowed_in_chat(chat)
    if not allowed:
        await update.message.reply_text(deny_msg)
        return ConversationHandler.END

    phone = _get_truemoney_wallet_phone()
    if not phone:
        await update.message.reply_text(
            "❌ ระบบเติมเครดิตด้วยซองอั่งเปายังไม่ได้ตั้งค่าเบอร์รับซอง\n"
            "กรุณาติดต่อแอดมินให้ตั้งค่าด้วย /setangpaophone ก่อน"
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["session_user_id"] = user.id
    context.user_data["command_msg_id"] = update.message.message_id
    await _send_force_reply_question(
        update,
        context,
        "กรุณาส่งลิงก์ซองอั่งเปา TrueMoney Wallet\n"
        "ตัวอย่าง: https://gift.truemoney.com/campaign?v=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        user_id=user.id,
        reply_to_message_id=update.message.message_id,
        flow_name="addmycredit",
    )
    return ADD_MYCREDIT_LINK


async def addmycredit_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return ADD_MYCREDIT_LINK
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return ADD_MYCREDIT_LINK

    user = update.effective_user
    voucher_hash = _extract_truemoney_voucher_hash(update.message.text)
    if not voucher_hash:
        await _send_force_reply_question(
            update,
            context,
            "❌ รูปแบบลิงก์ซองอั่งเปาไม่ถูกต้อง\n"
            "กรุณาส่งลิงก์รูปแบบ https://gift.truemoney.com/campaign?v=...",
            user_id=user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addmycredit",
        )
        return ADD_MYCREDIT_LINK

    phone = _get_truemoney_wallet_phone()
    if not phone:
        await update.message.reply_text("❌ แอดมินยังไม่ได้ตั้งค่าเบอร์รับซองอั่งเปา")
        context.user_data.clear()
        return ConversationHandler.END

    waiting = await update.message.reply_text("⏳ กำลังรับซองอั่งเปา กรุณารอสักครู่...")
    redeem_result = await asyncio.to_thread(_redeem_truemoney_voucher_sync, phone, voucher_hash)

    if not redeem_result.get("success"):
        status_code = str(redeem_result.get("status_code") or "")
        message = str(redeem_result.get("message") or "")
        await waiting.edit_text(
            "❌ รับซองอั่งเปาไม่สำเร็จ\n"
            f"เหตุผล: {_truemoney_error_message(status_code, message)}\n\n"
            "ระบบจะไม่เติมเครดิตหาก TrueMoney ไม่ยืนยันว่ารับเงินสำเร็จ"
        )
        _schedule_start_menu_after_finish(update, context, "addmycredit")
        context.user_data.clear()
        return ConversationHandler.END

    amount_baht = float(redeem_result["amount_baht"])
    rate = _get_truemoney_credit_rate()
    credit_amount = round(amount_baht * rate, 2)
    if credit_amount <= 0:
        await waiting.edit_text(
            "❌ รับซองอั่งเปาไม่สำเร็จ\n"
            "เหตุผล: จำนวนเครดิตที่คำนวณได้ไม่ถูกต้อง ระบบจึงไม่เติมเครดิต"
        )
        context.user_data.clear()
        return ConversationHandler.END

    try:
        record_result = db.add_truemoney_credit(
            user_id=user.id,
            username=user.username,
            voucher_hash=voucher_hash,
            phone=phone,
            amount_baht=amount_baht,
            credit_amount=credit_amount,
            status_code=str(redeem_result.get("status_code") or "SUCCESS"),
            redeemed_at=_thai_now_iso(),
            raw_response=_json_compact(redeem_result.get("raw_response") or {}),
        )
    except Exception:
        logger.exception("TrueMoney voucher redeemed but failed to record credit")
        await waiting.edit_text(
            "⚠️ TrueMoney ยืนยันว่ารับซองสำเร็จแล้ว แต่บอทบันทึกเครดิตไม่สำเร็จ\n"
            "กรุณาติดต่อแอดมินพร้อมเวลาที่เติมเงิน เพื่อให้ตรวจสอบจาก log ของบอท"
        )
        context.user_data.clear()
        return ConversationHandler.END

    if record_result.get("status") == "duplicate":
        await waiting.edit_text(
            "⚠️ ซองอั่งเปานี้ถูกบันทึกในระบบแล้ว\n"
            "ระบบจะไม่เติมเครดิตซ้ำ"
        )
        _schedule_start_menu_after_finish(update, context, "addmycredit")
        context.user_data.clear()
        return ConversationHandler.END

    balance = float(record_result.get("balance", db.get_credit(user.id)))
    if update.effective_chat.type == "private":
        success_text = (
            f"✅ คุณได้เติมเครดิต {_format_credit(credit_amount)} เครดิต\n"
            f"💸 ยอดซองอั่งเปา: {_format_credit(amount_baht)} บาท\n"
            f"💰 คุณมีเครดิตรวมทั้งหมด {_format_credit(balance)} เครดิต"
        )
    else:
        success_text = (
            f"✅ คุณได้เติมเครดิต {_format_credit(credit_amount)} เครดิต\n"
            f"💸 ยอดซองอั่งเปา: {_format_credit(amount_baht)} บาท"
        )
    await waiting.edit_text(success_text)

    _schedule_start_menu_after_finish(update, context, "addmycredit")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_addmycredit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return

    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ ยกเลิกการเติมเครดิตแล้ว")
    return ConversationHandler.END


async def cancel_addmycredit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ ยกเลิกการเติมเครดิตแล้ว")
    return ConversationHandler.END


# ── /Enterthecode ─────────────────────────────────────────────────────────────
async def enter_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username)
    if update.effective_chat.type == "private":
        db.set_dm_started(user.id)

    if not _credit_code_enabled():
        await update.message.reply_text("❌ ระบบกรอกโค้ดถูกปิดใช้งานชั่วคราว")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["session_user_id"] = user.id
    context.user_data["command_msg_id"] = update.message.message_id
    await _send_force_reply_question(
        update,
        context,
        "กรุณากรอก code",
        user_id=user.id,
        reply_to_message_id=update.message.message_id,
        flow_name="entercode",
    )
    return ENTER_CODE_VALUE


async def enter_code_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return ENTER_CODE_VALUE
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return ENTER_CODE_VALUE

    user = update.effective_user
    code_name = _normalize_credit_code_name(update.message.text)
    code_key = _credit_code_key(code_name)
    if not code_key:
        await _send_force_reply_question(
            update,
            context,
            "❌ กรุณากรอก code ให้ถูกต้อง",
            user_id=user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="entercode",
        )
        return ENTER_CODE_VALUE

    if not _credit_code_enabled():
        await update.message.reply_text("❌ ระบบกรอกโค้ดถูกปิดใช้งานชั่วคราว")
        context.user_data.clear()
        return ConversationHandler.END

    result = db.redeem_credit_code(code_key, user.id, user.username, _thai_now_iso())
    status = result.get("status")
    if status == "ok":
        if result.get("code_type") == "free_reset":
            await update.message.reply_text(
                f"✅ ใช้โค้ด {code_name} สำเร็จ\n"
                "รีเซ็ตจำนวนการใช้สิทธิ์ทดลองใช้ฟรีของคุณเป็น 0 แล้ว"
            )
        else:
            amount = result["credit_amount"]
            balance = result["balance"]
            await update.message.reply_text(
                f"✅ คุณได้รับเครดิต {_format_credit(amount)} เครดิตจากการกรอกโค้ด {code_name}\n"
                f"💰 เครดิตคงเหลือ: {_format_credit(balance)} เครดิต"
            )
    elif status == "not_found":
        await update.message.reply_text("❌ โค้ดนี้ไม่มีอยู่ในระบบ")
    elif status == "expired":
        await update.message.reply_text("⌛ โค้ดนี้หมดอายุแล้ว")
    elif status == "used_up":
        await update.message.reply_text("⚠️ โค้ดนี้มีการใช้งานครบตามจำนวนแล้ว")
    elif status == "already_used":
        await update.message.reply_text("ℹ️ คุณเคยกรอกโค้ดนี้แล้ว")
    else:
        await update.message.reply_text("❌ ไม่สามารถกรอกโค้ดได้ กรุณาลองใหม่อีกครั้ง")

    _schedule_start_menu_after_finish(update, context, "Enterthecode")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_entercode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return

    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ ยกเลิกการกรอกโค้ดแล้ว")
    return ConversationHandler.END


# ── /mycodes ─────────────────────────────────────────────────────────────────
async def mycodes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username)
    sort_order = _get_mycodes_sort_order()
    display_limit = _get_mycodes_display_limit()
    codes = db.get_user_codes(user.id, sort_order=sort_order, display_limit=display_limit)

    if not codes:
        await update.message.reply_text("📭 คุณยังไม่มีโค้ดใดๆ")
        _schedule_start_menu_after_finish(update, context, "mycodes")
        return

    sort_label = "ล่าสุดอยู่ล่าง" if sort_order == "newest_bottom" else "ล่าสุดอยู่บน"
    chunks = []
    chunk = f"📄 *โค้ดของคุณ* (แสดง {len(codes)} รายการ | {sort_label}):\n\n"
    for code in codes:
        gb_text = f"{code['gb_limit']} GB" if code["gb_limit"] > 0 else "ไม่จำกัด"
        entry = (
            f"📌 *{code['name']}*\n"
            f"🌐 เครือข่าย: {code['network']}\n"
            f"📅 หมดอายุ: {code['expire_date']}\n"
            f"💾 จำกัด: {gb_text}\n"
            f"🔗 `{code['link']}`\n\n"
        )
        if len(chunk) + len(entry) > 3800:
            chunks.append(chunk)
            chunk = entry
        else:
            chunk += entry
    chunks.append(chunk)

    in_group = update.effective_chat.type != "private"

    if in_group:
        try:
            for part in chunks:
                await context.bot.send_message(
                    chat_id=user.id, text=part, parse_mode="Markdown"
                )
            await update.message.reply_text(
                "✅ โค้ดของคุณถูกส่งไปยังแชทส่วนตัวแล้ว! โปรดตรวจสอบแชทส่วนตัวของคุณ 📬"
            )
        except Exception:
            _bot_username = (await context.bot.get_me()).username
            await update.message.reply_text(
                f"❌ ไม่สามารถส่งข้อความส่วนตัวได้ กรุณาเริ่มแชทกับบอทก่อนที่ @{_bot_username}"
            )
    else:
        for part in chunks:
            await update.message.reply_text(part, parse_mode="Markdown")
    _schedule_start_menu_after_finish(update, context, "mycodes")


# ── /addclient: network → package → filename → days → GB → DM ───────────────
def _network_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"📶 {network['name']}", callback_data=f"network_{network['id']}_{user_id}"
    )] for network in db.get_networks(active_only=True)]
    if _cancel_button_enabled():
        rows.append([InlineKeyboardButton("❌ ยกเลิก", callback_data=f"cancel_addclient_{user_id}")])
    return InlineKeyboardMarkup(rows)


def _package_keyboard(network_id: int, user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"📦 {package['name']} • {package['price_per_day']:g}/วัน",
        callback_data=f"package_{package['id']}_{user_id}"
    )] for package in db.get_packages(network_id, active_only=True)]
    rows.append([InlineKeyboardButton("⬅️ เลือกเครือข่ายใหม่", callback_data=f"backnet_{user_id}")])
    if _cancel_button_enabled():
        rows.append([InlineKeyboardButton("❌ ยกเลิก", callback_data=f"cancel_addclient_{user_id}")])
    return InlineKeyboardMarkup(rows)


async def addclient_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username)
    if update.effective_chat.type == "private": db.set_dm_started(user.id)
    if not _addclient_enabled():
        await update.message.reply_text("❌ ระบบขาย VPN ปิดใช้งานชั่วคราว"); return ConversationHandler.END
    allowed, deny_message = _addclient_allowed_in_chat(update.effective_chat, user.id)
    if not allowed:
        await update.message.reply_text(deny_message); return ConversationHandler.END
    if not await _require_dm_for_group(update, context):
        return ConversationHandler.END
    if not db.get_networks(active_only=True):
        await update.message.reply_text("❌ ยังไม่มีเครือข่ายที่เปิดใช้งาน"); return ConversationHandler.END
    context.user_data.clear()
    context.user_data.update(session_user_id=user.id, command_msg_id=update.message.message_id)
    sent = await update.message.reply_text("🌐 กรุณาเลือกเครือข่าย:", reply_markup=_network_keyboard(user.id))
    context.user_data["waiting_msg_id"] = sent.message_id
    return CHOOSE_NETWORK


async def choose_network(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, network_id, owner_id = query.data.split("_", 2); network_id, owner_id = int(network_id), int(owner_id)
    except (ValueError, IndexError):
        await query.answer("ข้อมูลเครือข่ายไม่ถูกต้อง", show_alert=True); return CHOOSE_NETWORK
    if query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True); return CHOOSE_NETWORK
    network = db.get_network(network_id, active_only=True)
    if not network:
        await query.answer("เครือข่ายนี้ถูกปิดหรือลบแล้ว", show_alert=True); return CHOOSE_NETWORK
    packages = db.get_packages(network_id, active_only=True)
    context.user_data["network_record"] = network
    await query.answer()
    if not packages:
        await query.edit_message_text(
            f"ℹ️ เครือข่าย {network['name']} ยังไม่มีรายการแพ็กเกจ",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ เลือกเครือข่ายใหม่", callback_data=f"backnet_{owner_id}")]]),
        )
        return SELECT_PACKAGE
    await query.edit_message_text(
        f"✅ เครือข่าย: {network['name']}\n📦 กรุณาเลือกรายการแพ็กเกจ:",
        reply_markup=_package_keyboard(network_id, owner_id),
    )
    return SELECT_PACKAGE


async def back_to_networks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: owner_id = int(query.data.rsplit("_", 1)[-1])
    except ValueError:
        await query.answer("ข้อมูลไม่ถูกต้อง", show_alert=True); return SELECT_PACKAGE
    if query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True); return SELECT_PACKAGE
    context.user_data.pop("network_record", None); context.user_data.pop("package_record", None)
    await query.answer()
    await query.edit_message_text("🌐 กรุณาเลือกเครือข่าย:", reply_markup=_network_keyboard(owner_id))
    return CHOOSE_NETWORK


async def choose_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, package_id, owner_id = query.data.split("_", 2); package_id, owner_id = int(package_id), int(owner_id)
    except (ValueError, IndexError):
        await query.answer("ข้อมูลแพ็กเกจไม่ถูกต้อง", show_alert=True); return SELECT_PACKAGE
    if query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True); return SELECT_PACKAGE
    network = context.user_data.get("network_record")
    package = db.get_package(package_id, active_only=True)
    if not network or not package or int(package["network_id"]) != int(network["id"]):
        await query.answer("แพ็กเกจนี้ถูกปิด ลบ หรือไม่ตรงกับเครือข่าย", show_alert=True); return SELECT_PACKAGE
    context.user_data["package_record"] = package
    await query.answer()
    await query.edit_message_text(f"✅ {network['name']} • {package['name']} • {package['price_per_day']:g} เครดิต/วัน")
    await _send_force_reply_question(
        update, context, "📝 กรุณาตั้งชื่อไฟล์:", user_id=owner_id,
        reply_to_message_id=context.user_data.get("command_msg_id"),
    )
    return ENTER_NAME


async def enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context): return ENTER_NAME
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False); return ENTER_NAME
    name = update.message.text.strip()
    if not re.match(r"^[\w\-\.]{1,50}$", name):
        await _send_force_reply_question(update, context, "❌ ชื่อไม่ถูกต้อง กรุณาลองใหม่:", user_id=update.effective_user.id, reply_to_message_id=update.message.message_id); return ENTER_NAME
    if db.code_name_exists(update.effective_user.id, name):
        await _send_force_reply_question(update, context, "❌ คุณมีชื่อนี้แล้ว กรุณาใช้ชื่ออื่น:", user_id=update.effective_user.id, reply_to_message_id=update.message.message_id); return ENTER_NAME
    context.user_data["name"] = name
    await _send_force_reply_question(update, context, "📅 กรุณาระบุจำนวนวัน (1–365 วัน):", user_id=update.effective_user.id, reply_to_message_id=update.message.message_id)
    return ENTER_DAYS


async def enter_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context): return ENTER_DAYS
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False); return ENTER_DAYS
    try:
        days = int(update.message.text.strip())
        if not 1 <= days <= 365: raise ValueError
    except ValueError:
        await _send_force_reply_question(update, context, "❌ กรุณาระบุจำนวนวันเป็นเลข 1–365:", user_id=update.effective_user.id, reply_to_message_id=update.message.message_id); return ENTER_DAYS
    package = context.user_data.get("package_record")
    if not package:
        await update.message.reply_text("❌ เซสชันแพ็กเกจหมดอายุ กรุณาเริ่ม /addclient ใหม่"); return ConversationHandler.END
    cost = round(days * float(package["price_per_day"]), 2)
    context.user_data.update(days=days, cost=cost)
    await _send_force_reply_question(update, context, f"📅 {days} วัน • ราคา {cost:.2f} เครดิต\n💾 กรุณาระบุ GB (0 = ไม่จำกัด):", user_id=update.effective_user.id, reply_to_message_id=update.message.message_id)
    return ENTER_GB


async def enter_gb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context): return ENTER_GB
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False); return ENTER_GB
    try:
        gb = float(update.message.text.strip())
        if gb < 0 or gb > 1_000_000: raise ValueError
    except ValueError:
        await _send_force_reply_question(update, context, "❌ กรุณาระบุ GB เป็นเลข 0 ขึ้นไป:", user_id=update.effective_user.id, reply_to_message_id=update.message.message_id); return ENTER_GB
    user=update.effective_user; network=context.user_data.get("network_record"); package=context.user_data.get("package_record")
    name=context.user_data.get("name"); days=context.user_data.get("days")
    if not network or not package or not name or not days:
        await update.message.reply_text("❌ เซสชันหมดอายุ กรุณาเริ่ม /addclient ใหม่"); context.user_data.clear(); return ConversationHandler.END
    network=db.get_network(int(network["id"]),True); package=db.get_package(int(package["id"]),True)
    if not network or not package or int(package["network_id"]) != int(network["id"]):
        await update.message.reply_text("❌ เครือข่ายหรือแพ็กเกจถูกปิด/ลบแล้ว ยังไม่มีการหักเครดิต"); context.user_data.clear(); return ConversationHandler.END
    # คำนวณราคาใหม่จากค่าล่าสุดบนเว็บก่อนหักเงินจริง
    cost=round(int(days)*float(package["price_per_day"]),2)
    if not xui.is_available():
        await update.message.reply_text("❌ ระบบ VPN Panel ไม่พร้อม ยังไม่มีการหักเครดิต"); context.user_data.clear(); return ConversationHandler.END
    if not db.try_deduct_credit(user.id,cost):
        await update.message.reply_text(f"❌ เครดิตไม่เพียงพอ\nมี {db.get_credit(user.id):.2f} / ต้องใช้ {cost:.2f} เครดิต"); context.user_data.clear(); return ConversationHandler.END
    processing=await update.message.reply_text("⏳ กำลังสร้างไฟล์และส่งทาง DM...")
    inbound_id=int(package["inbound_id"]); inbound_info=xui.get_inbound(inbound_id)
    remark=inbound_info.get("remark","").strip() if inbound_info else ""; full_name=f"{remark}-{name}" if remark else name
    result=xui.add_client(inbound_id,full_name,int(days),gb)
    if not result:
        _refund_credit_after_failure(user.id,cost,"3x-ui add_client failed"); await processing.edit_text("❌ สร้าง Client ไม่สำเร็จ ระบบคืนเครดิตแล้ว"); context.user_data.clear(); return ConversationHandler.END
    link=xui.generate_link(inbound_id,result["uuid"],full_name,flow=result.get("flow",""))
    if not link:
        xui.delete_client(full_name); _refund_credit_after_failure(user.id,cost,"3x-ui link generation failed"); await processing.edit_text("❌ สร้างลิงก์ไม่สำเร็จ ระบบคืนเครดิตแล้ว"); context.user_data.clear(); return ConversationHandler.END
    expire_date=(_thai_now()+datetime.timedelta(days=int(days))).strftime("%Y-%m-%d"); network_label=str(network["name"])
    db.save_code(user.id,full_name,result["uuid"],inbound_id,network_label,expire_date,gb,link)
    db.add_buy_log(user.id,user.username,full_name,network_label,int(days),gb,cost,link,_thai_now_str())
    db.add_audit_log(f"telegram:{user.id}","order.create",full_name,"",f"network={network['code']};package={package['name']};cost={cost}")
    gb_text=f"{gb:g} GB" if gb else "ไม่จำกัด"
    text=(f"✅ สร้างไฟล์สำเร็จ\n\n🌐 {network_label}\n📦 {package['name']}\n📌 {full_name}\n📅 {expire_date} ({days} วัน)\n💾 {gb_text}\n💰 หัก {cost:.2f} • คงเหลือ {db.get_credit(user.id):.2f}\n\n🔗 {link}")
    try:
        await context.bot.send_message(chat_id=user.id,text=text,parse_mode=None,disable_web_page_preview=True)
        db.set_dm_started(user.id, True)
        if update.effective_chat.type!="private": await processing.edit_text("✅ สร้างสำเร็จและส่งไฟล์ไปยัง DM แล้ว")
        else: await processing.delete()
    except Exception:
        db.set_dm_started(user.id, False)
        await processing.edit_text("⚠️ สร้างสำเร็จ แต่ส่ง DM ไม่ได้ กรุณาเปิดแชทกับบอทแล้วใช้ /mycodes")
    _schedule_start_menu_after_finish(update,context,"addclient"); context.user_data.clear(); return ConversationHandler.END


async def cancel_addclient_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; owner_id=_parse_owner_id(query.data)
    if owner_id is None or query.from_user.id!=owner_id:
        await _reject_foreign_interaction(update,is_callback=True); return
    await query.answer(); context.user_data.clear(); await query.edit_message_text("❌ ยกเลิกรายการแล้ว"); return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear(); await update.message.reply_text("❌ ยกเลิกรายการแล้ว"); return ConversationHandler.END


# ── /freeclient conversation ─────────────────────────────────────────────────
async def freeclient_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username)

    if update.effective_chat.type == "private":
        db.set_dm_started(user.id)

    if not _freeclient_enabled():
        await update.message.reply_text("❌ ตอนนี้ระบบทดลองใช้ฟรีถูกปิดใช้งาน")
        return ConversationHandler.END

    # ── ตรวจสอบช่องทางที่อนุญาต (/channelfreeclient) ─────────────────────
    ch_ok, ch_msg = _check_freeclient_channel(update.effective_chat)
    if not ch_ok:
        await update.message.reply_text(ch_msg)
        return ConversationHandler.END

    daily_limit = _get_freeclient_daily_limit()
    used_count, usage_label = _count_freeclient_usage(user.id)
    if used_count >= daily_limit:
        await update.message.reply_text(
            "⚠️ คุณใช้สิทธิ์ทดลองใช้ฟรีครบแล้ว\n"
            f"โหมดรีเซ็ต: {_freeclient_reset_mode_label()}\n"
            f"สิทธิ์ต่อรอบ: {daily_limit} ครั้ง/คน\n"
            f"ใช้ไปแล้ว ({usage_label}): {used_count} ครั้ง"
        )
        return ConversationHandler.END

    in_group = update.effective_chat.type != "private"
    if not await _require_dm_for_group(update, context):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["session_user_id"] = user.id
    context.user_data["command_msg_id"] = update.message.message_id

    keyboard = [
        [
            InlineKeyboardButton("📶 AIS", callback_data=f"free_network_ais_{user.id}"),
            InlineKeyboardButton("📶 TRUE", callback_data=f"free_network_true_{user.id}"),
        ]
    ]
    if _cancel_button_enabled():
        keyboard.append([InlineKeyboardButton("❌ /cancel", callback_data=f"cancel_freeclient_{user.id}")])

    sent = await update.message.reply_text(
        "🧪 *ทดลองใช้ฟรี*\n\n🌐 *กรุณาเลือกเครือข่าย:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    context.user_data["waiting_msg_id"] = sent.message_id
    return FREE_CHOOSE_NETWORK


async def free_choose_network(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return FREE_CHOOSE_NETWORK

    await query.answer()
    network = query.data.split("_")[2]
    network_record = db.get_network_by_code(network, active_only=True)
    packages = db.get_packages(int(network_record["id"]), active_only=True) if network_record else []
    if not packages:
        await query.edit_message_text(f"ℹ️ เครือข่าย {network.upper()} ยังไม่มีแพ็กเกจที่เปิดใช้งานและกำหนด Inbound ID")
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data["network"] = network
    context.user_data["free_package_id"] = int(packages[0]["id"])

    await query.edit_message_text(
        f"✅ เลือกเครือข่ายทดลอง: *{network.upper()}*",
        parse_mode="Markdown",
    )

    command_msg_id = context.user_data.get("command_msg_id")
    await _send_force_reply_question(
        update,
        context,
        text=(
            "📝 *กรุณาตั้งชื่อโค้ดทดลองของคุณ:*\n"
            "_(ใช้ตัวอักษร ตัวเลข ขีดกลาง หรือ ขีดล่าง)_"
        ),
        user_id=query.from_user.id,
        parse_mode="Markdown",
        reply_to_message_id=command_msg_id,
        flow_name="freeclient",
    )
    return FREE_ENTER_NAME


async def free_enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return FREE_ENTER_NAME

    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return FREE_ENTER_NAME

    name = update.message.text.strip()
    if not re.match(r"^[\w\-\.]{1,50}$", name):
        await _send_force_reply_question(
            update,
            context,
            "❌ ชื่อไม่ถูกต้อง กรุณาใช้ตัวอักษร/ตัวเลข/ขีด ไม่เกิน 50 ตัวอักษร:",
            user_id=update.effective_user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="freeclient",
        )
        return FREE_ENTER_NAME

    context.user_data["name"] = name
    await _send_force_reply_question(
        update,
        context,
        "💾 *กรุณาระบุ GB ที่ต้องการจำกัดสำหรับทดลอง:*\n_(หากไม่จำกัดพิมพ์ 0)_",
        user_id=update.effective_user.id,
        parse_mode="Markdown",
        reply_to_message_id=update.message.message_id,
        flow_name="freeclient",
    )
    return FREE_ENTER_GB


async def free_enter_gb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return FREE_ENTER_GB

    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return FREE_ENTER_GB

    try:
        gb = float(update.message.text.strip())
        if gb < 0:
            raise ValueError
    except ValueError:
        await _send_force_reply_question(
            update,
            context,
            "❌ กรุณาระบุตัวเลข GB (เช่น 10, 50.5 หรือ 0 สำหรับไม่จำกัด):",
            user_id=update.effective_user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="freeclient",
        )
        return FREE_ENTER_GB

    user = update.effective_user
    network = context.user_data["network"]
    name = context.user_data["name"]
    free_package = db.get_package(int(context.user_data.get("free_package_id", 0)), active_only=True)
    if not free_package:
        await update.message.reply_text("❌ แพ็กเกจทดลองถูกปิดหรือลบแล้ว กรุณาเริ่มใหม่")
        context.user_data.clear()
        return ConversationHandler.END
    inbound_id = int(free_package["inbound_id"])
    free_hours = _get_freeclient_hours()

    daily_limit = _get_freeclient_daily_limit()
    used_count, usage_label = _count_freeclient_usage(user.id)
    if used_count >= daily_limit:
        await update.message.reply_text(
            "⚠️ คุณใช้สิทธิ์ทดลองใช้ฟรีครบแล้ว\n"
            f"โหมดรีเซ็ต: {_freeclient_reset_mode_label()}\n"
            f"สิทธิ์ต่อรอบ: {daily_limit} ครั้ง/คน\n"
            f"ใช้ไปแล้ว ({usage_label}): {used_count} ครั้ง"
        )
        context.user_data.clear()
        return ConversationHandler.END

    processing_msg = await update.message.reply_text("⏳ กำลังสร้างโค้ดทดลอง กรุณารอสักครู่...")

    # ── ✅ FIX: ตรวจสอบ XUI พร้อมใช้งานก่อนทำงาน ──────────────────────────
    if not xui.is_available():
        await processing_msg.edit_text(
            "❌ ระบบ VPN Panel ไม่พร้อมใช้งานในขณะนี้\n"
            "กรุณาลองใหม่อีกครั้ง หรือติดต่อแอดมิน"
        )
        context.user_data.clear()
        return ConversationHandler.END

    inbound_info = xui.get_inbound(inbound_id)
    remark = inbound_info.get("remark", "").strip() if inbound_info else ""
    full_name = f"{remark}-FREE-{name}" if remark else f"FREE-{name}"

    result = xui.add_client(inbound_id, full_name, free_hours / 24, gb)
    if not result:
        await processing_msg.edit_text(
            "❌ เกิดข้อผิดพลาดในการเชื่อมต่อกรุณาลองใหม่หรือติดต่อแอดมิน"
        )
        context.user_data.clear()
        return ConversationHandler.END

    link = xui.generate_link(inbound_id, result["uuid"], full_name, flow=result.get("flow", ""))
    if not link:
        xui.delete_client(full_name)
        await processing_msg.edit_text(
            "❌ เกิดข้อผิดพลาดในการสร้างลิงก์ กรุณาลองใหม่"
        )
        context.user_data.clear()
        return ConversationHandler.END

    expire_at = _thai_now() + datetime.timedelta(hours=free_hours)
    expire_text = expire_at.strftime("%Y-%m-%d %H:%M")
    db.save_code(user.id, full_name, result["uuid"], inbound_id, f"{network.upper()}-FREE", expire_text, gb, link)

    # ── บันทึก log การทดลองฟรี (/logfree) ────────────────────────────────────
    db.add_free_log(
        user_id=user.id,
        username=user.username,
        code_name=full_name,
        network=network.upper(),
        hours=free_hours,
        gb=gb,
        link=link,
        created_at=_thai_now_str(),
    )

    await processing_msg.delete()

    gb_text = f"{gb} GB" if gb > 0 else "ไม่จำกัด"
    code_text = (
        f"✅ *สร้างโค้ดทดลองใช้ฟรีสำเร็จ!*\n\n"
        f"📌 ชื่อ: `{full_name}`\n"
        f"🌐 เครือข่าย: {network.upper()}\n"
        f"⏱ หมดอายุ: {expire_text} ({_format_hours(free_hours)} ชั่วโมง)\n"
        f"💾 จำกัด: {gb_text}\n\n"
        f"🔗 *ลิงก์:*\n`{link}`"
    )

    in_group = update.effective_chat.type != "private"
    if in_group:
        try:
            await context.bot.send_message(
                chat_id=user.id, text=code_text, parse_mode="Markdown"
            )
            db.set_dm_started(user.id, True)
            await update.message.reply_text(
                "✅ โค้ดทดลองใช้ฟรีถูกส่งไปยังแชทส่วนตัวแล้ว! โปรดตรวจสอบ DM 📬"
            )
        except Exception:
            db.set_dm_started(user.id, False)
            _bot_un_f = (await context.bot.get_me()).username
            await update.message.reply_text(
                "⚠️ สร้างโค้ดทดลองสำเร็จแล้ว แต่ไม่สามารถส่งไปยัง DM ได้ กรุณาเริ่มแชทกับบอทแล้วใช้ /mycodes เพื่อตรวจสอบโค้ดของคุณ",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "💬 กดเพื่อเริ่มแชทกับบอท",
                        url=f"https://t.me/{_bot_un_f}?start=welcome",
                    )
                ]]),
            )
    else:
        await update.message.reply_text(code_text, parse_mode="Markdown")

    _schedule_start_menu_after_finish(update, context, "freeclient")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_freeclient_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return

    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ ยกเลิกการสร้างโค้ดทดลองแล้ว")
    return ConversationHandler.END


# ── /checkprice ──────────────────────────────────────────────────────────────
async def checkprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = db.get_setting("price_per_day", 10.0)
    await update.message.reply_text(
        f"💵 ราคาต่อวันปัจจุบัน: *{price:.2f}* เครดิต/วัน",
        parse_mode="Markdown",
    )
    _schedule_start_menu_after_finish(update, context, "checkprice")


# ── Admin: /addcredits ───────────────────────────────────────────────────────
async def addcredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ รูปแบบ: `/addcredits @username จำนวน`\n"
            "หรือ: `/addcredits user_id จำนวน`",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ จำนวนเครดิตไม่ถูกต้อง (ต้องมากกว่า 0)")
        return

    target = args[0].lstrip("@")
    user_id = _resolve_user(target)
    if not user_id:
        await update.message.reply_text(f"❌ ไม่พบผู้ใช้ `{args[0]}`", parse_mode="Markdown")
        return

    # ✅ FIX: ensure_user ก่อนเพิ่มเครดิต ป้องกันกรณี user ยังไม่เคย /start บอท
    db.ensure_user(user_id, None)
    db.add_credit(user_id, amount)
    new_credit = db.get_credit(user_id)
    username = db.get_username(user_id) or str(user_id)
    display = f"@{username}" if not username.isdigit() else f"ID: {username}"
    await update.message.reply_text(
        f"✅ เพิ่มเครดิต {amount:.2f} เครดิต ให้ {display} สำเร็จ\n"
        f"💰 เครดิตปัจจุบัน: {new_credit:.2f} เครดิต",
    )
    _schedule_start_menu_after_finish(update, context, "addcredits")


# ── Admin: /Deletecredits ────────────────────────────────────────────────────
async def deletecredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ รูปแบบ: `/Deletecredits @username จำนวน`\n"
            "หรือ: `/Deletecredits user_id จำนวน`",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ จำนวนเครดิตไม่ถูกต้อง (ต้องมากกว่า 0)")
        return

    target = args[0].lstrip("@")
    user_id = _resolve_user(target)
    if not user_id:
        await update.message.reply_text(f"❌ ไม่พบผู้ใช้ `{args[0]}`", parse_mode="Markdown")
        return

    # ✅ FIX: ensure_user ก่อนลบเครดิต
    db.ensure_user(user_id, None)
    db.deduct_credit(user_id, amount)
    new_credit = db.get_credit(user_id)
    username = db.get_username(user_id) or str(user_id)
    display = f"@{username}" if not username.isdigit() else f"ID: {username}"
    await update.message.reply_text(
        f"✅ ลบเครดิต {amount:.2f} เครดิต จาก {display} สำเร็จ\n"
        f"💰 เครดิตปัจจุบัน: {new_credit:.2f} เครดิต",
    )
    _schedule_start_menu_after_finish(update, context, "deletecredits")


# ── Admin: /setprice ─────────────────────────────────────────────────────────
async def setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ รูปแบบ: `/setprice จำนวน`\nตัวอย่าง: `/setprice 15`",
            parse_mode="Markdown",
        )
        return

    try:
        price = float(args[0])
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ ราคาไม่ถูกต้อง")
        return

    db.set_setting("price_per_day", str(price))
    await update.message.reply_text(
        f"✅ ตั้งราคาต่อวันเป็น *{price:.2f}* เครดิต/วัน แล้ว",
        parse_mode="Markdown",
    )
    _schedule_start_menu_after_finish(update, context, "setprice")


# ── Admin: /Settingsmycredit — unified hub ───────────────────────────────────

def _smc_hub_text() -> str:
    enabled     = _truemoney_enabled()
    phone       = _get_truemoney_wallet_phone()
    rate        = _get_truemoney_credit_rate()
    mode        = _get_truemoney_channel_mode()
    mode_label  = _SMC_CHANNEL_LABELS.get(mode, mode)
    group_ids   = _get_truemoney_group_ids()
    ids_text    = ", ".join(str(i) for i in sorted(group_ids)) if group_ids else "ยังไม่ได้กำหนด"
    phone_text  = phone if phone else "ยังไม่ได้ตั้งค่า"
    status_icon = "🟢 เปิด" if enabled else "🔴 ปิด"
    needs_ids   = mode in ("specified_only", "specified_and_dm")
    ids_line    = f"\n🆔 Group IDs: {ids_text}" if needs_ids else ""
    return (
        "💳 *ตั้งค่าระบบเติมเครดิต (ซองอั่งเปา TrueMoney)*\n\n"
        f"📌 สถานะ: *{status_icon}*\n"
        f"📱 เบอร์รับซอง: *{phone_text}*\n"
        f"💱 อัตรา: *1 บาท = {_format_credit(rate)} เครดิต*\n"
        f"📢 ช่องทาง: *{mode_label}*{ids_line}\n\n"
        "ℹ️ *คำอธิบายตัวเลือก*\n"
        "• *เปิด/ปิด* — เปิดหรือปิดระบบเติมเครดิตทั้งหมด\n"
        "• *ตั้งเบอร์รับซอง* — เบอร์ TrueMoney ที่บอทจะกรอกเพื่อรับซอง\n"
        "• *ตั้งเครดิตต่อบาท* — กำหนดอัตราแลก เช่น 1 บาท = 1 เครดิต\n"
        "• *ช่องทางเติมเครดิต* — กำหนดว่าผู้ใช้ใช้ /addmycredit ได้ที่ไหนบ้าง\n"
        "• *เช็คเบอร์รับซอง* — ดูเบอร์และอัตราที่ตั้งค่าอยู่\n\n"
        "เลือกสิ่งที่ต้องการตั้งค่า:"
    )


def _smc_hub_keyboard() -> InlineKeyboardMarkup:
    enabled = _truemoney_enabled()
    toggle_label = "🔴 ปิดระบบเติมเครดิต" if enabled else "🟢 เปิดระบบเติมเครดิต"
    toggle_data  = "smc_disable" if enabled else "smc_enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label,              callback_data=toggle_data)],
        [InlineKeyboardButton("📱 ตั้งเบอร์รับซอง",    callback_data="smc_set_phone")],
        [InlineKeyboardButton("💱 ตั้งเครดิตต่อบาท",   callback_data="smc_set_rate")],
        [InlineKeyboardButton("📢 ช่องทางเติมเครดิต",   callback_data="smc_channel")],
        [InlineKeyboardButton("🔍 เช็คเบอร์รับซอง",     callback_data="smc_check")],
        [InlineKeyboardButton("❌ ปิดเมนู",              callback_data="smc_close")],
    ])


def _smc_channel_keyboard() -> InlineKeyboardMarkup:
    current = _get_truemoney_channel_mode()
    def btn(mode: str) -> InlineKeyboardButton:
        mark = "✅ " if mode == current else "   "
        return InlineKeyboardButton(mark + _SMC_CHANNEL_LABELS[mode], callback_data=f"smc_ch_{mode}")
    return InlineKeyboardMarkup([
        [btn("dm_only")],
        [btn("dm_and_group")],
        [btn("group_only")],
        [btn("specified_only")],
        [btn("specified_and_dm")],
        [InlineKeyboardButton("🔙 กลับ",    callback_data="smc_back")],
        [InlineKeyboardButton("❌ ยกเลิก",  callback_data="smc_close")],
    ])


async def settingsmycredit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["session_user_id"] = update.effective_user.id
    await update.message.reply_text(
        _smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard()
    )
    return SMC_HUB


async def smc_hub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ คุณไม่มีสิทธิ์", show_alert=True)
        return SMC_HUB
    await query.answer()
    data = query.data

    if data in ("smc_enable", "smc_disable"):
        db.set_setting(TRUEMONEY_ENABLED_KEY, "1" if data == "smc_enable" else "0")
        await query.edit_message_text(
            _smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard()
        )
        return SMC_HUB

    if data == "smc_check":
        phone = _get_truemoney_wallet_phone()
        rate  = _get_truemoney_credit_rate()
        await query.edit_message_text(
            f"🔍 *ตรวจสอบการตั้งค่าซองอั่งเปา*\n\n"
            f"📱 เบอร์รับซอง: *{phone if phone else 'ยังไม่ได้ตั้งค่า'}*\n"
            f"💱 อัตรา: *1 บาท = {_format_credit(rate)} เครดิต*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 กลับ", callback_data="smc_back")]]),
        )
        return SMC_HUB

    if data == "smc_channel":
        await query.edit_message_text(
            "📢 *กำหนดช่องทางที่ใช้งาน /addmycredit ได้*\n\nกรุณาเลือกช่องทาง:",
            parse_mode="Markdown",
            reply_markup=_smc_channel_keyboard(),
        )
        return SMC_HUB

    if data.startswith("smc_ch_"):
        mode = data[7:]
        db.set_setting(TRUEMONEY_CHANNEL_MODE_KEY, mode)
        if mode in ("specified_only", "specified_and_dm"):
            context.user_data["smc_pending_ids"] = True
            await query.edit_message_text(
                f"✅ เลือกช่องทาง: *{_SMC_CHANNEL_LABELS[mode]}*\n\n"
                f"📝 กรุณาตอบกลับข้อความด้านล่างด้วย Group ID ที่อนุญาต\n"
                f"_(คั่นด้วย `,` ถ้ามีหลายกลุ่ม เช่น `-1001234567890, -1009876543210`)_\n\n"
                f"หรือพิมพ์ `clear` เพื่อล้าง ID ทั้งหมด",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 กลับ",   callback_data="smc_back"),
                    InlineKeyboardButton("❌ ยกเลิก", callback_data="smc_close"),
                ]]),
            )
            fr_msg = await context.bot.send_message(
                chat_id=query.message.chat_id, text="📝 พิมพ์ Group ID:",
                reply_markup=ForceReply(selective=True, input_field_placeholder="-100xxxxxxxxxx"),
            )
            context.user_data["waiting_msg_id"] = fr_msg.message_id
            return SMC_ENTER_CHANNEL_IDS
        await query.edit_message_text(
            _smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard()
        )
        return SMC_HUB

    if data == "smc_set_phone":
        phone = _get_truemoney_wallet_phone()
        await query.edit_message_text(
            f"📱 *ตั้งค่าเบอร์รับซองอั่งเปา*\n\n"
            f"{'ปัจจุบัน: *' + phone + '*' if phone else 'ยังไม่ได้ตั้งค่า'}\n\n"
            f"📝 กรุณาตอบกลับข้อความด้านล่างด้วยเบอร์โทรศัพท์ 10 หลัก:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 กลับ",   callback_data="smc_back"),
                InlineKeyboardButton("❌ ยกเลิก", callback_data="smc_close"),
            ]]),
        )
        fr_msg = await context.bot.send_message(
            chat_id=query.message.chat_id, text="📝 พิมพ์เบอร์รับซอง:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="0812345678"),
        )
        context.user_data["waiting_msg_id"] = fr_msg.message_id
        return SMC_ENTER_PHONE

    if data == "smc_set_rate":
        rate = _get_truemoney_credit_rate()
        await query.edit_message_text(
            f"💱 *ตั้งค่าเครดิตต่อบาท*\n\n"
            f"ปัจจุบัน: *1 บาท = {_format_credit(rate)} เครดิต*\n\n"
            f"📝 กรุณาตอบกลับข้อความด้านล่างด้วยตัวเลข _(เช่น 1, 1.5, 2)_:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 กลับ",   callback_data="smc_back"),
                InlineKeyboardButton("❌ ยกเลิก", callback_data="smc_close"),
            ]]),
        )
        fr_msg = await context.bot.send_message(
            chat_id=query.message.chat_id, text="📝 พิมพ์จำนวนเครดิตต่อ 1 บาท:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="เช่น 1, 1.5"),
        )
        context.user_data["waiting_msg_id"] = fr_msg.message_id
        return SMC_ENTER_RATE

    if data == "smc_close":
        await query.edit_message_text("✅ ปิดเมนูตั้งค่าระบบเติมเครดิตแล้ว")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "smc_back":
        await query.edit_message_text(
            _smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard()
        )
        return SMC_HUB

    return SMC_HUB


async def smc_enter_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not is_reply_to_bot_question(update, context):
        return SMC_ENTER_PHONE
    phone = _normalize_truemoney_phone("".join((update.message.text or "").split()))
    if not _is_valid_truemoney_phone(phone):
        fr_msg = await update.message.reply_text(
            "❌ เบอร์ไม่ถูกต้อง ต้องเป็นตัวเลข 10 หลักและขึ้นต้นด้วย 0 กรุณาพิมพ์ใหม่:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="0812345678"),
        )
        context.user_data["waiting_msg_id"] = fr_msg.message_id
        return SMC_ENTER_PHONE
    db.set_setting(TRUEMONEY_WALLET_PHONE_KEY, phone)
    await update.message.reply_text(f"✅ ตั้งค่าเบอร์รับซองอั่งเปาเป็น *{phone}* แล้ว", parse_mode="Markdown")
    await update.message.reply_text(_smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard())
    context.user_data.pop("waiting_msg_id", None)
    return SMC_HUB


async def smc_enter_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not is_reply_to_bot_question(update, context):
        return SMC_ENTER_RATE
    rate = _parse_positive_amount((update.message.text or "").strip())
    if rate is None:
        fr_msg = await update.message.reply_text(
            "❌ กรุณาระบุตัวเลขที่มากกว่า 0 (เช่น 1, 1.5, 2):",
            reply_markup=ForceReply(selective=True, input_field_placeholder="เช่น 1, 1.5"),
        )
        context.user_data["waiting_msg_id"] = fr_msg.message_id
        return SMC_ENTER_RATE
    db.set_setting(TRUEMONEY_CREDIT_RATE_KEY, str(rate))
    await update.message.reply_text(f"✅ ตั้งค่าอัตราเป็น *1 บาท = {_format_credit(rate)} เครดิต* แล้ว", parse_mode="Markdown")
    await update.message.reply_text(_smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard())
    context.user_data.pop("waiting_msg_id", None)
    return SMC_HUB


async def smc_enter_channel_ids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not is_reply_to_bot_question(update, context):
        return SMC_ENTER_CHANNEL_IDS
    if not context.user_data.get("smc_pending_ids"):
        return SMC_ENTER_CHANNEL_IDS
    raw = (update.message.text or "").strip()
    if raw.lower() == "clear":
        db.set_setting(TRUEMONEY_GROUP_IDS_KEY, "")
        await update.message.reply_text("✅ ล้าง Group IDs ทั้งหมดแล้ว")
        await update.message.reply_text(_smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard())
        context.user_data.pop("smc_pending_ids", None)
        context.user_data.pop("waiting_msg_id", None)
        return SMC_HUB
    parts = raw.replace(",", " ").split()
    valid_ids, invalid = [], []
    for p in parts:
        p = p.strip()
        if p.lstrip("-").isdigit():
            valid_ids.append(int(p))
        elif p:
            invalid.append(p)
    if invalid:
        fr_msg = await update.message.reply_text(
            f"❌ ID ไม่ถูกต้อง: {', '.join(invalid)}\nกรุณาพิมพ์ใหม่:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="-100xxxxxxxxxx"),
        )
        context.user_data["waiting_msg_id"] = fr_msg.message_id
        return SMC_ENTER_CHANNEL_IDS
    if not valid_ids:
        fr_msg = await update.message.reply_text(
            "❌ ไม่พบ Group ID กรุณาพิมพ์ใหม่:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="-100xxxxxxxxxx"),
        )
        context.user_data["waiting_msg_id"] = fr_msg.message_id
        return SMC_ENTER_CHANNEL_IDS
    db.set_setting(TRUEMONEY_GROUP_IDS_KEY, ",".join(str(i) for i in valid_ids))
    await update.message.reply_text(f"✅ บันทึก Group IDs แล้ว: `{', '.join(str(i) for i in valid_ids)}`", parse_mode="Markdown")
    await update.message.reply_text(_smc_hub_text(), parse_mode="Markdown", reply_markup=_smc_hub_keyboard())
    context.user_data.pop("smc_pending_ids", None)
    context.user_data.pop("waiting_msg_id", None)
    return SMC_HUB


async def smc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ ยกเลิกการตั้งค่าระบบเติมเครดิตแล้ว")
    return ConversationHandler.END



# ── Admin: /setangpaophone ───────────────────────────────────────────────────
async def setangpaophone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ตั้งค่าเบอร์รับซองอั่งเปา TrueMoney Wallet — shortcut สำหรับแอดมิน"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    args = context.args
    if not args:
        phone = _get_truemoney_wallet_phone()
        await update.message.reply_text(
            f"📱 เบอร์รับซองอั่งเปาปัจจุบัน: *{phone if phone else 'ยังไม่ได้ตั้งค่า'}*\n\n"
            f"รูปแบบ: `/setangpaophone 0812345678`",
            parse_mode="Markdown",
        )
        return

    phone = _normalize_truemoney_phone(args[0])
    if not _is_valid_truemoney_phone(phone):
        await update.message.reply_text(
            "❌ เบอร์ไม่ถูกต้อง ต้องเป็นตัวเลข 10 หลักและขึ้นต้นด้วย 0\n"
            "ตัวอย่าง: `/setangpaophone 0812345678`",
            parse_mode="Markdown",
        )
        return

    db.set_setting(TRUEMONEY_WALLET_PHONE_KEY, phone)
    await update.message.reply_text(
        f"✅ ตั้งค่าเบอร์รับซองอั่งเปาเป็น *{phone}* แล้ว\n"
        f"💱 อัตราปัจจุบัน: 1 บาท = {_format_credit(_get_truemoney_credit_rate())} เครดิต",
        parse_mode="Markdown",
    )


async def checkangpaophone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ตรวจสอบเบอร์รับซองและอัตราเครดิต — shortcut สำหรับแอดมิน"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    phone = _get_truemoney_wallet_phone()
    await update.message.reply_text(
        "🔍 *ตรวจสอบการตั้งค่าซองอั่งเปา*\n\n"
        f"📱 เบอร์รับซอง: *{phone if phone else 'ยังไม่ได้ตั้งค่า'}*\n"
        f"💱 อัตรา: *1 บาท = {_format_credit(_get_truemoney_credit_rate())} เครดิต*\n"
        f"📌 สถานะ: *{'เปิด' if _truemoney_enabled() else 'ปิด'}*",
        parse_mode="Markdown",
    )


async def setangpaorate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ตั้งอัตราเครดิตต่อ 1 บาท — shortcut สำหรับแอดมิน"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not context.args:
        await update.message.reply_text(
            f"💱 อัตราปัจจุบัน: *1 บาท = {_format_credit(_get_truemoney_credit_rate())} เครดิต*\n\n"
            "รูปแบบ: `/setangpaorate 1`",
            parse_mode="Markdown",
        )
        return
    rate = _parse_positive_amount(context.args[0])
    if rate is None:
        await update.message.reply_text(
            "❌ กรุณาระบุตัวเลขที่มากกว่า 0\n"
            "ตัวอย่าง: `/setangpaorate 1` หรือ `/setangpaorate 1.5`",
            parse_mode="Markdown",
        )
        return
    db.set_setting(TRUEMONEY_CREDIT_RATE_KEY, str(rate))
    await update.message.reply_text(
        f"✅ ตั้งค่าอัตราเป็น *1 บาท = {_format_credit(rate)} เครดิต* แล้ว",
        parse_mode="Markdown",
    )


async def setinbound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if len(context.args) != 2:
        await update.message.reply_text("รูปแบบ: /setinbound PackageID InboundID\nตัวอย่าง: /setinbound 3 17")
        return
    try:
        package_id, inbound_id = int(context.args[0]), int(context.args[1])
        if package_id < 1 or inbound_id < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Package ID และ Inbound ID ต้องเป็นเลขตั้งแต่ 1 ขึ้นไป")
        return
    package = db.get_package(package_id, active_only=False)
    if not package or not db.update_package_inbound(package_id, inbound_id):
        await update.message.reply_text("❌ ไม่พบแพ็กเกจ")
        return
    db.add_audit_log(f"telegram:{update.effective_user.id}", "package.inbound.update", str(package_id), str(package["inbound_id"]), str(inbound_id))
    await update.message.reply_text(f"✅ ตั้ง Inbound ID ของ {package['name']} เป็น {inbound_id} แล้ว\nเว็บไซต์และบอทใช้ค่าใหม่ทันที")


# ── Admin: /startadmin ───────────────────────────────────────────────────────
def _admin_panel_text() -> str:
    networks = db.get_networks(active_only=True)
    active_packages = sum(int(n.get("active_package_count", 0)) for n in networks)
    public_url = os.getenv("PUBLIC_URL", os.getenv("WEB_ADMIN_URL", "")).strip()
    web_line = f"\n🌐 เว็บจัดการ: {public_url}" if public_url else "\n🌐 เว็บจัดการ: ตั้ง PUBLIC_URL เพื่อแสดงลิงก์"
    return (
        "🛠 ศูนย์ควบคุมบอท\n\n"
        f"🛍 ระบบขาย: {'🟢 เปิด' if _addclient_enabled() else '🔴 ปิด'}\n"
        f"🧪 ทดลองใช้ฟรี: {'🟢 เปิด' if _freeclient_enabled() else '🔴 ปิด'}\n"
        f"🎁 โค้ดเครดิต: {'🟢 เปิด' if _credit_code_enabled() else '🔴 ปิด'}\n"
        f"💸 TrueMoney: {'🟢 เปิด' if _truemoney_enabled() else '🔴 ปิด'}\n"
        f"💬 ซื้อใน DM: {'🟢 เปิด' if _buy_dm_enabled() else '🔴 ปิด'}\n"
        f"❌ ปุ่มยกเลิก: {'🟢 เปิด' if _cancel_button_enabled() else '🔴 ปิด'}\n"
        f"↩️ เมนูหลังจบ: {'🟢 เปิด' if _run_start_finish_enabled() else '🔴 ปิด'}\n"
        f"🌐 เครือข่ายเปิดใช้: {len(networks)} รายการ\n"
        f"📦 แพ็กเกจเปิดใช้: {active_packages} รายการ\n"
        f"🧩 Build: {BUILD_VERSION}" + web_line
    )


def _admin_panel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    def toggle(label: str, key: str, enabled: bool):
        return InlineKeyboardButton(f"{'🟢' if enabled else '⚪'} {label}", callback_data=f"admin|toggle:{key}|{user_id}")
    rows = [
        [toggle("ขาย VPN", ADDCLIENT_ENABLED_KEY, _addclient_enabled()), toggle("ทดลองใช้ฟรี", FREECLIENT_ENABLED_KEY, _freeclient_enabled())],
        [toggle("โค้ดเครดิต", CREDIT_CODE_ENABLED_KEY, _credit_code_enabled()), toggle("TrueMoney", TRUEMONEY_ENABLED_KEY, _truemoney_enabled())],
        [toggle("ซื้อใน DM", BUY_DM_ENABLED_KEY, _buy_dm_enabled()), toggle("ปุ่มยกเลิก", CANCEL_BUTTON_ENABLED_KEY, _cancel_button_enabled())],
        [toggle("เมนูหลังจบ", RUN_START_FINISH_ENABLED_KEY, _run_start_finish_enabled())],
        [InlineKeyboardButton("🌐 เครือข่ายและแพ็กเกจ", callback_data=f"admin|networks|{user_id}"), InlineKeyboardButton("🔄 รีเฟรช", callback_data=f"admin|refresh|{user_id}")],
    ]
    url = os.getenv("PUBLIC_URL", os.getenv("WEB_ADMIN_URL", "")).strip()
    if url.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton("🌐 เปิดเว็บไซต์จัดการ", url=url)])
    return InlineKeyboardMarkup(rows)


async def startadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้เมนูนี้")
        return
    # ไม่ใช้ Markdown เพราะ PUBLIC_URL/โดเมนอาจมีอักขระพิเศษ เช่น _ ( ) -
    await update.message.reply_text(
        _admin_panel_text(),
        parse_mode=None,
        reply_markup=_admin_panel_keyboard(update.effective_user.id),
        disable_web_page_preview=True,
    )


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, action, owner = query.data.split("|", 2)
        owner = int(owner)
    except (ValueError, IndexError):
        await query.answer("ข้อมูลไม่ถูกต้อง", show_alert=True); return
    if not is_admin(query.from_user.id) or query.from_user.id != owner:
        await query.answer("❌ คุณไม่มีสิทธิ์ใช้ปุ่มนี้", show_alert=True); return
    if action.startswith("toggle:"):
        key = action.split(":", 1)[1]
        allowed = {ADDCLIENT_ENABLED_KEY, FREECLIENT_ENABLED_KEY, CREDIT_CODE_ENABLED_KEY,
                   TRUEMONEY_ENABLED_KEY, BUY_DM_ENABLED_KEY, CANCEL_BUTTON_ENABLED_KEY,
                   RUN_START_FINISH_ENABLED_KEY}
        if key not in allowed:
            await query.answer("ไม่อนุญาตให้แก้ค่านี้", show_alert=True); return
        old = db.get_setting_text(key, "0")
        new_value = "0" if _setting_enabled(key, "0") else "1"
        db.set_setting(key, new_value)
        db.add_audit_log(f"telegram:{query.from_user.id}", "setting.toggle", key, old, new_value)
        await query.answer("บันทึกแล้ว เว็บไซต์จะเห็นค่าใหม่ทันที")
    elif action == "networks":
        networks = db.get_networks(active_only=True)
        lines = []
        for n in networks:
            lines.append(f"{n['sort_order']}. {n['name']} (Network ID {n['id']})")
            packages = db.get_packages(int(n['id']), active_only=False)
            lines.extend(f"   • {p['name']} (Package ID {p['id']}) — Inbound {p['inbound_id']}" for p in packages)
            if not packages:
                lines.append("   • ยังไม่มีแพ็กเกจ")
        text = "🌐 เครือข่ายและแพ็กเกจ\n\n" + ("\n".join(lines) if lines else "ยังไม่มีเครือข่าย") + "\n\nตั้งค่าจากบอท: /setinbound PackageID InboundID\nหรือตั้งจากหน้าแพ็กเกจบนเว็บไซต์"
        await query.answer()
        await query.edit_message_text(text, parse_mode=None, reply_markup=_admin_panel_keyboard(owner), disable_web_page_preview=True)
        return
    else:
        await query.answer("อัปเดตข้อมูลแล้ว")
    await query.edit_message_text(_admin_panel_text(), parse_mode=None, reply_markup=_admin_panel_keyboard(owner), disable_web_page_preview=True)


async def runstartflnish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if context.args:
        try:
            delay_seconds = float(context.args[0])
            if delay_seconds <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ รูปแบบ: /runstartflnish จำนวนวินาที\n"
                "ตัวอย่าง: /runstartflnish 5 หรือ /runstartflnish 10"
            )
            return
        db.set_setting(RUN_START_FINISH_DELAY_KEY, str(delay_seconds))

    if not _get_run_start_finish_commands():
        _set_run_start_finish_commands({"addclient"})
    db.set_setting(RUN_START_FINISH_ENABLED_KEY, "1")
    delay_text = _format_seconds(_get_run_start_finish_delay_seconds())
    await update.message.reply_text(
        "✅ เปิดระบบเด้งเมนู /start หลังจบคำสั่งแล้ว\n"
        f"⏱ หน่วงเวลา: {delay_text} วินาที\n"
        f"📌 คำสั่งที่เปิดใช้อยู่: {_format_run_start_finish_commands()}",
    )


async def stopstartflnish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    db.set_setting(RUN_START_FINISH_ENABLED_KEY, "0")
    await update.message.reply_text("⛔ ปิดระบบเด้งเมนู /start หลังจบคำสั่งทั้งหมดแล้ว")


async def addrunstartflnish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ รูปแบบ: /addrunstartflnish คำสั่ง\n"
            "ตัวอย่าง: /addrunstartflnish mycredit หรือ /addrunstartflnish /mycredit",
        )
        return

    command = _normalize_command_name(context.args[0])
    if not command:
        await update.message.reply_text("❌ กรุณาระบุชื่อคำสั่งให้ถูกต้อง")
        return

    commands = _get_run_start_finish_commands()
    commands.add(command)
    _set_run_start_finish_commands(commands)
    await update.message.reply_text(
        f"✅ เพิ่ม /{command} เข้าในรายการเด้งเมนู /start หลังจบคำสั่งแล้ว\n"
        f"📌 คำสั่งที่เปิดใช้อยู่: {_format_run_start_finish_commands()}",
    )


async def deleterunstartflnish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ รูปแบบ: /deleterunstartflnish คำสั่ง\n"
            "ตัวอย่าง: /deleterunstartflnish mycredit หรือ /deleterunstartflnish /mycredit",
        )
        return

    command = _normalize_command_name(context.args[0])
    commands = _get_run_start_finish_commands()
    if command not in commands:
        await update.message.reply_text(
            f"ℹ️ ไม่พบ /{command} ในรายการเด้งเมนู /start\n"
            f"📌 คำสั่งที่เปิดใช้อยู่: {_format_run_start_finish_commands()}",
        )
        return

    commands.remove(command)
    _set_run_start_finish_commands(commands)
    await update.message.reply_text(
        f"✅ ลบ /{command} ออกจากรายการเด้งเมนู /start แล้ว\n"
        f"📌 คำสั่งที่เปิดใช้อยู่: {_format_run_start_finish_commands()}",
    )


async def open_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args or not _is_cancel_button_target(context.args[0]):
        await update.message.reply_text(
            "❌ รูปแบบ: /open cancel หรือ /open /cancel"
        )
        return

    db.set_setting(CANCEL_BUTTON_ENABLED_KEY, "1")
    await update.message.reply_text(
        "✅ เปิดการแสดงปุ่ม /cancel แล้ว\n"
        "ผู้ใช้จะเห็นปุ่ม ❌ /cancel ในแต่ละขั้นตอนของ /addclient"
    )


async def close_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args or not _is_cancel_button_target(context.args[0]):
        await update.message.reply_text(
            "❌ รูปแบบ: /close cancel หรือ /close /cancel"
        )
        return

    db.set_setting(CANCEL_BUTTON_ENABLED_KEY, "0")
    await update.message.reply_text(
        "⛔ ปิดการแสดงปุ่ม /cancel แล้ว\n"
        "ผู้ใช้จะไม่เห็นปุ่ม ❌ /cancel ในขั้นตอนของ /addclient"
    )


async def buydm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    db.set_setting(BUY_DM_ENABLED_KEY, "1")
    await update.message.reply_text(
        "✅ เปิดให้ผู้ใช้ซื้อผ่าน DM ส่วนตัวกับบอทได้แล้ว\n"
        f"{_format_buy_policy()}"
    )


async def nobuydm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if context.args:
        group_ids = _get_allowed_buy_group_ids()
        added_group_ids = []
        invalid_group_ids = []
        for raw_group_id in context.args:
            group_id = _parse_int_setting_part(raw_group_id)
            if group_id is None:
                invalid_group_ids.append(raw_group_id)
            else:
                group_ids.add(group_id)
                added_group_ids.append(group_id)

        if invalid_group_ids:
            await update.message.reply_text(
                "❌ group ID ไม่ถูกต้อง: " + ", ".join(invalid_group_ids)
            )
            return

        db.set_setting(BUY_DM_ENABLED_KEY, "0")
        _set_allowed_buy_group_ids(group_ids)
        await update.message.reply_text(
            "⛔ ปิดการซื้อผ่าน DM แล้ว และจำกัดให้ซื้อได้เฉพาะกลุ่มที่กำหนด\n"
            f"เพิ่มกลุ่ม: {', '.join(str(group_id) for group_id in added_group_ids)}\n"
            f"{_format_buy_policy()}"
        )
        return

    db.set_setting(BUY_DM_ENABLED_KEY, "0")
    _set_allowed_buy_group_ids(set())
    await update.message.reply_text(
        "⛔ ปิดการซื้อผ่าน DM แล้ว ผู้ใช้ต้องซื้อผ่านกลุ่มเท่านั้น\n"
        "ตอนนี้ยังไม่ได้จำกัด group ID จึงซื้อได้จากทุกกลุ่ม\n"
        f"{_format_buy_policy()}"
    )


async def freeclienttime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ รูปแบบ: /freeclienttime จำนวนชั่วโมง\n"
            "ตัวอย่าง: /freeclienttime 1 หรือ /freeclienttime 2"
        )
        return

    try:
        hours = float(context.args[0])
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ กรุณาระบุจำนวนชั่วโมงเป็นตัวเลขมากกว่า 0")
        return

    db.set_setting(FREECLIENT_HOURS_KEY, str(hours))
    await update.message.reply_text(
        f"✅ ตั้งเวลาทดลองฟรีเป็น {_format_hours(hours)} ชั่วโมงแล้ว"
    )


async def freeclientlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    current = _get_freeclient_daily_limit()
    if not context.args:
        await update.message.reply_text(
            f"ℹ️ สิทธิ์ทดลองใช้ฟรีปัจจุบัน: {current} ครั้ง/คน/วัน\n\n"
            "รูปแบบ: /freeclientlimit จำนวน\n"
            "ตัวอย่าง: /freeclientlimit 1\n"
            "ใส่ 0 เพื่อไม่ให้ใช้สิทธิ์ทดลองฟรีได้"
        )
        return

    try:
        limit = int(float(context.args[0]))
        if limit < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ กรุณาระบุจำนวนเป็นเลขจำนวนเต็ม 0 ขึ้นไป")
        return

    db.set_setting(FREECLIENT_DAILY_LIMIT_KEY, str(limit))
    await update.message.reply_text(
        f"✅ ตั้งสิทธิ์ทดลองใช้ฟรีเป็น {limit} ครั้ง/คน/วันแล้ว"
    )


async def freeclient_resettime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    user_id = update.effective_user.id
    mode = _get_freeclient_reset_mode()
    midnight_icon = "✅" if mode == "midnight" else "⬜"
    rolling_icon = "✅" if mode == "rolling_24h" else "⬜"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{midnight_icon} ทุกคนรีเซ็ต 00:00 พร้อมกัน", callback_data=f"free_reset_mode_midnight_{user_id}")],
        [InlineKeyboardButton(f"{rolling_icon} ตัดตามเวลาที่ผู้ใช้สร้าง", callback_data=f"free_reset_mode_rolling_24h_{user_id}")],
    ])
    await update.message.reply_text(
        "กรุณาเลือกเวลารีเซ็ตเวลา code ฟรี\n\n"
        f"สถานะปัจจุบัน: {_freeclient_reset_mode_label(mode)}",
        reply_markup=keyboard,
    )


async def freeclient_resettime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.rsplit("_", 1)
    try:
        owner_id = int(parts[-1])
    except (ValueError, IndexError):
        owner_id = None
    if not is_admin(query.from_user.id):
        await query.answer("❌ คุณไม่มีสิทธิ์", show_alert=True)
        return
    if owner_id is not None and owner_id != query.from_user.id:
        await query.answer("⚠️ ปุ่มนี้ไม่ใช่ของคุณ", show_alert=True)
        return

    if query.data.startswith("free_reset_mode_midnight_"):
        mode = "midnight"
    elif query.data.startswith("free_reset_mode_rolling_24h_"):
        mode = "rolling_24h"
    else:
        await query.answer("❌ ตัวเลือกไม่ถูกต้อง", show_alert=True)
        return

    db.set_setting(FREECLIENT_RESET_MODE_KEY, mode)
    await query.answer()
    await query.edit_message_text(
        "✅ ตั้งค่าโหมดรีเซ็ตสิทธิ์ทดลองฟรีแล้ว\n\n"
        f"โหมดใหม่: {_freeclient_reset_mode_label(mode)}"
    )


async def resetfreeclientlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ รูปแบบ: /resetfreeclientlimit @username หรือ /resetfreeclientlimit user_id"
        )
        return

    target = context.args[0].lstrip("@")
    user_id = _resolve_user(target)
    if not user_id:
        await update.message.reply_text(f"❌ ไม่พบผู้ใช้ `{context.args[0]}`", parse_mode="Markdown")
        return

    db.ensure_user(user_id, None)
    db.set_freeclient_limit_reset(user_id, _thai_now_iso())
    username = db.get_username(user_id) or str(user_id)
    display = f"@{username}" if not username.isdigit() else f"ID: {username}"
    await update.message.reply_text(
        f"✅ รีเซ็ตจำนวนการใช้สิทธิ์ทดลองใช้ฟรีของ {display} เป็น 0 แล้ว\n"
        f"โหมดนับปัจจุบัน: {_freeclient_reset_mode_label()}"
    )


async def openfreeclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    db.set_setting(FREECLIENT_ENABLED_KEY, "1")
    await update.message.reply_text(
        f"✅ เปิดระบบทดลองใช้ฟรีแล้ว เวลาใช้งาน {_format_hours(_get_freeclient_hours())} ชั่วโมง"
    )


async def offfreeclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    db.set_setting(FREECLIENT_ENABLED_KEY, "0")
    await update.message.reply_text("⛔ ปิดระบบทดลองใช้ฟรีแล้ว")


# ── Admin: credit-code system ─────────────────────────────────────────────────
async def addcode_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return ConversationHandler.END

    user = update.effective_user
    context.user_data.clear()
    context.user_data["session_user_id"] = user.id
    context.user_data["command_msg_id"] = update.message.message_id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("เครดิต", callback_data=f"addcode_type_credit_{user.id}")],
        [InlineKeyboardButton("รีเซ็ตเวลาทดลอง", callback_data=f"addcode_type_free_reset_{user.id}")],
    ])
    await update.message.reply_text(
        "กรุณาเลือกฟังชั่น code",
        reply_markup=keyboard,
    )
    return ADD_CODE_TYPE


async def addcode_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return ADD_CODE_TYPE

    await query.answer()
    code_kind = query.data[len("addcode_type_"):].rsplit("_", 1)[0]
    if code_kind not in {"credit", "free_reset"}:
        await query.edit_message_text("❌ ตัวเลือกไม่ถูกต้อง")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["credit_code_kind"] = code_kind
    label = "เครดิต" if code_kind == "credit" else "รีเซ็ตเวลาทดลอง"
    await query.edit_message_text(f"เลือกฟังชั่น code: {label}")
    command_msg_id = context.user_data.get("command_msg_id")
    await _send_force_reply_question(
        update,
        context,
        "ตั้งชื่อโค้ด",
        user_id=query.from_user.id,
        reply_to_message_id=command_msg_id,
        flow_name="addcode",
    )
    return ADD_CODE_NAME


async def addcode_enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return ADD_CODE_NAME
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return ADD_CODE_NAME

    user = update.effective_user
    code_name = _normalize_credit_code_name(update.message.text)
    code_key = _credit_code_key(code_name)
    if not _is_valid_credit_code_name(code_name):
        await _send_force_reply_question(
            update,
            context,
            "❌ ชื่อโค้ดไม่ถูกต้อง กรุณาใช้ A-Z, 0-9, จุด, ขีดกลาง หรือขีดล่าง ไม่เกิน 64 ตัวอักษร",
            user_id=user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addcode",
        )
        return ADD_CODE_NAME

    if db.credit_code_exists(code_key):
        await _send_force_reply_question(
            update,
            context,
            "❌ มีโค้ดชื่อนี้อยู่แล้ว กรุณาตั้งชื่อใหม่",
            user_id=user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addcode",
        )
        return ADD_CODE_NAME

    context.user_data["credit_code_name"] = code_name
    context.user_data["credit_code_key"] = code_key
    if context.user_data.get("credit_code_kind") == "free_reset":
        context.user_data["credit_code_mode"] = "free_reset"
        await _send_force_reply_question(
            update,
            context,
            "จะกำหนดให้ใส่ได้กี่คน (ถ้าไม่จำกัดใส่ 0)",
            user_id=user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addcode",
        )
        return ADD_CODE_FIXED_MAX_USERS

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("เท่ากันทุกคน", callback_data=f"addcode_mode_fixed_{user.id}")],
        [InlineKeyboardButton("สุ่ม", callback_data=f"addcode_mode_random_{user.id}")],
    ])
    await update.message.reply_text(
        f"ชื่อโค้ด: {code_name}\nกรุณาเลือกรูปแบบเครดิต",
        reply_markup=keyboard,
    )
    return ADD_CODE_MODE


async def addcode_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return ADD_CODE_MODE

    await query.answer()
    mode = query.data.split("_")[2]
    context.user_data["credit_code_mode"] = mode
    command_msg_id = context.user_data.get("command_msg_id")

    if mode == "fixed":
        await query.edit_message_text("เลือกรูปแบบ: เท่ากันทุกคน")
        await _send_force_reply_question(
            update,
            context,
            "จะกำหนดให้ได้คนละกี่เครดิต",
            user_id=query.from_user.id,
            reply_to_message_id=command_msg_id,
            flow_name="addcode",
        )
        return ADD_CODE_FIXED_AMOUNT

    await query.edit_message_text("เลือกรูปแบบ: สุ่ม")
    await _send_force_reply_question(
        update,
        context,
        "จะให้ถึงกี่เครดิตถึงโค้ดจะเต็ม",
        user_id=query.from_user.id,
        reply_to_message_id=command_msg_id,
        flow_name="addcode",
    )
    return ADD_CODE_RANDOM_TOTAL


async def addcode_fixed_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return ADD_CODE_FIXED_AMOUNT
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return ADD_CODE_FIXED_AMOUNT

    amount = _parse_positive_amount(update.message.text)
    if amount is None:
        await _send_force_reply_question(
            update,
            context,
            "❌ กรุณากรอกจำนวนเครดิตเป็นตัวเลขมากกว่า 0",
            user_id=update.effective_user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addcode",
        )
        return ADD_CODE_FIXED_AMOUNT

    context.user_data["fixed_credit"] = amount
    await _send_force_reply_question(
        update,
        context,
        "จะกำหนดให้ใส่ได้กี่คน (ถ้าไม่จำกัดใส่ 0)",
        user_id=update.effective_user.id,
        reply_to_message_id=update.message.message_id,
        flow_name="addcode",
    )
    return ADD_CODE_FIXED_MAX_USERS


async def addcode_fixed_max_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return ADD_CODE_FIXED_MAX_USERS
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return ADD_CODE_FIXED_MAX_USERS

    try:
        max_uses = int(update.message.text.strip())
        if max_uses < 0:
            raise ValueError
    except ValueError:
        await _send_force_reply_question(
            update,
            context,
            "❌ กรุณากรอกจำนวนคนเป็นเลขจำนวนเต็ม 0 ขึ้นไป",
            user_id=update.effective_user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addcode",
        )
        return ADD_CODE_FIXED_MAX_USERS

    context.user_data["max_uses"] = max_uses
    await _send_force_reply_question(
        update,
        context,
        "กำหนดระยะเวลาหมดอายุ (ให้ใส่ตัวเลข)",
        user_id=update.effective_user.id,
        reply_to_message_id=update.message.message_id,
        flow_name="addcode",
    )
    return ADD_CODE_FIXED_DURATION


async def addcode_random_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reply_to_bot_question(update, context):
        return ADD_CODE_RANDOM_TOTAL
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return ADD_CODE_RANDOM_TOTAL

    total_credit = _parse_positive_amount(update.message.text)
    if total_credit is None:
        await _send_force_reply_question(
            update,
            context,
            "❌ กรุณากรอกเครดิตรวมเป็นตัวเลขมากกว่า 0",
            user_id=update.effective_user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addcode",
        )
        return ADD_CODE_RANDOM_TOTAL

    context.user_data["total_credit"] = total_credit
    await _send_force_reply_question(
        update,
        context,
        "กำหนดเวลาหมดอายุโค้ด (ให้ใส่ตัวเลข)",
        user_id=update.effective_user.id,
        reply_to_message_id=update.message.message_id,
        flow_name="addcode",
    )
    return ADD_CODE_RANDOM_DURATION


async def addcode_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_state = (
        ADD_CODE_RANDOM_DURATION
        if context.user_data.get("credit_code_mode") == "random"
        else ADD_CODE_FIXED_DURATION
    )
    if not is_reply_to_bot_question(update, context):
        return current_state
    if not is_session_owner_for_text(update, context):
        await _reject_foreign_interaction(update, is_callback=False)
        return current_state

    try:
        duration_amount = int(update.message.text.strip())
        if duration_amount <= 0:
            raise ValueError
    except ValueError:
        await _send_force_reply_question(
            update,
            context,
            "❌ กรุณากรอกระยะเวลาเป็นเลขจำนวนเต็มมากกว่า 0",
            user_id=update.effective_user.id,
            reply_to_message_id=update.message.message_id,
            flow_name="addcode",
        )
        return current_state

    context.user_data["duration_amount"] = duration_amount
    await update.message.reply_text(
        "กรุณาเลือกหน่วยเวลาหมดอายุ",
        reply_markup=_build_credit_code_unit_keyboard(update.effective_user.id),
    )
    return ADD_CODE_UNIT


async def addcode_unit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return ADD_CODE_UNIT

    await query.answer()
    unit = query.data.rsplit("_", 2)[1]
    mode = context.user_data.get("credit_code_mode")
    code_name = context.user_data.get("credit_code_name")
    code_key = context.user_data.get("credit_code_key")
    duration_amount = int(context.user_data.get("duration_amount", 1))
    expires_at = _thai_now() + _duration_to_delta(duration_amount, unit)
    expires_iso = expires_at.isoformat(timespec="seconds")
    created_at = _thai_now_iso()

    if mode == "fixed":
        fixed_credit = float(context.user_data["fixed_credit"])
        total_credit = 0.0
        max_uses = int(context.user_data["max_uses"])
    elif mode == "random":
        fixed_credit = 0.0
        total_credit = float(context.user_data["total_credit"])
        max_uses = 0
    else:
        fixed_credit = 0.0
        total_credit = 0.0
        max_uses = int(context.user_data["max_uses"])

    created = db.create_credit_code(
        code_key=code_key,
        code_name=code_name,
        mode=mode,
        fixed_credit=fixed_credit,
        total_credit=total_credit,
        max_uses=max_uses,
        expires_at=expires_iso,
        created_by=query.from_user.id,
        created_at=created_at,
    )
    if not created:
        await query.edit_message_text("❌ มีโค้ดชื่อนี้อยู่แล้ว กรุณาสร้างใหม่ด้วยชื่ออื่น")
        context.user_data.clear()
        return ConversationHandler.END

    if mode == "fixed":
        detail = (
            f"เครดิตต่อคน: {_format_credit(fixed_credit)} เครดิต\n"
            f"จำนวนผู้ใช้: {_format_limit_count(max_uses)}"
        )
        created_title = "✅ สร้างโค้ดเครดิตสำเร็จ"
    elif mode == "random":
        detail = (
            f"เครดิตรวมก่อนโค้ดเต็ม: {_format_credit(total_credit)} เครดิต\n"
            "การแจก: สุ่มเครดิตจากยอดคงเหลือของโค้ดจนกว่าจะเต็ม"
        )
        created_title = "✅ สร้างโค้ดเครดิตสำเร็จ"
    else:
        detail = (
            f"จำนวนผู้ใช้: {_format_limit_count(max_uses)}\n"
            "ผลลัพธ์เมื่อกรอก: รีเซ็ตจำนวนการใช้สิทธิ์ทดลองใช้ฟรีของผู้ใช้เป็น 0"
        )
        created_title = "✅ สร้างโค้ดรีเซ็ตเวลาทดลองสำเร็จ"

    await query.edit_message_text(
        f"{created_title}\n\n"
        f"ชื่อโค้ด: {code_name}\n"
        f"รูปแบบ: {_credit_code_mode_label(mode)}\n"
        f"{detail}\n"
        f"อายุโค้ด: {duration_amount} {_unit_label(unit)}\n"
        f"หมดอายุ: {_format_thai_datetime(expires_iso)}\n\n"
        "ผู้ใช้สามารถกรอกได้ที่ /Enterthecode"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_addcode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    owner_id = _parse_owner_id(query.data)
    if owner_id is None or query.from_user.id != owner_id:
        await _reject_foreign_interaction(update, is_callback=True)
        return

    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ ยกเลิกการสร้างโค้ดแล้ว")
    return ConversationHandler.END


async def deletecode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not context.args:
        await update.message.reply_text("❌ รูปแบบ: /deletecode ชื่อโค้ด\nตัวอย่าง: /deletecode test")
        return

    code_name = _normalize_credit_code_name(context.args[0])
    if db.delete_credit_code(_credit_code_key(code_name)):
        await update.message.reply_text(f"✅ ลบโค้ด {code_name} ออกจากระบบแล้ว")
    else:
        await update.message.reply_text("❌ ไม่พบโค้ดนี้ในระบบ")


async def checkcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    codes = db.get_all_credit_codes()
    if not codes:
        await update.message.reply_text("📭 ยังไม่มีโค้ดในระบบ")
        return

    header = (
        f"🎁 โค้ดทั้งหมดในระบบ\n"
        f"สถานะระบบกรอกโค้ด: {'เปิด' if _credit_code_enabled() else 'ปิด'}\n"
        f"จำนวนโค้ด: {len(codes)}\n\n"
    )
    entries = []
    for idx, code in enumerate(codes, start=1):
        if code["mode"] == "fixed":
            detail = (
                f"เครดิตต่อคน: {_format_credit(code['fixed_credit'])} | "
                f"ใช้แล้ว: {int(code['used_count'])}/{_format_limit_count(int(code['max_uses']))}"
            )
        elif code["mode"] == "random":
            remaining = max(0.0, float(code["total_credit"]) - float(code["distributed_credit"]))
            detail = (
                f"เครดิตรวม: {_format_credit(code['total_credit'])} | "
                f"แจกแล้ว: {_format_credit(code['distributed_credit'])} | "
                f"คงเหลือ: {_format_credit(remaining)}"
            )
        else:
            detail = (
                "ผลลัพธ์: รีเซ็ตสิทธิ์ทดลองใช้ฟรี | "
                f"ใช้แล้ว: {int(code['used_count'])}/{_format_limit_count(int(code['max_uses']))}"
            )
        entries.append(
            f"[{idx}] {code['code_name']}\n"
            f"รูปแบบ: {_credit_code_mode_label(code['mode'])}\n"
            f"{detail}\n"
            f"สถานะ: {_credit_code_status_text(code)}\n"
            f"หมดอายุ: {_format_thai_datetime(code['expires_at'])}\n"
            f"สร้างเมื่อ: {_format_thai_datetime(code['created_at'])}\n"
            f"สร้างโดย: {code['created_by'] or '-'}\n"
            "------------------------------"
        )
    await _send_chunked_text(update, header, entries)


async def statuscode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    current = _credit_code_enabled()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ON (เปิด)", callback_data="creditcode_status_on"),
        InlineKeyboardButton("❌ OFF (ปิด)", callback_data="creditcode_status_off"),
    ]])
    await update.message.reply_text(
        "🎁 ระบบกรอกโค้ด\n\n"
        f"สถานะปัจจุบัน: {'🟢 เปิดอยู่' if current else '🔴 ปิดอยู่'}\n"
        "กรุณาเลือกสถานะที่ต้องการ:",
        reply_markup=keyboard,
    )


async def statuscode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ คุณไม่มีสิทธิ์", show_alert=True)
        return

    await query.answer()
    enabled = query.data.endswith("_on")
    db.set_setting(CREDIT_CODE_ENABLED_KEY, "1" if enabled else "0")
    await query.edit_message_text(
        "🎁 ระบบกรอกโค้ด\n\n"
        f"{'✅ เปิดใช้งานแล้ว' if enabled else '⛔ ปิดใช้งานแล้ว'}"
    )


async def checkusercode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not context.args:
        await update.message.reply_text("❌ รูปแบบ: /checkusercode ชื่อโค้ด\nตัวอย่าง: /checkusercode test")
        return

    code_name = _normalize_credit_code_name(context.args[0])
    code_key = _credit_code_key(code_name)
    code = db.get_credit_code(code_key)
    if not code:
        await update.message.reply_text("❌ ไม่พบโค้ดนี้ในระบบ")
        return

    redemptions = db.get_credit_code_redemptions(code_key)
    if not redemptions:
        await update.message.reply_text(f"📭 ยังไม่มีผู้ใช้กรอกโค้ด {code['code_name']}")
        return

    header = (
        f"👥 ผู้ใช้ที่กรอกโค้ด {code['code_name']}\n"
        f"รูปแบบ: {_credit_code_mode_label(code['mode'])}\n"
        f"ใช้แล้ว: {_credit_code_usage_text(code)}\n"
        f"จำนวนรายการ: {len(redemptions)}\n\n"
    )
    entries = []
    for idx, item in enumerate(redemptions, start=1):
        username = item.get("username") or "-"
        display = f"@{username}" if username != "-" else "-"
        result_line = (
            "ผลลัพธ์: รีเซ็ตสิทธิ์ทดลองใช้ฟรี"
            if code["mode"] == "free_reset"
            else f"เครดิตที่ได้รับ: {_format_credit(item['credit_amount'])}"
        )
        entries.append(
            f"[{idx}] ผู้ใช้: {display}\n"
            f"User ID: {item['user_id']}\n"
            f"{result_line}\n"
            f"เวลา: {_format_thai_datetime(item['redeemed_at'])}\n"
            "------------------------------"
        )
    await _send_chunked_text(update, header, entries)


# ── Admin: /logbuy ────────────────────────────────────────────────────────────
async def logbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงประวัติการซื้อ /addclient ของ user ที่ระบุ"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ รูปแบบ: /logbuy @username หรือ /logbuy user_id"
        )
        return

    target = context.args[0].lstrip("@")
    user_id = _resolve_user(target)
    if not user_id:
        await update.message.reply_text(
            f"❌ ไม่พบผู้ใช้ `{context.args[0]}`", parse_mode="Markdown"
        )
        return

    display_limit = _get_log_display_limit_buy()
    logs = db.get_buy_log(user_id, display_limit)
    username = db.get_username(user_id) or str(user_id)
    display_name = f"@{username}" if not str(username).isdigit() else f"ID: {username}"

    if not logs:
        await update.message.reply_text(
            f"📋 ไม่พบประวัติการซื้อของ {display_name} (ID: {user_id})"
        )
        return

    total_stored = db.LOG_MAX_ENTRIES
    sep = "─" * 30
    header = (
        f"🧾 *ประวัติการซื้อ* {display_name}\n"
        f"🆔 User ID: `{user_id}`\n"
        f"📊 แสดง {len(logs)} รายการล่าสุด (บันทึกสูงสุด {total_stored} รายการ)\n"
        f"{sep}\n"
    )

    # สร้างรายการแต่ละ entry (เก่า → ใหม่ / ล่าสุดอยู่ด้านล่างสุด)
    entries = []
    for idx, entry in enumerate(logs, start=1):
        gb_text = f"{entry['gb']} GB" if entry.get("gb", 0) > 0 else "ไม่จำกัด"
        entries.append(
            f"*[{idx}]* 🕐 {entry['created_at']}\n"
            f"   👤 Username: {entry['username'] or '-'}\n"
            f"   📌 ชื่อโค้ด: `{entry['code_name']}`\n"
            f"   🌐 เครือข่าย: {entry['network']}\n"
            f"   📅 จำนวนวัน: {entry['days']} วัน\n"
            f"   💾 จำกัด GB: {gb_text}\n"
            f"   💰 ราคา: {entry['cost']:.2f} เครดิต\n"
            f"   🔗 `{entry['link']}`\n"
            f"{sep}"
        )

    # ส่งแบบ chunk ไม่เกิน 4000 ตัวอักษรต่อข้อความ
    current_chunk = header
    for entry_text in entries:
        block = entry_text + "\n"
        if len(current_chunk) + len(block) > 4000:
            await update.message.reply_text(
                current_chunk, parse_mode="Markdown", disable_web_page_preview=True
            )
            current_chunk = block
        else:
            current_chunk += block

    if current_chunk.strip():
        await update.message.reply_text(
            current_chunk, parse_mode="Markdown", disable_web_page_preview=True
        )


# ── Admin: /logfree ───────────────────────────────────────────────────────────
async def logfree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงประวัติการทดลองฟรี /freeclient ของ user ที่ระบุ"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ รูปแบบ: /logfree @username หรือ /logfree user_id"
        )
        return

    target = context.args[0].lstrip("@")
    user_id = _resolve_user(target)
    if not user_id:
        await update.message.reply_text(
            f"❌ ไม่พบผู้ใช้ `{context.args[0]}`", parse_mode="Markdown"
        )
        return

    display_limit = _get_log_display_limit_free()
    logs = db.get_free_log(user_id, display_limit)
    username = db.get_username(user_id) or str(user_id)
    display_name = f"@{username}" if not str(username).isdigit() else f"ID: {username}"

    if not logs:
        await update.message.reply_text(
            f"🧪 ไม่พบประวัติการทดลองฟรีของ {display_name} (ID: {user_id})"
        )
        return

    total_stored = db.LOG_MAX_ENTRIES
    sep = "─" * 30
    header = (
        f"🧪 *ประวัติการทดลองฟรี* {display_name}\n"
        f"🆔 User ID: `{user_id}`\n"
        f"📊 แสดง {len(logs)} รายการล่าสุด (บันทึกสูงสุด {total_stored} รายการ)\n"
        f"{sep}\n"
    )

    entries = []
    for idx, entry in enumerate(logs, start=1):
        gb_text = f"{entry['gb']} GB" if entry.get("gb", 0) > 0 else "ไม่จำกัด"
        entries.append(
            f"*[{idx}]* 🕐 {entry['created_at']}\n"
            f"   👤 Username: {entry['username'] or '-'}\n"
            f"   📌 ชื่อโค้ด: `{entry['code_name']}`\n"
            f"   🌐 เครือข่าย: {entry['network']}\n"
            f"   ⏱ เวลาทดลอง: {_format_hours(entry['hours'])} ชั่วโมง\n"
            f"   💾 จำกัด GB: {gb_text}\n"
            f"   🔗 `{entry['link']}`\n"
            f"{sep}"
        )

    current_chunk = header
    for entry_text in entries:
        block = entry_text + "\n"
        if len(current_chunk) + len(block) > 4000:
            await update.message.reply_text(
                current_chunk, parse_mode="Markdown", disable_web_page_preview=True
            )
            current_chunk = block
        else:
            current_chunk += block

    if current_chunk.strip():
        await update.message.reply_text(
            current_chunk, parse_mode="Markdown", disable_web_page_preview=True
        )


# ── Admin: /listaddclient ─────────────────────────────────────────────────────
async def listaddclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """กำหนดจำนวนรายการที่แสดงใน /logbuy"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    current = _get_log_display_limit_buy()
    if not context.args:
        await update.message.reply_text(
            f"📋 จำนวนรายการที่แสดงใน /logbuy ปัจจุบัน: *{current}* รายการ\n\n"
            f"❌ รูปแบบ: /listaddclient จำนวน\n"
            f"ตัวอย่าง: /listaddclient 50\n\n"
            f"_(บันทึกสูงสุดเสมอ {db.LOG_MAX_ENTRIES} รายการ แต่แสดงเพียง N รายการล่าสุด)_",
            parse_mode="Markdown",
        )
        return

    try:
        n = int(context.args[0])
        if n < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ กรุณาระบุตัวเลขจำนวนเต็มที่มากกว่า 0")
        return

    db.set_setting(LOG_DISPLAY_LIMIT_BUY_KEY, str(n))
    await update.message.reply_text(
        f"✅ กำหนดจำนวนรายการที่แสดงใน /logbuy เป็น *{n}* รายการล่าสุดแล้ว\n"
        f"_(ยังคงบันทึกสูงสุด {db.LOG_MAX_ENTRIES} รายการเหมือนเดิม)_",
        parse_mode="Markdown",
    )


# ── Admin: /listfreeclient ────────────────────────────────────────────────────
async def listfreeclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """กำหนดจำนวนรายการที่แสดงใน /logfree"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    current = _get_log_display_limit_free()
    if not context.args:
        await update.message.reply_text(
            f"🧪 จำนวนรายการที่แสดงใน /logfree ปัจจุบัน: *{current}* รายการ\n\n"
            f"❌ รูปแบบ: /listfreeclient จำนวน\n"
            f"ตัวอย่าง: /listfreeclient 50\n\n"
            f"_(บันทึกสูงสุดเสมอ {db.LOG_MAX_ENTRIES} รายการ แต่แสดงเพียง N รายการล่าสุด)_",
            parse_mode="Markdown",
        )
        return

    try:
        n = int(context.args[0])
        if n < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ กรุณาระบุตัวเลขจำนวนเต็มที่มากกว่า 0")
        return

    db.set_setting(LOG_DISPLAY_LIMIT_FREE_KEY, str(n))
    await update.message.reply_text(
        f"✅ กำหนดจำนวนรายการที่แสดงใน /logfree เป็น *{n}* รายการล่าสุดแล้ว\n"
        f"_(ยังคงบันทึกสูงสุด {db.LOG_MAX_ENTRIES} รายการเหมือนเดิม)_",
        parse_mode="Markdown",
    )



# ── Admin: /Sorting ───────────────────────────────────────────────────────────
async def sorting_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    current = _get_mycodes_sort_order()
    keyboard = [
        [InlineKeyboardButton(
            "⬆️ ล่าสุดบน (ใหม่อยู่บนสุดของข้อความ)" + (" ✅" if current == "newest_top" else ""),
            callback_data="sorting_newest_top",
        )],
        [InlineKeyboardButton(
            "⬇️ ล่าสุดล่าง (ใหม่อยู่ล่างสุด ไม่ต้องเลื่อนหา)" + (" ✅" if current == "newest_bottom" else ""),
            callback_data="sorting_newest_bottom",
        )],
    ]
    label = "ล่าสุดบน ⬆️" if current == "newest_top" else "ล่าสุดล่าง ⬇️"
    await update.message.reply_text(
        f"🔃 *ตั้งค่าการเรียงโค้ดใน /mycodes*\n\n"
        f"โหมดปัจจุบัน: *{label}*\n\n"
        f"⬆️ *ล่าสุดบน* — โค้ดที่สร้างล่าสุดจะอยู่บนสุดของข้อความ\n"
        f"⬇️ *ล่าสุดล่าง* — โค้ดที่สร้างล่าสุดจะอยู่ล่างสุด (แนะนำ: ไม่ต้องเลื่อนหา)\n\n"
        f"กรุณาเลือก:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def sorting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้", show_alert=True)
        return
    await query.answer()

    mode = query.data.split("_", 1)[1]  # "newest_top" or "newest_bottom"
    db.set_setting("mycodes_sort_order", mode)

    label = "ล่าสุดบน ⬆️" if mode == "newest_top" else "ล่าสุดล่าง ⬇️"
    desc = "โค้ดที่สร้างล่าสุดจะอยู่บนสุดของข้อความ" if mode == "newest_top" else "โค้ดที่สร้างล่าสุดจะอยู่ล่างสุด (ไม่ต้องเลื่อนหา)"

    keyboard = [
        [InlineKeyboardButton(
            "⬆️ ล่าสุดบน (ใหม่อยู่บนสุดของข้อความ)" + (" ✅" if mode == "newest_top" else ""),
            callback_data="sorting_newest_top",
        )],
        [InlineKeyboardButton(
            "⬇️ ล่าสุดล่าง (ใหม่อยู่ล่างสุด ไม่ต้องเลื่อนหา)" + (" ✅" if mode == "newest_bottom" else ""),
            callback_data="sorting_newest_bottom",
        )],
    ]
    await query.edit_message_text(
        f"✅ ตั้งการเรียงเป็น *{label}* แล้ว\n\nℹ️ {desc}\n\nกรุณาเลือก (เพื่อเปลี่ยนได้อีก):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Admin: /Purchaseinformation ───────────────────────────────────────────────
async def purchaseinformation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        current = _get_mycodes_store_limit()
        await update.message.reply_text(
            f"ℹ️ จำนวนโค้ดที่เก็บต่อผู้ใช้ปัจจุบัน: *{current}* รายการ\n\n"
            f"รูปแบบ: `/Purchaseinformation จำนวน`\n"
            f"ตัวอย่าง: `/Purchaseinformation 100`",
            parse_mode="Markdown",
        )
        return

    try:
        n = int(context.args[0])
        if n < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ กรุณาระบุตัวเลขที่มากกว่า 0")
        return

    db.set_setting("mycodes_store_limit", str(n))
    # Sync display limit ไม่ให้เกิน store limit
    current_display = _get_mycodes_display_limit()
    if current_display > n:
        db.set_setting("mycodes_display_limit", str(n))
        await update.message.reply_text(
            f"✅ ตั้งจำนวนเก็บข้อมูลโค้ดเป็น *{n}* รายการ/ผู้ใช้ แล้ว\n"
            f"⚠️ ปรับจำนวนแสดงผล (/Showpurchaselist) ลดเป็น *{n}* ด้วยเพราะเกินจำนวนที่เก็บ",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"✅ ตั้งจำนวนเก็บข้อมูลโค้ดเป็น *{n}* รายการ/ผู้ใช้ แล้ว\n"
            f"📊 จำนวนแสดงผลปัจจุบัน: *{current_display}* รายการ",
            parse_mode="Markdown",
        )


# ── Admin: /Showpurchaselist ──────────────────────────────────────────────────
async def showpurchaselist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    if not context.args:
        current_display = _get_mycodes_display_limit()
        current_store = _get_mycodes_store_limit()
        await update.message.reply_text(
            f"ℹ️ แสดงผลปัจจุบัน: *{current_display}* รายการ (เก็บในฐานข้อมูล: *{current_store}* รายการ)\n\n"
            f"รูปแบบ: `/Showpurchaselist จำนวน`\n"
            f"ตัวอย่าง: `/Showpurchaselist 50`",
            parse_mode="Markdown",
        )
        return

    try:
        n = int(context.args[0])
        if n < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ กรุณาระบุตัวเลขที่มากกว่า 0")
        return

    store_limit = _get_mycodes_store_limit()
    if n > store_limit:
        n = store_limit
        await update.message.reply_text(
            f"⚠️ ปรับเป็น *{n}* เพราะไม่สามารถแสดงเกินจำนวนที่เก็บได้ (เก็บ: *{store_limit}* รายการ)",
            parse_mode="Markdown",
        )
    db.set_setting("mycodes_display_limit", str(n))
    await update.message.reply_text(
        f"✅ ตั้งจำนวนแสดงผล /mycodes เป็น *{n}* รายการล่าสุด แล้ว\n"
        f"📦 ฐานข้อมูลยังคงเก็บ *{store_limit}* รายการต่อผู้ใช้",
        parse_mode="Markdown",
    )

# ── Fallback: ดักจับ callback ที่ไม่ได้รับการ handle (คนอื่นกดปุ่มของคนอื่น) ──


# ── Admin: /logbuyall ─────────────────────────────────────────────────────────
async def logbuyall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงประวัติการซื้อ /addclient ของผู้ใช้ทุกคนรวมกัน"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    display_limit = _get_log_display_limit_buy()
    logs = db.get_buy_log_all(display_limit)

    if not logs:
        await update.message.reply_text("📋 ยังไม่มีประวัติการซื้อในระบบ")
        return

    sep = "─" * 30
    header = (
        f"🧾 *ประวัติการซื้อทั้งหมด* (ทุกผู้ใช้)\n"
        f"📊 แสดง {len(logs)} รายการล่าสุด (บันทึกสูงสุด {db.LOG_MAX_ENTRIES} รายการ)\n"
        f"{sep}\n"
    )

    entries = []
    for idx, entry in enumerate(logs, start=1):
        gb_text = f"{entry['gb']} GB" if entry.get("gb", 0) > 0 else "ไม่จำกัด"
        uid = entry["user_id"]
        uname = entry.get("username") or "-"
        display = f"@{uname}" if uname != "-" and not str(uname).isdigit() else f"ID: {uid}"
        entries.append(
            f"*[{idx}]* 🕐 {entry['created_at']}\n"
            f"   👤 ผู้ใช้: {display} (`{uid}`)\n"
            f"   📌 ชื่อโค้ด: `{entry['code_name']}`\n"
            f"   🌐 เครือข่าย: {entry['network']}\n"
            f"   📅 จำนวนวัน: {entry['days']} วัน\n"
            f"   💾 จำกัด GB: {gb_text}\n"
            f"   💰 ราคา: {entry['cost']:.2f} เครดิต\n"
            f"   🔗 `{entry['link']}`\n"
            f"{sep}"
        )

    current_chunk = header
    for entry_text in entries:
        block = entry_text + "\n"
        if len(current_chunk) + len(block) > 4000:
            await update.message.reply_text(
                current_chunk, parse_mode="Markdown", disable_web_page_preview=True
            )
            current_chunk = block
        else:
            current_chunk += block

    if current_chunk.strip():
        await update.message.reply_text(
            current_chunk, parse_mode="Markdown", disable_web_page_preview=True
        )


# ── Admin: /logfreeall ────────────────────────────────────────────────────────
async def logfreeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงประวัติการทดลองฟรี /freeclient ของผู้ใช้ทุกคนรวมกัน"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    display_limit = _get_log_display_limit_free()
    logs = db.get_free_log_all(display_limit)

    if not logs:
        await update.message.reply_text("🧪 ยังไม่มีประวัติการทดลองฟรีในระบบ")
        return

    sep = "─" * 30
    header = (
        f"🧪 *ประวัติการทดลองฟรีทั้งหมด* (ทุกผู้ใช้)\n"
        f"📊 แสดง {len(logs)} รายการล่าสุด (บันทึกสูงสุด {db.LOG_MAX_ENTRIES} รายการ)\n"
        f"{sep}\n"
    )

    entries = []
    for idx, entry in enumerate(logs, start=1):
        gb_text = f"{entry['gb']} GB" if entry.get("gb", 0) > 0 else "ไม่จำกัด"
        uid = entry["user_id"]
        uname = entry.get("username") or "-"
        display = f"@{uname}" if uname != "-" and not str(uname).isdigit() else f"ID: {uid}"
        entries.append(
            f"*[{idx}]* 🕐 {entry['created_at']}\n"
            f"   👤 ผู้ใช้: {display} (`{uid}`)\n"
            f"   📌 ชื่อโค้ด: `{entry['code_name']}`\n"
            f"   🌐 เครือข่าย: {entry['network']}\n"
            f"   ⏱ เวลาทดลอง: {_format_hours(entry['hours'])} ชั่วโมง\n"
            f"   💾 จำกัด GB: {gb_text}\n"
            f"   🔗 `{entry['link']}`\n"
            f"{sep}"
        )

    current_chunk = header
    for entry_text in entries:
        block = entry_text + "\n"
        if len(current_chunk) + len(block) > 4000:
            await update.message.reply_text(
                current_chunk, parse_mode="Markdown", disable_web_page_preview=True
            )
            current_chunk = block
        else:
            current_chunk += block

    if current_chunk.strip():
        await update.message.reply_text(
            current_chunk, parse_mode="Markdown", disable_web_page_preview=True
        )



# ── Admin: /toggleaddclient ───────────────────────────────────────────────────
async def toggleaddclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงปุ่ม ON/OFF เพื่อเปิด-ปิดระบบสร้างโค้ด /addclient"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    current = _addclient_enabled()
    status_text = "🟢 เปิดอยู่" if current else "🔴 ปิดอยู่"
    keyboard = [
        [
            InlineKeyboardButton("✅ ON  (เปิด)", callback_data="addclient_toggle_on"),
            InlineKeyboardButton("❌ OFF (ปิด)", callback_data="addclient_toggle_off"),
        ]
    ]
    await update.message.reply_text(
        f"🛒 *ระบบสร้างโค้ด /addclient*\n\n"
        f"สถานะปัจจุบัน: {status_text}\n\n"
        f"กรุณาเลือกสถานะที่ต้องการ:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def addclient_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """รับ callback จากปุ่ม ON/OFF ของ /toggleaddclient"""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ คุณไม่มีสิทธิ์", show_alert=True)
        return

    await query.answer()
    action = query.data  # "addclient_toggle_on" or "addclient_toggle_off"

    if action == "addclient_toggle_on":
        db.set_setting(ADDCLIENT_ENABLED_KEY, "1")
        await query.edit_message_text(
            "🛒 *ระบบสร้างโค้ด /addclient*\n\n"
            "✅ *เปิดใช้งานแล้ว* — ผู้ใช้สามารถสร้างโค้ด/เช่าบริการได้ตามปกติ",
            parse_mode="Markdown",
        )
    else:
        db.set_setting(ADDCLIENT_ENABLED_KEY, "0")
        await query.edit_message_text(
            "🛒 *ระบบสร้างโค้ด /addclient*\n\n"
            "❌ *ปิดใช้งานแล้ว* — ทุกคนรวมถึงแอดมินจะไม่สามารถสร้างโค้ด/เช่าบริการได้",
            parse_mode="Markdown",
        )

# ── Admin: /channelfreeclient ─────────────────────────────────────────────────
def _cfree_menu_text() -> str:
    mode = _get_freeclient_channel_mode()
    label = _CFREE_MODE_LABELS.get(mode, mode)
    ids = _get_freeclient_group_ids()
    ids_line = ""
    if mode in ("specified_and_dm", "specified_only"):
        ids_line = f"\nID กลุ่มที่กำหนด: {', '.join(str(i) for i in sorted(ids)) if ids else 'ยังไม่ได้กำหนด'}"
    return (
        f"🧪 *ตั้งค่าช่องทางใช้งาน /freeclient*\n\n"
        f"สถานะปัจจุบัน: *{label}*{ids_line}\n\n"
        f"กรุณาเลือกช่องทางที่ต้องการ:"
    )


async def channelfreeclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🌐 DM และกลุ่มทั้งหมด",       callback_data=f"cfree_dm_and_group_{update.effective_user.id}")],
        [InlineKeyboardButton("👥 กลุ่มเท่านั้น",             callback_data=f"cfree_group_only_{update.effective_user.id}")],
        [InlineKeyboardButton("🔒 กลุ่มที่กำหนด + DM",       callback_data=f"cfree_specified_and_dm_{update.effective_user.id}")],
        [InlineKeyboardButton("🔐 กลุ่มที่กำหนดเท่านั้น",   callback_data=f"cfree_specified_only_{update.effective_user.id}")],
        [InlineKeyboardButton("💬 DM เท่านั้น",               callback_data=f"cfree_dm_only_{update.effective_user.id}")],
    ]
    await update.message.reply_text(
        _cfree_menu_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CFREE_ENTER_IDS


async def cfree_choose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """รับ callback จากการเลือก mode ของ /channelfreeclient"""
    query = update.callback_query
    await query.answer()

    # ตรวจ owner
    parts = query.data.rsplit("_", 1)
    try:
        owner_id = int(parts[-1])
    except (ValueError, IndexError):
        owner_id = None
    if owner_id != query.from_user.id:
        await query.answer("⚠️ ปุ่มนี้ไม่ใช่ของคุณ", show_alert=True)
        return CFREE_ENTER_IDS

    # แยก mode ออกจาก callback_data (ตัด _{user_id} ออก)
    data_without_owner = query.data[len("cfree_"):].rsplit("_", 1)[0]
    # mapping: cfree_dm_and_group_{id} → dm_and_group
    mode_map = {
        "dm_and_group":    "dm_and_group",
        "group_only":      "group_only",
        "specified_and_dm":"specified_and_dm",
        "specified_only":  "specified_only",
        "dm_only":         "dm_only",
    }
    # data_without_owner is like "dm_and_group" already
    mode = data_without_owner
    if mode not in mode_map:
        await query.edit_message_text("❌ ตัวเลือกไม่ถูกต้อง")
        return ConversationHandler.END

    db.set_setting(FREECLIENT_CHANNEL_MODE_KEY, mode)
    label = _CFREE_MODE_LABELS.get(mode, mode)

    if mode in ("specified_and_dm", "specified_only"):
        context.user_data["cfree_pending_ids"] = True
        context.user_data["cfree_owner_id"] = query.from_user.id
        await query.edit_message_text(
            f"✅ เลือกโหมด: *{label}*\n\n"
            f"📝 กรุณาพิมพ์ **ID กลุ่ม** ที่ต้องการอนุญาต\n"
            f"(คั่นด้วยเครื่องหมาย `,` ถ้ามีหลายกลุ่ม)\n"
            f"ตัวอย่าง: `-1001234567890, -1009876543210`\n\n"
            f"หรือพิมพ์ `clear` เพื่อล้าง ID ทั้งหมด",
            parse_mode="Markdown",
        )
        return CFREE_ENTER_IDS
    else:
        await query.edit_message_text(
            f"✅ *บันทึกแล้ว!*\n\n"
            f"ช่องทางใช้งาน /freeclient: *{label}*",
            parse_mode="Markdown",
        )
        return ConversationHandler.END


async def cfree_enter_ids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """รับ group IDs text สำหรับ /channelfreeclient"""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text.strip()

    if text.lower() == "clear":
        db.set_setting(FREECLIENT_GROUP_IDS_KEY, "")
        mode = _get_freeclient_channel_mode()
        label = _CFREE_MODE_LABELS.get(mode, mode)
        await update.message.reply_text(
            f"✅ ล้าง ID กลุ่มทั้งหมดแล้ว\nโหมดปัจจุบัน: *{label}*",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    ids = set()
    invalid = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            invalid.append(part)

    if invalid:
        await update.message.reply_text(
            f"❌ ID ไม่ถูกต้อง: {', '.join(invalid)}\n"
            f"กรุณากรอกเฉพาะตัวเลข เช่น -1001234567890\n"
            f"หรือพิมพ์ `clear` เพื่อล้าง"
        )
        return CFREE_ENTER_IDS

    _set_freeclient_group_ids(ids)
    mode = _get_freeclient_channel_mode()
    label = _CFREE_MODE_LABELS.get(mode, mode)
    await update.message.reply_text(
        f"✅ *บันทึกแล้ว!*\n\n"
        f"โหมด: *{label}*\n"
        f"ID กลุ่มที่กำหนด: {', '.join(str(i) for i in sorted(ids))}",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

async def handle_foreign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler ลำดับท้ายสุด — รับ callback query ที่ ConversationHandler ไม่ได้ handle
    หมายความว่าผู้กดปุ่มนี้ไม่ได้อยู่ใน state ที่ถูกต้อง (ไม่ใช่เจ้าของ session)
    """
    await update.callback_query.answer(
        "⚠️ คุณไม่สามารถใช้งานของผู้อื่นได้",
        show_alert=True,
    )


# ── Helper ────────────────────────────────────────────────────────────────────
def _resolve_user(target: str):
    """Resolve @username or numeric user_id string to user_id integer.
    รองรับ user_id ที่เป็นจำนวนลบด้วย (int() แทน isdigit())
    """
    try:
        return int(target)
    except ValueError:
        return db.get_user_id_by_username(target)


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงเวอร์ชันเพื่อยืนยันว่า Railway ใช้ไฟล์ล่าสุดจริง"""
    await update.message.reply_text(f"Bio-shop Bot {BUILD_VERSION}", parse_mode=None)


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """จับข้อผิดพลาดไม่ให้ handler ล้มแบบไม่มีคำอธิบาย และไม่พิมพ์ข้อมูลลับ"""
    logger.error("Telegram handler error: %s", type(context.error).__name__)
    effective_message = getattr(update, "effective_message", None)
    if effective_message:
        try:
            await effective_message.reply_text(
                "⚠️ เกิดข้อผิดพลาดชั่วคราว กรุณาลองใหม่อีกครั้ง หากยังพบปัญหาให้แจ้งแอดมิน",
                parse_mode=None,
            )
        except Exception:
            logger.error("Unable to send safe error message")


# ── Main ──────────────────────────────────────────────────────────────────────

# ── 3x-ui session keep-alive ─────────────────────────────────────────────────
async def _xui_keepalive_job(context) -> None:
    """
    Re-login to 3x-ui ทุก SESSION_REFRESH_MINUTES นาที
    ป้องกัน session cookie หมดอายุระหว่าง runtime
    (สาเหตุของ "Expecting value: line 1 column 1" error)
    """
    from xui_api import SESSION_REFRESH_MINUTES as _SRM
    ok = xui.keep_alive()
    if ok:
        logger.info(f"✅ 3x-ui keep-alive สำเร็จ (refresh ทุก {_SRM} นาที)")
    else:
        logger.warning("⚠️  3x-ui keep-alive ล้มเหลว — จะ retry รอบถัดไปอัตโนมัติ")




async def _expired_purchase_cleanup_job(context) -> None:
    try:
        result = db.cleanup_expired_purchase_data(grace_days=7)
        if result["clients"] or result["purchase_logs"]:
            logger.info("🧹 ลบข้อมูลซื้อที่หมดอายุเกิน 7 วัน: clients=%s logs=%s", result["clients"], result["purchase_logs"])
    except Exception:
        logger.exception("ล้างข้อมูลซื้อหมดอายุไม่สำเร็จ")


# ── Startup hook: แจ้ง Admin เมื่อ 3x-ui ไม่สามารถเชื่อมต่อได้ ──────────────
async def _on_startup(app: "Application") -> None:
    """
    post_init hook — รันอัตโนมัติหลัง bot พร้อมทำงาน
    แจ้ง Admin ถ้า 3x-ui ไม่พร้อมใช้งาน พร้อมแนะนำวิธีแก้
    """
    try:
        result = db.cleanup_expired_purchase_data(grace_days=7)
        logger.info("🧹 ตรวจข้อมูลหมดอายุ: ลบ clients=%s logs=%s", result["clients"], result["purchase_logs"])
    except Exception:
        logger.exception("ตรวจข้อมูลหมดอายุเมื่อเริ่มระบบไม่สำเร็จ")

    if xui._logged_in:
        mode = "API Token" if xui._api_token_mode else "Username/Password"
        logger.info(f"✅ 3x-ui พร้อมใช้งาน (mode: {mode}) — startup OK")
        return

    xui_url = os.getenv("XUI_URL", "(ไม่ได้ตั้งค่า)")
    has_token = bool(os.getenv("XUI_API_TOKEN", "").strip())

    if has_token:
        # API Token mode แต่ยัง login ไม่ผ่าน (token ผิดหรือ URL ผิด)
        msg = (
            "⚠️ *แจ้งเตือนระบบ*\n\n"
            "❌ Bot เริ่มต้นสำเร็จ แต่ *API Token ไม่ผ่าน* 3x\\-ui\n\n"
            "ผลที่ตามมา: คำสั่ง `/addclient` และ `/freeclient` จะไม่ทำงาน\n\n"
            f"🔗 XUI\\_URL: `{xui_url}`\n\n"
            "สาเหตุที่เป็นไปได้:\n"
            "1\\. `XUI_API_TOKEN` ผิดหรือถูกปิดใช้งาน — ไปสร้าง token ใหม่ใน Panel\n"
            "2\\. `XUI_URL` ไม่ถูกต้อง\n"
            "3\\. Server 3x\\-ui offline หรือ port ปิด\n\n"
            "👉 Panel Settings → Security → API Tokens → Create/Copy ใหม่"
        )
    else:
        msg = (
            "⚠️ *แจ้งเตือนระบบ*\n\n"
            "❌ Bot เริ่มต้นสำเร็จ แต่ *ไม่สามารถเชื่อมต่อ 3x\\-ui* ได้\n\n"
            "ผลที่ตามมา: คำสั่ง `/addclient` และ `/freeclient` จะไม่ทำงาน\n\n"
            f"🔗 XUI\\_URL: `{xui_url}`\n\n"
            "สาเหตุที่เป็นไปได้:\n"
            "1\\. IP ของ Railway ถูก ban โดย fail2ban ของ panel\n"
            "2\\. `XUI_URL` ไม่มี Sub\\-Path \\(เช่น `/secretpath`\\)\n"
            "3\\. `XUI_USERNAME` หรือ `XUI_PASSWORD` ผิด\n"
            "4\\. Server 3x\\-ui offline หรือ port ปิด\n\n"
            "💡 *แนะนำ:* ใช้ API Token แทน — ไม่โดน fail2ban\n"
            "Panel Settings → Security → API Tokens\n"
            "จากนั้นเพิ่ม `XUI_API_TOKEN` ใน Railway Variables"
        )

    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(
                chat_id=admin_id,
                text=msg,
                parse_mode="MarkdownV2",
            )
            logger.info(f"📨 ส่งแจ้งเตือน XUI ไม่พร้อมใช้งาน → admin {admin_id}")
        except Exception as e:
            logger.warning(f"⚠️  ส่งแจ้งเตือน admin {admin_id} ไม่ได้: {e}")


def main():
    db.init_db()
    from web_admin import start_web_server
    start_web_server()

    if not xui.login():
        logger.warning("⚠️  ไม่สามารถ login เข้า 3x-ui ได้ กรุณาตรวจสอบ XUI_URL, XUI_USERNAME, XUI_PASSWORD")

    # ✅ FIX: ย้าย timeouts มาที่ ApplicationBuilder (ไม่ใช้ run_polling)
    # แก้ PTBDeprecationWarning ที่เกิดขึ้นทุกครั้งตอน start
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .get_updates_connect_timeout(15)
        .get_updates_read_timeout(15)
        .get_updates_write_timeout(15)
        .get_updates_pool_timeout(15)
        .post_init(_on_startup)          # ✅ แจ้ง admin เมื่อ XUI ไม่ตอบสนอง
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addclient", addclient_start)],
        states={
            CHOOSE_NETWORK: [
                CallbackQueryHandler(choose_network, pattern=r"^network_\d+_\d+$"),
                CallbackQueryHandler(cancel_addclient_callback, pattern=r"^cancel_addclient_\d+$"),
            ],
            SELECT_PACKAGE: [
                CallbackQueryHandler(choose_package, pattern=r"^package_\d+_\d+$"),
                CallbackQueryHandler(back_to_networks, pattern=r"^backnet_\d+$"),
                CallbackQueryHandler(cancel_addclient_callback, pattern=r"^cancel_addclient_\d+$"),
            ],
            ENTER_NAME: [
                CallbackQueryHandler(cancel_addclient_callback, pattern=r"^cancel_addclient_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name),
            ],
            ENTER_DAYS: [
                CallbackQueryHandler(cancel_addclient_callback, pattern=r"^cancel_addclient_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_days),
            ],
            ENTER_GB: [
                CallbackQueryHandler(cancel_addclient_callback, pattern=r"^cancel_addclient_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_gb),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_addclient_callback, pattern=r"^cancel_addclient_\d+$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,  # ✅ FIX: suppress PTBUserWarning
    )

    free_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("freeclient", freeclient_start)],
        states={
            FREE_CHOOSE_NETWORK: [
                CallbackQueryHandler(free_choose_network, pattern=r"^free_network_(ais|true)_\d+$"),
                CallbackQueryHandler(cancel_freeclient_callback, pattern=r"^cancel_freeclient_\d+$"),
            ],
            FREE_ENTER_NAME: [
                CallbackQueryHandler(cancel_freeclient_callback, pattern=r"^cancel_freeclient_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, free_enter_name),
            ],
            FREE_ENTER_GB: [
                CallbackQueryHandler(cancel_freeclient_callback, pattern=r"^cancel_freeclient_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, free_enter_gb),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_freeclient_callback, pattern=r"^cancel_freeclient_\d+$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,  # ✅ FIX: suppress PTBUserWarning
    )

    enter_code_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("Enterthecode", enter_code_start),
            CommandHandler("enterthecode", enter_code_start),
        ],
        states={
            ENTER_CODE_VALUE: [
                CallbackQueryHandler(cancel_entercode_callback, pattern=r"^cancel_entercode_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_code_value),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_entercode_callback, pattern=r"^cancel_entercode_\d+$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    addmycredit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addmycredit", addmycredit_start)],
        states={
            ADD_MYCREDIT_LINK: [
                CallbackQueryHandler(cancel_addmycredit_callback, pattern=r"^cancel_addmycredit_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addmycredit_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_addmycredit),
            CallbackQueryHandler(cancel_addmycredit_callback, pattern=r"^cancel_addmycredit_\d+$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    addcode_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addcode", addcode_start)],
        states={
            ADD_CODE_TYPE: [
                CallbackQueryHandler(addcode_type_callback, pattern=r"^addcode_type_(credit|free_reset)_\d+$"),
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
            ],
            ADD_CODE_NAME: [
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcode_enter_name),
            ],
            ADD_CODE_MODE: [
                CallbackQueryHandler(addcode_mode_callback, pattern=r"^addcode_mode_(fixed|random)_\d+$"),
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
            ],
            ADD_CODE_FIXED_AMOUNT: [
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcode_fixed_amount),
            ],
            ADD_CODE_FIXED_MAX_USERS: [
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcode_fixed_max_users),
            ],
            ADD_CODE_FIXED_DURATION: [
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcode_duration),
            ],
            ADD_CODE_RANDOM_TOTAL: [
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcode_random_total),
            ],
            ADD_CODE_RANDOM_DURATION: [
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcode_duration),
            ],
            ADD_CODE_UNIT: [
                CallbackQueryHandler(addcode_unit_callback, pattern=r"^addcode_unit_(s|m|h|d|mo|y)_\d+$"),
                CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_addcode_callback, pattern=r"^cancel_addcode_\d+$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    # Public commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("version", version_cmd))
    app.add_handler(CommandHandler("mycredit", mycredit))
    app.add_handler(CommandHandler("mycodes", mycodes))
    app.add_handler(CommandHandler("checkprice", checkprice))
    app.add_handler(conv_handler)
    app.add_handler(free_conv_handler)
    app.add_handler(enter_code_conv_handler)
    app.add_handler(addmycredit_conv_handler)
    app.add_handler(addcode_conv_handler)

    # Admin commands (case-insensitive variants)
    app.add_handler(CommandHandler("addcredits", addcredits))
    app.add_handler(CommandHandler("Deletecredits", deletecredits))
    app.add_handler(CommandHandler("deletecredits", deletecredits))
    app.add_handler(CommandHandler("setprice", setprice))
    app.add_handler(CommandHandler("setangpaophone", setangpaophone))
    app.add_handler(CommandHandler("checkangpaophone", checkangpaophone))
    app.add_handler(CommandHandler("setangpaorate", setangpaorate))
    # ── /Settingsmycredit ConversationHandler ───────────────────────────────
    smc_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("Settingsmycredit", settingsmycredit_cmd),
            CommandHandler("settingsmycredit", settingsmycredit_cmd),
        ],
        states={
            SMC_HUB: [
                CallbackQueryHandler(smc_hub_callback, pattern=r"^smc_(enable|disable|check|channel|set_phone|set_rate|back|close|ch_.+)$"),
            ],
            SMC_ENTER_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, smc_enter_phone),
                CallbackQueryHandler(smc_hub_callback, pattern=r"^smc_(back|close)$"),
            ],
            SMC_ENTER_RATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, smc_enter_rate),
                CallbackQueryHandler(smc_hub_callback, pattern=r"^smc_(back|close)$"),
            ],
            SMC_ENTER_CHANNEL_IDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, smc_enter_channel_ids),
                CallbackQueryHandler(smc_hub_callback, pattern=r"^smc_(back|close)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", smc_cancel)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )
    app.add_handler(smc_conv_handler)
    app.add_handler(CommandHandler("startadmin", startadmin))
    app.add_handler(CommandHandler("setinbound", setinbound))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^admin\|"))
    app.add_handler(CommandHandler("runstartflnish", runstartflnish))
    app.add_handler(CommandHandler("stopstartflnish", stopstartflnish))
    app.add_handler(CommandHandler("addrunstartflnish", addrunstartflnish))
    app.add_handler(CommandHandler("deleterunstartflnish", deleterunstartflnish))
    app.add_handler(CommandHandler("open", open_setting))
    app.add_handler(CommandHandler("close", close_setting))
    app.add_handler(CommandHandler("buydm", buydm))
    app.add_handler(CommandHandler("nobuydm", nobuydm))
    app.add_handler(CommandHandler("freeclientlimit", freeclientlimit))
    app.add_handler(CommandHandler("freeclienttime", freeclienttime))
    app.add_handler(CommandHandler("freeclientResettime", freeclient_resettime))
    app.add_handler(CommandHandler("freeclientresettime", freeclient_resettime))
    app.add_handler(CommandHandler("resetfreeclientlimit", resetfreeclientlimit))
    app.add_handler(CommandHandler("openfreeclient", openfreeclient))
    app.add_handler(CommandHandler("offfreeclient", offfreeclient))
    app.add_handler(CallbackQueryHandler(freeclient_resettime_callback, pattern=r"^free_reset_mode_(midnight|rolling_24h)_\d+$"))

    # Log & display-limit commands (Features 1–4)
    app.add_handler(CommandHandler("logbuy", logbuy))
    app.add_handler(CommandHandler("logfree", logfree))
    app.add_handler(CommandHandler("logbuyall", logbuyall))
    app.add_handler(CommandHandler("logfreeall", logfreeall))
    app.add_handler(CommandHandler("listaddclient", listaddclient))
    app.add_handler(CommandHandler("listfreeclient", listfreeclient))

    # Fallback handler — ต้องลงทะเบียนหลังสุดเสมอ
    # รับ callback query ที่ไม่มี handler อื่นรับ → แจ้งผู้ใช้ว่าไม่สามารถยุ่งได้
    # Addclient toggle
    app.add_handler(CommandHandler("toggleaddclient", toggleaddclient))
    app.add_handler(CallbackQueryHandler(addclient_toggle_callback, pattern=r"^addclient_toggle_(on|off)$"))
    app.add_handler(CommandHandler("Sorting", sorting_cmd))
    app.add_handler(CommandHandler("sorting", sorting_cmd))
    app.add_handler(CallbackQueryHandler(sorting_callback, pattern=r"^sorting_(newest_top|newest_bottom)$"))
    app.add_handler(CommandHandler("Purchaseinformation", purchaseinformation))
    app.add_handler(CommandHandler("purchaseinformation", purchaseinformation))
    app.add_handler(CommandHandler("Showpurchaselist", showpurchaselist))
    app.add_handler(CommandHandler("showpurchaselist", showpurchaselist))
    app.add_handler(CommandHandler("deletecode", deletecode))
    app.add_handler(CommandHandler("checkcode", checkcode))
    app.add_handler(CommandHandler("statuscode", statuscode))
    app.add_handler(CommandHandler("checkusercode", checkusercode))
    app.add_handler(CallbackQueryHandler(statuscode_callback, pattern=r"^creditcode_status_(on|off)$"))

    # ── channelfreeclient ConversationHandler ────────────────────────────────
    channel_free_handler = ConversationHandler(
        entry_points=[CommandHandler("channelfreeclient", channelfreeclient)],
        states={
            CFREE_ENTER_IDS: [
                CallbackQueryHandler(cfree_choose_callback, pattern=r"^cfree_(dm_and_group|group_only|specified_and_dm|specified_only|dm_only)_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, cfree_enter_ids),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )


    # ✅ v23 Fix: channel_free_handler ถูกสร้างแต่ไม่เคย add → /channelfreeclient พัง
    app.add_handler(channel_free_handler)

    app.add_handler(CallbackQueryHandler(handle_foreign_callback))


    # ── Register keep-alive job ─────────────────────────────────────────
    # ใช้ interval = SESSION_REFRESH_MINUTES (50 นาที)
    # API Token mode: keep_alive จะ re-verify token ทุก 50 นาที (lightweight GET request)
    # Session mode: keep_alive จะ re-login ทุก 50 นาที ก่อน session 60 นาทีหมด
    from xui_api import SESSION_REFRESH_MINUTES as _SRM
    app.job_queue.run_repeating(
        _xui_keepalive_job,
        interval=_SRM * 60,    # 50 นาที (วินาที)
        first=_SRM * 60,       # เริ่มครั้งแรกหลัง 50 นาที
        name="xui_keepalive",
    )
    app.job_queue.run_repeating(
        _expired_purchase_cleanup_job, interval=6 * 60 * 60, first=5 * 60,
        name="expired_purchase_cleanup",
    )

    app.add_error_handler(telegram_error_handler)
    logger.info("🤖 Bio-shop Bot กำลังเริ่มต้น... BUILD=%s", BUILD_VERSION)
    # ✅ FIX: ลบ timeout params ออกจาก run_polling (ย้ายไป ApplicationBuilder แล้ว)
    # drop_pending_updates=True ลด Conflict 409 ตอน redeploy บน Railway
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
