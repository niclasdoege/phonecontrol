import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adb_control import ADBControl

logger = logging.getLogger(__name__)
ROUTINES_FILE = Path(__file__).parent / "routines.json"


class RoutineRunner:
    def __init__(self, adb: "ADBControl"):
        self.adb = adb
        self._running: dict[str, asyncio.Task] = {}

    # ── persistence ────────────────────────────────────────────────────────────

    def load(self) -> dict:
        if ROUTINES_FILE.exists():
            try:
                return json.loads(ROUTINES_FILE.read_text())
            except Exception:
                return {}
        return {}

    def save(self, routines: dict):
        ROUTINES_FILE.write_text(json.dumps(routines, indent=2))

    def add(self, name: str, steps: list[dict]) -> dict:
        routines = self.load()
        routines[name] = {"steps": steps, "created": time.time()}
        self.save(routines)
        return routines[name]

    def delete(self, name: str) -> bool:
        routines = self.load()
        if name not in routines:
            return False
        del routines[name]
        self.save(routines)
        return True

    # ── execution ──────────────────────────────────────────────────────────────

    def run(self, name: str) -> bool:
        routines = self.load()
        if name not in routines:
            raise KeyError(f"Routine '{name}' not found")
        steps = routines[name]["steps"]

        async def _execute():
            for step in steps:
                action = step.get("action", "")
                try:
                    await self._execute_step(action, step)
                except Exception as e:
                    logger.error(f"Step {action} failed: {e}")

        task = asyncio.create_task(_execute())
        self._running[name] = task
        task.add_done_callback(lambda _: self._running.pop(name, None))
        return True

    def stop(self, name: str) -> bool:
        if name in self._running:
            self._running[name].cancel()
            return True
        return False

    def running(self) -> list[str]:
        return list(self._running.keys())

    async def _execute_step(self, action: str, step: dict):
        adb = self.adb
        if action == "tap":
            adb.tap(step["x"], step["y"])

        elif action == "swipe":
            adb.swipe(step["x1"], step["y1"], step["x2"], step["y2"],
                      step.get("duration_ms", 300))

        elif action == "long_press":
            adb.long_press(step["x"], step["y"], step.get("duration_ms", 800))

        elif action == "key":
            adb.keyevent(step["key"])

        elif action == "type":
            adb.type_text(step["text"])

        elif action == "scroll_up":
            adb.scroll_up(step.get("x"), step.get("y"), step.get("amount", 1))

        elif action == "scroll_down":
            adb.scroll_down(step.get("x"), step.get("y"), step.get("amount", 1))

        elif action == "sleep":
            await asyncio.sleep(step.get("duration", 0.5))

        elif action == "tap_text":
            text = step["text"]
            timeout = step.get("timeout", 5)
            deadline = time.time() + timeout
            while True:
                dump = adb.ui_dump()
                for el in dump.get("elements", []):
                    if (el.get("text") or "") == text or (el.get("label") or "") == text:
                        adb.tap(el["center"]["x"], el["center"]["y"])
                        await asyncio.sleep(0.6)
                        return
                if time.time() >= deadline:
                    logger.warning(f"tap_text: '{text}' not found after {timeout}s")
                    return
                await asyncio.sleep(0.4)

        elif action == "wait_text":
            text = step["text"]
            timeout = step.get("timeout", 10)
            gone = step.get("gone", False)
            deadline = time.time() + timeout
            while time.time() < deadline:
                dump = adb.ui_dump()
                texts = [e.get("text") or "" for e in dump.get("elements", [])]
                found = text in texts
                if found != gone:
                    return
                await asyncio.sleep(0.5)
            logger.warning(f"wait_text: '{text}' timeout after {timeout}s")

        elif action == "repeat":
            sub_steps = step.get("steps", [])
            for _ in range(step.get("times", 1)):
                for s in sub_steps:
                    await self._execute_step(s.get("action", ""), s)
                    await asyncio.sleep(0.05)
        else:
            logger.warning(f"Unknown action: {action}")
