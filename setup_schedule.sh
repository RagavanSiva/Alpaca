#!/usr/bin/env bash
# Schedules the trading bot on Linux to run every weekday at the 9:30 AM ET market open.
#
# Preferred: a systemd user timer scheduled directly in America/New_York time, so US
# daylight saving is handled regardless of the server's timezone.
# Fallback (no systemd, e.g. containers): cron with CRON_TZ=America/New_York.
# Either way the bot starts at 9:25 ET, skips market holidays, and waits for the open.
#
# Setup:
#   python3 -m venv venv && venv/bin/pip install -r requirements.txt
#   cp .env.example .env   # then add your Alpaca keys
#   chmod +x setup_schedule.sh && ./setup_schedule.sh
# Remove:
#   ./setup_schedule.sh --remove
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJECT_DIR/venv/bin/python"
SCRIPT="$PROJECT_DIR/trading_bot.py"
UNIT="alpaca-bot"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CRON_TAG="# alpaca-ma-crossover-bot"

has_systemd_user() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

remove_cron() {
  if command -v crontab >/dev/null 2>&1 && crontab -l >/dev/null 2>&1; then
    crontab -l | grep -vF "$CRON_TAG" | crontab -
  fi
}

if [[ "${1:-}" == "--remove" ]]; then
  if has_systemd_user; then
    systemctl --user disable --now "$UNIT.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT.service" "$UNIT_DIR/$UNIT.timer"
    systemctl --user daemon-reload
  fi
  remove_cron
  echo "Schedule removed."
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Virtualenv not found at $PYTHON"
  echo "Create it with: python3 -m venv venv && venv/bin/pip install -r requirements.txt"
  exit 1
fi
[[ -f "$PROJECT_DIR/.env" ]] || echo "Warning: $PROJECT_DIR/.env not found. Copy .env.example to .env and add your keys."

if has_systemd_user; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=Alpaca moving average crossover bot

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON $SCRIPT --wait-for-open
TimeoutStartSec=2h
EOF
  cat > "$UNIT_DIR/$UNIT.timer" <<EOF
[Unit]
Description=Run the Alpaca bot at the 9:30 AM ET market open

[Timer]
OnCalendar=Mon..Fri *-*-* 09:25:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT.timer"
  remove_cron
  if ! loginctl enable-linger "$USER" 2>/dev/null; then
    echo "Note: run 'sudo loginctl enable-linger $USER' so the bot also runs while you are logged out."
  fi
  echo "Installed systemd timer '$UNIT.timer':"
  systemctl --user list-timers "$UNIT.timer" --no-pager
else
  if ! command -v crontab >/dev/null 2>&1; then
    echo "Neither systemd user services nor cron are available on this system."
    exit 1
  fi
  {
    crontab -l 2>/dev/null | grep -vF "$CRON_TAG" || true
    echo "CRON_TZ=America/New_York $CRON_TAG"
    echo "25 9 * * 1-5 cd \"$PROJECT_DIR\" && \"$PYTHON\" \"$SCRIPT\" --wait-for-open >/dev/null 2>&1 $CRON_TAG"
  } | crontab -
  echo "Installed cron job (9:25 AM ET, Mon-Fri)."
  echo "Note: if your cron ignores CRON_TZ (Debian/Ubuntu's default cron does), set the"
  echo "server timezone to America/New_York or use a system with systemd."
fi
