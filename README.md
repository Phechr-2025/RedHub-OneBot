# Bio-shop Bot v33.2 — Telegram + Web Control

## Hotfix v33.2

- เพิ่ม Build marker `v33.2-hotfix` ใน Startup log และคำสั่ง `/version` เพื่อตรวจว่า Railway ใช้ไฟล์ใหม่จริง
- เพิ่ม Telegram error handler และป้องกันข้อความไดนามิกในระบบสินค้าไม่ให้เกิด Markdown parsing error
- ZIP รุ่นนี้วางไฟล์ไว้ที่รากของ ZIP โดยตรง ป้องกัน Deploy ใช้ `main.py` เก่าจากโฟลเดอร์ผิด

## Hotfix v33.1

- แก้ `/startadmin` ขึ้น `Can't parse entities` เมื่อ `PUBLIC_URL` หรือชื่อสินค้ามีอักขระพิเศษ
- เมนูแอดมินและรายการสินค้าไม่ใช้ Telegram Markdown กับข้อมูลแบบไดนามิกแล้ว
- ลดระดับ Log ของ `httpx`/`httpcore` เพื่อไม่ให้ URL Telegram API ซึ่งมี Bot Token ปรากฏใน Console
- หาก Token เคยปรากฏใน Log ต้อง `/revoke` ผ่าน BotFather และเปลี่ยน `BOT_TOKEN` ใหม่


บอทขาย VPN ผ่าน 3x-ui พร้อมเว็บควบคุมที่ใช้ฐานข้อมูลเดียวกับ Telegram แบบทันที

## สิ่งที่เพิ่มในรุ่นนี้

- เว็บควบคุม Responsive: ภาพรวม, ตั้งค่า, สินค้า, ผู้ใช้/เครดิต, ออเดอร์ และ Audit log
- ล็อกอินด้วย Signed HttpOnly session cookie
- จัดการแพ็กเกจ: ชื่อ, รายละเอียด, วัน, GB, ราคา, เครือข่าย, สถานะ และลำดับ
- `/addclient` เลือกแพ็กเกจด้วยปุ่ม จากนั้นเลือกเครือข่าย ตั้งชื่อ และยืนยัน
- `/startadmin` เปลี่ยนเป็น Control Center แบบปุ่ม เปิด/ปิดระบบหลักได้โดยไม่ต้องจำคำสั่ง
- การแก้ผ่านเว็บและ Telegram เขียนลง SQLite `settings`/`products` ชุดเดียวกัน จึงตรงกันทันทีโดยไม่ต้องรีสตาร์ต
- เพิ่ม transaction หักเครดิตแบบ atomic, ตรวจสินค้าอีกครั้งก่อนหักเงิน, คืนเครดิตเมื่อ 3x-ui ล้มเหลว และบันทึก audit
- รองรับ URL จาก IP หรือโดเมน เว็บรับฟังที่ `0.0.0.0:$PORT`

## ไฟล์

```text
main.py           Telegram bot และเมนูปุ่ม
web_admin.py      เว็บไซต์/API ควบคุม
 database.py       SQLite, settings, products, users, logs, audit
xui_api.py        ตัวเชื่อม 3x-ui
requirements.txt  dependencies
railway.toml       Railway start + health check
.env.example      ตัวอย่าง environment variables
```

## Environment variables

คัดลอก `.env.example` เป็น `.env` แล้วแก้ค่าจริง ห้าม commit `.env`

### จำเป็น

| ตัวแปร | ตัวอย่าง | ความหมาย |
|---|---|---|
| `BOT_TOKEN` | `123:abc` | Token จาก BotFather |
| `ADMIN_IDS` | `111111,222222` | Telegram ID แอดมิน |
| `XUI_URL` | `https://panel.example.com:2053/path` | URL 3x-ui รวม secret path |
| `XUI_API_TOKEN` | `...` | API token ของ 3x-ui (แนะนำ) |
| `AIS_INBOUND_ID` | `1` | Inbound สำหรับ AIS |
| `TRUE_INBOUND_ID` | `2` | Inbound สำหรับ TRUE |
| `WEB_ADMIN_PASSWORD` | รหัสยาวสุ่ม | รหัสผ่านเว็บ ห้ามปล่อยว่าง |
| `WEB_ADMIN_SECRET` | random 32+ chars | ใช้เซ็น session cookie |

### เว็บและฐานข้อมูล

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---:|---|
| `WEB_ADMIN_USERNAME` | `admin` | ชื่อผู้ใช้เว็บ |
| `WEB_ADMIN_ENABLED` | `1` | เปิดเว็บควบคุม |
| `WEB_HOST` | `0.0.0.0` | Interface ที่รับ request |
| `PORT` | `8080` | พอร์ตเว็บ (Railway กำหนดให้อัตโนมัติ) |
| `PUBLIC_URL` | ว่าง | URL ที่ปุ่ม “เปิดเว็บไซต์” ในบอทใช้ |
| `DB_PATH` | `/data/bot.db` | ตำแหน่ง SQLite; ต้องอยู่บน persistent volume |
| `WEB_COOKIE_SECURE` | `1` | ใช้ `1` สำหรับ HTTPS; หากเข้า IP ผ่าน HTTP ในเครือข่ายปิดให้ตั้ง `0` |

> การเปิดเว็บสู่ Internet ต้องใช้ HTTPS, รหัสผ่านแข็งแรง, `WEB_COOKIE_SECURE=1` และไม่ควรเปิดพอร์ตฐานข้อมูล

## รันในเครื่อง/VPS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

เปิด `http://IP:8080` หรือผูก reverse proxy/โดเมน เช่น `https://bot.example.com`

### Nginx ตัวอย่าง

```nginx
server {
    listen 443 ssl http2;
    server_name bot.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Railway

1. Push โฟลเดอร์นี้ขึ้น GitHub
2. สร้าง Railway service และตั้ง Variables ตาม `.env.example`
3. สร้าง Volume แล้ว mount `/data`
4. ตั้ง `DB_PATH=/data/bot.db`
5. ตั้ง `PUBLIC_URL` เป็นโดเมน Railway หรือโดเมนส่วนตัว
6. Deploy; `railway.toml` จะรัน `python main.py` และตรวจ `/health`

## การใช้งาน

- ผู้ใช้: `/start`, `/addclient`, `/freeclient`, `/mycredit`, `/addmycredit`, `/mycodes`, `/Enterthecode`, `/checkprice`
- แอดมิน: `/startadmin` เพื่อเปิดเมนูปุ่ม
- คำสั่งแอดมินเดิมยังคงรองรับเพื่อความเข้ากันได้ แต่การเปิด/ปิดระบบหลักควรทำจากปุ่ม
- สินค้าที่แก้บนเว็บจะปรากฏใน `/addclient` ครั้งถัดไปทันที
- ปุ่มซื้อจะอ่านสินค้าใหม่อีกครั้งก่อนหักเครดิต ป้องกันการซื้อสินค้าที่ถูกปิดหรือราคาเปลี่ยนระหว่างขั้นตอน

## Health & backup

- Health check: `GET /health`
- สำรองระบบ: หยุดโปรเซสชั่วคราวแล้ว copy ไฟล์ `bot.db` รวมไฟล์ `bot.db-wal`/`bot.db-shm` หากมี
- ไม่ควรใช้ SQLite บน filesystem ชั่วคราว เพราะข้อมูลจะหายเมื่อ redeploy

## หมายเหตุการทดสอบ

โค้ดผ่าน `py_compile`, smoke test ของ schema/products/users/settings และตรวจ UI ที่ความละเอียด 1440×900 แล้ว การเชื่อม Telegram และ 3x-ui จริงต้องทดสอบด้วย token/inbound ของผู้ดูแลระบบหลัง deploy
