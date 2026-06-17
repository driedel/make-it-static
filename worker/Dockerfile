FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/daniloriedel/make-it-staticify"
LABEL org.opencontainers.image.description="Make it Staticify — RQ worker"

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    gcc \
    python3-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove gcc python3-dev

COPY . .
RUN chmod +x scrape.sh

CMD ["python", "worker.py"]
