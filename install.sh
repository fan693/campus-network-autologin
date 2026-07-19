#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="campus-autologin"
PROGRAM_DIR="/usr/local/lib/campus-autologin"
CONFIG_DIR="/etc/campus-autologin"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SERVICE_FILE="/etc/systemd/system/campus-autologin.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo bash install.sh 运行。" >&2
  exit 1
fi

for command_name in python3 nmcli systemctl install runuser; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "缺少命令：${command_name}" >&2
    exit 1
  fi
done

ACTIVE_DEVICE="$({
  LC_ALL=C nmcli -t -f DEVICE,TYPE,STATE device status |
    awk -F: '$2 == "wifi" && $3 == "connected" {print $1; exit}'
} || true)"
if [[ -z "${ACTIVE_DEVICE}" ]]; then
  ACTIVE_DEVICE="$({
    LC_ALL=C nmcli -t -f DEVICE,TYPE,STATE device status |
      awk -F: '$3 == "connected" && ($2 == "ethernet" || $2 == "wifi") {print $1; exit}'
  } || true)"
fi
if [[ -z "${ACTIVE_DEVICE}" ]]; then
  echo "没有检测到已连接的 Wi-Fi 或有线网络。" >&2
  echo "请先连接目标校园网，再重新安装。" >&2
  exit 1
fi

ACTIVE_CONNECTION="$(LC_ALL=C nmcli -g GENERAL.CONNECTION device show "${ACTIVE_DEVICE}")"
ACTIVE_TYPE="$(LC_ALL=C nmcli -g GENERAL.TYPE device show "${ACTIVE_DEVICE}")"
if [[ -z "${ACTIVE_CONNECTION}" || "${ACTIVE_CONNECTION}" == "--" ]]; then
  echo "无法读取 ${ACTIVE_DEVICE} 当前使用的 NetworkManager 连接。" >&2
  exit 1
fi

echo "检测到连接：${ACTIVE_CONNECTION}"
echo "检测到网卡：${ACTIVE_DEVICE}"

EXISTING_CONFIG=""
if [[ -f "${CONFIG_FILE}" ]]; then
  EXISTING_CONFIG="${CONFIG_FILE}"
elif [[ -f /etc/cqu-autologin/config.json ]]; then
  EXISTING_CONFIG="/etc/cqu-autologin/config.json"
  echo "检测到 v3 CQU 配置，将在向导中作为默认值导入。"
fi

TEMP_CONFIG="$(mktemp)"
trap 'rm -f "${TEMP_CONFIG}"' EXIT
chmod 0600 "${TEMP_CONFIG}"

CONFIG_ARGS=(
  "${SCRIPT_DIR}/configure.py"
  --output "${TEMP_CONFIG}"
  --network-name "${ACTIVE_CONNECTION}"
  --interface "${ACTIVE_DEVICE}"
)
if [[ -n "${EXISTING_CONFIG}" ]]; then
  CONFIG_ARGS+=(--existing "${EXISTING_CONFIG}")
fi
python3 "${CONFIG_ARGS[@]}"

if [[ "${ACTIVE_TYPE}" == "wifi" ]]; then
  echo "正在启用 Wi-Fi 自动重连……"
  nmcli connection modify "${ACTIVE_CONNECTION}" \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    connection.autoconnect-retries 0 \
    802-11-wireless.powersave 2
  nmcli radio wifi on
fi

if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  NOLOGIN_SHELL="$(command -v nologin || true)"
  [[ -n "${NOLOGIN_SHELL}" ]] || NOLOGIN_SHELL="/usr/sbin/nologin"
  useradd --system --home-dir /nonexistent --shell "${NOLOGIN_SHELL}" "${SERVICE_USER}"
fi

systemctl disable --now cqu-autologin.service 2>/dev/null || true
systemctl stop campus-autologin.service 2>/dev/null || true

install -d -o root -g root -m 0755 "${PROGRAM_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -o root -g root -m 0755 "${SCRIPT_DIR}/campus_autologin.py" \
  "${PROGRAM_DIR}/campus_autologin.py"
install -o root -g root -m 0755 "${SCRIPT_DIR}/configure.py" \
  "${PROGRAM_DIR}/configure.py"
install -o root -g root -m 0644 "${SCRIPT_DIR}/campus-autologin.service" \
  "${SERVICE_FILE}"
install -o root -g "${SERVICE_USER}" -m 0640 "${TEMP_CONFIG}" "${CONFIG_FILE}"

runuser -u "${SERVICE_USER}" -- /usr/bin/python3 \
  "${PROGRAM_DIR}/campus_autologin.py" \
  --config "${CONFIG_FILE}" \
  --check-config

systemctl daemon-reload
systemctl enable --now campus-autologin.service

echo
echo "安装完成（支持 Ubuntu 20.04 / 22.04 / 24.04）。"
echo "立即测试：sudo systemctl stop campus-autologin && sudo -u ${SERVICE_USER} /usr/bin/python3 ${PROGRAM_DIR}/campus_autologin.py --config ${CONFIG_FILE} --once"
echo "恢复服务：sudo systemctl start campus-autologin"
echo "查看状态：sudo systemctl status campus-autologin --no-pager"
echo "查看日志：sudo journalctl -u campus-autologin -n 50 --no-pager"
