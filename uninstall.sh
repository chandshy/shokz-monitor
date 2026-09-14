#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now shokz-monitor.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/shokz-monitor.service"
systemctl --user daemon-reload
pip3 uninstall -y shokz-monitor 2>/dev/null || true
rm -f "$HOME/.config/autostart/shokz-monitor.desktop"
rm -rf "$HOME/.cache/shokz-monitor"
echo "shokz-monitor removed."
