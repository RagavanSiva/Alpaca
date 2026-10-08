"""Moving average crossover bot for Alpaca paper trading.

Usage (Windows: venv\\Scripts\\python, Linux: venv/bin/python):
    python trading_bot.py                    # check now, trade on crossover / stop-loss
    python trading_bot.py --dry-run          # only report the signal
    python trading_bot.py --wait-for-open    # scheduled mode: skip holidays, wait for 9:30 ET

Scheduling: setup_schedule.ps1 (Windows Task Scheduler) or setup_schedule.sh (Linux systemd/cron).
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest, StopOrderRequest
from dotenv import load_dotenv

import config

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "logs" / "trading_bot.log"
ET = ZoneInfo("America/New_York")

log = logging.getLogger("trading_bot")


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s ET [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda *args: datetime.now(ET).timetuple()
    for handler in (logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def wait_for_market_open(client: TradingClient) -> bool:
    """Return False if today is not a trading day; otherwise sleep until the opening bell."""
    today = datetime.now(ET).date()
    calendar = client.get_calendar(GetCalendarRequest(start=today, end=today))
    if not calendar or calendar[0].date != today:
        log.info("Market closed today (%s). Nothing to do.", today)
        return False

    market_open = datetime.combine(today, calendar[0].open.time(), ET)
    delay = (market_open - datetime.now(ET)).total_seconds()
    if delay > 0:
        log.info("Waiting %.0f seconds for market open at %s ET", delay, market_open.strftime("%H:%M"))
        time.sleep(delay)
    elif delay < -15 * 60:
        log.warning("Started %.0f minutes after market open", -delay / 60)
    return True


def fetch_prices(symbol: str, days: int) -> pd.Series:
    # Pull a wider window, then keep the last `days` completed trading sessions.
    history = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
    if history.empty:
        raise RuntimeError(f"No price data returned for {symbol}")
    close = history["Close"].dropna()
    now = datetime.now(ET)
    if close.index[-1].date() == now.date() and now.hour < 16:
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
        return client.get_open_position(symbol)
    except APIError:
        return None


def stop_price_for(entry: float) -> float:
    return round(entry * (1 - config.STOP_LOSS_PCT / 100), 2)


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
        if o.side == OrderSide.SELL and o.order_type == OrderType.STOP:
            client.cancel_order_by_id(o.id)
            wait_for_order(client, o.id, {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED}, timeout=10)
            log.info("[%s] Canceled previous stop order %s", symbol, o.id)


def place_stop(client: TradingClient, symbol: str, qty: float, entry: float, dry_run: bool) -> None:
    stop = stop_price_for(entry)
    if dry_run:
        log.info("[%s] [dry-run] Would place stop-loss: sell %g share(s) at $%.2f", symbol, qty, stop)
        return
    order = client.submit_order(StopOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, stop_price=stop,
    ))
    log.info("[%s] Stop-loss placed: sell %g share(s) if price <= $%.2f (id=%s, valid today)", symbol, qty, stop, order.id)


def sell_all(client: TradingClient, symbol: str, reason: str, dry_run: bool) -> None:
    if dry_run:
        log.info("[%s] [dry-run] Would sell entire position (%s)", symbol, reason)
        return
    order = client.close_position(symbol)
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
        symbol=symbol, notional=amount, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
    ))
    log.info("[%s] Buy order submitted: $%.2f (id=%s)", symbol, amount, order.id)
    order = wait_for_order(client, order.id, {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED})
    if order.status != OrderStatus.FILLED:
        log.warning("[%s] Buy not filled yet (status=%s). Stop-loss will be placed on the next run.", symbol, order.status.value)
        return
    qty, price = float(order.filled_qty), float(order.filled_avg_price)
    log.info("[%s] Bought %g share(s) at $%.2f", symbol, qty, price)
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
    log.info("[%s] Data: %d completed trading days ending %s", symbol, len(close), df.index[-1].date())
    log.info("[%s] Previous day (%s): %d-day MA %.2f, %d-day MA %.2f", symbol, df.index[-2].date(),
             config.SHORT_WINDOW, prev.short_ma, config.LONG_WINDOW, prev.long_ma)
    log.info("[%s] Latest day   (%s): close %.2f, %d-day MA %.2f, %d-day MA %.2f", symbol, df.index[-1].date(),
             last.close, config.SHORT_WINDOW, last.short_ma, config.LONG_WINDOW, last.long_ma)
    log.info("[%s] Signal: %s", symbol, signal or "none")

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
    stop = stop_price_for(entry)
    log.info("[%s] Holding %g share(s): entry $%.2f, now $%.2f (%+.2f%%), stop-loss $%.2f",
             symbol, qty, entry, price, (price / entry - 1) * 100, stop)
    if not dry_run:
        cancel_stop_orders(client, symbol)

    if price <= stop:
        sell_all(client, symbol, f"stop-loss: ${price:.2f} <= ${stop:.2f}", dry_run)
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
    if args.wait_for_open and not wait_for_market_open(client):
        return 0

    failed = []
    for symbol in config.SYMBOLS:
        try:
            trade_symbol(client, symbol, args.dry_run)
        except Exception:
            log.exception("[%s] Failed", symbol)
            failed.append(symbol)

    log.info("Done: %d symbol(s) checked, %d failed%s", len(config.SYMBOLS), len(failed),
             f" ({', '.join(failed)})" if failed else "")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Moving average crossover trading bot")
    parser.add_argument("--dry-run", action="store_true", help="Report the signal without placing orders")
    parser.add_argument("--wait-for-open", action="store_true",
                        help="Skip non-trading days and wait until the market opens before trading")
    args = parser.parse_args()

    setup_logging()
    try:
        return run(args)
    except Exception:
        log.exception("Bot run failed")
        return 1
    finally:
        log.info("-" * 60)


if __name__ == "__main__":
    sys.exit(main())
