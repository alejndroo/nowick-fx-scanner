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
RISK_PCT = 0.25          # fraction of account equity risked per trade (0.25 = 25%)
MAX_OPEN_TRADES = 3      # bot will not open a new trade while this many of its own are open

# Safety net matching the "no overnight holding" rule: force-close every
# position this bot opened once UTC reaches this hour, even if SL/TP hasn't
# been hit yet. Session signals only fire 07:00-13:00 UTC, so 21:00 gives a
# wide buffer while still guaranteeing nothing carries overnight.
FORCE_CLOSE_HOUR_UTC = 21

# --- Optional: Telegram confirmations (reuse the same bot/chat as the signal bot) ---
TELEGRAM_BOT_TOKEN = ""  # leave "" to disable
TELEGRAM_CHAT_ID = ""
