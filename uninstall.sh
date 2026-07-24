#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo bash uninstall.sh 运行。" >&2
  exit 1
fi

REMOTE_SERVICE_NAME="campus-remote-recovery.service"
REMOTE_USER_FILE="/etc/campus-autologin/remote-user"

if [[ -f "${REMOTE_USER_FILE}" ]]; then
  REMOTE_USER="$(head -n 1 "${REMOTE_USER_FILE}")"
  if getent passwd "${REMOTE_USER}" >/dev/null; then
    REMOTE_UID="$(id -u "${REMOTE_USER}")"
    REMOTE_HOME="$(getent passwd "${REMOTE_USER}" | cut -d: -f6)"
    if [[ -S "/run/user/${REMOTE_UID}/bus" ]]; then
      runuser -u "${REMOTE_USER}" -- env \
        HOME="${REMOTE_HOME}" \
        XDG_RUNTIME_DIR="/run/user/${REMOTE_UID}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${REMOTE_UID}/bus" \
        systemctl --user disable --now "${REMOTE_SERVICE_NAME}" 2>/dev/null || true
    fi
    rm -f "${REMOTE_HOME}/.config/systemd/user/${REMOTE_SERVICE_NAME}"
    rm -f "${REMOTE_HOME}/.config/systemd/user/default.target.wants/${REMOTE_SERVICE_NAME}"
  fi
fi

systemctl disable --now campus-autologin.service 2>/dev/null || true
rm -f /etc/systemd/system/campus-autologin.service
rm -rf /usr/local/lib/campus-autologin
rm -rf /etc/campus-autologin
systemctl daemon-reload
systemctl reset-failed campus-autologin.service 2>/dev/null || true

if getent passwd campus-autologin >/dev/null; then
  userdel campus-autologin
fi

echo "已卸载 v4 校园网自动认证、远程软件恢复服务和本机保存的 v4 账号配置。"
echo "NetworkManager 中原有的 Wi-Fi/有线连接配置未删除。"
