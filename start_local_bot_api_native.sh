#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
DATA_DIR="${ROOT_DIR}/telegram-bot-api-data"
DOWNLOADS_DIR="${ROOT_DIR}/downloads"
RUNTIME_FILE="${ROOT_DIR}/.local_bot_api_runtime.json"
PID_FILE="${ROOT_DIR}/telegram-bot-api-native.pid"
LOG_FILE="${ROOT_DIR}/telegram-bot-api-native.log"
PORT="${LOCAL_BOT_API_PORT:-8081}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "فایل .env پیدا نشد: ${ENV_FILE}"
  exit 1
fi

set -a
source "${ENV_FILE}"
set +a

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID is required in .env}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is required in .env}"

BIN_CANDIDATES=()
if [[ -n "${LOCAL_BOT_API_BINARY:-}" ]]; then
  BIN_CANDIDATES+=("${LOCAL_BOT_API_BINARY}")
fi
if command -v telegram-bot-api >/dev/null 2>&1; then
  BIN_CANDIDATES+=("$(command -v telegram-bot-api)")
fi
BIN_CANDIDATES+=(
  "${ROOT_DIR}/bin/telegram-bot-api"
  "${ROOT_DIR}/.vendor/telegram-bot-api/build/telegram-bot-api"
  "${ROOT_DIR}/.vendor/telegram-bot-api/build/telegram-bot-api/telegram-bot-api"
)

BOT_API_BIN=""
for candidate in "${BIN_CANDIDATES[@]}"; do
  if [[ -n "${candidate}" && -x "${candidate}" ]]; then
    BOT_API_BIN="${candidate}"
    break
  fi
done

if [[ -z "${BOT_API_BIN}" ]]; then
  echo "telegram-bot-api binary پیدا نشد."
  echo "Install it on PATH or set LOCAL_BOT_API_BINARY in .env."
  exit 1
fi

mkdir -p "${DATA_DIR}"
mkdir -p "${DOWNLOADS_DIR}"

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(cat "${PID_FILE}")"
  if [[ -n "${existing_pid}" ]] && kill -0 "${existing_pid}" >/dev/null 2>&1; then
    echo "Local Bot API native process is already running with PID ${existing_pid}."
    echo "URL: http://localhost:${PORT}"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

cat > "${RUNTIME_FILE}" <<EOF
{"mode":"native","shared_downloads_path":"${DOWNLOADS_DIR}"}
EOF

(
  cd "${DATA_DIR}"
  nohup "${BOT_API_BIN}" \
    --local \
    --http-port "${PORT}" \
    --api-id "${TELEGRAM_API_ID}" \
    --api-hash "${TELEGRAM_API_HASH}" \
    > "${LOG_FILE}" 2>&1 &
  echo $! > "${PID_FILE}"
)

sleep 2

if ! kill -0 "$(cat "${PID_FILE}")" >/dev/null 2>&1; then
  echo "Local Bot API native process failed to start. Check ${LOG_FILE}"
  exit 1
fi

echo "Local Bot API server started on http://localhost:${PORT}"
echo "Binary: ${BOT_API_BIN}"
echo "Storage: ${DATA_DIR}"
echo "Shared downloads path: ${DOWNLOADS_DIR}"
echo "Log file: ${LOG_FILE}"
