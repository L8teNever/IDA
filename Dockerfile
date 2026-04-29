FROM python:3.11-slim

WORKDIR /app

# System-Abhängigkeiten für faster-whisper / ctranslate2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

CMD ["python", "main.py"]
