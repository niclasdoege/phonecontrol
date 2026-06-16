import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from adb_control import ADBControl
from gn_tester import GNTester
from routines import RoutineRunner
from video_stream import VideoStreamer

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

adb     = ADBControl()
runner  = RoutineRunner(adb)
video   = VideoStreamer(adb, fps=15, width=720)
tester  = GNTester(adb)


class UICache:
    """
    Background loop: refreshes UI tree every `interval` seconds and pushes
    updates to all connected WebSocket clients immediately on each refresh.
    HTTP clients get the cached result instantly (no per-request blocking).
    """
    def __init__(self, adb_ctrl, interval: float = 1.0):
        self._adb      = adb_ctrl
        self._interval = interval
        self._cache: dict = {"elements": [], "raw_xml": ""}
        self._updated  = 0.0
        self._task: asyncio.Task | None = None
        self._lock     = asyncio.Lock()
        self._refresh_event = asyncio.Event()
        self._ws_clients: list = []   # WebSocket subscribers
        self._reset_ui   = False      # kill UIAutomator server on next loop to bust stale cache
        self._stale_count = 0         # consecutive stale dumps; debounces the reset

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def get(self) -> dict:
        age = int((time.time() - self._updated) * 1000) if self._updated else -1
        return {**self._cache, "age_ms": age}

    async def refresh_now(self):
        """Signal the loop to refresh immediately (called after user actions)."""
        self._refresh_event.set()

    async def subscribe(self, ws) -> None:
        """Register a WebSocket; send current cache immediately then push on updates."""
        self._ws_clients.append(ws)
        try:
            current = await self.get()
            payload = {
                **current,
                "screen_width":  self._adb.screen_width,
                "screen_height": self._adb.screen_height,
            }
            await ws.send_json(payload)
        except Exception:
            pass

    def unsubscribe(self, ws) -> None:
        try:
            self._ws_clients.remove(ws)
        except ValueError:
            pass

    async def _push(self, payload: dict):
        dead = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)

    async def _loop(self):
        while True:
            try:
                try:
                    await asyncio.wait_for(self._refresh_event.wait(), timeout=self._interval)
                    self._refresh_event.clear()
                except asyncio.TimeoutError:
                    pass

                if not self._adb._connected:
                    await asyncio.sleep(0.5)
                    continue

                async with self._lock:
                    if self._reset_ui:
                        self._reset_ui = False
                        await self._adb.kill_uiautomator()

                    # Run UIAutomator dump and foreground-package check concurrently
                    loop = asyncio.get_event_loop()
                    result, real_pkg = await asyncio.gather(
                        self._adb.ui_dump_async(),
                        loop.run_in_executor(None, self._adb.foreground_package),
                    )

                    # A failed dump (truncated / empty / timeout) carries an
                    # "error". Keep the last good overlay rather than blanking
                    # the screen on a transient hiccup. Polling cadence is
                    # unchanged — the next cycle just tries again.
                    if result.get("error"):
                        self._stale_count = 0
                        continue

                    ui_pkg = result.get("foreground_package", "")
                    stale  = bool(real_pkg and ui_pkg and real_pkg != ui_pkg)
                    # Debounce the UIAutomator cache-bust: only kill it once a
                    # mismatch persists across cycles, so brief app-transition
                    # flicker doesn't SIGKILL an in-flight dump and corrupt it.
                    if stale:
                        self._stale_count += 1
                        if self._stale_count >= 2:
                            self._reset_ui = True
                            self._stale_count = 0
                    else:
                        self._stale_count = 0

                    result = {
                        **result,
                        "foreground_package": real_pkg or ui_pkg,
                        **({"elements": [], "stale_ui": True} if stale else {}),
                    }

                    self._cache   = result
                    self._updated = time.time()
                    if result.get("screen_width"):
                        self._adb.screen_width  = result["screen_width"]
                        self._adb.screen_height = result["screen_height"]

                # Push to all WebSocket subscribers immediately
                if self._ws_clients:
                    payload = {
                        **self._cache,
                        "age_ms": 0,
                        "screen_width":  self._adb.screen_width,
                        "screen_height": self._adb.screen_height,
                    }
                    await self._push(payload)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"UICache loop: {e}")
                await asyncio.sleep(1)


ui_cache = UICache(adb, interval=0.5)

MJPEG_BOUNDARY = b"--frame"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if adb.connect_usb():
        logger.info(f"Auto-connected to {adb.device_serial}")
    else:
        logger.info("No ADB device at startup – connect via web UI")
    video.start()
    ui_cache.start()
    await ui_cache.refresh_now()   # warm up immediately
    yield
    video.stop()
    ui_cache.stop()


app = FastAPI(title="Phone Control", lifespan=lifespan)


# ── Pydantic models ────────────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    host:   Optional[str] = None
    port:   int = 5555
    serial: Optional[str] = None

class TapRequest(BaseModel):
    x: int; y: int

class SwipeRequest(BaseModel):
    x1: int; y1: int; x2: int; y2: int
    duration_ms: int = 300

class KeyRequest(BaseModel):
    key: str

class TypeRequest(BaseModel):
    text: str

class ScrollRequest(BaseModel):
    direction: str = "down"
    x: Optional[int] = None
    y: Optional[int] = None
    amount: int = 1

class RoutineStep(BaseModel):
    action: str
    x:          Optional[int]   = None
    y:          Optional[int]   = None
    x1:         Optional[int]   = None
    y1:         Optional[int]   = None
    x2:         Optional[int]   = None
    y2:         Optional[int]   = None
    duration_ms: Optional[int]  = None
    duration:   Optional[float] = None
    key:        Optional[str]   = None
    text:       Optional[str]   = None
    amount:     Optional[int]   = None
    times:      Optional[int]   = None
    steps:      Optional[List[dict]] = None

class RoutineCreate(BaseModel):
    steps: List[RoutineStep]


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def ui():
    with open("static/index.html") as f:
        return f.read()


@app.get("/dynamicuipage", response_class=HTMLResponse)
async def dynamic_ui_page():
    return HTMLResponse(open("static/dynamicui.html").read())

@app.get("/mirror", response_class=HTMLResponse)
async def mirror_page():
    """Clean phone mirror — just the screen + clickable overlay. Designed for COMET."""
    return HTMLResponse(open("static/mirror.html").read())

@app.websocket("/ws/ui")
async def ws_ui(ws: WebSocket):
    """Push UI tree updates to clients as fast as UIAutomator can run."""
    await ws.accept()
    await ui_cache.subscribe(ws)
    try:
        while True:
            await ws.receive_text()   # keepalive; client sends "ping" periodically
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ui_cache.unsubscribe(ws)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(config: str = "", history: str = "", question: str = "What should I do next?"):
    """
    Visual dashboard for browser-based AI analysis.
    Shows the live phone screen alongside current context.
    Playwright screenshots this page and sends it to the AI chatbot.
    """
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0f172a; color:#e2e8f0; font-family:monospace; display:flex; height:100vh; }}
.screen {{ width:380px; flex-shrink:0; background:#000; display:flex; align-items:center; justify-content:center; }}
.screen img {{ max-width:100%; max-height:100vh; object-fit:contain; display:block; }}
.info {{ flex:1; padding:24px; overflow-y:auto; display:flex; flex-direction:column; gap:16px; }}
h2 {{ color:#38bdf8; font-size:14px; text-transform:uppercase; letter-spacing:2px; border-bottom:1px solid #1e40af; padding-bottom:6px; }}
pre {{ background:#1e293b; border-radius:6px; padding:14px; font-size:12px; white-space:pre-wrap; word-break:break-all; }}
.question {{ background:#1e3a5f; border-left:4px solid #38bdf8; padding:14px; border-radius:0 6px 6px 0; font-size:13px; }}
.instructions {{ background:#14532d; border-left:4px solid #22c55e; padding:14px; border-radius:0 6px 6px 0; font-size:12px; }}
</style>
</head>
<body>
<div class="screen">
  <img src="/api/screenshot?t=__TS__" onerror="this.alt='no screen'">
</div>
<div class="info">
  <div>
    <h2>Current Config Under Test</h2>
    <pre>{config or "(none)"}</pre>
  </div>
  <div>
    <h2>Test History</h2>
    <pre>{history or "(no previous attempts)"}</pre>
  </div>
  <div>
    <h2>Question for AI</h2>
    <div class="question">{question}</div>
  </div>
  <div>
    <h2>Required Response Format</h2>
    <div class="instructions">Respond with ONLY a JSON object — no other text:<br><br>
{{"state":"main_menu|settings|in_game|loading|crashed|error|unknown",
"description":"what you see in one sentence",
"game_running":true/false,
"fps":"number or null",
"error_text":"visible error or null",
"stable":true/false,
"actions":[
  {{"do":"tap","x":100,"y":200,"why":"reason"}},
  {{"do":"swipe","x1":100,"y1":300,"x2":100,"y2":100,"ms":300,"why":"reason"}},
  {{"do":"key","key":"back","why":"reason"}},
  {{"do":"type","text":"hello","why":"reason"}},
  {{"do":"wait","seconds":2,"why":"reason"}}
],
"done_with_config":false,
"config_failed":false,
"notes":"optional"}}
    </div>
  </div>
</div>
<script>
// Replace timestamp placeholder so screenshot is fresh
document.querySelector('img').src = '/api/screenshot?t=' + Date.now();
</script>
</body>
</html>""")


# -- device --------------------------------------------------------------------

@app.get("/api/status")
async def status():
    return adb.status()

@app.get("/api/devices")
async def list_devices():
    return adb.devices()

@app.post("/api/connect")
async def connect(req: ConnectRequest):
    if req.serial:
        adb.device_serial = req.serial
        adb._connected = True
        adb._refresh_screen_size()
    elif req.host:
        if not adb.connect_tcp(req.host, req.port):
            raise HTTPException(400, "Could not connect via TCP")
    else:
        if not adb.connect_usb():
            raise HTTPException(400, "No USB device found")
    # Restart video + UI cache so they pick up the new device
    video.stop(); video.start()
    await ui_cache.refresh_now()
    return {"ok": True, "device": adb.device_serial}

@app.post("/api/disconnect")
async def disconnect():
    adb.disconnect()
    return {"ok": True}

class PairRequest(BaseModel):
    host: str
    port: int   # pairing port from the "Pair device" sub-screen
    code: str   # 6-digit pairing code

class WirelessConnectRequest(BaseModel):
    host: str
    port: int   # connection port from the MAIN Wireless Debugging screen

@app.post("/api/pair")
async def pair(req: PairRequest):
    """
    Android 11+ wireless pairing only — does NOT auto-connect.
    After this succeeds the user must call /api/wireless_connect with the
    connection port shown on the MAIN Wireless Debugging screen.
    """
    import subprocess
    result = subprocess.run(
        ["adb", "pair", f"{req.host}:{req.port}"],
        input=req.code + "\n",
        capture_output=True, text=True, timeout=15,
    )
    out = result.stdout + result.stderr
    if result.returncode != 0 and "Successfully paired" not in out:
        raise HTTPException(400, f"Pairing failed: {out.strip()}")
    return {"ok": True, "pair_output": out.strip()}

@app.post("/api/wireless_connect")
async def wireless_connect(req: WirelessConnectRequest):
    """Connect to a paired Android 11+ device using the connection port."""
    if not adb.connect_tcp(req.host, req.port):
        raise HTTPException(400, f"Could not connect to {req.host}:{req.port}. Make sure Wireless Debugging is still enabled and the port matches the main screen.")
    video.stop()
    video.start()
    return {"ok": True, "device": adb.device_serial}

@app.post("/api/tcpip")
async def enable_tcpip():
    """Switch the currently-connected USB device to TCP mode on port 5555."""
    import subprocess
    result = subprocess.run(
        ["adb", "tcpip", "5555"],
        capture_output=True, text=True, timeout=10,
    )
    out = result.stdout + result.stderr
    return {"ok": result.returncode == 0, "output": out.strip()}


# -- app management -----------------------------------------------------------

def _get_foreground_package() -> str:
    """Return the package of the currently focused activity."""
    stdout, _, _ = adb._shell(
        "dumpsys", "activity", "activities"
    )
    for line in stdout.splitlines():
        if "topResumedActivity" in line or "ResumedActivity" in line:
            # format: ...ActivityRecord{... u0 pkg/activity ...}
            m = re.search(r'\bu0\s+([\w.]+)/', line)
            if m:
                return m.group(1)
    return ""

class ForceStopRequest(BaseModel):
    package: Optional[str] = None

@app.post("/api/force_stop")
async def force_stop(req: ForceStopRequest):
    """Force-stop an app. Omit package to kill the currently focused app."""
    _require_device()
    pkg = req.package
    if not pkg:
        pkg = await _in_thread(_get_foreground_package)
    if not pkg:
        raise HTTPException(400, "Could not determine foreground package")
    await _in_thread(adb._shell, "am", "force-stop", pkg)
    await ui_cache.refresh_now()
    return {"ok": True, "killed": pkg}

@app.get("/api/current_app")
async def current_app():
    """Return the package and activity currently in the foreground."""
    _require_device()
    pkg = await _in_thread(_get_foreground_package)
    return {"package": pkg or None}


# -- input ---------------------------------------------------------------------

def _in_thread(fn, *args):
    """Run a blocking ADB call in a thread so the event loop stays free."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, fn, *args)

@app.post("/api/tap")
async def tap(req: TapRequest):
    _require_device()
    await _in_thread(adb.tap, req.x, req.y)
    await ui_cache.refresh_now()
    return {"ok": True}

@app.post("/api/swipe")
async def swipe(req: SwipeRequest):
    _require_device()
    await _in_thread(adb.swipe, req.x1, req.y1, req.x2, req.y2, req.duration_ms)
    await ui_cache.refresh_now()
    return {"ok": True}

@app.post("/api/long_press")
async def long_press(req: TapRequest):
    _require_device()
    await _in_thread(adb.long_press, req.x, req.y)
    await ui_cache.refresh_now()
    return {"ok": True}

@app.post("/api/key")
async def key(req: KeyRequest):
    _require_device()
    await _in_thread(adb.keyevent, req.key)
    await ui_cache.refresh_now()
    return {"ok": True}

@app.post("/api/rotate")
async def rotate_screen():
    _require_device()
    def _do_rotate():
        cur = adb._shell("settings get system user_rotation")[0].strip()
        try:
            nxt = (int(cur) + 1) % 4
        except ValueError:
            nxt = 1
        adb._shell("settings put system accelerometer_rotation 0")
        adb._shell(f"settings put system user_rotation {nxt}")
    await _in_thread(_do_rotate)
    await ui_cache.refresh_now()
    return {"ok": True}

@app.post("/api/type")
async def type_text(req: TypeRequest):
    _require_device()
    await _in_thread(adb.type_text, req.text)
    return {"ok": True}

@app.post("/api/scroll")
async def scroll(req: ScrollRequest):
    _require_device()
    if req.direction == "up":
        await _in_thread(adb.scroll_up, req.x, req.y, req.amount)
    else:
        await _in_thread(adb.scroll_down, req.x, req.y, req.amount)
    await ui_cache.refresh_now()
    return {"ok": True}

@app.get("/api/ui")
async def ui_dump():
    """
    Cached UIAutomator tree — returns instantly from background refresh.
    age_ms tells you how old the cached result is.
    """
    _require_device()
    return await ui_cache.get()

@app.get("/api/screenshot")
async def screenshot():
    _require_device()
    # Prefer the latest frame already captured by the video pipeline
    frame = video.latest_frame
    if frame:
        ct = "image/jpeg" if frame[:2] == b"\xff\xd8" else "image/png"
        return Response(content=frame, media_type=ct)
    png = await adb.screenshot_png()
    if not png:
        raise HTTPException(503, "Screenshot failed")
    return Response(content=png, media_type="image/png")


# -- video stream --------------------------------------------------------------

@app.get("/video")
async def mjpeg_video():
    """
    MJPEG stream — open in browser as <img src="/video"> or use in ai_tester.py.
    If scrcpy+ffmpeg available: smooth ~15fps H.264-decoded JPEG.
    Otherwise: screencap fallback at ~4fps PNG wrapped as MJPEG.
    """
    async def generate():
        async for frame in video.frames():
            if frame is None:
                break
            ct = "image/jpeg" if frame[:2] == b"\xff\xd8" else "image/png"
            yield (
                MJPEG_BOUNDARY
                + b"\r\nContent-Type: " + ct.encode()
                + b"\r\nContent-Length: " + str(len(frame)).encode()
                + b"\r\n\r\n"
                + frame
                + b"\r\n"
            )

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- WebSocket screen (kept for browser UI) ------------------------------------

@app.websocket("/ws/screen")
async def ws_screen(ws: WebSocket):
    await ws.accept()
    try:
        async for frame in video.frames():
            if frame is None:
                break
            await ws.send_bytes(frame)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"ws_screen: {e}")


# -- routines ------------------------------------------------------------------

@app.get("/api/routines")
async def list_routines():
    return runner.load()

@app.post("/api/routines/{name}")
async def create_routine(name: str, body: RoutineCreate):
    steps = [s.model_dump(exclude_none=True) for s in body.steps]
    return runner.add(name, steps)

@app.post("/api/routines/{name}/run")
async def run_routine(name: str):
    try:
        runner.run(name)
        return {"ok": True, "running": name}
    except KeyError as e:
        raise HTTPException(404, str(e))

@app.post("/api/routines/{name}/stop")
async def stop_routine(name: str):
    runner.stop(name)
    return {"ok": True}

@app.delete("/api/routines/{name}")
async def delete_routine(name: str):
    if not runner.delete(name):
        raise HTTPException(404, "Not found")
    return {"ok": True}

@app.get("/api/routines/running")
async def running_routines():
    return runner.running()


# -- smart actions (UI-tree-aware, no coordinates needed) ----------------------

class TapTextRequest(BaseModel):
    text: str
    timeout: float = 0.0   # 0 = try once; >0 = wait up to N seconds

class WaitTextRequest(BaseModel):
    text: str
    timeout: float = 10.0
    gone: bool = False      # if True, wait until text DISAPPEARS

@app.get("/api/ui_texts")
async def ui_texts():
    """Compact visible-text snapshot — served from cache, instant."""
    _require_device()
    dump = await ui_cache.get()
    texts = [e["text"] for e in dump.get("elements", []) if e.get("text")]
    return {"texts": texts, "count": len(texts), "age_ms": dump.get("age_ms", 0)}

@app.post("/api/tap_text")
async def tap_text_route(req: TapTextRequest):
    """Find element by text and tap. Uses cache; triggers immediate refresh on success."""
    _require_device()
    deadline = time.time() + max(req.timeout, 0)
    while True:
        dump = await ui_cache.get()
        for el in dump.get("elements", []):
            if (el.get("text") or "") == req.text or (el.get("label") or "") == req.text:
                await _in_thread(adb.tap, el["center"]["x"], el["center"]["y"])
                await ui_cache.refresh_now()
                return {"ok": True, "tapped": req.text, "at": el["center"]}
        if time.time() >= deadline:
            raise HTTPException(404, f"Text '{req.text}' not found on screen")
        # Wait for the next cache refresh cycle before retrying
        await asyncio.sleep(0.5)
        await ui_cache.refresh_now()

@app.post("/api/wait_text")
async def wait_text_route(req: WaitTextRequest):
    """Wait until text appears (or disappears). Polls the background cache."""
    _require_device()
    deadline = time.time() + req.timeout
    while time.time() < deadline:
        dump = await ui_cache.get()
        texts = [e.get("text") or "" for e in dump.get("elements", [])]
        found = req.text in texts
        if found != req.gone:
            elapsed = round(req.timeout - (deadline - time.time()), 1)
            return {"ok": True, "found": found, "elapsed": elapsed}
        await asyncio.sleep(0.5)
        await ui_cache.refresh_now()
    raise HTTPException(408, f"Timeout: '{req.text}' {'still present' if req.gone else 'not found'} after {req.timeout}s")

# -- GameNative tester ---------------------------------------------------------

class TesterMatrix(BaseModel):
    dx_wrapper:          Optional[List[str]]  = None
    dxvk_version:        Optional[List[str]]  = None
    adrenotools_turnip:  Optional[List[bool]] = None
    present_modes:       Optional[List[str]]  = None
    renderer_present_mode: Optional[List[str]] = None
    test_duration:       int = 45

@app.post("/api/tester/start")
async def tester_start(matrix: TesterMatrix):
    _require_device()
    m = {k: v for k, v in matrix.model_dump().items() if v is not None}
    if not tester.start(m):
        raise HTTPException(400, "Already running")
    return {"ok": True}

@app.post("/api/tester/stop")
async def tester_stop():
    tester.stop()
    return {"ok": True}

@app.get("/api/tester/status")
async def tester_status():
    return tester.status()

@app.get("/api/tester/log")
async def tester_log():
    return StreamingResponse(
        tester.log_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _require_device():
    if not adb._connected:
        raise HTTPException(503, "No device connected")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False)
