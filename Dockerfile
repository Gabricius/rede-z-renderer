FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        rclone \
        fonts-dejavu \
        curl \
        ca-certificates \
        procps \
        bc \
        gawk \
        coreutils \
        findutils \
        grep \
        sed \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/rede-z-renderer

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /tmp/rede-z && chmod 777 /tmp/rede-z

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
