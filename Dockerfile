FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INK_DB_PATH=/data/ink.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ink_app ./ink_app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 5000

# 两 worker + 基于文件的 SQLite；写操作均为短事务，单桶场景足够
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "ink_app.wsgi:app"]
