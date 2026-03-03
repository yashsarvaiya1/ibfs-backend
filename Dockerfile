# backend/Dockerfile
FROM python:3.11-slim

# Updated syntax: ENV KEY=VALUE
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ... (keep the apt-get install block exactly as it was) ...
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    # ... rest of the dependencies ...
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
