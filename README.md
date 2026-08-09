# Phone Control

Phone Control is a small FastAPI web app for controlling an Android phone over ADB from a browser. It provides a live phone mirror, clickable UIAutomator overlays, keyboard/input helpers, simple routines, screenshots/video streaming, and optional GameNative configuration testing tools.

This project was vibe-coded as a practical personal tool. Treat it as a useful developer utility and starting point, not polished production software. It assumes Linux, ADB, and a trusted local network.

## Features

- Connect to Android devices over USB, TCP/IP, or Android 11+ wireless debugging.
- Browser dashboard for pairing, connecting, key events, typing, tapping, and API calls.
- Live mirror at `/mirror` with clickable UI overlay from UIAutomator.
- MJPEG video stream at `/video` with `scrcpy`/`ffmpeg` when available and `screencap` fallback.
- REST API for taps, swipes, key events, screenshots, UI dumps, app force-stop, routines, and text-aware actions.
- Optional GameNative config tester and browser-based AI tester scripts.

## Requirements

- Linux machine with Python 3.10+.
- Android Debug Bridge (`adb`).
- `scrcpy` and `ffmpeg` for smoother video streaming.
- Android phone with Developer Options and USB debugging or Wireless debugging enabled.
- Optional: Chromium/Playwright for `browser_tester.py`.
- Optional: `ANTHROPIC_API_KEY` for `ai_tester.py`.

## Quick Start

```bash
./install.sh
sudo .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8080
```

Open:

```text
http://localhost:8080
```

If you are connecting from another machine on the same network, use the host machine's LAN IP:

```text
http://<host-ip>:8080
```

## Manual Setup

```bash
sudo apt-get update
sudo apt-get install -y adb scrcpy ffmpeg python3-pip python3-venv chromium-browser

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Run the server:

```bash
sudo .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8080
```

`sudo` is often needed for USB ADB access unless your udev rules are already configured.

## Phone Setup

For USB:

1. Enable Developer Options on the phone.
2. Enable USB debugging.
3. Plug in the phone and approve the RSA prompt.
4. Use the web UI's USB connect button or run `adb devices` to verify the device is visible.

For wireless debugging on Android 11+:

1. Enable Wireless debugging on the phone.
2. In the web UI, use the pairing IP, pairing port, and six-digit pairing code.
3. After pairing, use the connection port shown on the main Wireless debugging screen.

For TCP/IP mode from USB:

```bash
adb tcpip 5555
adb connect <phone-ip>:5555
```

## Main Pages

- `/` - main dashboard and connection controls.
- `/mirror` - clean live phone mirror with clickable overlay.
- `/dynamicuipage` - alternate dynamic UI view.
- `/dashboard` - visual context page used by browser-based AI testers.

## Useful API Endpoints

- `GET /api/status`
- `GET /api/devices`
- `POST /api/connect`
- `POST /api/disconnect`
- `POST /api/tap`
- `POST /api/swipe`
- `POST /api/key`
- `POST /api/type`
- `GET /api/ui`
- `GET /api/screenshot`
- `GET /video`
- `POST /api/tap_text`
- `POST /api/wait_text`
- `POST /api/force_stop`

Example:

```bash
curl -X POST http://localhost:8080/api/key \
  -H 'Content-Type: application/json' \
  -d '{"key":"HOME"}'
```

## Routines

Routines are stored in `routines.json` and can be created or run through the API:

```bash
curl http://localhost:8080/api/routines
```

Keep personal or device-specific routines out of commits if they contain private app names, coordinates, or workflow details.

## GameNative Testing

The built-in GameNative tester is available from the web UI's advanced panel. The test matrix defaults live in `configs.py`.

Browser-based tester, using your logged-in browser session:

```bash
python browser_tester.py --game "Your Game Name" --provider chatgpt
```

Claude API tester:

```bash
ANTHROPIC_API_KEY=sk-... python ai_tester.py --game "Your Game Name"
```

Generated test output such as `results.json`, tester logs, and `browser_data/` is ignored by git.

## Systemd Service

`phonecontrol.service` is included as a starting point. Before installing it, edit:

- `User=`
- `WorkingDirectory=`
- `ExecStart=`
- `ADB_VENDOR_KEYS=`

Then install:

```bash
sudo cp phonecontrol.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now phonecontrol
```

## Security Notes

This app exposes phone control over HTTP. Do not run it on an untrusted network or expose it directly to the internet. Anyone who can reach the web server may be able to control the connected phone.

Do not commit:

- `.env` files or API keys.
- Browser session data.
- Runtime logs.
- Personal test results.

## GitHub Readiness

The repo is suitable for GitHub as a personal/dev-tool project. Before publishing, consider adding a license and replacing the hard-coded service file paths with your own target install path or a template.
