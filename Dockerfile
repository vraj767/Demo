FROM python:3.12-slim

# libssl-dev + gcc needed to compile cryptg (fast AES-IGE for Telethon).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libssl-dev gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir cryptg && \
    pip install --no-cache-dir -r requirements.txt && \
    python -c "from telegram.ext import Application; print('✓ telegram.ext.Application import OK')"

COPY . .

EXPOSE 8080

CMD ["python", "-u", "main.py"]
