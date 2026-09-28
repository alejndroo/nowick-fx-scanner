"""Copy this file to config.py (same folder) and fill in your real values.
config.py itself is gitignored — never committed, never shared — because
it holds your real MT5 login."""

# --- MT5 account (Terminal -> right-click account -> "Copy" for exact values) ---
MT5_LOGIN = 12345678            # your account number, as an integer
MT5_PASSWORD = "your-password"  # the account's trade password, not your MT5 app password
MT5_SERVER = "YourBroker-Live"  # exact server name, e.g. "ICMarketsSC-Live01"

# If your broker lists pairs as e.g. "EURUSD.m" or "EURUSDm" instead of plain
# "EURUSD", set the suffix here (check Market Watch). Leave "" if plain.
SYMBOL_SUFFIX = ""

# --- Risk ---
# Each trade risks an amount planner.py computes deterministically from the
# recent win/loss streak, ALWAYS within this band (0.15-0.20 = 15-20% of
# equity) — never a random draw. This range is only the fallback default
# used when the dashboard's Risk % field hasn't been explicitly set (that
# always takes priority when present).
RISK_PCT_MIN = 0.15
RISK_PCT_MAX = 0.20
MAX_OPEN_TRADES = 6      # bot will not open a new trade while this many of its own are open (any mix of pairs)

# Hard portfolio-level cap: refuses a new trade if the TOTAL worst-case loss
# across all currently open positions (each one's own distance to its own
# SL) plus this new trade's own risk would exceed this fraction of equity.
# Exists because MAX_OPEN_TRADES x RISK_PCT_MAX alone allows >100% of equity
# at risk simultaneously across pairs that often move together (e.g. EUR/
# GBP/AUD/NZD crosses) — this is the structural check for that gap.
AGGREGATE_RISK_CAP_PCT = 0.50

# No overnight force-close: open positions run to their own SL/TP whenever
# that happens, including overnight/across sessions. Only NEW entries are
# time-gated, via engine.py's own session filter (07:00-20:45 UTC), not by
# anything in this file.

# --- Optional: Telegram confirmations (reuse the same bot/chat as the signal bot) ---
TELEGRAM_BOT_TOKEN = ""  # leave "" to disable
TELEGRAM_CHAT_ID = ""

# --- Dashboard (Firebase) ---
# Path to the service-account JSON file (Firebase console -> Project settings
# -> Service accounts -> Generate new private key), placed next to bot.py.
FIREBASE_SERVICE_ACCOUNT_PATH = "firebase-service-account.json"
FIREBASE_DB_URL = "https://fxapp-fc0bc-default-rtdb.firebaseio.com"
