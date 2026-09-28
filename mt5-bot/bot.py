"""Nowick MT5 auto-trader.

Scans all 27 pairs with the Nowick signal engine (engine.py — kept
byte-identical to the TradingView indicator's logic, always) and executes
real trades directly on the connected MT5 account.

Two adaptive layers sit around that fixed signal logic, never inside it:
- planner.py, recomputed every scan cycle: chooses a risk % within the
  approved RISK_PCT_MIN..MAX band based on the recent win/loss streak,
  pauses individual pairs that are meaningfully underperforming, and can
  halt all new entries for the rest of the day via a daily loss circuit
  breaker. Every decision it makes is logged with a plain-text reason and
  pushed to the dashboard — never a silent adjustment.
- journal.py: a permanent record of every trade's full context (risk used,
  session hour, day of week, ATR at entry, outcome), which is what the
  planner actually reasons from.

Must run on the Windows machine that has the MT5 terminal open and logged
in. New entries only fire 07:00-20:45 UTC (engine.py's own session filter,
London open through 15 min before NY close) — open positions are NOT
force-closed at any time; they run to their own SL/TP, including overnight.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

import MetaTrader5 as mt5
import requests

import config
import journal
import mt5_broker as broker
import firebase_push
import planner
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


def process_pair(pair: str, engine_state: dict, seeded: dict, now: datetime, plan: dict, journal_data: dict, day_start_balance: float | None) -> None:
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

        if plan.get("daily_loss_limit_hit"):
            continue  # the daily circuit breaker notification already happened once, in main() — no per-signal spam here

        # LIVE re-check, not just the plan snapshot from the start of this
        # scan cycle: if pair #5's fill just breached the daily limit, pairs
        # #6-27 in this SAME ~60s cycle must not still trade on the stale
        # plan — this is the actual circuit breaker; the plan flag above is
        # only a fast-path/notification convenience.
        if day_start_balance and day_start_balance > 0:
            live_pnl = journal.today_realized_pnl(journal_data, now)
            if live_pnl < 0 and (-live_pnl / day_start_balance) >= planner.DAILY_LOSS_LIMIT_PCT:
                continue

        if pair in plan.get("paused_pairs", []):
            continue  # planner: this pair's recent win rate is meaningfully below average, sitting out today

        if broker.open_positions_count() >= config.MAX_OPEN_TRADES:
            notify(f"⚠️ Skipped {display_symbol(pair)} {sig['dir']} — {config.MAX_OPEN_TRADES} trades already open.")
            continue

        risk_distance = abs(sig["entry"] - sig["sl"])
        account = mt5.account_info()
        if account is None:
            notify("❌ Could not read account info — skipping this signal.")
            continue
        risk_pct = firebase_push.get_risk_pct(plan["risk_pct"])
        risk_amount = account.equity * risk_pct
        # Hard ceiling independent of the risk_pct calculation above — 25%
        # headroom over the approved max, purely as a structural backstop
        # against a sizing bug or bad SL fill, never meant to bind normally.
        safety_ceiling = account.equity * config.RISK_PCT_MAX * 1.25

        try:
            lots = broker.lots_for_risk(symbol, risk_amount, risk_distance, max_risk_amount=safety_ceiling)
        except Exception as e:
            notify(f"❌ Position sizing refused/failed for {display_symbol(pair)}: {e}")
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
        except Exception as e:
            notify(f"❌ Order failed for {display_symbol(pair)} {sig['dir']}: {e}")
            continue

        d = digits_for(pair)
        notify(
            f"✅ *{sig['dir']} {display_symbol(pair)}* executed\n"
            f"Lots: {lots}\n"
            f"Entry: `{res['price']:.{d}f}`\n"
            f"SL: `{sl:.{d}f}`\n"
            f"TP: `{tp:.{d}f}`\n"
            f"Risk: {risk_pct * 100:.0f}% equity"
        )
        try:
            journal.record_open(journal_data, res["ticket"], pair, sig["dir"], res["price"], sl, tp, risk_pct, sig.get("atr"), now)
        except Exception:
            # The trade is real and already confirmed above — a journal
            # write failure must never look like a failed order.
            print(f"Journal write failed for ticket {res['ticket']}:", traceback.format_exc())


def main() -> None:
    broker.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER)
    notify("🤖 Nowick MT5 auto-trader started.")
    print("Connected to MT5. Auto-trading loop running — leave this window open.")

    state = load_state()
    engine_state = state.setdefault("engine_state", {})
    seeded = state.setdefault("seeded", {})
    journal_data = journal.load_journal()
    try:
        added = firebase_push.backfill_journal_from_firebase(journal_data)
        if added:
            print(f"Journal backfill: pulled {added} historical trade(s) from Firebase — the planner learns from these too, not just new ones.")
    except Exception:
        print("Journal backfill failed (non-fatal, continuing):", traceback.format_exc())
    tick = 0
    SCAN_EVERY_N_TICKS = 60  # candle-scanning stays on its original ~60s cadence
    # The plan is recomputed every scan cycle (not just at startup) so it
    # reacts WITHIN the same day. Seeded with a safe no-op plan (mid-band
    # risk, nothing paused) so the very first cycle before any computation
    # still behaves sanely.
    plan = {"risk_pct": (config.RISK_PCT_MIN + config.RISK_PCT_MAX) / 2, "paused_pairs": [], "daily_loss_limit_hit": False}
    day_start_balance = state.get("day_start_balance")
    day_start_date = state.get("day_start_date")
    daily_limit_notified_date = None
    last_reconcile_problems: list = []

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

            # No forced overnight close: open positions are left to run to
            # their own SL/TP, whenever that happens. Only NEW entries are
            # time-gated — engine.py's own session filter (07:00-20:45 UTC)
            # already stops new signals from arming outside that window,
            # so nothing extra is needed here for that half.

            if tick % SCAN_EVERY_N_TICKS == 0:
                today_str = now.strftime("%Y-%m-%d")
                if day_start_date != today_str:
                    account_now = mt5.account_info()
                    if account_now is not None:
                        day_start_balance = account_now.balance
                        day_start_date = today_str
                        state["day_start_balance"] = day_start_balance
                        state["day_start_date"] = day_start_date
                        daily_limit_notified_date = None  # allow the breaker notice to fire again on the new day

                try:
                    plan = planner.compute_plan(journal_data, day_start_balance or 0.0, now)
                    firebase_push.push_insights(plan)
                except Exception:
                    print("Planner failed (falling back to last known plan):", traceback.format_exc())

                try:
                    problems = firebase_push.reconcile()
                except Exception:
                    problems = ["Reconciliation call itself raised — see traceback."]
                    print("Reconcile call failed:", traceback.format_exc())
                if problems and problems != last_reconcile_problems:
                    notify("⚠️ Dashboard/MT5 mismatch detected:\n" + "\n".join(f"- {p}" for p in problems))
                elif not problems and last_reconcile_problems:
                    notify("✅ Dashboard/MT5 mismatch resolved — back in sync.")
                last_reconcile_problems = problems

                if plan.get("daily_loss_limit_hit") and daily_limit_notified_date != today_str:
                    notify(f"🛑 Daily loss limit hit ({plan.get('today_realized_pnl')} realized) — no new entries until tomorrow. Open positions are unaffected.")
                    daily_limit_notified_date = today_str

                ok_count = 0
                for pair in PAIRS:
                    try:
                        process_pair(pair, engine_state, seeded, now, plan, journal_data, day_start_balance)
                        ok_count += 1
                    except Exception:
                        print(f"Error processing {pair}:", traceback.format_exc())
                save_state(state)
                open_count = broker.open_positions_count()
                # Visible heartbeat: a live console should show SOME proof of
                # life every ~60s, not just the one-time startup line — this
                # is the difference between "confirmed scanning" and "assumed
                # scanning because nothing crashed."
                print(f"[{now.isoformat(timespec='seconds')}] Scan cycle: {ok_count}/{len(PAIRS)} pairs OK, {open_count} open position(s), "
                      f"connected={broker.is_connected()}, risk={plan['risk_pct']*100:.1f}%, paused={plan.get('paused_pairs') or 'none'}")

            # Runs every ~1s regardless of the scan cadence above, so the
            # app's balance/equity/open-position numbers feel truly live even
            # though new M15 candles only matter once a minute.
            try:
                firebase_push.sync_to_firebase(journal_data)
            except Exception:
                print("Firebase sync failed:", traceback.format_exc())

        except Exception:
            print("Loop error:", traceback.format_exc())

        tick += 1
        time.sleep(1)


if __name__ == "__main__":
    main()
