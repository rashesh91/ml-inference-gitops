#!/bin/bash
# Install Sanjana AI Bridge on FreeSWITCH server
# Run as root on the FreeSWITCH server
# Usage: bash install.sh [gateway_url]
#   gateway_url defaults to wss://aitest.lintel.in/ws
set -euo pipefail

GATEWAY_WS="${1:-wss://aitest.lintel.in/ws}"
INSTALL_DIR=/opt/sanjana-bridge
SERVICE_FILE=/etc/systemd/system/sanjana-bridge.service
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Sanjana Bridge Installer ==="
echo "Gateway: $GATEWAY_WS"
echo "Install dir: $INSTALL_DIR"
echo ""

# ── 1. Dependencies ───────────────────────────────────────────────────────────
echo "[1/5] Installing system dependencies..."
apt-get update -q
apt-get install -y python3 python3-venv python3-pip

# ── 2. Install bridge files ───────────────────────────────────────────────────
echo "[2/5] Installing bridge files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/bridge.py" "$INSTALL_DIR/bridge.py"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

# ── 3. Python venv + deps ─────────────────────────────────────────────────────
echo "[3/5] Creating Python venv and installing deps..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ── 4. Install FreeSWITCH dialplan ───────────────────────────────────────────
echo "[4/5] Installing FreeSWITCH dialplan..."
if [ -d /etc/freeswitch/dialplan/default ]; then
    cp "$SCRIPT_DIR/dialplan.xml" /etc/freeswitch/dialplan/default/99_sanjana.xml
    echo "  Dialplan installed. Edit /etc/freeswitch/dialplan/default/99_sanjana.xml"
    echo "  to set YOUR_DID_NUMBER, then reload: fs_cli -x 'reloadxml'"
else
    echo "  WARNING: /etc/freeswitch/dialplan/default not found."
    echo "  Manually copy dialplan.xml to your FreeSWITCH dialplan directory."
fi

# ── 5. Install and start systemd service ─────────────────────────────────────
echo "[5/5] Installing systemd service..."
sed "s|wss://aitest.lintel.in/ws|$GATEWAY_WS|g" \
    "$SCRIPT_DIR/sanjana-bridge.service" > "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable sanjana-bridge
systemctl restart sanjana-bridge

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Service status:"
systemctl status sanjana-bridge --no-pager
echo ""
echo "Logs:     journalctl -u sanjana-bridge -f"
echo "Test call: dial 9999 in FreeSWITCH"
echo ""
echo "NEXT STEPS:"
echo "  1. Edit /etc/freeswitch/dialplan/default/99_sanjana.xml"
echo "     Replace YOUR_DID_NUMBER with your actual inbound DID"
echo "  2. Reload FreeSWITCH dialplan: fs_cli -x 'reloadxml'"
echo "  3. Call 9999 to test"
