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
RISK_PCT = 0.15          # fraction of account equity risked per trade (0.15 = 15%)
MAX_OPEN_TRADES = 6      # bot will not open a new trade while this many of its own are open (any mix of pairs)

# Safety net matching the "no overnight holding" rule: force-close every
# position this bot opened once UTC reaches this hour, even if SL/TP hasn't
# been hit yet. Session signals only fire 07:00-20:45 UTC (London open to
# 15 min before NY close), so 21:00 gives a clean buffer past that.
FORCE_CLOSE_HOUR_UTC = 21

# --- Optional: Telegram confirmations (reuse the same bot/chat as the signal bot) ---
TELEGRAM_BOT_TOKEN = ""  # leave "" to disable
TELEGRAM_CHAT_ID = ""

# --- Dashboard (Firebase) ---
# Path to the service-account JSON file (Firebase console -> Project settings
# -> Service accounts -> Generate new private key), placed next to bot.py.
FIREBASE_SERVICE_ACCOUNT_PATH = "firebase-service-account.json"
FIREBASE_DB_URL = "https://fxapp-fc0bc-default-rtdb.firebaseio.com"
