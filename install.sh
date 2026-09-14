#!/usr/bin/env bash
# install.sh — Install shokz-monitor for the current user.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Checking dependencies…"
MISSING=()
python3 -c "import dbus"   2>/dev/null || MISSING+=(python3-dbus)
python3 -c "import cairo"  2>/dev/null || MISSING+=(python3-cairo)
python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1'); from gi.repository import AyatanaAppIndicator3" \
                           2>/dev/null || MISSING+=(gir1.2-ayatanaappindicator3-0.1)
python3 -c "import gi; gi.require_version('Notify','0.7'); from gi.repository import Notify" \
                           2>/dev/null || MISSING+=(gir1.2-notify-0.7)

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "Installing missing packages: ${MISSING[*]}"
    sudo apt-get install -y "${MISSING[@]}"
fi

echo "==> Installing package…"
pip3 install --user -e "$REPO_DIR"

echo "==> Installing systemd user service…"
rm -f "$HOME/.config/autostart/shokz-monitor.desktop"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO_DIR/data/shokz-monitor.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now shokz-monitor.service

echo
echo "Done. Starts with the graphical session and restarts if it exits."
echo
echo "If battery level doesn't appear, enable BlueZ experimental features:"
echo "  sudo sed -i '/^\[Policy\]/a Experimental = true' /etc/bluetooth/main.conf"
echo "  sudo systemctl restart bluetooth"
