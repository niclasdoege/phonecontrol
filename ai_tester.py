#!/usr/bin/env python3
"""
Autonomous GameNative config tester.

Uses Claude vision to navigate the GameNative app on your Android phone,
apply different configurations, and find one that runs a game stably.

Usage:
    ANTHROPIC_API_KEY=sk-... python ai_tester.py [--game "Game Name"] [--server http://localhost:8080]

Requires:
    - phonecontrol server running (python -m uvicorn app:app --port 8080)
    - Phone connected via ADB and USB debugging enabled
    - anthropic package: pip install anthropic
"""

import anthropic
import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from configs import CONFIGS, STABILITY_WINDOW, OBSERVE_INTERVAL, MAX_ACTIONS_PER_CONFIG

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"tester_{datetime.now():%Y%m%d_%H%M%S}.log"),
    ],
)
log = logging.getLogger(__name__)

RESULTS_FILE = Path("results.json")


# ── Phone server helpers ───────────────────────────────────────────────────────

class Phone:
    def __init__(self, server: str):
        self.base = server.rstrip("/")
        self._session = requests.Session()

    def _post(self, path: str, body: dict, silent: bool = False):
        try:
            r = self._session.post(f"{self.base}{path}", json=body, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if not silent:
                log.warning(f"POST {path} failed: {e}")
            return {}

    def screenshot_b64(self) -> str | None:
        try:
            r = self._session.get(f"{self.base}/api/screenshot", timeout=30)
            r.raise_for_status()
            return base64.standard_b64encode(r.content).decode()
        except Exception as e:
            log.warning(f"Screenshot failed: {e}")
            return None

    def tap(self, x: int, y: int, wait: float = 0.6):
        log.info(f"  tap({x}, {y})")
        self._post("/api/tap", {"x": x, "y": y})
        time.sleep(wait)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, wait: float = 0.6):
        log.info(f"  swipe({x1},{y1} → {x2},{y2})")
        self._post("/api/swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms})
        time.sleep(wait)

    def long_press(self, x: int, y: int, wait: float = 1.0):
        log.info(f"  long_press({x}, {y})")
        self._post("/api/long_press", {"x": x, "y": y})
        time.sleep(wait)

    def key(self, k: str, wait: float = 0.5):
        log.info(f"  key({k})")
        self._post("/api/key", {"key": k})
        time.sleep(wait)

    def type_text(self, text: str, wait: float = 0.5):
        log.info(f"  type({text!r})")
        self._post("/api/type", {"text": text})
        time.sleep(wait)

    def is_alive(self) -> bool:
        try:
            r = self._session.get(f"{self.base}/api/status", timeout=5)
            return r.ok and r.json().get("connected", False)
        except Exception:
            return False


# ── Claude helper ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an autonomous agent controlling an Android phone via ADB to test GameNative app configurations.

Your goal: find a configuration that lets a Windows game run inside GameNative without crashing or exiting,
ideally running smoothly for at least 30 seconds.

You receive screenshots and must respond with a JSON object. Never include text outside the JSON.

Response schema:
{
  "state": "main_menu | game_list | settings | in_game | loading | crashed | error_dialog | unknown",
  "description": "1-sentence description of what you see",
  "game_running": true/false,
  "fps": "number string or null",
  "error_text": "visible error/crash text or null",
  "stable": true/false,   // game running smoothly with no obvious problems
  "actions": [
    // Each action is one of:
    {"do": "tap",        "x": 100, "y": 200,   "why": "..."},
    {"do": "swipe",      "x1":100,"y1":300,"x2":100,"y2":100,"ms":300, "why":"..."},
    {"do": "long_press", "x": 100, "y": 200,   "why": "..."},
    {"do": "key",        "key": "back",          "why": "..."},
    {"do": "type",       "text": "hello",        "why": "..."},
    {"do": "wait",       "seconds": 3,           "why": "..."}
  ],
  "done_with_config": true/false,  // set true when you've finished applying this config and want to observe
  "config_failed": true/false,     // set true if game crashed/frozen and we should move to next config
  "notes": "optional observations"
}

Rules:
- Be precise with coordinates — they must be within visible UI elements.
- Keep actions minimal per turn; take one screenshot after each significant action.
- If you see an error dialog, dismiss it first (tap OK/Close/Back).
- If the game has been running without crashing for the observation window, report stable=true.
- If you cannot find a setting mentioned in the config, skip it and note it.
- Screen coordinates are absolute phone pixels, not display pixels.
"""

def ask_claude(client: anthropic.Anthropic, screenshot_b64: str, user_msg: str) -> dict:
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    },
                    {"type": "text", "text": user_msg},
                ],
            }],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Claude returned non-JSON: {e}\nRaw: {raw[:400]}")
        return {"state": "unknown", "description": "parse error", "actions": [], "game_running": False,
                "stable": False, "done_with_config": False, "config_failed": False}
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return {"state": "unknown", "description": str(e), "actions": [], "game_running": False,
                "stable": False, "done_with_config": False, "config_failed": False}


def execute_actions(phone: Phone, actions: list[dict]):
    for a in actions:
        do = a.get("do", "")
        why = a.get("why", "")
        log.info(f"    → {do}: {why}")
        if do == "tap":
            phone.tap(int(a["x"]), int(a["y"]))
        elif do == "swipe":
            phone.swipe(int(a["x1"]), int(a["y1"]), int(a["x2"]), int(a["y2"]),
                        int(a.get("ms", 300)))
        elif do == "long_press":
            phone.long_press(int(a["x"]), int(a["y"]))
        elif do == "key":
            phone.key(a["key"])
        elif do == "type":
            phone.type_text(a["text"])
        elif do == "wait":
            secs = float(a.get("seconds", 1))
            log.info(f"  waiting {secs}s…")
            time.sleep(secs)


# ── Results ────────────────────────────────────────────────────────────────────

def load_results() -> list[dict]:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return []

def save_result(config: dict, outcome: str, notes: str, duration: float):
    results = load_results()
    results.append({
        "config": config,
        "outcome": outcome,   # "stable" | "crashed" | "skipped" | "timeout"
        "notes": notes,
        "duration_s": round(duration, 1),
        "timestamp": datetime.now().isoformat(),
    })
    RESULTS_FILE.write_text(json.dumps(results, indent=2))
    log.info(f"Result saved: {outcome}")


# ── Main loop ──────────────────────────────────────────────────────────────────

def test_config(phone: Phone, client: anthropic.Anthropic, config: dict, game: str) -> str:
    """
    Returns: "stable" | "crashed" | "timeout"
    """
    log.info(f"\n{'─'*60}")
    log.info(f"Testing config: {json.dumps(config)}")
    log.info(f"{'─'*60}")

    config_str = ", ".join(f"{k}={v}" for k, v in config.items())
    action_count = 0
    observe_start = None
    phase = "apply"   # apply | observe

    while action_count < MAX_ACTIONS_PER_CONFIG:
        shot = phone.screenshot_b64()
        if shot is None:
            log.warning("No screenshot — phone disconnected?")
            time.sleep(3)
            continue

        if phase == "apply":
            prompt = (
                f"Target game: \"{game}\"\n"
                f"Config to apply: {config_str}\n\n"
                f"Current action count: {action_count}/{MAX_ACTIONS_PER_CONFIG}\n\n"
                "Navigate to GameNative settings, apply the config values, then launch the game. "
                "Set done_with_config=true once you've launched the game and are ready to observe it. "
                "Set config_failed=true if the game immediately crashes or you cannot proceed."
            )
        else:
            elapsed = time.time() - observe_start
            remaining = STABILITY_WINDOW - elapsed
            prompt = (
                f"Target game: \"{game}\"\n"
                f"Config under test: {config_str}\n\n"
                f"OBSERVATION PHASE: {elapsed:.0f}s elapsed, {remaining:.0f}s remaining.\n\n"
                "Is the game running stably? Report fps if visible. "
                f"If it has run without crashing for {STABILITY_WINDOW}s, set stable=true. "
                "If it crashed/exited, set config_failed=true."
            )

        analysis = ask_claude(client, shot, prompt)
        log.info(f"  state={analysis.get('state')} | game_running={analysis.get('game_running')} | "
                 f"stable={analysis.get('stable')} | fps={analysis.get('fps')}")
        if analysis.get("description"):
            log.info(f"  Claude: {analysis['description']}")

        # Execute any actions
        actions = analysis.get("actions", [])
        execute_actions(phone, actions)
        action_count += len(actions)

        # State transitions
        if analysis.get("config_failed"):
            log.info("Config failed (crash/exit detected)")
            return "crashed"

        if phase == "apply" and analysis.get("done_with_config"):
            log.info("Config applied — entering observation phase")
            phase = "observe"
            observe_start = time.time()
            time.sleep(OBSERVE_INTERVAL)
            continue

        if phase == "observe":
            elapsed = time.time() - observe_start
            if analysis.get("stable") and elapsed >= STABILITY_WINDOW:
                fps = analysis.get("fps", "?")
                log.info(f"STABLE! Ran {elapsed:.0f}s, fps={fps}")
                return "stable"
            if elapsed >= STABILITY_WINDOW:
                # Time's up — if it's still running and not crashed, call it stable
                if analysis.get("game_running"):
                    log.info(f"Observation window complete — game still running ({elapsed:.0f}s)")
                    return "stable"
                log.info("Observation window ended — game not running")
                return "crashed"
            # Still observing — wait and take another screenshot
            time.sleep(OBSERVE_INTERVAL)
            continue

        # If no actions and not in a terminal state, wait a moment and retry
        if not actions:
            time.sleep(2)

    log.info("Max actions reached — giving up on this config")
    return "timeout"


def main():
    parser = argparse.ArgumentParser(description="Autonomous GameNative config tester")
    parser.add_argument("--game",   default="", help="Name of game to test (as it appears in GameNative)")
    parser.add_argument("--server", default="http://localhost:8080", help="phonecontrol server URL")
    parser.add_argument("--key",    default=os.environ.get("ANTHROPIC_API_KEY", ""), help="Anthropic API key")
    parser.add_argument("--skip-done", action="store_true", help="Skip configs already in results.json")
    args = parser.parse_args()

    if not args.key:
        print("ERROR: Set ANTHROPIC_API_KEY env var or pass --key")
        sys.exit(1)

    phone = Phone(args.server)
    if not phone.is_alive():
        print(f"ERROR: phonecontrol server not reachable at {args.server}")
        print("Start it with: python -m uvicorn app:app --host 0.0.0.0 --port 8080")
        sys.exit(1)
    log.info(f"Phone connected via {args.server}")

    client = anthropic.Anthropic(api_key=args.key)

    done_configs = set()
    if args.skip_done:
        for r in load_results():
            done_configs.add(json.dumps(r["config"], sort_keys=True))

    winner = None
    for i, config in enumerate(CONFIGS):
        key_str = json.dumps(config, sort_keys=True)
        if key_str in done_configs:
            log.info(f"Skipping already-tested config: {config}")
            continue

        t0 = time.time()
        outcome = test_config(phone, client, config, args.game)
        elapsed = time.time() - t0
        save_result(config, outcome, "", elapsed)

        if outcome == "stable":
            log.info(f"\n{'='*60}")
            log.info(f"WINNER FOUND: {json.dumps(config, indent=2)}")
            log.info(f"{'='*60}\n")
            winner = config
            break

        # Go back to GameNative main screen before next config
        for _ in range(3):
            phone.key("back")
        time.sleep(1)

    if winner:
        print(f"\n✓ Working config:\n{json.dumps(winner, indent=2)}")
    else:
        results = load_results()
        print(f"\n✗ No stable config found after {len(results)} tests.")
        print("Results saved to results.json")


if __name__ == "__main__":
    main()
