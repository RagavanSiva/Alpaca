"""Streamlit dashboard for the Alpaca moving average crossover bot.

Run:  venv\\Scripts\\streamlit run dashboard.py
"""

import subprocess
from datetime import datetime

import pandas as pd
import streamlit as st
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import config
from trading_bot import ET, LOG_FILE, detect_crossover, fetch_prices, make_client, stop_price_for

TASK_NAME = "Alpaca MA Crossover Bot"

st.set_page_config(page_title="Alpaca Bot Dashboard", layout="wide")


@st.cache_resource
def client():
    return make_client()


@st.cache_data(ttl=300)
def strategy_data(symbol: str) -> tuple[str | None, pd.DataFrame]:
    return detect_crossover(fetch_prices(symbol, config.LOOKBACK_DAYS), config.SHORT_WINDOW, config.LONG_WINDOW)


@st.cache_data(ttl=60)
def scheduled_task_info() -> dict[str, str] | None:
    cmd = (f"$i = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}' -ErrorAction Stop; "
           f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}'; "
           "\"$($t.State)|$($i.NextRunTime)|$($i.LastRunTime)|$($i.LastTaskResult)\"")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    if result.returncode != 0 or "|" not in result.stdout:
        return None
    state, next_run, last_run, last_result = result.stdout.strip().split("|")
    return {"state": state, "next_run": next_run, "last_run": last_run, "last_result": last_result}


def money(value) -> str:
    return f"${float(value):,.2f}"


def section_account(tc) -> None:
    acct = tc.get_account()
    clock = tc.get_clock()
    equity, last_equity = float(acct.equity), float(acct.last_equity)
    day_pl = equity - last_equity

    st.subheader(f"Account {acct.account_number} ({'paper' if config.PAPER else 'LIVE'})")
    cols = st.columns(5)
    cols[0].metric("Equity", money(equity), f"{day_pl:+,.2f} today")
    cols[1].metric("Cash", money(acct.cash))
    cols[2].metric("Buying power", money(acct.buying_power))
    cols[3].metric("Positions value", money(acct.long_market_value))
    cols[4].metric("Market", "Open" if clock.is_open else "Closed",
                   f"{'closes' if clock.is_open else 'opens'} {(clock.next_close if clock.is_open else clock.next_open).astimezone(ET):%a %H:%M} ET",
                   delta_color="off")


def section_positions(tc) -> None:
    st.subheader("Positions")
    positions = tc.get_all_positions()
    if not positions:
        st.info("No open positions.")
        return
    df = pd.DataFrame([{
        "Symbol": p.symbol,
        "Qty": float(p.qty),
        "Avg entry": float(p.avg_entry_price),
        "Current": float(p.current_price),
        "Market value": float(p.market_value),
        "Unrealized P/L": float(p.unrealized_pl),
        "P/L %": float(p.unrealized_plpc) * 100,
        "Stop-loss": stop_price_for(float(p.avg_entry_price)) if p.symbol in config.SYMBOLS else None,
    } for p in positions])
    st.dataframe(df, hide_index=True, width="stretch", column_config={
        "Qty": st.column_config.NumberColumn(format="%.6g"),
        "Avg entry": st.column_config.NumberColumn(format="$%.2f"),
        "Current": st.column_config.NumberColumn(format="$%.2f"),
        "Market value": st.column_config.NumberColumn(format="$%.2f"),
        "Unrealized P/L": st.column_config.NumberColumn(format="$%.2f"),
        "P/L %": st.column_config.NumberColumn(format="%.2f%%"),
        "Stop-loss": st.column_config.NumberColumn(format="$%.2f", help="Bot sells if price falls to this level; blank = not managed by the bot"),
    })


def last_crossover(df: pd.DataFrame) -> tuple[pd.Timestamp | None, str]:
    above = df.short_ma > df.long_ma
    crosses = df.index[above.ne(above.shift()) & above.shift().notna()]
    if not len(crosses):
        return None, ""
    return crosses[-1], "golden cross (buy)" if above[crosses[-1]] else "death cross (sell)"


def section_strategy() -> None:
    short, long = config.SHORT_WINDOW, config.LONG_WINDOW
    st.subheader(f"Strategy: {short}/{long}-day MA crossover")
    st.caption(f"Each buy: {config.POSITION_SIZE_PCT:g}% of equity (capped at available cash, min ${config.MIN_ORDER_USD:.2f}) "
               f"· Stop-loss: {config.STOP_LOSS_PCT:g}% below entry")

    rows, data = [], {}
    for symbol in config.SYMBOLS:
        try:
            signal, df = data[symbol] = strategy_data(symbol)
        except Exception as exc:
            rows.append({"Symbol": symbol, "Signal": f"ERROR: {exc}"})
            continue
        last = df.iloc[-1]
        cross_date, kind = last_crossover(df)
        rows.append({
            "Symbol": symbol,
            "Signal": (signal or "none").upper(),
            "Trend": f"{short}-day {'above' if last.short_ma > last.long_ma else 'below'} {long}-day",
            "Close": last.close,
            f"{short}-day MA": last.short_ma,
            f"{long}-day MA": last.long_ma,
            "Gap %": (last.short_ma / last.long_ma - 1) * 100,
            "Last crossover": f"{cross_date:%Y-%m-%d} {kind}" if cross_date is not None else f"none in {len(df)} days",
        })
    price = st.column_config.NumberColumn(format="$%.2f")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Close": price, f"{short}-day MA": price, f"{long}-day MA": price,
        "Gap %": st.column_config.NumberColumn(format="%+.2f%%", help="How far the short MA is from the long MA; near 0 means a crossover is close"),
    })
    if not data:
        return

    symbol = st.selectbox("Chart", list(data))
    signal, df = data[symbol]
    last = df.iloc[-1]
    cols = st.columns(4)
    cols[0].metric("Last close", money(last.close), f"as of {df.index[-1]:%Y-%m-%d}", delta_color="off")
    cols[1].metric(f"{short}-day MA", money(last.short_ma))
    cols[2].metric(f"{long}-day MA", money(last.long_ma))
    cols[3].metric("Signal", (signal or "none").upper(), delta_color="off")

    chart = df.rename(columns={"close": "Close", "short_ma": f"{short}-day MA", "long_ma": f"{long}-day MA"})
    chart.index = chart.index.tz_localize(None)
    st.line_chart(chart, height=350)


def section_schedule() -> None:
    st.subheader("Scheduled task")
    info = scheduled_task_info()
    if info is None:
        st.warning(f"Scheduled task '{TASK_NAME}' not found. Run setup_schedule.ps1 to create it.")
        return
    cols = st.columns(4)
    cols[0].metric("State", info["state"])
    cols[1].metric("Next run (local)", info["next_run"] or "-")
    cols[2].metric("Last run (local)", info["last_run"] if not info["last_run"].startswith("11/30/1999") else "never")
    results = {"0": "OK", "267011": "Not run yet", "267009": "Running"}
    cols[3].metric("Last result", results.get(info["last_result"], f"Error {info['last_result']}"))


def section_orders(tc) -> None:
    st.subheader("Recent orders")
    orders = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=20))
    if not orders:
        st.info("No orders yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Submitted (ET)": o.submitted_at.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
        "Symbol": o.symbol,
        "Side": o.side.value,
        "Qty": o.qty,
        "Type": o.order_type.value,
        "Status": o.status.value,
        "Filled qty": o.filled_qty,
        "Fill price": f"${float(o.filled_avg_price):,.2f}" if o.filled_avg_price else "",
        "Stop price": f"${float(o.stop_price):,.2f}" if o.stop_price else "",
        "Amount": f"${float(o.notional):,.2f}" if o.notional else "",
    } for o in orders]), hide_index=True, width="stretch")


def section_log() -> None:
    st.subheader("Bot log")
    if not LOG_FILE.exists():
        st.info("No log yet. It is created on the first bot run.")
        return
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    n = st.slider("Lines to show", 20, 500, 100, step=20)
    st.code("\n".join(reversed(lines[-n:])) or "(empty)", language=None)


def section_settings(tc) -> None:
    st.header("Trade settings")
    with st.form("settings"):
        size = st.number_input("Position size (% of equity per buy)", min_value=0.01, max_value=100.0,
                               value=float(config.POSITION_SIZE_PCT), step=1.0,
                               help="Each buy uses this % of account equity, capped at available cash")
        min_order = st.number_input("Minimum order ($)", min_value=1.0, value=float(config.MIN_ORDER_USD), step=1.0,
                                    help="Buys smaller than this are skipped. Alpaca's minimum is $1")
        stop = st.number_input("Stop-loss (% below entry)", min_value=0.1, max_value=50.0,
                               value=float(config.STOP_LOSS_PCT), step=0.5,
                               help="Sell a position if it falls this % below the average entry price")
        saved = st.form_submit_button("Save", type="primary", width="stretch")
    if saved:
        config.save_settings({"POSITION_SIZE_PCT": size, "MIN_ORDER_USD": min_order, "STOP_LOSS_PCT": stop})
        st.success("Saved. The bot uses these from its next run.")

    acct = tc.get_account()
    per_buy = min(float(acct.equity) * config.POSITION_SIZE_PCT / 100, float(acct.non_marginable_buying_power))
    st.caption(f"At current equity ({money(acct.equity)}), each buy is about {money(per_buy)}.")
    st.caption("Stop-loss changes apply to open positions from the next run, when their stop orders are renewed.")


config.load_settings()
st.title("Alpaca Trading Bot Dashboard")
top = st.columns([6, 1])
top[0].caption(f"Updated {datetime.now(ET):%Y-%m-%d %H:%M:%S} ET")
if top[1].button("Refresh", width="stretch"):
    st.cache_data.clear()
    st.rerun()

try:
    tc = client()
    with st.sidebar:
        section_settings(tc)
    section_account(tc)
    section_positions(tc)
    st.divider()
    section_strategy()
    st.divider()
    left, right = st.columns(2)
    with left:
        section_schedule()
        section_orders(tc)
    with right:
        section_log()
except Exception as exc:
    st.error(f"Dashboard error: {exc}")
    st.exception(exc)
