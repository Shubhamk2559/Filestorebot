#!/usr/bin/env bash
# One-command deploy on a fresh Ubuntu/Debian VPS:   bash deploy.sh
# Re-run it any time to update (rebuilds and restarts only what changed).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "👉 .env created. Fill BOT_TOKEN, OWNER_ID, MONGO_URI, API_ID, API_HASH and DOMAIN, then run: bash deploy.sh"
  exit 1
fi

for v in BOT_TOKEN OWNER_ID MONGO_URI DOMAIN; do
  val="$(grep -E "^${v}=" .env | head -1 | cut -d= -f2- || true)"
  if [ -z "$val" ] || echo "$val" | grep -qE "your-|example\.com|123456789|ABC-your"; then
    echo "❌ ${v} in .env is still the example value. Edit .env first."; exit 1
  fi
done

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
if ! command -v docker >/dev/null 2>&1; then
  echo "📦 Installing Docker..."
  curl -fsSL https://get.docker.com | $SUDO sh
fi

DC="$SUDO docker compose"
$SUDO docker compose version >/dev/null 2>&1 || DC="$SUDO docker-compose"

$DC up -d --build --remove-orphans
echo
$DC ps
echo
echo "✅ Done. Logs:  $DC logs -f bot"
echo "   Health:  https://$(grep -E '^DOMAIN=' .env | cut -d= -f2-)/health"
