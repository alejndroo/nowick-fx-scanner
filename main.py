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
from datetime import datetime, timedelta, timezone

from oanda import fetch_candles
from engine import run_engine
from pairs import PAIRS, pip_size, display_symbol
import telegram

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

DEFAULT_STATE = {
    "paused": False, "last_alert": {}, "pairs": {}, "history": [],
    "last_update_id": None, "consecutive_full_failures": 0, "outage_alerted": False,
}

# Signals older than this are treated as stale catch-up (e.g. after a long
# outage) — they're marked seen but never pushed to Telegram, since an entry
# price from hours ago is no longer actionable.
MAX_SIGNAL_AGE = timedelta(hours=2)


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print("state.json unreadable/corrupt — starting from a fresh default state.", file=sys.stderr)
    return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    """Atomic write: never leaves state.json truncated if the process dies mid-write."""
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp_path, STATE_PATH)


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
    engine_state = state.setdefault("engine_state", {})

    now = datetime.now(timezone.utc)
    ok_count = 0

    for pair in PAIRS:
        try:
            seed = engine_state.get(pair)
            # First time ever seeing this pair: fetch OANDA's max window (5000
            # M15 bars, ~52 days) so the seeded trend/swing state has the best
            # chance of matching Pine's true continuously-running history.
            # Once seeded, later runs persist state and only need a normal
            # window for HBC/LBC/pivot lookback context.
            m15_count = 5000 if seed is None else 1500
            m15 = fetch_candles(pair, "M15", count=m15_count)
            h1 = fetch_candles(pair, "H1", count=2000)  # deep history so EMA150 is fully converged
            if len(m15) < 100 or len(h1) < 160:
                continue

            result = run_engine(pair, m15, h1, pip_size(pair), seed=seed)
            pairs_snapshot[pair] = {"trend": result["trend"], "pending": result["pending"]}
            engine_state[pair] = result["state"]
            ok_count += 1

            last_seen = last_alert.get(pair)
            new_signals = [s for s in result["signals"] if last_seen is None or s["time"] > last_seen]

            if last_seen is None:
                # First time we've ever scanned this pair: don't dump history,
                # just mark "caught up" from here.
                if result["signals"]:
                    last_alert[pair] = result["signals"][-1]["time"]
                continue

            for sig in new_signals:
                sig_time = datetime.fromisoformat(sig["time"].replace("Z", "+00:00"))
                is_stale = (now - sig_time) > MAX_SIGNAL_AGE
                if not is_stale:
                    digits = decimals_for(pair)
                    text = telegram.fmt_signal(display_symbol(pair), sig["dir"], sig["entry"], sig["sl"], sig["tp"], digits)
                    telegram.send(text)
                    history.append({"time": sig["time"], "pair": display_symbol(pair), "dir": sig["dir"]})
                last_alert[pair] = sig["time"]

        except Exception:
            print(f"Error processing {pair}:", traceback.format_exc(), file=sys.stderr)
            continue

    # ---- sustained-failure watchdog: tell the user if the bot has gone dark ----
    if ok_count == 0:
        state["consecutive_full_failures"] = state.get("consecutive_full_failures", 0) + 1
        if state["consecutive_full_failures"] >= 3 and not state.get("outage_alerted"):
            try:
                telegram.send("⚠️ *Nowick scanner outage* — every pair has failed for 3+ runs in a row "
                               "(check the OANDA API key / rate limits / GitHub Actions logs).")
                state["outage_alerted"] = True
            except Exception:
                pass
    else:
        if state.get("outage_alerted"):
            try:
                telegram.send("✅ Nowick scanner recovered — signals resuming normally.")
            except Exception:
                pass
        state["consecutive_full_failures"] = 0
        state["outage_alerted"] = False

    state["history"] = history[-50:]  # keep it bounded
    save_state(state)


if __name__ == "__main__":
    main()
