"""
GameNative automated config tester.
Navigates via UIAutomator tree (no screenshots for menus).
Screenshots only when observing game state.
"""
import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

log = logging.getLogger(__name__)

GAME_NAME = "Beacon.Pines.v1.1.1"


@dataclass
class TestResult:
    config: dict
    outcome: str  # "stable" | "crash" | "no_launch" | "skipped" | "error"
    notes: str = ""
    duration_s: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self):
        return {
            "config": self.config,
            "outcome": self.outcome,
            "notes": self.notes,
            "duration_s": round(self.duration_s, 1),
            "timestamp": self.timestamp,
        }


class GNTester:
    def __init__(self, adb):
        self._adb = adb
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._log_q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._results: list[TestResult] = []
        self._current_config: Optional[dict] = None
        self._step = "idle"
        self._total = 0
        self._done = 0

    # ── public ──────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "running": self._running,
            "step": self._step,
            "current_config": self._current_config,
            "done": self._done,
            "total": self._total,
            "results": [r.to_dict() for r in self._results],
        }

    def start(self, matrix: dict) -> bool:
        if self._running:
            return False
        self._running = True
        self._results = []
        self._done = 0
        self._task = asyncio.create_task(self._run(matrix))
        return True

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        self._step = "stopped"

    async def log_stream(self) -> AsyncIterator[str]:
        """SSE generator — yields 'data: {...}\\n\\n' log entries."""
        while self._running or not self._log_q.empty():
            try:
                entry = await asyncio.wait_for(self._log_q.get(), timeout=1.0)
                yield f"data: {json.dumps(entry)}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
        yield f"data: {json.dumps({'level':'done','msg':'Stream ended','t':time.time()})}\n\n"

    # ── UI helpers ───────────────────────────────────────────────────────────────

    def _ui(self) -> dict:
        return self._adb.ui_dump()

    def _find(self, text: str, ui: dict | None = None) -> Optional[dict]:
        if ui is None:
            ui = self._ui()
        for e in ui.get("elements", []):
            if (e.get("text") or "") == text or (e.get("label") or "") == text or (e.get("content_desc") or "") == text:
                return e
        return None

    def _find_contains(self, substr: str, ui: dict | None = None) -> Optional[dict]:
        if ui is None:
            ui = self._ui()
        s = substr.lower()
        for e in ui.get("elements", []):
            if s in (e.get("text") or "").lower():
                return e
        return None

    def _texts(self, ui: dict | None = None) -> list[str]:
        if ui is None:
            ui = self._ui()
        return [e.get("text") or "" for e in ui.get("elements", []) if e.get("text")]

    async def _tap(self, x: int, y: int, wait: float = 0.7):
        self._adb.tap(x, y)
        await asyncio.sleep(wait)

    async def _tap_el(self, el: dict, wait: float = 0.7):
        c = el["center"]
        await self._tap(c["x"], c["y"], wait)

    async def _tap_text(self, text: str, timeout: float = 8.0, wait: float = 0.7) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ui = self._ui()
            el = self._find(text, ui)
            if el:
                await self._tap_el(el, wait)
                return True
            await asyncio.sleep(0.4)
        self._emit("warn", f"'{text}' not found after {timeout:.0f}s")
        return False

    async def _wait_text(self, text: str, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._find(text):
                return True
            await asyncio.sleep(0.4)
        return False

    async def _back(self, n: int = 1, wait: float = 0.6):
        for _ in range(n):
            self._adb.keyevent("BACK")
            await asyncio.sleep(wait)

    # ── navigation ───────────────────────────────────────────────────────────────

    async def _ensure_gn_main(self, retries: int = 8):
        """Press BACK until we see Play/Options on the GN main screen."""
        for _ in range(retries):
            t = self._texts()
            if "Play" in t or "Options" in t:
                return True
            self._adb.keyevent("BACK")
            await asyncio.sleep(1.0)
        return False

    async def _open_options_menu(self) -> bool:
        """Tap the Options button on the GN game card."""
        if not await self._tap_text("Options", timeout=5):
            return False
        return await self._wait_text("Edit container", timeout=6)

    async def _open_edit_container(self) -> bool:
        if not await self._tap_text("Edit container", timeout=5):
            return False
        return await self._wait_text("Graphics", timeout=8)

    async def _open_graphics_tab(self) -> bool:
        ui = self._ui()
        el = self._find("Graphics", ui)
        if not el:
            return False
        await self._tap_el(el, wait=1.0)
        # Confirm we see at least one Graphics-specific item
        return await self._wait_text("DX Wrapper", timeout=5)

    async def _scroll_to_top(self):
        sw = self._adb.screen_width or 1116
        sh = self._adb.screen_height or 2480
        self._adb.swipe(sw // 2, sh // 3, sw // 2, sh * 4 // 5, 250)
        await asyncio.sleep(0.4)
        self._adb.swipe(sw // 2, sh // 3, sw // 2, sh * 4 // 5, 250)
        await asyncio.sleep(0.4)

    async def _scroll_down_a_bit(self):
        sw = self._adb.screen_width or 1116
        sh = self._adb.screen_height or 2480
        self._adb.swipe(sw // 2, sh * 3 // 5, sw // 2, sh // 3, 300)
        await asyncio.sleep(0.4)

    # ── setting helpers ───────────────────────────────────────────────────────────

    async def _set_dropdown(self, label: str, value: str) -> bool:
        """
        Tap a dropdown row and select `value` from the dialog that appears.
        GameNative dropdowns show current value directly below the label text.
        Tapping either label or value row opens the picker.
        """
        # Try scrolling up first so the item is definitely visible
        await self._scroll_to_top()
        ui = self._ui()
        el = self._find(label, ui)
        if not el:
            # Maybe below fold — scroll down and retry
            await self._scroll_down_a_bit()
            ui = self._ui()
            el = self._find(label, ui)
        if not el:
            self._emit("warn", f"Dropdown '{label}' not found")
            return False

        await self._tap_el(el, wait=1.2)

        # Picker dialog should now show — find and tap the desired value
        found = await self._tap_text(value, timeout=5, wait=0.5)
        if not found:
            self._emit("warn", f"Option '{value}' not in picker — trying scroll")
            await self._scroll_down_a_bit()
            found = await self._tap_text(value, timeout=3, wait=0.5)

        await asyncio.sleep(0.5)
        return found

    def _find_switch_near(self, label_el: dict, ui: dict) -> Optional[dict]:
        """Find a Switch widget vertically close to a label element."""
        ly = label_el["center"]["y"]
        for e in ui.get("elements", []):
            cls = e.get("class", "")
            if "Switch" in cls or "Toggle" in cls or "CompoundButton" in cls:
                if abs(e["center"]["y"] - ly) < 120:
                    return e
        return None

    async def _set_toggle(self, label: str, want_on: bool) -> bool:
        """Find a toggle by its label text and set it to on/off."""
        await self._scroll_to_top()
        ui = self._ui()
        label_el = self._find(label, ui)
        if not label_el:
            await self._scroll_down_a_bit()
            ui = self._ui()
            label_el = self._find(label, ui)
        if not label_el:
            self._emit("warn", f"Toggle '{label}' not found")
            return False

        sw_el = self._find_switch_near(label_el, ui)
        if sw_el:
            current = sw_el.get("checked", False)
            if bool(current) == want_on:
                self._emit("info", f"Toggle '{label}' already {'ON' if want_on else 'OFF'}")
                return True
            await self._tap_el(sw_el)
        else:
            # Fallback: tap the label row (usually the whole row is the toggle)
            await self._tap_el(label_el)

        await asyncio.sleep(0.4)
        # Verify
        ui2 = self._ui()
        label2 = self._find(label, ui2)
        sw2 = self._find_switch_near(label2, ui2) if label2 else None
        final = sw2.get("checked", None) if sw2 else None
        if final is not None:
            ok = bool(final) == want_on
            self._emit("info", f"Toggle '{label}' → {'ON' if final else 'OFF'}" + ("" if ok else " (mismatch!)"))
        return True

    async def _apply_config(self, config: dict):
        """Apply all settings in config to the open Graphics tab."""
        dx = config.get("dx_wrapper")
        if dx:
            self._emit("info", f"DX Wrapper → {dx}")
            await self._set_dropdown("DX Wrapper", dx)

        if dx == "DXVK" and config.get("dxvk_version"):
            self._emit("info", f"DXVK Version → {config['dxvk_version']}")
            await self._set_dropdown("DXVK Version", config["dxvk_version"])

        if "adrenotools_turnip" in config:
            want = config["adrenotools_turnip"]
            self._emit("info", f"Adrenotools Turnip → {'ON' if want else 'OFF'}")
            await self._set_toggle("Use Adrenotools Turnip", want)

        if config.get("present_modes"):
            self._emit("info", f"Present Modes → {config['present_modes']}")
            await self._set_dropdown("Present Modes", config["present_modes"])

        if config.get("renderer_present_mode"):
            self._emit("info", f"Renderer Present Mode → {config['renderer_present_mode']}")
            await self._set_dropdown("Renderer Present Mode", config["renderer_present_mode"])

    # ── game observation ─────────────────────────────────────────────────────────

    async def _launch_and_observe(self, duration: float) -> dict:
        if not await self._tap_text("Play", timeout=8):
            return {"outcome": "no_launch", "notes": "Play button not found"}

        self._emit("info", f"Game launching — waiting 8s for startup")
        await asyncio.sleep(8)

        start = time.time()
        check_interval = 6.0
        while self._running and (time.time() - start) < duration:
            remaining = duration - (time.time() - start)
            self._emit("info", f"Observing… {remaining:.0f}s remaining")
            await asyncio.sleep(check_interval)

            ui = self._ui()
            texts = self._texts(ui)

            # Game crashed back to GN main screen
            if any(t in texts for t in ["Play", "Options", "Uninstall"]):
                return {"outcome": "crash", "notes": "Returned to GN main — game exited"}

            # Android "App stopped" dialog
            stopped = next((t for t in texts if "stopped" in t.lower() or "not responding" in t.lower()), None)
            if stopped:
                return {"outcome": "crash", "notes": stopped}

        if not self._running:
            return {"outcome": "skipped", "notes": "Stopped by user"}

        return {"outcome": "stable", "notes": f"Stable for {duration:.0f}s"}

    # ── main loop ────────────────────────────────────────────────────────────────

    async def _run(self, matrix: dict):
        try:
            configs = _expand_matrix(matrix)
            self._total = len(configs)
            self._emit("info", f"Starting {self._total} config(s) — {matrix.get('test_duration', 45)}s per run")

            for i, cfg in enumerate(configs):
                if not self._running:
                    break
                self._current_config = cfg
                self._emit("start", f"Config {i+1}/{self._total}: {_config_label(cfg)}")

                result = await self._test_one(cfg)
                self._results.append(result)
                self._done = i + 1
                icon = {"stable": "✅", "crash": "❌", "no_launch": "⚠️", "skipped": "⏭", "error": "🔥"}.get(result.outcome, "?")
                self._emit("result", f"{icon} {result.outcome.upper()}: {result.notes}", result=result.to_dict())

            self._emit("done", f"Finished {self._done}/{self._total} configs")
        except asyncio.CancelledError:
            self._emit("info", "Cancelled")
        except Exception as e:
            self._emit("error", f"Fatal: {e}")
            log.exception("GNTester")
        finally:
            self._running = False
            self._step = "idle"
            self._current_config = None

    async def _test_one(self, config: dict) -> TestResult:
        t0 = time.time()
        try:
            self._step = "navigating"
            self._emit("info", "Navigating to GN main screen")
            if not await self._ensure_gn_main():
                return TestResult(config=config, outcome="error", notes="Could not reach GN main screen", duration_s=time.time()-t0)

            self._emit("info", "Opening Options → Edit container")
            if not await self._open_options_menu():
                return TestResult(config=config, outcome="error", notes="Options menu failed", duration_s=time.time()-t0)
            if not await self._open_edit_container():
                return TestResult(config=config, outcome="error", notes="Edit container failed", duration_s=time.time()-t0)

            self._emit("info", "Opening Graphics tab")
            if not await self._open_graphics_tab():
                return TestResult(config=config, outcome="error", notes="Graphics tab failed", duration_s=time.time()-t0)

            self._step = "configuring"
            await self._apply_config(config)

            # Navigate back (config saves automatically)
            self._emit("info", "Saving config (back × 2)")
            await self._back(2, wait=0.8)

            self._step = "launching"
            obs = await self._launch_and_observe(config.get("test_duration", 45))

            self._step = "returning"
            self._emit("info", "Returning to GN main")
            await self._return_to_gn()

            return TestResult(config=config, outcome=obs["outcome"], notes=obs["notes"], duration_s=time.time()-t0)

        except Exception as e:
            log.exception("_test_one")
            await self._return_to_gn()
            return TestResult(config=config, outcome="error", notes=str(e), duration_s=time.time()-t0)

    async def _return_to_gn(self):
        for _ in range(12):
            t = self._texts()
            if "Play" in t or "Options" in t:
                return
            self._adb.keyevent("BACK")
            await asyncio.sleep(1.2)

    # ── logging ──────────────────────────────────────────────────────────────────

    def _emit(self, level: str, msg: str, **extra):
        entry = {"t": time.time(), "level": level, "msg": msg, **extra}
        log.info(f"[GN] {msg}")
        try:
            self._log_q.put_nowait(entry)
        except Exception:
            pass


# ── helpers ───────────────────────────────────────────────────────────────────

def _expand_matrix(matrix: dict) -> list[dict]:
    duration = matrix.get("test_duration", 45)
    keys = [k for k in matrix if k not in ("test_duration",) and isinstance(matrix[k], list)]
    if not keys:
        return [{"test_duration": duration}]
    combos = []
    for vals in itertools.product(*[matrix[k] for k in keys]):
        d = dict(zip(keys, vals))
        d["test_duration"] = duration
        combos.append(d)
    return combos


def _config_label(cfg: dict) -> str:
    parts = []
    for k, v in cfg.items():
        if k == "test_duration":
            continue
        if isinstance(v, bool):
            parts.append(f"{k}={'ON' if v else 'OFF'}")
        else:
            parts.append(f"{k}={v}")
    return " | ".join(parts)
