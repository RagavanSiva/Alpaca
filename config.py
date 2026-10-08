import json
from pathlib import Path

SYMBOLS = ["AAPL", "MSFT", "NVDA", "SPY", "JPM"]
SHORT_WINDOW = 20
LONG_WINDOW = 50
LOOKBACK_DAYS = 100
PAPER = True

# Position sizing: each buy uses this % of account equity, capped at available cash.
# Orders are placed as dollar amounts, so fractional shares are bought when needed.
POSITION_SIZE_PCT = 20
MIN_ORDER_USD = 1.00  # Alpaca's minimum for fractional/notional orders

# Stop-loss: sell a position if it falls this % below the average entry price.
# A stop order is placed at the open each trading day for every held position.
STOP_LOSS_PCT = 5

# The values above are defaults. Settings saved from the dashboard go to
# settings.json and override them for both the dashboard and the bot.
SETTINGS_FILE = Path(__file__).with_name("settings.json")
EDITABLE_SETTINGS = ("POSITION_SIZE_PCT", "MIN_ORDER_USD", "STOP_LOSS_PCT")


def load_settings() -> None:
    if SETTINGS_FILE.exists():
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        globals().update({k: float(v) for k, v in saved.items() if k in EDITABLE_SETTINGS})


def save_settings(values: dict[str, float]) -> None:
    values = {k: float(values[k]) for k in EDITABLE_SETTINGS}
    SETTINGS_FILE.write_text(json.dumps(values, indent=2), encoding="utf-8")
    globals().update(values)


load_settings()
