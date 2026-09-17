import os
import random
import sqlite3
import datetime
from typing import Optional, List, Dict, Any

_TZ_THAI = datetime.timezone(datetime.timedelta(hours=7))

def _now_th_str() -> str:
    return datetime.datetime.now(_TZ_THAI).strftime("%d/%m/%Y %H:%M:%S")

DB_PATH = os.getenv("DB_PATH", "/data/bot.db")

# ── จำนวนรายการ log สูงสุดที่เก็บต่อ user ต่อประเภท (เปลี่ยนได้ที่นี่) ──────
LOG_MAX_ENTRIES = 100


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrency
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            credit     REAL    NOT NULL DEFAULT 0,
            dm_started INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            uuid        TEXT    NOT NULL,
            inbound_id  INTEGER NOT NULL,
            network     TEXT    NOT NULL,
            expire_date TEXT    NOT NULL,
            gb_limit    REAL    NOT NULL DEFAULT 0,
            link        TEXT    NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS buy_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            username   TEXT,
            code_name  TEXT    NOT NULL,
            network    TEXT    NOT NULL,
            days       INTEGER NOT NULL DEFAULT 0,
            gb         REAL    NOT NULL DEFAULT 0,
            cost       REAL    NOT NULL DEFAULT 0,
            link       TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS free_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            username   TEXT,
            code_name  TEXT    NOT NULL,
            network    TEXT    NOT NULL,
            hours      REAL    NOT NULL DEFAULT 1,
            gb         REAL    NOT NULL DEFAULT 0,
            link       TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS credit_codes (
            code_key           TEXT PRIMARY KEY,
            code_name          TEXT    NOT NULL,
            mode               TEXT    NOT NULL,
            fixed_credit       REAL    NOT NULL DEFAULT 0,
            total_credit       REAL    NOT NULL DEFAULT 0,
            distributed_credit REAL    NOT NULL DEFAULT 0,
            max_uses           INTEGER NOT NULL DEFAULT 0,
            used_count         INTEGER NOT NULL DEFAULT 0,
            expires_at         TEXT    NOT NULL,
            created_at         TEXT    NOT NULL,
            created_by         INTEGER,
            active             INTEGER NOT NULL DEFAULT 1
        );


        CREATE TABLE IF NOT EXISTS freeclient_limit_resets (
            user_id  INTEGER PRIMARY KEY,
            reset_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS credit_code_redemptions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            code_key      TEXT    NOT NULL,
            code_name     TEXT    NOT NULL,
            user_id       INTEGER NOT NULL,
            username      TEXT,
            credit_amount REAL    NOT NULL,
            redeemed_at   TEXT    NOT NULL,
            FOREIGN KEY (code_key) REFERENCES credit_codes(code_key) ON DELETE CASCADE,
            UNIQUE (code_key, user_id)
        );

        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            days        INTEGER NOT NULL CHECK(days > 0),
            gb_limit    REAL NOT NULL DEFAULT 0 CHECK(gb_limit >= 0),
            price       REAL NOT NULL CHECK(price >= 0),
            network     TEXT NOT NULL DEFAULT 'BOTH' CHECK(network IN ('AIS','TRUE','BOTH')),
            active      INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS networks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name        TEXT NOT NULL,
            inbound_id  INTEGER NOT NULL CHECK(inbound_id > 0),
            active      INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 10 CHECK(sort_order > 0),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            actor       TEXT NOT NULL,
            action      TEXT NOT NULL,
            target      TEXT NOT NULL DEFAULT '',
            old_value   TEXT NOT NULL DEFAULT '',
            new_value   TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_buy_log_created ON buy_log(id DESC);
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

        CREATE TABLE IF NOT EXISTS truemoney_redemptions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_hash  TEXT    NOT NULL UNIQUE,
            user_id       INTEGER NOT NULL,
            username      TEXT,
            phone         TEXT    NOT NULL,
            amount_baht   REAL    NOT NULL,
            credit_amount REAL    NOT NULL,
            status_code   TEXT    NOT NULL DEFAULT 'SUCCESS',
            redeemed_at   TEXT    NOT NULL,
            raw_response  TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        """
    )

    # Default settings (existing)
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('price_per_day', '2')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('run_start_finish_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('run_start_finish_commands', 'addclient')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('cancel_button_enabled', '0')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('buy_dm_enabled', '0')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('buy_group_ids', '')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('run_start_finish_delay_seconds', '5')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('freeclient_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('freeclient_hours', '1')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('freeclient_daily_limit', '1')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('freeclient_reset_mode', 'midnight')")
    # Feature 3 & 4: จำนวนรายการที่แสดง (ค่าเริ่มต้น = LOG_MAX_ENTRIES)
    conn.execute(f"INSERT OR IGNORE INTO settings VALUES ('log_display_limit_buy', '{LOG_MAX_ENTRIES}')")
    conn.execute(f"INSERT OR IGNORE INTO settings VALUES ('log_display_limit_free', '{LOG_MAX_ENTRIES}')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('addclient_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('mycodes_store_limit', '100')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('mycodes_display_limit', '100')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('mycodes_sort_order', 'newest_bottom')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('credit_code_enabled', '1')")
    # freeclient channel mode
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('freeclient_channel_mode', 'dm_and_group')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('freeclient_group_ids', '')")
    conn.execute(
        "INSERT OR IGNORE INTO settings VALUES ('truemoney_wallet_phone', ?)",
        (os.getenv("TRUEMONEY_WALLET_PHONE", "").strip(),),
    )
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('truemoney_credit_rate', '1')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('truemoney_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('truemoney_channel_mode', 'dm_only')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('truemoney_group_ids', '')")
    # เครือข่ายเริ่มต้นต้องมี AIS/TRUE และห้ามใช้ inbound_id หรือลำดับเป็น 0
    if conn.execute("SELECT COUNT(*) AS n FROM networks").fetchone()["n"] == 0:
        ais_id = max(1, int(os.getenv("AIS_INBOUND_ID", "1")))
        true_id = max(1, int(os.getenv("TRUE_INBOUND_ID", "2")))
        conn.executemany(
            "INSERT INTO networks (code, name, inbound_id, active, sort_order) VALUES (?, ?, ?, 1, ?)",
            [("AIS", "AIS", ais_id, 10), ("TRUE", "TRUE", true_id, 20)],
        )
    conn.commit()

    # Migration: v4.6 เปลี่ยนค่าเริ่มต้นการซื้อเป็น /nobuydm สำหรับผู้ใช้ทั่วไป
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'buy_dm_default_migrated_v46'"
    ).fetchone()
    if row is None:
        conn.execute("UPDATE settings SET value = '0' WHERE key = 'buy_dm_enabled'")
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('buy_dm_default_migrated_v46', '1')")
        conn.commit()

    # Migration: เพิ่มคอลัมน์ dm_started สำหรับฐานข้อมูลเก่าที่ยังไม่มีคอลัมน์นี้
    try:
        conn.execute("ALTER TABLE users ADD COLUMN dm_started INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # คอลัมน์มีอยู่แล้ว ข้ามได้เลย

    conn.close()


# ── User helpers ──────────────────────────────────────────────────────────────

def ensure_user(user_id: int, username: Optional[str]):
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (user_id, username),
    )
    if username:
        conn.execute(
            "UPDATE users SET username = ? WHERE user_id = ?",
            (username, user_id),
        )
    conn.commit()
    conn.close()


def get_credit(user_id: int) -> float:
    conn = _get_conn()
    row = conn.execute(
        "SELECT credit FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return float(row["credit"]) if row else 0.0


def add_credit(user_id: int, amount: float):
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, credit) VALUES (?, NULL, 0)",
        (user_id,),
    )
    conn.execute(
        "UPDATE users SET credit = credit + ? WHERE user_id = ?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()


def deduct_credit(user_id: int, amount: float):
    """Deduct credit; floor at 0 so balance never goes negative."""
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, credit) VALUES (?, NULL, 0)",
        (user_id,),
    )
    conn.execute(
        "UPDATE users SET credit = MAX(0, credit - ?) WHERE user_id = ?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()


def try_deduct_credit(user_id: int, amount: float) -> bool:
    """
    Atomically check credit balance and deduct in a single BEGIN IMMEDIATE transaction.
    Returns True on success, False if credit is insufficient.

    ป้องกัน race condition กรณีคำสั่งพร้อมกัน 2 คำสั่ง (e.g., /addclient concurrent requests):
    SQLite BEGIN IMMEDIATE จะล็อก write lock ทันที ทำให้ transaction ที่สองต้องรอ
    ไม่มีทางที่ทั้งคู่ผ่าน credit check พร้อมกันได้
    """
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, credit) VALUES (?, NULL, 0)",
            (user_id,),
        )
        row = conn.execute(
            "SELECT credit FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        balance = float(row["credit"]) if row else 0.0
        if balance < amount:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE users SET credit = credit - ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_id_by_username(username: str) -> Optional[int]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return int(row["user_id"]) if row else None


def get_username(user_id: int) -> Optional[str]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT username FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["username"] if row else None


# ── DM-started helpers ────────────────────────────────────────────────────────

def set_dm_started(user_id: int):
    """บันทึกว่า user นี้เคย /start บอทใน DM ส่วนตัวแล้ว"""
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET dm_started = 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_dm_started_users() -> list:
    """คืนรายการ user_id ทั้งหมดที่เคย /start บอทใน DM"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT user_id FROM users WHERE dm_started = 1"
    ).fetchall()
    conn.close()
    return [row["user_id"] for row in rows]


def has_dm_started(user_id: int) -> bool:
    """คืนค่า True ถ้า user เคย /start บอทใน DM ส่วนตัวมาก่อน"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT dm_started FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return bool(row["dm_started"]) if row else False


def get_all_users() -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT user_id, username, credit FROM users ORDER BY credit DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Client / code helpers ─────────────────────────────────────────────────────

def code_name_exists(user_id: int, name: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM clients WHERE user_id = ? AND name = ?",
        (user_id, name),
    ).fetchone()
    conn.close()
    return row is not None


def save_code(
    user_id: int,
    name: str,
    uuid: str,
    inbound_id: int,
    network: str,
    expire_date: str,
    gb_limit: float,
    link: str,
):
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO clients
            (user_id, name, uuid, inbound_id, network, expire_date, gb_limit, link)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, name, uuid, inbound_id, network, expire_date, gb_limit, link),
    )
    # Rolling window: เก็บแค่ mycodes_store_limit รายการล่าสุดต่อ user
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'mycodes_store_limit'"
        ).fetchone()
        store_limit = max(1, int(float(row["value"]))) if row else 100
    except Exception:
        store_limit = 100
    conn.execute(
        """DELETE FROM clients WHERE user_id = ?
           AND id NOT IN (
               SELECT id FROM clients WHERE user_id = ?
               ORDER BY id DESC LIMIT ?
           )""",
        (user_id, user_id, store_limit),
    )
    conn.commit()
    conn.close()


def get_user_codes(
    user_id: int,
    sort_order: str = "newest_bottom",
    display_limit: int = 100,
) -> List[Dict]:
    """
    ดึงโค้ดของ user
    - sort_order: 'newest_bottom' (เก่า→ใหม่, ใหม่อยู่ล่าง) หรือ 'newest_top' (ใหม่→เก่า, ใหม่อยู่บน)
    - display_limit: จำนวนรายการที่แสดง (ไม่เกินที่เก็บจริง)
    """
    conn = _get_conn()
    if sort_order == "newest_top":
        rows = conn.execute(
            """SELECT * FROM clients WHERE user_id = ?
               ORDER BY id DESC LIMIT ?""",
            (user_id, display_limit),
        ).fetchall()
    else:  # newest_bottom — ใหม่สุดอยู่ล่างสุด
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT * FROM clients WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?
               ) ORDER BY id ASC""",
            (user_id, display_limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Settings helpers ──────────────────────────────────────────────────────────

def get_setting(key: str, default: Any = None) -> Any:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return float(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def get_setting_text(key: str, default: str = "") -> str:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return str(row["value"]) if row is not None else default


def set_setting(key: str, value: str):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


# ── Credit-code helpers ───────────────────────────────────────────────────────

def credit_code_exists(code_key: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM credit_codes WHERE code_key = ?",
        (code_key,),
    ).fetchone()
    conn.close()
    return row is not None


def create_credit_code(
    code_key: str,
    code_name: str,
    mode: str,
    fixed_credit: float,
    total_credit: float,
    max_uses: int,
    expires_at: str,
    created_by: int,
    created_at: str,
) -> bool:
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO credit_codes
                (code_key, code_name, mode, fixed_credit, total_credit,
                 max_uses, expires_at, created_at, created_by, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                code_key,
                code_name,
                mode,
                fixed_credit,
                total_credit,
                max_uses,
                expires_at,
                created_at,
                created_by,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def delete_credit_code(code_key: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM credit_codes WHERE code_key = ?", (code_key,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_credit_code(code_key: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM credit_codes WHERE code_key = ?",
        (code_key,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_credit_codes() -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM credit_codes ORDER BY created_at DESC, code_name ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_credit_code_redemptions(code_key: str) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM credit_code_redemptions
        WHERE code_key = ?
        ORDER BY id ASC
        """,
        (code_key,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def redeem_credit_code(
    code_key: str,
    user_id: int,
    username: Optional[str],
    redeemed_at: str,
) -> Dict[str, Any]:
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, credit) VALUES (?, ?, 0)",
            (user_id, username),
        )
        if username:
            conn.execute(
                "UPDATE users SET username = ? WHERE user_id = ?",
                (username, user_id),
            )

        code = conn.execute(
            "SELECT * FROM credit_codes WHERE code_key = ?",
            (code_key,),
        ).fetchone()
        if code is None or int(code["active"]) != 1:
            conn.rollback()
            return {"status": "not_found"}

        if str(code["expires_at"]) <= redeemed_at:
            conn.rollback()
            return {"status": "expired", "code": dict(code)}

        used = conn.execute(
            """
            SELECT 1 FROM credit_code_redemptions
            WHERE code_key = ? AND user_id = ?
            """,
            (code_key, user_id),
        ).fetchone()
        if used is not None:
            conn.rollback()
            return {"status": "already_used", "code": dict(code)}

        max_uses = int(code["max_uses"])
        used_count = int(code["used_count"])
        if max_uses > 0 and used_count >= max_uses:
            conn.rollback()
            return {"status": "used_up", "code": dict(code)}

        mode = str(code["mode"])
        if mode == "free_reset":
            credit_amount = 0.0
            conn.execute(
                """
                INSERT INTO credit_code_redemptions
                    (code_key, code_name, user_id, username, credit_amount, redeemed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    code_key,
                    code["code_name"],
                    user_id,
                    username,
                    credit_amount,
                    redeemed_at,
                ),
            )
            conn.execute(
                """
                UPDATE credit_codes
                SET used_count = used_count + 1
                WHERE code_key = ?
                """,
                (code_key,),
            )
            conn.execute(
                """
                INSERT INTO freeclient_limit_resets (user_id, reset_at)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET reset_at = excluded.reset_at
                """,
                (user_id, redeemed_at),
            )
            conn.commit()
            return {
                "status": "ok",
                "code": dict(code),
                "code_type": "free_reset",
                "reset_at": redeemed_at,
                "credit_amount": credit_amount,
            }

        if mode == "fixed":
            credit_amount = float(code["fixed_credit"])
            if credit_amount <= 0:
                conn.rollback()
                return {"status": "used_up", "code": dict(code)}
        else:
            total_credit = float(code["total_credit"])
            distributed_credit = float(code["distributed_credit"])
            remaining_credit = round(total_credit - distributed_credit, 2)
            if remaining_credit <= 0:
                conn.rollback()
                return {"status": "used_up", "code": dict(code)}

            remaining_cents = max(1, int(round(remaining_credit * 100)))
            credit_amount = random.randint(1, remaining_cents) / 100
            credit_amount = min(round(credit_amount, 2), remaining_credit)

        conn.execute(
            """
            INSERT INTO credit_code_redemptions
                (code_key, code_name, user_id, username, credit_amount, redeemed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                code_key,
                code["code_name"],
                user_id,
                username,
                credit_amount,
                redeemed_at,
            ),
        )
        conn.execute(
            """
            UPDATE credit_codes
            SET used_count = used_count + 1,
                distributed_credit = distributed_credit + ?
            WHERE code_key = ?
            """,
            (credit_amount, code_key),
        )
        conn.execute(
            "UPDATE users SET credit = credit + ? WHERE user_id = ?",
            (credit_amount, user_id),
        )
        balance = conn.execute(
            "SELECT credit FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.commit()
        return {
            "status": "ok",
            "code": dict(code),
            "credit_amount": credit_amount,
            "balance": float(balance["credit"]) if balance else credit_amount,
        }
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"status": "already_used"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_truemoney_credit(
    user_id: int,
    username: Optional[str],
    voucher_hash: str,
    phone: str,
    amount_baht: float,
    credit_amount: float,
    status_code: str,
    redeemed_at: str,
    raw_response: str = "",
) -> Dict[str, Any]:
    """
    บันทึกซองอั่งเปาและเพิ่มเครดิตใน transaction เดียวกัน
    คืน status duplicate ถ้าซองนี้เคยถูกบันทึกแล้ว เพื่อกันการเติมซ้ำในฐานข้อมูลบอท
    """
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, credit) VALUES (?, ?, 0)",
            (user_id, username),
        )
        if username:
            conn.execute(
                "UPDATE users SET username = ? WHERE user_id = ?",
                (username, user_id),
            )

        conn.execute(
            """
            INSERT INTO truemoney_redemptions
                (voucher_hash, user_id, username, phone, amount_baht, credit_amount,
                 status_code, redeemed_at, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                voucher_hash,
                user_id,
                username,
                phone,
                amount_baht,
                credit_amount,
                status_code,
                redeemed_at,
                raw_response,
            ),
        )
        conn.execute(
            "UPDATE users SET credit = credit + ? WHERE user_id = ?",
            (credit_amount, user_id),
        )
        balance = conn.execute(
            "SELECT credit FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.commit()
        return {
            "status": "ok",
            "balance": float(balance["credit"]) if balance else credit_amount,
        }
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"status": "duplicate"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Log helpers ───────────────────────────────────────────────────────────────

def add_buy_log(
    user_id: int,
    username: Optional[str],
    code_name: str,
    network: str,
    days: int,
    gb: float,
    cost: float,
    link: str,
    created_at: str,
):
    """
    บันทึก log การซื้อ /addclient
    เก็บสูงสุด LOG_MAX_ENTRIES รายการต่อ user — อันเก่าสุดจะถูกลบเมื่อเกินลิมิต
    """
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO buy_log (user_id, username, code_name, network, days, gb, cost, link, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, code_name, network, days, gb, cost, link, created_at),
    )
    # ลบรายการเกิน LOG_MAX_ENTRIES (เก็บ id ล่าสุด LOG_MAX_ENTRIES รายการ, ลบที่เหลือ)
    conn.execute(
        """
        DELETE FROM buy_log
        WHERE user_id = ?
          AND id NOT IN (
              SELECT id FROM buy_log
              WHERE user_id = ?
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (user_id, user_id, LOG_MAX_ENTRIES),
    )
    conn.commit()
    conn.close()


def get_buy_log(user_id: int, display_limit: int = LOG_MAX_ENTRIES) -> List[Dict]:
    """
    ดึง log การซื้อของ user
    - เก็บสูงสุด LOG_MAX_ENTRIES แต่แสดงเพียง display_limit รายการล่าสุด
    - เรียงจากเก่า → ใหม่ (รายการล่าสุดอยู่ด้านล่างสุดของข้อความ)
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT * FROM buy_log
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY id ASC
        """,
        (user_id, display_limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_free_log(
    user_id: int,
    username: Optional[str],
    code_name: str,
    network: str,
    hours: float,
    gb: float,
    link: str,
    created_at: str,
):
    """
    บันทึก log การทดลอง /freeclient
    เก็บสูงสุด LOG_MAX_ENTRIES รายการต่อ user — อันเก่าสุดจะถูกลบเมื่อเกินลิมิต
    """
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO free_log (user_id, username, code_name, network, hours, gb, link, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, code_name, network, hours, gb, link, created_at),
    )
    conn.execute(
        """
        DELETE FROM free_log
        WHERE user_id = ?
          AND id NOT IN (
              SELECT id FROM free_log
              WHERE user_id = ?
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (user_id, user_id, LOG_MAX_ENTRIES),
    )
    conn.commit()
    conn.close()


def get_free_log(user_id: int, display_limit: int = LOG_MAX_ENTRIES) -> List[Dict]:
    """
    ดึง log การทดลองของ user
    - เก็บสูงสุด LOG_MAX_ENTRIES แต่แสดงเพียง display_limit รายการล่าสุด
    - เรียงจากเก่า → ใหม่ (รายการล่าสุดอยู่ด้านล่างสุดของข้อความ)
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT * FROM free_log
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY id ASC
        """,
        (user_id, display_limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_freeclient_limit_reset(user_id: int, reset_at: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, credit) VALUES (?, NULL, 0)",
        (user_id,),
    )
    conn.execute(
        """
        INSERT INTO freeclient_limit_resets (user_id, reset_at)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET reset_at = excluded.reset_at
        """,
        (user_id, reset_at),
    )
    conn.commit()
    conn.close()


def get_freeclient_limit_reset(user_id: int) -> Optional[str]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT reset_at FROM freeclient_limit_resets WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return str(row["reset_at"]) if row else None


def count_free_log_by_date(user_id: int, date_prefix: str) -> int:
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM free_log
        WHERE user_id = ? AND created_at LIKE ?
        """,
        (user_id, f"{date_prefix}%"),
    ).fetchone()
    conn.close()
    return int(row["total"]) if row else 0


def get_buy_log_all(display_limit: int = LOG_MAX_ENTRIES) -> List[Dict]:
    """
    ดึง log การซื้อของผู้ใช้ทุกคนรวมกัน (/logbuyall)
    - เรียงจากเก่า → ใหม่ (รายการล่าสุดอยู่ด้านล่างสุดของข้อความ)
    - จำกัดจำนวนรายการตาม display_limit
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT * FROM buy_log
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY id ASC
        """,
        (display_limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_free_log_all(display_limit: int = LOG_MAX_ENTRIES) -> List[Dict]:
    """
    ดึง log การทดลองฟรีของผู้ใช้ทุกคนรวมกัน (/logfreeall)
    - เรียงจากเก่า → ใหม่ (รายการล่าสุดอยู่ด้านล่างสุดของข้อความ)
    - จำกัดจำนวนรายการตาม display_limit
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT * FROM free_log
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY id ASC
        """,
        (display_limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]



# ── Network helpers (editable from web, read live by Telegram) ────────────────
def get_networks(active_only: bool = True) -> List[Dict]:
    conn = _get_conn()
    sql = "SELECT * FROM networks"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order ASC, id ASC"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_network(network_id: int, active_only: bool = True) -> Optional[Dict]:
    conn = _get_conn()
    sql = "SELECT * FROM networks WHERE id = ?" + (" AND active = 1" if active_only else "")
    row = conn.execute(sql, (network_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_network(code: str, name: str, inbound_id: int, active: bool, sort_order: int) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO networks (code, name, inbound_id, active, sort_order) VALUES (?, ?, ?, ?, ?)",
        (code.strip().upper(), name.strip(), inbound_id, int(active), sort_order),
    )
    conn.commit(); network_id = int(cur.lastrowid); conn.close()
    return network_id


def update_network(network_id: int, code: str, name: str, inbound_id: int,
                   active: bool, sort_order: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        """UPDATE networks SET code=?, name=?, inbound_id=?, active=?, sort_order=?,
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (code.strip().upper(), name.strip(), inbound_id, int(active), sort_order, network_id),
    )
    conn.commit(); changed = cur.rowcount > 0; conn.close()
    return changed


def delete_network(network_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM networks WHERE id = ?", (network_id,))
    conn.commit(); changed = cur.rowcount > 0; conn.close()
    return changed


# ── Web control / product helpers ─────────────────────────────────────────────
def get_products(active_only: bool = True) -> List[Dict]:
    conn = _get_conn()
    sql = "SELECT * FROM products"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order ASC, id ASC"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product(product_id: int, active_only: bool = True) -> Optional[Dict]:
    conn = _get_conn()
    sql = "SELECT * FROM products WHERE id = ?" + (" AND active = 1" if active_only else "")
    row = conn.execute(sql, (product_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_product(name: str, description: str, days: int, gb_limit: float,
                   price: float, network: str, active: bool, sort_order: int) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO products (name, description, days, gb_limit, price, network, active, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name.strip(), description.strip(), days, gb_limit, price, network, int(active), sort_order),
    )
    conn.commit()
    product_id = int(cur.lastrowid)
    conn.close()
    return product_id


def update_product(product_id: int, name: str, description: str, days: int,
                   gb_limit: float, price: float, network: str, active: bool,
                   sort_order: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        """UPDATE products SET name=?, description=?, days=?, gb_limit=?, price=?,
           network=?, active=?, sort_order=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (name.strip(), description.strip(), days, gb_limit, price, network, int(active), sort_order, product_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_product(product_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def search_users(query: str = "", limit: int = 100) -> List[Dict]:
    conn = _get_conn()
    query = query.strip().lstrip("@")
    if query:
        like = f"%{query}%"
        rows = conn.execute(
            """SELECT user_id, username, credit, dm_started, created_at FROM users
               WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ?
               ORDER BY id IS NULL, credit DESC LIMIT ?""".replace("ORDER BY id IS NULL, ", "ORDER BY "),
            (like, like, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT user_id, username, credit, dm_started, created_at FROM users ORDER BY credit DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_audit_log(actor: str, action: str, target: str = "", old_value: str = "", new_value: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO audit_log (actor, action, target, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
        (str(actor)[:100], str(action)[:100], str(target)[:200], str(old_value)[:2000], str(new_value)[:2000]),
    )
    conn.commit()
    conn.close()


def get_audit_logs(limit: int = 100) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_dashboard_overview() -> Dict[str, Any]:
    conn = _get_conn()
    users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    orders = conn.execute("SELECT COUNT(*) AS n FROM buy_log").fetchone()["n"]
    revenue = conn.execute("SELECT COALESCE(SUM(cost), 0) AS n FROM buy_log").fetchone()["n"]
    active_networks = conn.execute("SELECT COUNT(*) AS n FROM networks WHERE active=1").fetchone()["n"]
    recent = conn.execute("SELECT * FROM buy_log ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    return {"users": users, "orders": orders, "revenue": float(revenue),
            "active_networks": active_networks, "active_products": active_networks, "recent_orders": [dict(r) for r in recent]}
