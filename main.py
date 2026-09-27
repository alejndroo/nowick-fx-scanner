"""Orchestrator: runs every 15 minutes via GitHub Actions.

For each of the 27 pairs: fetch fresh OANDA candles, replay the exact
Nowick Signal Engine logic, and Telegram-alert on any BRAND NEW trigger.
Also polls Telegram for slash commands each run. State (dedupe info,
pause flag, last snapshot, command offset) persists in state.json,
which the workflow commits back to the repo after every run.
"""
import json
import os
import sys
import traceback
from datetime import datetime, timezone

from oanda import fetch_candles
from engine import run_engine
from pairs import PAIRS, pip_size, display_symbol
import telegram

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"paused": False, "last_alert": {}, "pairs": {}, "history": [], "last_update_id": None}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def decimals_for(pair: str) -> int:
    return 3 if pair.endswith("JPY") else 5


def main() -> None:
    state = load_state()

    # Process any pending Telegram commands first, regardless of pause state.
    try:
        new_offset = telegram.handle_commands(state)
        if new_offset is not None:
            state["last_update_id"] = new_offset
    except Exception:
        print("Telegram command polling failed:", traceback.format_exc(), file=sys.stderr)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["last_run_utc"] = now_iso

    if state.get("paused"):
        save_state(state)
        print("Paused — skipping scan.")
        return

    last_alert = state.setdefault("last_alert", {})
    pairs_snapshot = state.setdefault("pairs", {})
    history = state.setdefault("history", [])

    for pair in PAIRS:
        try:
            m15 = fetch_candles(pair, "M15", count=1500)
            h1 = fetch_candles(pair, "H1", count=400)
            if len(m15) < 100 or len(h1) < 160:
                continue

            result = run_engine(pair, m15, h1, pip_size(pair))
            pairs_snapshot[pair] = {"trend": result["trend"], "pending": result["pending"]}

            last_seen = last_alert.get(pair)
            new_signals = [s for s in result["signals"] if last_seen is None or s["time"] > last_seen]

            if last_seen is None:
                # First time we've ever scanned this pair: don't dump history,
                # just mark "caught up" from here.
                if result["signals"]:
                    last_alert[pair] = result["signals"][-1]["time"]
                continue

            for sig in new_signals:
                digits = decimals_for(pair)
                text = telegram.fmt_signal(display_symbol(pair), sig["dir"], sig["entry"], sig["sl"], sig["tp"], digits)
                telegram.send(text)
                history.append({"time": sig["time"], "pair": display_symbol(pair), "dir": sig["dir"]})
                last_alert[pair] = sig["time"]

        except Exception:
            print(f"Error processing {pair}:", traceback.format_exc(), file=sys.stderr)
            continue

    state["history"] = history[-50:]  # keep it bounded
    save_state(state)


if __name__ == "__main__":
    main()
