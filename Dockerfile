FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# ffmpeg: duration / size / preview of the videos uploaded from a ZIP (Vault Pack). Optional, the bot works without it.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd -m -u 10001 bot && mkdir -p /data/zipwork && chown -R bot:bot /app /data
USER bot

# /health listens here (Caddy / Koyeb / Render inject or proxy PORT).
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/health', timeout=5)"]
CMD ["python", "main.py"]
