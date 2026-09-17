# Bio-shop Bot v37.0

ระบบ Telegram Bot สำหรับสร้างไฟล์ VPN พร้อมเว็บไซต์ควบคุมที่ใช้ฐานข้อมูลเดียวกันแบบเรียลไทม์

## ความสามารถปัจจุบัน

- ควบคุมบอทและตั้งค่าระบบผ่านเว็บไซต์
- จัดการเครือข่าย AIS, TRUE และเครือข่ายเพิ่มเติม
- เพิ่ม แก้ไข ลบ และเปิด–ปิดแพ็กเกจแยกตามเครือข่าย
- กำหนด Inbound ID ของ 3x-ui แยกในแต่ละแพ็กเกจ
- จัดลำดับเครือข่ายและแพ็กเกจใหม่อัตโนมัติเป็น 1..N
- ข้อมูลจากเว็บไซต์และบอทซิงก์ผ่าน SQLite เดียวกัน
- ส่งไฟล์หรือลิงก์ที่สร้างสำเร็จไปยัง DM
- รองรับทดลองใช้ฟรี เครดิต โค้ดเครดิต และ TrueMoney

## ลำดับการซื้อ

```text
เลือกเครือข่าย
→ เลือกแพ็กเกจ
→ ตั้งชื่อไฟล์
→ ระบุจำนวนวัน
→ ระบุ GB
→ สร้างและส่ง DM
```

หากเครือข่ายยังไม่มีแพ็กเกจ บอทจะแจ้งว่า `ยังไม่มีรายการแพ็กเกจ`

## การตั้งค่า Inbound ID

ไม่ต้องกำหนด `AIS_INBOUND_ID` หรือ `TRUE_INBOUND_ID` ใน Environment variables

### ผ่านเว็บไซต์

```text
เครือข่าย → จัดการแพ็กเกจ → เพิ่มหรือแก้ไขแพ็กเกจ → Inbound ID ใน 3x-ui
```

### ผ่านบอท

เปิด `/startadmin` เพื่อดู Package ID แล้วใช้:

```text
/setinbound PackageID InboundID
```

ตัวอย่าง:

```text
/setinbound 3 17
```

ระบบทดลองใช้ฟรีใช้ Inbound ID ของแพ็กเกจแรกที่เปิดใช้งานในเครือข่ายนั้น

## Environment variables

### จำเป็น

| ตัวแปร | รายละเอียด |
|---|---|
| `BOT_TOKEN` | Token ของ Telegram Bot |
| `ADMIN_IDS` | Telegram User ID ของผู้ดูแล คั่นด้วย comma |
| `XUI_URL` | URL ของ 3x-ui Panel |
| `XUI_API_TOKEN` | API Token ของ 3x-ui หากใช้งาน |
| `XUI_USERNAME` | Username ของ 3x-ui เมื่อไม่ใช้ API Token |
| `XUI_PASSWORD` | Password ของ 3x-ui เมื่อไม่ใช้ API Token |
| `WEB_ADMIN_PASSWORD` | รหัสผ่านเว็บไซต์จัดการ |
| `WEB_ADMIN_SECRET` | ค่าสุ่มอย่างน้อย 32 ตัวอักษรสำหรับเซสชัน |

### ตัวเลือก

| ตัวแปร | ค่าแนะนำ | รายละเอียด |
|---|---:|---|
| `DB_PATH` | `/data/bot.db` | ตำแหน่งฐานข้อมูล SQLite |
| `WEB_ADMIN_ENABLED` | `1` | เปิดเว็บไซต์จัดการ |
| `WEB_ADMIN_USERNAME` | `admin` | ชื่อผู้ใช้เว็บไซต์ |
| `WEB_HOST` | `0.0.0.0` | Host ของเว็บเซิร์ฟเวอร์ |
| `PORT` | `8080` | Port ของเว็บเซิร์ฟเวอร์ |
| `PUBLIC_URL` | URL ที่ Deploy | ลิงก์เว็บไซต์ที่แสดงในบอท |
| `WEB_COOKIE_SECURE` | `1` | ใช้ Secure Cookie บน HTTPS |
| `TRUEMONEY_WALLET_PHONE` | ว่าง | เบอร์รับซอง TrueMoney |

ดูตัวอย่างทั้งหมดใน `env.example`

## Railway

1. อัปโหลดไฟล์ทั้งหมดไว้ที่ root ของ repository
2. ตั้ง Environment variables ตามตารางด้านบน
3. ตั้ง Start Command เป็น:

```text
python main.py
```

4. ใช้ Dockerfile ที่อยู่ใน root
5. ตั้ง Volume ให้ `/data` เพื่อเก็บฐานข้อมูลถาวร
6. Health check ใช้ `/health`

ไม่ใช้คำสั่ง `uvicorn main:app` เพราะ `main.py` เป็นตัวเริ่มทั้ง Telegram Bot และเว็บไซต์

## รันในเครื่องหรือ VPS

```bash
python -m pip install -r requirements.txt
python main.py
```

## เมนูผู้ดูแล

ใช้ `/startadmin` เพื่อ:

- เปิด–ปิดระบบหลัก
- ดูจำนวนเครือข่ายและแพ็กเกจ
- ดู Network ID, Package ID และ Inbound ID ปัจจุบัน
- เปิดเว็บไซต์จัดการ

## ไฟล์หลัก

| ไฟล์ | หน้าที่ |
|---|---|
| `main.py` | Telegram Bot และจุดเริ่มระบบ |
| `database.py` | ฐานข้อมูล การตั้งค่า เครือข่ายและแพ็กเกจ |
| `web_admin.py` | เว็บไซต์ควบคุมและ API |
| `xui_api.py` | ติดต่อ 3x-ui |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | สร้าง Container |
| `railway.toml` | ตั้งค่า Railway |
| `env.example` | ตัวอย่าง Environment variables |
| `VERSION.txt` | หมายเลขรุ่นปัจจุบัน |
| `CHANGELOG.txt` | สรุปการทำงานของรุ่นปัจจุบัน |

## รุ่นปัจจุบัน

```text
Bio-shop Bot v37.0-no-inbound-env
```
