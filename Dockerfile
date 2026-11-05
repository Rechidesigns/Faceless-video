FROM python:3.12-slim

# ffmpeg (with libass for captions) + fontconfig
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fontconfig ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /usr/share/fonts/truetype/app && cp fonts/*.ttf /usr/share/fonts/truetype/app/ && fc-cache -f

# Persistent data (videos, channels, connected accounts) goes on a Railway volume mounted at /data
ENV DATA_DIR=/data PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
