#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="campus-autologin"
PROGRAM_DIR="/usr/local/lib/campus-autologin"
CONFIG_FILE="/etc/campus-autologin/config.json"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo bash configure.sh 运行。" >&2
  exit 1
fi
if [[ ! -f "${CONFIG_FILE}" || ! -f "${PROGRAM_DIR}/configure.py" ]]; then
  echo "没有找到已安装的 v4 配置，请先执行 install.sh。" >&2
  exit 1
fi

TEMP_CONFIG="$(mktemp)"
trap 'rm -f "${TEMP_CONFIG}"' EXIT
chmod 0600 "${TEMP_CONFIG}"

/usr/bin/python3 "${PROGRAM_DIR}/configure.py" \
  --existing "${CONFIG_FILE}" \
  --output "${TEMP_CONFIG}"

chown root:"${SERVICE_USER}" "${TEMP_CONFIG}"
chmod 0640 "${TEMP_CONFIG}"
runuser -u "${SERVICE_USER}" -- /usr/bin/python3 \
  "${PROGRAM_DIR}/campus_autologin.py" \
  --config "${TEMP_CONFIG}" \
  --check-config
install -o root -g "${SERVICE_USER}" -m 0640 "${TEMP_CONFIG}" "${CONFIG_FILE}"
systemctl restart campus-autologin.service

echo "校园网配置已更新，服务已重启。"
