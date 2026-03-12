FROM python:3.12-slim

# ─── System deps ─────────────────────────────────────────────────────────────
# libssl-dev + gcc needed to compile cryptg (fast AES-IGE for Telethon).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libssl-dev gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# ─── Python deps — ORDER IS CRITICAL ────────────────────────────────────────
#
# ROOT CAUSE:
#   There is a standalone PyPI package called "telegram" (completely unrelated
#   to python-telegram-bot). If it exists in the pip cache or gets pulled in
#   as a transitive dep it shadows python-telegram-bot's "telegram" namespace:
#     ImportError: cannot import name 'Application' from 'telegram'
#
# WHY THE PREVIOUS FIX FAILED:
#   We ran `pip uninstall telegram` BEFORE installing requirements.txt.
#   But pip can re-install it while resolving requirements.txt dependencies.
#   So the uninstall had no lasting effect.
#
# CORRECT FIX — three steps in the right order:
#   STEP 1: Install ALL requirements (including cryptg) normally.
#   STEP 2: AFTER everything is installed, forcibly remove "telegram".
#           Now nothing can re-install it because we're done with pip install.
#   STEP 3: Force-reinstall python-telegram-bot so its files are laid down
#           last and own the "telegram" namespace definitively.
#   STEP 4: Verify the import works at BUILD TIME so Railway build fails
#           loudly instead of crashing silently at runtime.
#
# --no-cache-dir prevents Railway from reusing any stale cached wheels.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir cryptg && \
    pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y telegram || true && \
    pip install --no-cache-dir --force-reinstall "python-telegram-bot==21.10" && \
    python -c "from telegram import Application; print('✓ telegram import OK')"

COPY . .

EXPOSE 8080

CMD ["python", "-u", "main.py"]
