#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "=== Phone Control - Install ==="

# System packages
sudo apt-get update -qq
sudo apt-get install -y adb scrcpy python3-pip python3-venv

# System packages for browser automation
sudo apt-get install -y chromium-browser ffmpeg

# Python venv
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Playwright browser (try bundled, fall back silently to system chromium)
.venv/bin/playwright install chromium 2>/dev/null || \
  echo "  (Playwright bundled Chromium unavailable — will use system /usr/bin/chromium-browser)"

echo ""
echo "=== Done ==="
echo ""
echo "Start the server:"
echo "  sudo .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8080"
echo ""
echo "Or install as a service:"
echo "  sudo cp phonecontrol.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now phonecontrol"
echo ""
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo "Autonomous config tester (no API key):"
echo "  python browser_tester.py --game 'Your Game Name' --provider chatgpt"
echo "  python browser_tester.py --game 'Your Game Name' --provider claude"
echo "  (Browser opens — log in once, then press Enter to start)"
echo ""
echo "Phone setup:"
echo "  USB:  Enable Developer Options → USB Debugging on phone, plug in USB"
echo "  WiFi: Enable Developer Options → Wireless Debugging, or"
echo "        plug in USB first, run:  adb tcpip 5555"
echo "        then unplug and connect: adb connect <phone-ip>:5555"
