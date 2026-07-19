#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo bash uninstall.sh 运行。" >&2
  exit 1
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

echo "已卸载 v4 校园网自动认证服务和本机保存的 v4 账号配置。"
echo "NetworkManager 中原有的 Wi-Fi/有线连接配置未删除。"
