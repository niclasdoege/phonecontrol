import asyncio
import logging
import re
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ADB keycode constants
KEYCODES = {
    "back":        "KEYCODE_BACK",
    "home":        "KEYCODE_HOME",
    "recents":     "KEYCODE_APP_SWITCH",
    "recent":      "KEYCODE_APP_SWITCH",
    "volume_up":   "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "power":       "KEYCODE_POWER",
    "wake":        "KEYCODE_WAKEUP",
    "sleep":       "KEYCODE_SLEEP",
    "enter":       "KEYCODE_ENTER",
    "backspace":   "KEYCODE_DEL",
    "del":         "KEYCODE_DEL",
    "tab":         "KEYCODE_TAB",
    "space":       "KEYCODE_SPACE",
    "escape":      "KEYCODE_ESCAPE",
    "esc":         "KEYCODE_ESCAPE",
    "menu":        "KEYCODE_MENU",
    "media_play":  "KEYCODE_MEDIA_PLAY_PAUSE",
    "media_next":  "KEYCODE_MEDIA_NEXT",
    "media_prev":  "KEYCODE_MEDIA_PREVIOUS",
    "brightness_up":   "KEYCODE_BRIGHTNESS_UP",
    "brightness_down": "KEYCODE_BRIGHTNESS_DOWN",
    "screenshot":  "KEYCODE_SYSRQ",
    "up":    "KEYCODE_DPAD_UP",
    "down":  "KEYCODE_DPAD_DOWN",
    "left":  "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
}


class ADBControl:
    def __init__(self):
        self.device_serial: Optional[str] = None
        self.screen_width: int = 0
        self.screen_height: int = 0
        self._connected = False
        self._last_tap: Optional[tuple[int, int]] = None

    # ── internals ──────────────────────────────────────────────────────────────

    def _cmd(self, *args, timeout: int = 10) -> tuple[str, str, int]:
        cmd = ["adb"]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        cmd += list(str(a) for a in args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip(), r.stderr.strip(), r.returncode
        except subprocess.TimeoutExpired:
            return "", "timeout", 1
        except FileNotFoundError:
            return "", "adb not found in PATH", 1

    async def _cmd_async(self, *args) -> bytes:
        cmd = ["adb"]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        cmd += list(str(a) for a in args)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout

    def _shell(self, *args, timeout: int = 10) -> tuple[str, str, int]:
        return self._cmd("shell", *args, timeout=timeout)

    # ── connection ─────────────────────────────────────────────────────────────

    def devices(self) -> list[dict]:
        stdout, _, _ = self._cmd("devices", "-l")
        result = []
        for line in stdout.splitlines()[1:]:
            line = line.strip()
            if not line or "offline" in line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                info = {"serial": parts[0]}
                for p in parts[2:]:
                    if ":" in p:
                        k, v = p.split(":", 1)
                        info[k] = v
                result.append(info)
        return result

    def connect_usb(self) -> bool:
        devs = self.devices()
        if not devs:
            return False
        self.device_serial = devs[0]["serial"]
        self._refresh_screen_size()
        self._connected = True
        return True

    def connect_tcp(self, host: str, port: int = 5555) -> bool:
        stdout, _, rc = self._cmd("connect", f"{host}:{port}")
        if rc == 0 or "connected" in stdout.lower():
            self._cmd("devices")  # force refresh
            devs = self.devices()
            for d in devs:
                if host in d["serial"]:
                    self.device_serial = d["serial"]
                    break
            self._refresh_screen_size()
            self._connected = True
            return True
        return False

    def disconnect(self):
        if self.device_serial:
            self._cmd("disconnect", self.device_serial)
        self.device_serial = None
        self._connected = False

    def is_connected(self) -> bool:
        if not self.device_serial:
            return False
        _, _, rc = self._shell("echo", "ok")
        self._connected = rc == 0
        return self._connected

    def _refresh_screen_size(self):
        stdout, _, _ = self._shell("wm", "size")
        m = re.search(r"(\d+)x(\d+)", stdout)
        if m:
            # wm size returns WIDTHxHEIGHT
            self.screen_width = int(m.group(1))
            self.screen_height = int(m.group(2))
            logger.info(f"Screen: {self.screen_width}x{self.screen_height}")

    # ── input ──────────────────────────────────────────────────────────────────

    def tap(self, x: int, y: int):
        self._shell("input", "tap", x, y)
        self._last_tap = (int(x), int(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300):
        self._shell("input", "swipe", x1, y1, x2, y2, duration_ms)

    def long_press(self, x: int, y: int, duration_ms: int = 800):
        self.swipe(x, y, x, y, duration_ms)

    def keyevent(self, key: str):
        code = KEYCODES.get(key.lower(), key.upper() if not key.startswith("KEYCODE_") else key)
        self._shell("input", "keyevent", code)

    def type_text(self, text: str):
        if not text:
            return
        # `input text` only delivers to the field that currently holds input
        # focus, and returns rc=0 even when nothing is focused (text silently
        # lost). The mirror taps the field on mousedown, but that focus is not
        # reliably held by the time the user hits Enter, so re-focus the last
        # tapped point and let the IME attach before typing.
        if self._last_tap is not None:
            x, y = self._last_tap
            self._shell("input", "tap", x, y)
            time.sleep(0.2)
        # Pass the whole string as one single-quoted argument: this forwards
        # spaces, %, and shell metacharacters to the device shell intact, so
        # `input text` types the literal text. Avoids the old %s-for-space
        # encoding (which collided with literal % in the text).
        quoted = "'" + text.replace("'", "'\\''") + "'"
        out, err, rc = self._shell("input", "text", quoted)
        if rc != 0 or err:
            logger.warning("type_text failed rc=%s err=%r text=%r", rc, err, text)

    def scroll_up(self, x: int = None, y: int = None, amount: int = 1):
        cx = x if x is not None else self.screen_width // 2
        cy = y if y is not None else self.screen_height // 2
        dist = 600 * amount
        self.swipe(cx, cy, cx, cy + dist, 200)

    def scroll_down(self, x: int = None, y: int = None, amount: int = 1):
        cx = x if x is not None else self.screen_width // 2
        cy = y if y is not None else self.screen_height // 2
        dist = 600 * amount
        self.swipe(cx, cy, cx, cy - dist, 200)

    # ── screen capture ─────────────────────────────────────────────────────────

    async def screenshot_png(self) -> Optional[bytes]:
        data = await self._cmd_async("exec-out", "screencap", "-p")
        if not data or not data.startswith(b"\x89PNG"):
            return None
        return data

    # ── ui accessibility tree ──────────────────────────────────────────────────

    def ui_dump(self) -> dict:
        """Synchronous wrapper kept for routines/gn_tester running in threads."""
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(self._ui_dump_blocking)
            return future.result(timeout=20)

    async def kill_uiautomator(self):
        """Kill the UIAutomator server process to bust its stale accessibility cache."""
        cmd = ["adb"]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        cmd += ["shell", "killall -9 uiautomator 2>/dev/null; true"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=3)
        except Exception:
            pass

    async def ui_dump_async(self) -> dict:
        """Non-blocking async UI dump — does not stall the event loop."""
        cmd = ["adb"]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        # Per-invocation temp file ($$ = device shell PID): a dump that gets
        # SIGKILL'd mid-write leaves its own partial file, never one another
        # dump will `cat`. `&& cat` runs only if the dump exited cleanly, so a
        # killed dump yields empty output (→ error → keep last good) instead of
        # truncated XML.
        cmd += ["shell",
                "f=/data/local/tmp/_uidump_$$.xml; uiautomator dump $f >/dev/null 2>&1 "
                "&& cat $f; rm -f $f"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            stdout = stdout_b.decode("utf-8", errors="replace").strip()
        except asyncio.TimeoutError:
            return {"elements": [], "raw_xml": "", "error": "uiautomator timeout"}
        except Exception as e:
            return {"elements": [], "raw_xml": "", "error": str(e)}
        return self._parse_ui_xml(stdout)

    def _ui_dump_blocking(self) -> dict:
        stdout, stderr, _ = self._cmd(
            "shell",
            "f=/data/local/tmp/_uidump_$$.xml; uiautomator dump $f >/dev/null 2>&1 "
            "&& cat $f; rm -f $f",
            timeout=15,
        )
        if not stdout:
            return {"elements": [], "raw_xml": "", "error": stderr or "no output"}
        return self._parse_ui_xml(stdout)

    @staticmethod
    def _parse_ui_xml(stdout: str) -> dict:
        import xml.etree.ElementTree as ET
        xml_start = stdout.find("<?xml")
        if xml_start == -1:
            xml_start = stdout.find("<hierarchy")
        raw_xml = stdout[xml_start:] if xml_start != -1 else stdout
        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as e:
            return {"elements": [], "raw_xml": raw_xml, "error": str(e)}

        rotation = int(root.attrib.get("rotation", "0"))
        screen_w = screen_h = 0

        elements = []
        for node in root.iter("node"):
            a = node.attrib
            coords = re.findall(r'\[(\d+),(\d+)\]', a.get("bounds", ""))
            if len(coords) == 2:
                x1, y1 = int(coords[0][0]), int(coords[0][1])
                x2, y2 = int(coords[1][0]), int(coords[1][1])
                # Root node (covers full screen) has x1=0, y1=0
                if x1 == 0 and y1 == 0 and x2 > screen_w:
                    screen_w, screen_h = x2, y2
            else:
                x1 = y1 = x2 = y2 = 0
            text = a.get("text", "").strip()
            desc = a.get("content-desc", "").strip()
            elements.append({
                "class":        a.get("class", "").split(".")[-1],
                "text":         text,
                "content_desc": desc,
                "label":        text or desc,
                "resource_id":  a.get("resource-id", "").split("/")[-1],
                "clickable":    a.get("clickable") == "true",
                "scrollable":   a.get("scrollable") == "true",
                "focusable":    a.get("focusable") == "true",
                "checked":      a.get("checked") == "true",
                "enabled":      a.get("enabled") == "true",
                "bounds": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "center": {"x": (x1 + x2) // 2, "y": (y1 + y2) // 2},
            })
        # Determine foreground package from the most common package attribute
        from collections import Counter
        pkg_counts = Counter(
            node.attrib.get("package", "")
            for node in root.iter("node")
            if node.attrib.get("package")
        )
        foreground_package = pkg_counts.most_common(1)[0][0] if pkg_counts else ""

        return {
            "elements":           elements,
            "raw_xml":            raw_xml,
            "rotation":           rotation,
            "screen_width":       screen_w,
            "screen_height":      screen_h,
            "foreground_package": foreground_package,
        }

    # ── status ─────────────────────────────────────────────────────────────────

    def foreground_package(self) -> str:
        """Fast foreground app check — pipe grep inside Android shell so only one line returns."""
        stdout, _, _ = self._cmd("shell", "dumpsys window | grep -m1 mCurrentFocus")
        m = re.search(r'u\d+\s+([\w.]+)/', stdout)
        return m.group(1) if m else ""

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "device": self.device_serial,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
        }
