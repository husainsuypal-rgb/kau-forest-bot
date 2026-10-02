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
COPY . .

CMD ["python", "forest_bot.py"]
