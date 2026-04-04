#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
DATA_DIR="${ROOT_DIR}/telegram-bot-api-data"
DOWNLOADS_DIR="${ROOT_DIR}/downloads"
RUNTIME_FILE="${ROOT_DIR}/.local_bot_api_runtime.json"
CONTAINER_NAME="telegram-bot-api-local"
PORT="${LOCAL_BOT_API_PORT:-8081}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker نصب نیست."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon فعال نیست. اول Docker Desktop را اجرا کن."
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "فایل .env پیدا نشد: ${ENV_FILE}"
  exit 1
fi

set -a
source "${ENV_FILE}"
set +a

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID is required in .env}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is required in .env}"

mkdir -p "${DATA_DIR}"
mkdir -p "${DOWNLOADS_DIR}"

cat > "${RUNTIME_FILE}" <<EOF
{"mode":"docker","shared_downloads_path":"/downloads"}
EOF

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p "${PORT}:8081" \
  -v "${DATA_DIR}:/var/lib/telegram-bot-api" \
  -v "${DOWNLOADS_DIR}:/downloads" \
  -e TELEGRAM_API_ID="${TELEGRAM_API_ID}" \
  -e TELEGRAM_API_HASH="${TELEGRAM_API_HASH}" \
  -e TELEGRAM_LOCAL=1 \
  aiogram/telegram-bot-api:latest >/dev/null

echo "Local Bot API server started on http://localhost:${PORT}"
echo "Storage: ${DATA_DIR}"
echo "Shared downloads mount: ${DOWNLOADS_DIR} -> /downloads"
