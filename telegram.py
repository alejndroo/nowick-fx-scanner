"""Telegram Bot API helpers: sending alerts and handling slash commands."""
import os
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

HELP_TEXT = (
    "🤖 *Nowick FX Signal Bot*\n\n"
    "/status — current trend + pending setups on all pairs\n"
    "/pairs — list the 27 monitored pairs\n"
    "/pause — stop sending new signal alerts\n"
    "/resume — resume sending alerts\n"
    "/history — last 10 signals sent\n"
    "/ping — check the bot is alive and when it last ran\n"
    "/help — show this message\n"
)


def send(text: str) -> None:
    requests.post(
        f"{API}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )


def fmt_signal(pair_display: str, dir_: str, entry: float, sl: float, tp: float, digits: int) -> str:
    emoji = "🟢📈" if dir_ == "BUY" else "🔴📉"
    action = "BUY" if dir_ == "BUY" else "SELL"
    return (
        f"{emoji} *{action} {pair_display}*\n\n"
        f"Entry: `{entry:.{digits}f}`\n"
        f"SL: `{sl:.{digits}f}`\n"
        f"TP: `{tp:.{digits}f}`\n"
        f"R:R  1:1 ⚖️"
    )


def get_updates(offset: int | None):
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{API}/getUpdates", params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("result", [])


def handle_commands(state: dict) -> int | None:
    """Poll for new /commands since the last processed update, reply to each.

    Returns the highest update_id seen (to advance the offset), or None if
    nothing new arrived.
    """
    last_id = state.get("last_update_id")
    updates = get_updates(last_id + 1 if last_id is not None else None)
    if not updates:
        return None

    highest = last_id
    for u in updates:
        highest = u["update_id"]
        msg = u.get("message", {})
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd = text.split()[0].lower()

        if cmd == "/start" or cmd == "/help":
            send(HELP_TEXT)
        elif cmd == "/status":
            from pairs import display_symbol
            pairs_state = state.get("pairs", {})
            if not pairs_state:
                send("No scan data yet — the next run will populate this.")
            else:
                bulls = [display_symbol(p) for p, v in pairs_state.items() if v.get("trend") == 1]
                bears = [display_symbol(p) for p, v in pairs_state.items() if v.get("trend") == -1]
                pending = [f"{display_symbol(p)} ({v['pending']['dir']}, {v['pending']['bars_left']} bars left)"
                           for p, v in pairs_state.items() if v.get("pending")]
                lines = [f"📊 *Market snapshot* (as of `{state.get('last_run_utc', '?')}` UTC)\n"]
                lines.append(f"🟢 Bullish: {', '.join(bulls) if bulls else '—'}")
                lines.append(f"🔴 Bearish: {', '.join(bears) if bears else '—'}")
                lines.append(f"⏳ Pending retest: {', '.join(pending) if pending else '—'}")
                send("\n".join(lines))
        elif cmd == "/pairs":
            from pairs import PAIRS, display_symbol
            send("📋 *Monitored pairs (27):*\n" + ", ".join(display_symbol(p) for p in PAIRS))
        elif cmd == "/pause":
            state["paused"] = True
            send("⏸️ Alerts paused. Send /resume to turn them back on.")
        elif cmd == "/resume":
            state["paused"] = False
            send("▶️ Alerts resumed.")
        elif cmd == "/ping":
            from datetime import datetime, timezone
            send(f"🏓 Pong! Last scan run: `{state.get('last_run_utc', 'never')}` UTC.\nNow: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`")
        elif cmd == "/history":
            hist = state.get("history", [])[-10:]
            if not hist:
                send("No signals sent yet.")
            else:
                lines = [f"{h['time']}  {h['dir']} {h['pair']}" for h in hist]
                send("🕘 *Last signals:*\n" + "\n".join(lines))
        else:
            send("Unknown command. Send /help to see what I understand.")

    return highest
