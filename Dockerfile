FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ติดตั้ง dependencies ก่อน เพื่อให้ Railway ใช้ Docker layer cache ได้
COPY requirements.txt ./
RUN python -m pip install --prefer-binary -r requirements.txt

# คัดลอก source หลัง dependencies ลดเวลาสร้างใหม่เมื่อแก้เฉพาะโค้ด
COPY . ./

# หยุด Build ทันทีหาก Python มี syntax error
RUN python -m py_compile main.py database.py xui_api.py web_admin.py

EXPOSE 8080

# main.py เริ่มทั้ง Telegram polling และเว็บควบคุมใน process เดียว
CMD ["python", "main.py"]
