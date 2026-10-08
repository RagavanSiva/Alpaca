"""Streamlit dashboard for the Alpaca moving average crossover bot.

Run:  venv\\Scripts\\streamlit run dashboard.py   (Windows)
      venv/bin/streamlit run dashboard.py       (Linux)

Set DASHBOARD_MODE=remote or local to override the automatic detection of
whether the dashboard runs on the same machine as the bot.

When the bot runs on GitHub Actions, set GITHUB_REPO and GITHUB_TOKEN (see
github_store.py) and the dashboard reads the schedule, log and settings from GitHub.
"""

import hmac
import json
import os
import subprocess
import sys
from datetime import datetime

import pandas as pd
import streamlit as st
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import config
from github_store import WORKFLOW, GitHubStore, next_scheduled_run
from trading_bot import BASE_DIR, ET, LOG_FILE, detect_crossover, fetch_prices, make_client, stop_price_for

WINDOWS_TASK = "Alpaca MA Crossover Bot"  # created by setup_schedule.ps1
SYSTEMD_UNIT = "alpaca-bot"               # created by setup_schedule.sh
CRON_TAG = "# alpaca-ma-crossover-bot"    # created by setup_schedule.sh (no-systemd fallback)

# The bot, its schedule, log and settings.json live on the machine that runs the bot.
# When the dashboard is hosted elsewhere (e.g. Streamlit Cloud, which has no .env file)
# those aren't reachable.
_mode = os.getenv("DASHBOARD_MODE", "").lower()
ON_BOT_PC = _mode == "local" or (_mode != "remote" and (BASE_DIR / ".env").exists())
GH = GitHubStore.from_env()  # set when the bot runs on GitHub Actions
CAN_EDIT_SETTINGS = GH is not None or ON_BOT_PC
REMOTE_NOTE = ("Only available when the dashboard runs on the machine that runs the bot, "
               "or when GITHUB_REPO and GITHUB_TOKEN are configured.")

st.set_page_config(page_title="Alpaca Bot Dashboard", layout="wide")


@st.cache_resource
def client():
    return make_client()


@st.cache_data(ttl=300)
def strategy_data(symbol: str) -> tuple[str | None, pd.DataFrame]:
    return detect_crossover(fetch_prices(symbol, config.LOOKBACK_DAYS), config.SHORT_WINDOW, config.LONG_WINDOW)


def run_cmd(args: list[str]) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def windows_schedule() -> dict[str, str] | None:
    out = run_cmd(["powershell", "-NoProfile", "-Command",
                   f"$i = Get-ScheduledTaskInfo -TaskName '{WINDOWS_TASK}' -ErrorAction Stop; "
                   f"$t = Get-ScheduledTask -TaskName '{WINDOWS_TASK}'; "
                   "\"$($t.State)|$($i.NextRunTime)|$($i.LastRunTime)|$($i.LastTaskResult)\""])
    if not out or "|" not in out:
        return None
    state, next_run, last_run, code = out.strip().split("|")
    results = {"0": "OK", "267011": "Not run yet", "267009": "Running"}
    return {
        "type": f"Windows Task Scheduler: {WINDOWS_TASK}",
        "state": state,
        "next_run": next_run or "-",
        "last_run": "never" if last_run.startswith("11/30/1999") else last_run,
        "last_result": results.get(code, f"Error {code}"),
    }


def systemd_props(unit: str, *props: str) -> dict[str, str]:
    out = run_cmd(["systemctl", "--user", "show", unit, *(f"--property={p}" for p in props)])
    return dict(line.split("=", 1) for line in (out or "").splitlines() if "=" in line)


def linux_schedule() -> dict[str, str] | None:
    timer = systemd_props(f"{SYSTEMD_UNIT}.timer", "LoadState", "ActiveState", "NextElapseUSecRealtime", "LastTriggerUSec")
    if timer.get("LoadState") == "loaded":
        svc = systemd_props(f"{SYSTEMD_UNIT}.service", "ActiveState", "Result", "ExecMainStatus")
        last_run = timer.get("LastTriggerUSec", "")
        if last_run in ("", "n/a", "0"):
            last_run, last_result = "never", "Not run yet"
        elif svc.get("ActiveState") == "activating":
            last_result = "Running"
        elif svc.get("Result") == "success":
            last_result = "OK"
        else:
            last_result = f"Error ({svc.get('Result')}, exit {svc.get('ExecMainStatus')})"
        return {
            "type": f"systemd timer: {SYSTEMD_UNIT}.timer",
            "state": timer.get("ActiveState", "-"),
            "next_run": timer.get("NextElapseUSecRealtime") or "-",
            "last_run": last_run,
            "last_result": last_result,
        }
    if CRON_TAG in (run_cmd(["crontab", "-l"]) or ""):
        return {"type": "cron", "state": "Scheduled", "next_run": "Mon-Fri 09:25 ET",
                "last_run": "see bot log", "last_result": "see bot log"}
    return None


@st.cache_data(ttl=60)
def scheduled_task_info() -> dict[str, str] | None:
    return windows_schedule() if sys.platform == "win32" else linux_schedule()


@st.cache_data(ttl=30)
def gh_runs() -> list[dict]:
    return GH.workflow_runs(10)


@st.cache_data(ttl=60)
def gh_log() -> str | None:
    return GH.read_text("trading_bot.log")


@st.cache_data(ttl=30)
def gh_settings() -> dict | None:
    text = GH.read_text("settings.json")
    return json.loads(text) if text else None


def run_result(run: dict) -> str:
    if run["status"] != "completed":
        return run["status"].replace("_", " ").title()
    return "OK" if run["conclusion"] == "success" else (run["conclusion"] or "unknown").replace("_", " ").title()


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


def section_schedule_github() -> None:
    st.subheader("Scheduled runs (GitHub Actions)")
    runs = gh_runs()
    last = runs[0] if runs else None
    cols = st.columns(3)
    cols[0].metric("Next scheduled run", f"{next_scheduled_run().astimezone(ET):%a %b %d %H:%M} ET",
                   "then waits for the 9:30 open", delta_color="off")
    cols[1].metric("Last run", datetime.fromisoformat(last["run_started_at"]).astimezone(ET).strftime("%a %b %d %H:%M ET") if last else "never")
    cols[2].metric("Last result", run_result(last) if last else "Not run yet")

    dry, live = st.columns(2)
    if dry.button("Dry run now", width="stretch", help="Checks signals and logs what it would do, without placing orders"):
        GH.dispatch(dry_run=True)
        st.success("Dry run started. Refresh in a minute to see it.")
    if live.button("Run now (places orders)", width="stretch", help="Same as the scheduled run, but starts immediately"):
        GH.dispatch(dry_run=False)
        st.success("Run started. Refresh in a minute to see it.")

    if runs:
        st.dataframe(pd.DataFrame([{
            "Started (ET)": datetime.fromisoformat(r["run_started_at"]).astimezone(ET).strftime("%Y-%m-%d %H:%M"),
            "Trigger": {"schedule": "scheduled", "workflow_dispatch": "manual"}.get(r["event"], r["event"]),
            "Result": run_result(r),
            "Details": r["html_url"],
        } for r in runs]), hide_index=True, width="stretch",
            column_config={"Details": st.column_config.LinkColumn(display_text="open")})
    else:
        st.info(f"No runs yet. Make sure .github/workflows/{WORKFLOW} is pushed and the Alpaca secrets are set on GitHub.")


def section_schedule() -> None:
    if GH:
        section_schedule_github()
        return
    st.subheader("Scheduled task")
    if not ON_BOT_PC:
        st.info(REMOTE_NOTE)
        return
    info = scheduled_task_info()
    if info is None:
        script = "setup_schedule.ps1" if sys.platform == "win32" else "./setup_schedule.sh"
        st.warning(f"No bot schedule found. Run {script} to create it.")
        return
    st.caption(info["type"])
    cols = st.columns(4)
    cols[0].metric("State", info["state"])
    cols[1].metric("Next run", info["next_run"])
    cols[2].metric("Last run", info["last_run"])
    cols[3].metric("Last result", info["last_result"])


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
    if GH:
        text = gh_log()
    elif LOG_FILE.exists():
        text = LOG_FILE.read_text(encoding="utf-8")
    else:
        text = None
    if text is None:
        st.info("No log yet. It is created on the first bot run." if GH or ON_BOT_PC else REMOTE_NOTE)
        return
    lines = text.splitlines()
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
        saved = st.form_submit_button("Save", type="primary", width="stretch", disabled=not CAN_EDIT_SETTINGS)
    if not CAN_EDIT_SETTINGS:
        st.warning("Read-only here: the bot reads settings.json on the machine it runs on, so changes saved on "
                   "this server would not reach it. Configure GITHUB_REPO and GITHUB_TOKEN, or open the "
                   "dashboard on the bot's machine, to change settings.")
    if saved:
        values = {"POSITION_SIZE_PCT": size, "MIN_ORDER_USD": min_order, "STOP_LOSS_PCT": stop}
        if GH:
            GH.write_text("settings.json", config.settings_json(values), "Update bot settings from dashboard")
            gh_settings.clear()
            config.apply_settings(values)
        else:
            config.save_settings(values)
        st.success("Saved. The bot uses these from its next run.")

    acct = tc.get_account()
    per_buy = min(float(acct.equity) * config.POSITION_SIZE_PCT / 100, float(acct.non_marginable_buying_power))
    st.caption(f"At current equity ({money(acct.equity)}), each buy is about {money(per_buy)}.")
    st.caption("Stop-loss changes apply to open positions from the next run, when their stop orders are renewed.")


def show(section, *args) -> None:
    try:
        section(*args)
    except Exception as exc:
        st.error(f"{section.__name__.removeprefix('section_').title()} error: {exc}")
        st.exception(exc)


st.title("Alpaca Trading Bot Dashboard")

if password := os.getenv("DASHBOARD_PASSWORD"):
    if not st.session_state.get("authenticated"):
        entered = st.text_input("Password", type="password")
        if entered and hmac.compare_digest(entered, password):
            st.session_state.authenticated = True
            st.rerun()
        if entered:
            st.error("Wrong password.")
        st.stop()

top = st.columns([6, 1])
top[0].caption(f"Updated {datetime.now(ET):%Y-%m-%d %H:%M:%S} ET")
if top[1].button("Refresh", width="stretch"):
    st.cache_data.clear()
    st.rerun()

config.load_settings()
if GH:
    try:
        config.apply_settings(gh_settings() or {})
    except Exception as exc:
        st.warning(f"Could not load settings from GitHub, showing defaults: {exc}")

try:
    tc = client()
except Exception as exc:
    st.error(f"Could not connect to Alpaca: {exc}")
    st.stop()

with st.sidebar:
    show(section_settings, tc)
show(section_account, tc)
show(section_positions, tc)
st.divider()
show(section_strategy)
st.divider()
left, right = st.columns(2)
with left:
    show(section_schedule)
    show(section_orders, tc)
with right:
    show(section_log)
