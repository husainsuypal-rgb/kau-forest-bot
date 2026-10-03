FROM python:3.12-slim

# Installs the real Tesseract OCR engine as a system package — this is
# the part pip install pytesseract does NOT do, and the actual cause of
# the "couldn't read the image" failures on every screenshot.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Cache-bust marker: 2026-10-03-v2 — bumping this forces Docker to
# re-run every layer below instead of reusing a stale cached COPY,
# which is the usual fix when pushed code changes aren't actually
# showing up in the running container despite a "successful" deploy.
COPY . .

CMD ["python", "forest_bot.py"]
