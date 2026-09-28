"""Fill these in before running bot.py. Never share this file — it holds
your real MT5 login."""

# --- MT5 account (Terminal -> right-click account -> "Copy" for exact values) ---
MT5_LOGIN = 12345678            # your account number, as an integer
MT5_PASSWORD = "your-password"  # the account's trade password, not your MT5 app password
MT5_SERVER = "YourBroker-Live"  # exact server name, e.g. "ICMarketsSC-Live01"

# If your broker lists pairs as e.g. "EURUSD.m" or "EURUSDm" instead of plain
# "EURUSD", set the suffix here (check Market Watch). Leave "" if plain.
SYMBOL_SUFFIX = ""

# --- Risk ---
# Each trade independently risks a random amount in this range (0.15-0.20 =
# 15-20% of equity), unless overridden live from the dashboard's Risk %
# field — that always takes priority when set, this range is only the
# fallback used when the dashboard hasn't been touched.
RISK_PCT_MIN = 0.15
RISK_PCT_MAX = 0.20
MAX_OPEN_TRADES = 6      # bot will not open a new trade while this many of its own are open (any mix of pairs)

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
