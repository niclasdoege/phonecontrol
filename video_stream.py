"""
Video streaming pipeline — priority order:
  1. adb screenrecord → ffmpeg → MJPEG   (works on any Android 5+, no scrcpy needed)
  2. scrcpy → ffmpeg → MJPEG             (if scrcpy is installed)
  3. adb screencap fallback              (always works, ~4fps)
"""

import asyncio
import logging
import shutil
from typing import AsyncIterator

log = logging.getLogger(__name__)

SCRCPY_OK = shutil.which("scrcpy") is not None
FFMPEG_OK  = shutil.which("ffmpeg") is not None

_SOI = b"\xff\xd8\xff"
_EOI = b"\xff\xd9"


class VideoStreamer:
    def __init__(self, adb, fps: int = 15, width: int = 720):
        self._adb     = adb
        self._fps     = fps
        self._width   = width
        self._running = False
        self._latest_frame: bytes | None = None
        self._subscribers: list[asyncio.Queue] = []
        self._loop_task: asyncio.Task | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._read_loop())
        log.info(f"VideoStreamer started (scrcpy={SCRCPY_OK}, ffmpeg={FFMPEG_OK})")

    def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
        for q in self._subscribers:
            q.put_nowait(None)

    async def frames(self) -> AsyncIterator[bytes]:
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        self._subscribers.append(q)
        try:
            while True:
                frame = await q.get()
                if frame is None:
                    break
                yield frame
        finally:
            self._subscribers.remove(q)

    @property
    def latest_frame(self) -> bytes | None:
        return self._latest_frame

    # ── internal ───────────────────────────────────────────────────────────────

    def _broadcast(self, frame: bytes):
        self._latest_frame = frame
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    async def _read_loop(self):
        screenrecord_failures = 0
        while self._running:
            if not self._adb._connected:
                await asyncio.sleep(1)
                continue

            # Try persistent screencap stream first (single connection, reliable)
            log.info("Starting persistent screencap stream…")
            success = await self._run_screencap_stream()
            if success or not self._running:
                continue

            # Fallback: individual screencap calls
            log.info("Screencap stream failed, using single-shot screencap fallback")
            await self._run_screencap_fallback()

            if self._running:
                await asyncio.sleep(1)

    async def _run_screencap_stream(self) -> bool:
        """
        Persistent shell session: `adb exec-out sh -c 'while true; do screencap -p; done'`
        Streams concatenated PNGs over a single ADB connection — faster than per-frame adb calls.
        Splits frames on PNG magic bytes + IEND marker.
        Returns True if at least one frame was produced.
        """
        _PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
        _IEND      = b"IEND\xae\x42\x60\x82"
        serial_args = ["-s", self._adb.device_serial] if self._adb.device_serial else []

        interval_ms = max(100, int(1000 / min(self._fps, 5)))  # at most 5fps via screencap
        cmd = [
            "adb", *serial_args, "exec-out",
            "sh", "-c",
            f"while true; do screencap -p; sleep {interval_ms/1000:.2f}; done",
        ]
        log.info(f"screencap stream: {interval_ms}ms interval (max {1000//interval_ms}fps)")

        got_frame = False
        proc = None
        consecutive_empty = 0
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            buf = b""
            while self._running:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(131072), timeout=5)
                except asyncio.TimeoutError:
                    log.warning("screencap stream: no data for 5s")
                    break
                if not chunk:
                    break
                buf += chunk

                # Extract complete PNG frames
                while True:
                    start = buf.find(_PNG_MAGIC)
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(_IEND, start + 8)
                    if end == -1:
                        # Keep from start so next chunk completes the frame
                        buf = buf[start:]
                        break
                    frame = buf[start : end + 8]
                    buf = buf[end + 8:]
                    if frame.startswith(_PNG_MAGIC):
                        self._broadcast(frame)
                        got_frame = True
                        consecutive_empty = 0
                    else:
                        consecutive_empty += 1
                        if consecutive_empty > 10:
                            log.warning("screencap stream: too many bad frames, restarting")
                            break
        except Exception as e:
            log.warning(f"screencap stream: {e}")
        finally:
            if proc:
                try: proc.terminate()
                except Exception: pass

        return got_frame

    async def _run_screenrecord_pipeline(self) -> bool:
        """
        Runs via shell pipe so bash connects adb stdout → ffmpeg stdin natively.
        Returns True if at least one JPEG frame was produced.
        First frame must arrive within 8s or we give up fast.
        """
        serial = self._adb.device_serial or ""
        s_flag = f"-s {serial}" if serial else ""

        sw, sh = self._adb.screen_width, self._adb.screen_height
        if sw and sh:
            h = round(self._width * sh / sw)
            h = h if h % 2 == 0 else h + 1
            w = self._width if self._width % 2 == 0 else self._width + 1
            size = f"{w}x{h}"
        else:
            size = f"{self._width}x{round(self._width * 16/9)}"

        shell_cmd = (
            f"adb {s_flag} exec-out screenrecord --output-format=h264 --size={size} - "
            f"| ffmpeg -hide_banner -loglevel error"
            f" -fflags +nobuffer+flush_packets+discardcorrupt"
            f" -f h264 -probesize 32 -analyzeduration 0"
            f" -i pipe:0"
            f" -f image2pipe -vcodec mjpeg -q:v 5 -r {self._fps}"
            f" -flush_packets 1 pipe:1"
        )

        log.info(f"screenrecord shell pipe: size={size}")
        got_frame = False
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            buf = b""
            first_frame_timeout = 3  # give up fast — screencap is the reliable fallback
            while self._running:
                timeout = first_frame_timeout if not got_frame else 10
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=timeout)
                except asyncio.TimeoutError:
                    log.warning(f"screenrecord: no {'first ' if not got_frame else ''}frame in {timeout}s")
                    break
                if not chunk:
                    break
                buf += chunk
                buf, produced = self._extract_frames(buf)
                if produced:
                    got_frame = True
                    first_frame_timeout = 10  # relax after first frame
        except Exception as e:
            log.warning(f"screenrecord pipeline: {e}")
        finally:
            if proc:
                try: proc.terminate()
                except Exception: pass

        return got_frame

    async def _run_scrcpy_pipeline(self):
        serial_args = ["-s", self._adb.device_serial] if self._adb.device_serial else []
        scrcpy_cmd = [
            "scrcpy", *serial_args,
            "--no-display", "--record", "pipe:1", "--record-format", "h264",
            f"--max-fps={self._fps}", "--video-bit-rate=2M",
        ]
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "h264", "-i", "pipe:0",
            "-f", "image2pipe", "-vcodec", "mjpeg",
            "-q:v", "6", "-vf", f"scale={self._width}:-2",
            "-r", str(self._fps), "pipe:1",
        ]
        log.info("Starting scrcpy|ffmpeg pipeline")
        try:
            scrcpy = await asyncio.create_subprocess_exec(
                *scrcpy_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdin=scrcpy.stdout,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            buf = b""
            while self._running:
                try:
                    chunk = await asyncio.wait_for(ffmpeg.stdout.read(32768), timeout=8)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                buf += chunk
                buf, _ = self._extract_frames(buf)
        except Exception as e:
            log.warning(f"scrcpy pipeline: {e}")
        finally:
            for p in [locals().get("ffmpeg"), locals().get("scrcpy")]:
                if p:
                    try: p.terminate()
                    except Exception: pass

    async def _run_screencap_fallback(self, retry_after=None):
        """Run screencap loop until stopped or connection lost. Max 1fps to not overwhelm wireless ADB."""
        interval = max(0.5, 1.0 / min(self._fps, 2))
        log.info(f"Using screencap fallback at {1/interval:.1f}fps")
        consecutive_failures = 0
        while self._running:
            if not self._adb._connected:
                await asyncio.sleep(1)
                continue
            frame = await self._adb.screenshot_png()
            if frame:
                consecutive_failures = 0
                self._broadcast(frame)
            else:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    log.warning("screencap: 5 consecutive failures — marking device disconnected")
                    self._adb._connected = False
                    consecutive_failures = 0
                    break
            await asyncio.sleep(interval)

    def _extract_frames(self, buf: bytes) -> tuple[bytes, bool]:
        """Extract all complete JPEG frames from buf. Returns (remaining_buf, got_any)."""
        got = False
        while True:
            start = buf.find(_SOI)
            if start == -1:
                return b"", got
            end = buf.find(_EOI, start + 2)
            if end == -1:
                return buf[start:], got
            self._broadcast(buf[start : end + 2])
            buf = buf[end + 2:]
            got = True
        return buf, got
