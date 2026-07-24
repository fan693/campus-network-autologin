#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="campus-autologin"
PROGRAM_DIR="/usr/local/lib/campus-autologin"
CONFIG_DIR="/etc/campus-autologin"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SERVICE_FILE="/etc/systemd/system/campus-autologin.service"
REMOTE_USER_FILE="${CONFIG_DIR}/remote-user"
REMOTE_SERVICE_NAME="campus-remote-recovery.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

desktop_user() {
  local candidate="${SUDO_USER:-}"
  if [[ -n "${candidate}" && "${candidate}" != "root" ]] && getent passwd "${candidate}" >/dev/null; then
    printf '%s\n' "${candidate}"
    return
  fi
  if command -v loginctl >/dev/null 2>&1; then
    loginctl list-sessions --no-legend 2>/dev/null |
      awk '$3 != "root" && $2 >= 1000 {print $3; exit}'
  fi
}

user_systemctl() {
  local user_name="$1" user_id="$2" user_home="$3"
  shift 3
  runuser -u "${user_name}" -- env \
    HOME="${user_home}" \
    XDG_RUNTIME_DIR="/run/user/${user_id}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${user_id}/bus" \
    systemctl --user "$@"
}

remove_remote_service() {
  local user_name="$1" user_id user_home unit_dir
  getent passwd "${user_name}" >/dev/null || return 0
  user_id="$(id -u "${user_name}")"
  user_home="$(getent passwd "${user_name}" | cut -d: -f6)"
  unit_dir="${user_home}/.config/systemd/user"
  if [[ -S "/run/user/${user_id}/bus" ]]; then
    user_systemctl "${user_name}" "${user_id}" "${user_home}" \
      disable --now "${REMOTE_SERVICE_NAME}" 2>/dev/null || true
  fi
  rm -f "${unit_dir}/${REMOTE_SERVICE_NAME}"
  rm -f "${unit_dir}/default.target.wants/${REMOTE_SERVICE_NAME}"
}

install_remote_recovery() {
  local user_name="$1" user_id user_group user_home unit_dir detection
  user_id="$(id -u "${user_name}")"
  user_group="$(id -gn "${user_name}")"
  user_home="$(getent passwd "${user_name}" | cut -d: -f6)"
  unit_dir="${user_home}/.config/systemd/user"

  if detection="$(runuser -u "${user_name}" -- env HOME="${user_home}" \
    /usr/bin/python3 "${PROGRAM_DIR}/remote_recovery.py" --detect)"; then
    printf '检测到远程控制软件：\n%s\n' "${detection}"
  else
    local status=$?
    if [[ "${status}" -eq 3 ]]; then
      echo "未检测到支持的远程控制软件，跳过掉线恢复服务。"
      remove_remote_service "${user_name}"
      rm -f "${REMOTE_USER_FILE}"
      return 0
    fi
    echo "远程控制软件检测失败（退出码 ${status}），不影响校园网服务安装。" >&2
    return 0
  fi

  install -d -o "${user_name}" -g "${user_group}" -m 0755 "${unit_dir}"
  install -d -o "${user_name}" -g "${user_group}" -m 0755 \
    "${unit_dir}/default.target.wants"
  install -o "${user_name}" -g "${user_group}" -m 0644 \
    "${SCRIPT_DIR}/${REMOTE_SERVICE_NAME}" "${unit_dir}/${REMOTE_SERVICE_NAME}"
  ln -sfn "../${REMOTE_SERVICE_NAME}" \
    "${unit_dir}/default.target.wants/${REMOTE_SERVICE_NAME}"
  chown -h "${user_name}:${user_group}" \
    "${unit_dir}/default.target.wants/${REMOTE_SERVICE_NAME}"
  printf '%s\n' "${user_name}" > "${REMOTE_USER_FILE}"
  chmod 0644 "${REMOTE_USER_FILE}"

  if [[ -S "/run/user/${user_id}/bus" ]]; then
    user_systemctl "${user_name}" "${user_id}" "${user_home}" daemon-reload
    user_systemctl "${user_name}" "${user_id}" "${user_home}" \
      enable --now "${REMOTE_SERVICE_NAME}"
    echo "已为 ${user_name} 启用远程控制软件掉线恢复。"
  else
    echo "已为 ${user_name} 安装远程恢复服务，将在该用户下次登录桌面时启动。"
  fi
}

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
install -o root -g root -m 0755 "${SCRIPT_DIR}/remote_recovery.py" \
  "${PROGRAM_DIR}/remote_recovery.py"
install -o root -g root -m 0644 "${SCRIPT_DIR}/campus-autologin.service" \
  "${SERVICE_FILE}"
install -o root -g "${SERVICE_USER}" -m 0640 "${TEMP_CONFIG}" "${CONFIG_FILE}"

runuser -u "${SERVICE_USER}" -- /usr/bin/python3 \
  "${PROGRAM_DIR}/campus_autologin.py" \
  --config "${CONFIG_FILE}" \
  --check-config

systemctl daemon-reload
systemctl enable --now campus-autologin.service

DESKTOP_USER="$(desktop_user)"
if [[ -n "${DESKTOP_USER}" ]]; then
  if [[ -f "${REMOTE_USER_FILE}" ]]; then
    PREVIOUS_REMOTE_USER="$(head -n 1 "${REMOTE_USER_FILE}")"
    if [[ -n "${PREVIOUS_REMOTE_USER}" && "${PREVIOUS_REMOTE_USER}" != "${DESKTOP_USER}" ]]; then
      remove_remote_service "${PREVIOUS_REMOTE_USER}"
    fi
  fi
  install_remote_recovery "${DESKTOP_USER}"
else
  echo "未找到当前桌面用户，跳过远程控制软件掉线恢复服务。"
fi

echo
echo "安装完成（支持 Ubuntu 20.04 / 22.04 / 24.04）。"
echo "立即测试：sudo systemctl stop campus-autologin && sudo -u ${SERVICE_USER} /usr/bin/python3 ${PROGRAM_DIR}/campus_autologin.py --config ${CONFIG_FILE} --once"
echo "恢复服务：sudo systemctl start campus-autologin"
echo "查看状态：sudo systemctl status campus-autologin --no-pager"
echo "查看日志：sudo journalctl -u campus-autologin -n 50 --no-pager"
