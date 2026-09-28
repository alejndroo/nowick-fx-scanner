"""The 27 forex pairs from the watchlist, in OANDA instrument format."""

PAIRS = [
    "GBP_CHF", "GBP_CAD", "CAD_JPY", "EUR_CAD", "EUR_GBP", "EUR_USD",
    "NZD_JPY", "AUD_CHF", "EUR_CHF", "EUR_AUD", "USD_JPY", "CHF_JPY",
    "NZD_USD", "AUD_JPY", "GBP_JPY", "EUR_JPY", "NZD_CAD", "NZD_CHF",
    "GBP_NZD", "USD_CHF", "EUR_NZD", "AUD_USD", "AUD_NZD", "AUD_CAD",
    "USD_CAD", "CAD_CHF", "GBP_AUD",
]


def pip_size(pair: str) -> float:
    """Standard forex pip size: 0.01 for JPY-quoted pairs, 0.0001 otherwise."""
    return 0.01 if pair.endswith("JPY") else 0.0001


def display_symbol(pair: str) -> str:
    """GBP_CHF -> GBPCHF, for clean display in Telegram messages."""
    return pair.replace("_", "")
