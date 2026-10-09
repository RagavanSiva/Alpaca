"""Moving average crossover bot for Alpaca paper trading.

Usage (Windows: venv\\Scripts\\python, Linux: venv/bin/python):
    python trading_bot.py                    # check now, trade on crossover / stop-loss
    python trading_bot.py --dry-run          # only report the signal
    python trading_bot.py --wait-for-open    # scheduled mode: wait for 9:30 ET; crypto only on market holidays

Scheduling: setup_schedule.ps1 (Windows Task Scheduler) or setup_schedule.sh (Linux systemd/cron).
"""

import argparse
import io
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import (GetCalendarRequest, GetOrdersRequest, MarketOrderRequest, StopLimitOrderRequest,
                                     StopOrderRequest)
from dotenv import load_dotenv

import config
from notifier import email_configured, send_email

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "logs" / "trading_bot.log"
ET = ZoneInfo("America/New_York")

log = logging.getLogger("trading_bot")
RUN_LOG = io.StringIO()     # this run's log lines, included in the notification email
ALERTS: list[str] = []      # signals and stop-loss hits found during this run


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s ET [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda *args: datetime.now(ET).timetuple()
    for handler in (logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout),
                    logging.StreamHandler(RUN_LOG)):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def is_crypto(symbol: str) -> bool:
    return "/" in symbol  # Alpaca crypto pairs look like "BTC/USD"


def position_symbol(symbol: str) -> str:
    return symbol.replace("/", "")  # Alpaca reports the BTC/USD position as "BTCUSD"


def round_price(price: float, crypto: bool = False) -> float:
    if crypto:
        return float(f"{price:.6g}")  # 6 significant digits: works for BTC and for DOGE
    return round(price, 2 if price >= 1 else 4)


def wait_for_market_open(client: TradingClient) -> bool:
    """Return False if today is not a stock trading day; otherwise sleep until the opening bell."""
    today = datetime.now(ET).date()
    calendar = client.get_calendar(GetCalendarRequest(start=today, end=today))
    if not calendar or calendar[0].date != today:
        log.info("Stock market closed today (%s).", today)
        return False

    market_open = datetime.combine(today, calendar[0].open.time(), ET)
    delay = (market_open - datetime.now(ET)).total_seconds()
    if delay > 0:
        log.info("Waiting %.0f seconds for market open at %s ET", delay, market_open.strftime("%H:%M"))
        time.sleep(delay)
    elif delay < -15 * 60:
        log.warning("Started %.0f minutes after market open", -delay / 60)
    return True


def fetch_crypto_closes(symbol: str, days: int) -> pd.Series:
    # Alpaca's own crypto prices (the exchange the bot trades on); no API keys required.
    start = datetime.now(timezone.utc) - timedelta(days=days + 10)
    bars = CryptoHistoricalDataClient().get_crypto_bars(
        CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start)).df
    if bars.empty:
        raise RuntimeError(f"No price data returned for {symbol}")
    return bars.loc[symbol]["close"]


def fetch_prices(symbol: str, days: int) -> pd.Series:
    # Pull a wider window, then keep the last `days` completed trading sessions.
    if is_crypto(symbol):
        close = fetch_crypto_closes(symbol, days)
    else:
        history = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
        if history.empty:
            raise RuntimeError(f"No price data returned for {symbol}")
        close = history["Close"]
    close = close.dropna()
    last_bar = close.index[-1]
    now = datetime.now(last_bar.tz or ET)  # stock bars are New York days, crypto bars are UTC days
    if last_bar.date() == now.date() and (is_crypto(symbol) or now.hour < 16):
        close = close.iloc[:-1]  # today's bar is still in progress
    return close.tail(days)


def detect_crossover(close: pd.Series, short: int, long: int) -> tuple[str | None, pd.DataFrame]:
    df = pd.DataFrame({
        "close": close,
        "short_ma": close.rolling(short).mean(),
        "long_ma": close.rolling(long).mean(),
    }).dropna()
    if len(df) < 2:
        raise RuntimeError(f"Not enough data for a {long}-day moving average crossover check")

    prev, curr = df.iloc[-2], df.iloc[-1]
    if prev.short_ma <= prev.long_ma and curr.short_ma > curr.long_ma:
        return "buy", df
    if prev.short_ma >= prev.long_ma and curr.short_ma < curr.long_ma:
        return "sell", df
    return None, df


def get_position(client: TradingClient, symbol: str) -> Position | None:
    try:
        return client.get_open_position(position_symbol(symbol))
    except APIError:
        return None


def stop_price_for(entry: float, crypto: bool = False) -> float:
    return round_price(entry * (1 - config.STOP_LOSS_PCT / 100), crypto)


def wait_for_order(client: TradingClient, order_id, done: set[OrderStatus], timeout: float = 30) -> Order:
    deadline = time.monotonic() + timeout
    order = client.get_order_by_id(order_id)
    while order.status not in done and time.monotonic() < deadline:
        time.sleep(1)
        order = client.get_order_by_id(order_id)
    return order


def cancel_stop_orders(client: TradingClient, symbol: str) -> None:
    """Cancel open stop-loss orders so their shares are free to sell or re-protect."""
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
    for o in orders:
        if o.side == OrderSide.SELL and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            client.cancel_order_by_id(o.id)
            wait_for_order(client, o.id, {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED}, timeout=10)
            log.info("[%s] Canceled previous stop order %s", symbol, o.id)


def place_stop(client: TradingClient, symbol: str, qty: float, entry: float, dry_run: bool) -> None:
    crypto = is_crypto(symbol)
    stop = stop_price_for(entry, crypto)
    if dry_run:
        log.info("[%s] [dry-run] Would place stop-loss: sell %g unit(s) at $%s", symbol, qty, stop)
        return
    if crypto:
        limit = round_price(stop * (1 - config.CRYPTO_STOP_LIMIT_BUFFER_PCT / 100), crypto)
        order = client.submit_order(StopLimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            stop_price=stop, limit_price=limit,
        ))
        log.info("[%s] Stop-loss placed: sell %g unit(s) if price <= $%s, limit $%s (id=%s, good until canceled)",
                 symbol, qty, stop, limit, order.id)
        return
    order = client.submit_order(StopOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, stop_price=stop,
    ))
    log.info("[%s] Stop-loss placed: sell %g share(s) if price <= $%.2f (id=%s, valid today)", symbol, qty, stop, order.id)


def sell_all(client: TradingClient, symbol: str, reason: str, dry_run: bool) -> None:
    if dry_run:
        log.info("[%s] [dry-run] Would sell entire position (%s)", symbol, reason)
        return
    order = client.close_position(position_symbol(symbol))
    log.info("[%s] Sell order submitted for entire position (%s) (id=%s, status=%s)",
             symbol, reason, order.id, order.status.value)


def buy(client: TradingClient, symbol: str, dry_run: bool) -> None:
    acct = client.get_account()
    available = float(acct.non_marginable_buying_power)
    amount = round(min(float(acct.equity) * config.POSITION_SIZE_PCT / 100, available), 2)
    log.info("[%s] Sizing: %g%% of equity $%s, available cash $%.2f -> $%.2f",
             symbol, config.POSITION_SIZE_PCT, acct.equity, available, amount)
    if amount < config.MIN_ORDER_USD:
        log.warning("[%s] Buy signal, but $%.2f is below the $%.2f minimum. Skipping.", symbol, amount, config.MIN_ORDER_USD)
        return
    if dry_run:
        log.info("[%s] [dry-run] Would buy $%.2f, then place a %g%% stop-loss", symbol, amount, config.STOP_LOSS_PCT)
        return

    order = client.submit_order(MarketOrderRequest(
        symbol=symbol, notional=amount, side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY,
    ))
    log.info("[%s] Buy order submitted: $%.2f (id=%s)", symbol, amount, order.id)
    order = wait_for_order(client, order.id, {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED})
    if order.status != OrderStatus.FILLED:
        log.warning("[%s] Buy not filled yet (status=%s). Stop-loss will be placed on the next run.", symbol, order.status.value)
        return
    price = float(order.filled_avg_price)
    # Use the held quantity, not filled_qty: Alpaca takes crypto fees out of the coins bought.
    position = get_position(client, symbol)
    qty = float(position.qty) if position else float(order.filled_qty)
    log.info("[%s] Bought %g unit(s) at $%s", symbol, qty, round_price(price, is_crypto(symbol)))
    place_stop(client, symbol, qty, price, dry_run)


def make_client() -> TradingClient:
    load_dotenv(BASE_DIR / ".env")
    api_key, secret_key = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")
    return TradingClient(api_key, secret_key, paper=config.PAPER)


def trade_symbol(client: TradingClient, symbol: str, dry_run: bool) -> None:
    close = fetch_prices(symbol, config.LOOKBACK_DAYS)
    signal, df = detect_crossover(close, config.SHORT_WINDOW, config.LONG_WINDOW)
    prev, last = df.iloc[-2], df.iloc[-1]
    log.info("[%s] Data: %d completed %s ending %s", symbol, len(close),
             "days" if is_crypto(symbol) else "trading days", df.index[-1].date())
    log.info("[%s] Previous day (%s): %d-day MA %.6g, %d-day MA %.6g", symbol, df.index[-2].date(),
             config.SHORT_WINDOW, prev.short_ma, config.LONG_WINDOW, prev.long_ma)
    log.info("[%s] Latest day   (%s): close %.6g, %d-day MA %.6g, %d-day MA %.6g", symbol, df.index[-1].date(),
             last.close, config.SHORT_WINDOW, last.short_ma, config.LONG_WINDOW, last.long_ma)
    log.info("[%s] Signal: %s", symbol, signal or "none")
    if signal:
        ALERTS.append(f"{symbol}: {signal.upper()} signal - {config.SHORT_WINDOW}-day MA crossed "
                      f"{'above' if signal == 'buy' else 'below'} {config.LONG_WINDOW}-day MA "
                      f"({last.short_ma:.6g} vs {last.long_ma:.6g}), close ${last.close:.6g}")

    position = get_position(client, symbol)
    if position is None:
        if signal == "buy":
            buy(client, symbol, dry_run)
        elif signal == "sell":
            log.info("[%s] Sell signal, but no position held. Nothing to sell.", symbol)
        else:
            log.info("[%s] No crossover and no position. Nothing to do.", symbol)
        return

    qty, entry, price = float(position.qty), float(position.avg_entry_price), float(position.current_price)
    stop = stop_price_for(entry, is_crypto(symbol))
    log.info("[%s] Holding %g unit(s): entry $%s, now $%s (%+.2f%%), stop-loss $%s", symbol, qty,
             round_price(entry, is_crypto(symbol)), round_price(price, is_crypto(symbol)), (price / entry - 1) * 100, stop)
    if not dry_run:
        cancel_stop_orders(client, symbol)

    if price <= stop:
        ALERTS.append(f"{symbol}: STOP-LOSS hit - price ${price:g} <= stop ${stop:g} (entry ${entry:g})")
        sell_all(client, symbol, f"stop-loss: ${price:g} <= ${stop:g}", dry_run)
    elif signal == "sell":
        sell_all(client, symbol, "death cross", dry_run)
    else:
        if signal == "buy":
            log.info("[%s] Buy signal, but already holding. Not adding more.", symbol)
        place_stop(client, symbol, qty, entry, dry_run)


def run(args: argparse.Namespace) -> int:
    log.info("Running crossover bot for %s (short=%d, long=%d, size=%g%% of equity, stop-loss=%g%%, paper=%s, dry_run=%s)",
             ", ".join(config.SYMBOLS), config.SHORT_WINDOW, config.LONG_WINDOW, config.POSITION_SIZE_PCT,
             config.STOP_LOSS_PCT, config.PAPER, args.dry_run)

    client = make_client()
    symbols = config.SYMBOLS
    if args.wait_for_open and not wait_for_market_open(client):
        symbols = [s for s in symbols if is_crypto(s)]  # crypto trades every day
        if not symbols:
            log.info("Nothing to do.")
            return 0
        log.info("Checking crypto only: %s", ", ".join(symbols))

    failed = []
    for symbol in symbols:
        try:
            trade_symbol(client, symbol, args.dry_run)
        except Exception:
            log.exception("[%s] Failed", symbol)
            failed.append(symbol)

    log.info("Done: %d symbol(s) checked, %d failed%s", len(symbols), len(failed),
             f" ({', '.join(failed)})" if failed else "")
    return 1 if failed else 0


def notify(dry_run: bool) -> None:
    """Email a summary if this run found any signal or stop-loss hit."""
    if not ALERTS:
        return
    if not email_configured():
        log.warning("Signal found, but email is not configured (NOTIFY_EMAIL, SMTP_USER, SMTP_PASSWORD). No email sent.")
        return
    tags = ", ".join(a.split(" - ")[0].replace(":", "") for a in ALERTS)
    subject = f"Alpaca bot{' [DRY RUN]' if dry_run else ''}{' [PAPER]' if config.PAPER else ''}: {tags}"
    body = ("\n".join(ALERTS)
            + ("\n\nDry run: no orders were placed." if dry_run else "")
            + "\n\nFull log of this run:\n\n" + RUN_LOG.getvalue())
    try:
        log.info("Notification email sent to %s", send_email(subject, body))
    except Exception:
        log.exception("Failed to send notification email")


def main() -> int:
    parser = argparse.ArgumentParser(description="Moving average crossover trading bot")
    parser.add_argument("--dry-run", action="store_true", help="Report the signal without placing orders")
    parser.add_argument("--wait-for-open", action="store_true",
                        help="Skip non-trading days and wait until the market opens before trading")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    setup_logging()
    try:
        return run(args)
    except Exception:
        log.exception("Bot run failed")
        return 1
    finally:
        notify(args.dry_run)
        log.info("-" * 60)


if __name__ == "__main__":
    sys.exit(main())
