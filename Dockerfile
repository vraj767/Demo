FROM python:3.12-slim

# libssl-dev + build tools required by cryptg (fast AES-IGE for Telethon)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libssl-dev gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# cryptg installed first so it compiles with libssl available
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir cryptg && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "-u", "main.py"]
