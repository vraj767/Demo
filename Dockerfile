FROM python:3.12-slim

# ─── System deps ─────────────────────────────────────────────────────────────
# libssl-dev + build tools needed to compile cryptg (fast AES for Telethon).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libssl-dev gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# ─── Python deps ─────────────────────────────────────────────────────────────
# CRITICAL install order:
#
#   ROOT CAUSE OF BUG:
#     There is a standalone PyPI package called "telegram" (unrelated to PTB).
#     If it gets installed — either from a stale Railway build cache or as an
#     accidental transitive dependency — it shadows python-telegram-bot's
#     "telegram" namespace, causing:
#       ImportError: cannot import name 'Application' from 'telegram'
#
#   FIX:
#     1. Upgrade pip (clears resolver state).
#     2. Force-remove "telegram" if present.
#     3. Install cryptg first (needs libssl for C-extension compile).
#     4. Install python-telegram-bot EXPLICITLY before telethon so the correct
#        "telegram" namespace is registered first.
#     5. Install the rest of requirements.txt.
#     --no-cache-dir on every step prevents Railway from reusing a stale wheel.
RUN pip install --no-cache-dir --upgrade pip && \
    pip uninstall -y telegram || true && \
    pip install --no-cache-dir cryptg && \
    pip install --no-cache-dir "python-telegram-bot==21.10" && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "-u", "main.py"]
