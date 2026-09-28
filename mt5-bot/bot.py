"""Nowick MT5 auto-trader.

Runs the exact same signal engine as the GitHub Actions bot (engine.py,
pairs.py — byte-for-byte identical, copied unchanged from that repo) but
instead of sending a Telegram alert, executes the trade directly on your
MT5 account via the locally running MT5 terminal.

Must run on the Windows machine that has the MT5 terminal open and logged
in. Leave this window open during trading hours (07:00-13:00 UTC, per the
engine's session filter).
"""
import json
import os
import random
import time
import traceback
from datetime import datetime, timezone

import MetaTrader5 as mt5
import requests

import config
import mt5_broker as broker
import firebase_push
from engine import run_engine
from pairs import PAIRS, pip_size, display_symbol

STATE_PATH = os.path.join(os.path.dirname(__file__), "mt5_state.json")
STALE_SIGNAL_SECONDS = 3600  # don't chase a signal more than an hour old (execution latency safety)


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print("mt5_state.json unreadable — starting fresh.")
    return {"engine_state": {}, "seeded": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_PATH)


def send_telegram(text: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception:
        pass


def notify(text: str) -> None:
    """Every skip/failure/execution notice goes through this, not
    send_telegram() directly — send_telegram() silently no-ops when
    TELEGRAM_BOT_TOKEN is unset (the shipped default), which previously made
    a failed trade or a skipped signal produce ZERO visible output anywhere.
    This guarantees it's always at least printed to this console.
    """
    plain = text.replace("*", "").replace("`", "")
    print(plain)
    send_telegram(text)


def digits_for(pair: str) -> int:
    return 3 if pair.endswith("JPY") else 5


def process_pair(pair: str, engine_state: dict, seeded: dict, now: datetime) -> None:
    symbol = broker.mt5_symbol(pair, config.SYMBOL_SUFFIX)
    if mt5.symbol_info(symbol) is None:
        return  # not offered by this broker, or the suffix/name doesn't match
    mt5.symbol_select(symbol, True)

    seed = engine_state.get(pair)
    m15_count = 5000 if seed is None else 1500
    m15 = broker.fetch_candles(symbol, "M15", m15_count)
    h1 = broker.fetch_candles(symbol, "H1", 5000)
    if len(m15) < 100 or len(h1) < 160:
        return

    result = run_engine(pair, m15, h1, pip_size(pair), seed=seed)
    engine_state[pair] = result["state"]

    is_first_seed = pair not in seeded
    seeded[pair] = True
    if is_first_seed:
        return  # first run for this pair: seed state only, never trade the historical backfill

    for sig in result["signals"]:
        sig_time = datetime.fromisoformat(sig["time"].replace("Z", "+00:00"))
        if (now - sig_time).total_seconds() > STALE_SIGNAL_SECONDS:
            continue

        if broker.open_positions_count() >= config.MAX_OPEN_TRADES:
            notify(f"⚠️ Skipped {display_symbol(pair)} {sig['dir']} — {config.MAX_OPEN_TRADES} trades already open.")
            continue

        risk_distance = abs(sig["entry"] - sig["sl"])
        account = mt5.account_info()
        if account is None:
            notify("❌ Could not read account info — skipping this signal.")
            continue
        default_risk_pct = random.uniform(config.RISK_PCT_MIN, config.RISK_PCT_MAX)
        risk_pct = firebase_push.get_risk_pct(default_risk_pct)
        risk_amount = account.equity * risk_pct

        try:
            lots = broker.lots_for_risk(symbol, risk_amount, risk_distance)
        except Exception as e:
            notify(f"❌ Position sizing failed for {display_symbol(pair)}: {e}")
            continue

        # Anchor SL/TP to the REAL fill price, not the historical retest level
        # (which may be seconds-to-minutes stale by execution time). Same risk
        # distance and 1:1 reward as the engine computed — only the anchor moves.
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue
        fill_price = tick.ask if sig["dir"] == "BUY" else tick.bid
        if sig["dir"] == "BUY":
            sl = fill_price - risk_distance
            tp = fill_price + risk_distance
        else:
            sl = fill_price + risk_distance
            tp = fill_price - risk_distance

        try:
            res = broker.place_order(symbol, sig["dir"], lots, sl, tp)
            d = digits_for(pair)
            notify(
                f"✅ *{sig['dir']} {display_symbol(pair)}* executed\n"
                f"Lots: {lots}\n"
                f"Entry: `{res['price']:.{d}f}`\n"
                f"SL: `{sl:.{d}f}`\n"
                f"TP: `{tp:.{d}f}`\n"
                f"Risk: {risk_pct * 100:.0f}% equity"
            )
        except Exception as e:
            notify(f"❌ Order failed for {display_symbol(pair)} {sig['dir']}: {e}")


def main() -> None:
    broker.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER)
    notify("🤖 Nowick MT5 auto-trader started.")
    print("Connected to MT5. Auto-trading loop running — leave this window open.")

    state = load_state()
    engine_state = state.setdefault("engine_state", {})
    seeded = state.setdefault("seeded", {})
    last_force_close_date = None
    tick = 0
    SCAN_EVERY_N_TICKS = 60  # candle-scanning stays on its original ~60s cadence

    while True:
        try:
            now = datetime.now(timezone.utc)

            # mt5.* calls return None/empty (never raise) on a dropped
            # terminal/account session — without this check, a total
            # disconnect looks EXACTLY like "no pairs matched right now" on
            # the console: no error, no symptom, just silence forever.
            if not broker.is_connected():
                print(f"[{now.isoformat(timespec='seconds')}] MT5 session appears disconnected — attempting reconnect...")
                if broker.reconnect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER):
                    notify("✅ MT5 reconnected after a dropped session.")
                else:
                    print("Reconnect failed — will retry next cycle.")
                    tick += 1
                    time.sleep(1)
                    continue

            if now.hour >= config.FORCE_CLOSE_HOUR_UTC and last_force_close_date != now.date():
                tickets = [p.ticket for p in (mt5.positions_get() or []) if p.magic == broker.MAGIC]
                if tickets:
                    try:
                        firebase_push.mark_force_closing(tickets)
                    except Exception:
                        print("mark_force_closing failed:", traceback.format_exc())
                closed = broker.close_all()
                if closed:
                    notify(f"🔒 No-overnight cutoff: force-closed {closed} open position(s).")
                last_force_close_date = now.date()

            if tick % SCAN_EVERY_N_TICKS == 0:
                ok_count = 0
                for pair in PAIRS:
                    try:
                        process_pair(pair, engine_state, seeded, now)
                        ok_count += 1
                    except Exception:
                        print(f"Error processing {pair}:", traceback.format_exc())
                save_state(state)
                open_count = broker.open_positions_count()
                # Visible heartbeat: a live console should show SOME proof of
                # life every ~60s, not just the one-time startup line — this
                # is the difference between "confirmed scanning" and "assumed
                # scanning because nothing crashed."
                print(f"[{now.isoformat(timespec='seconds')}] Scan cycle: {ok_count}/{len(PAIRS)} pairs OK, {open_count} open position(s), connected={broker.is_connected()}")

            # Runs every ~1s regardless of the scan cadence above, so the
            # app's balance/equity/open-position numbers feel truly live even
            # though new M15 candles only matter once a minute.
            try:
                firebase_push.sync_to_firebase()
            except Exception:
                print("Firebase sync failed:", traceback.format_exc())

        except Exception:
            print("Loop error:", traceback.format_exc())

        tick += 1
        time.sleep(1)


if __name__ == "__main__":
    main()
