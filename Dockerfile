FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
# TgCrypto = fast MTProto encryption (makes Stream/Download noticeably faster). It needs a C compiler to build,
# so gcc is installed only for this step and removed again. If it fails the bot still works (just slower streaming).
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && pip install -r requirements.txt \
 && (pip install tgcrypto || echo "TgCrypto not installed - streaming will use the slower pure-Python crypto") \
 && apt-get purge -y --auto-remove gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

COPY . .
RUN useradd -m -u 10001 bot && chown -R bot:bot /app
USER bot

# /health listens here (Caddy / Koyeb / Render inject or proxy PORT).
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/health', timeout=5)"]
CMD ["python", "main.py"]
