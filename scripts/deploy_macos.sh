#!/usr/bin/env bash
set -euo pipefail

LABEL="${WECHAT_CODEX_MULTI_LABEL:-com.wechat-codex-multi}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${WECHAT_CODEX_MULTI_VENV:-$PROJECT_DIR/.venv}"
CONFIG_FILE="${WECHAT_CODEX_MULTI_CONFIG:-$PROJECT_DIR/config.json}"
PLIST_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/wechat-codex-multi"
START_SERVICE=1
SETUP_ACCOUNT=1
INSTALL_NODE=1

usage() {
  cat <<EOF
Usage: scripts/deploy_macos.sh [options]

Options:
  --no-start        Install and write launchd plist, but do not start the service.
  --skip-account    Do not run the interactive WeChat add-account flow.
  --skip-npm        Do not run npm install.
  --config PATH     Use a custom config path. Default: $PROJECT_DIR/config.json
  --venv PATH       Use a custom Python virtualenv path. Default: $PROJECT_DIR/.venv
  -h, --help        Show this help.

Environment:
  PYTHON_BIN                 Python executable used to create the venv.
  WECHAT_CODEX_MULTI_LABEL   launchd label. Default: com.wechat-codex-multi
  WECHAT_CODEX_MULTI_CONFIG  Config file path.
  WECHAT_CODEX_MULTI_VENV    Virtualenv directory.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-start)
      START_SERVICE=0
      shift
      ;;
    --skip-account)
      SETUP_ACCOUNT=0
      shift
      ;;
    --skip-npm)
      INSTALL_NODE=0
      shift
      ;;
    --config)
      CONFIG_FILE="$2"
      shift 2
      ;;
    --venv)
      VENV_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  printf '[deploy] %s\n' "$*"
}

die() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

abs_path() {
  "$PYTHON_BIN" - "$1" <<'PY'
import os
import sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

if [ "$(uname -s)" != "Darwin" ]; then
  die "This deployment script targets macOS launchd. Current OS: $(uname -s)"
fi

require_command "$PYTHON_BIN"

CONFIG_FILE="$(abs_path "$CONFIG_FILE")"
VENV_DIR="$(abs_path "$VENV_DIR")"

cd "$PROJECT_DIR"
log "Project: $PROJECT_DIR"
log "Virtualenv: $VENV_DIR"
log "Config: $CONFIG_FILE"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  if [ -d "$VENV_DIR" ]; then
    log "Removing broken virtualenv: $VENV_DIR"
    rm -rf "$VENV_DIR"
  fi
  log "Creating Python virtualenv"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  log "Reusing existing Python virtualenv"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

log "Installing Python dependencies"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PIP" install -r "$PROJECT_DIR/requirements.txt"

if [ "$INSTALL_NODE" -eq 1 ]; then
  if command -v npm >/dev/null 2>&1; then
    log "Installing npm dependencies"
    npm install
  else
    log "npm not found; skipping npm install"
  fi
fi

if [ ! -f "$CONFIG_FILE" ]; then
  log "Creating config from config.example.json"
  mkdir -p "$(dirname "$CONFIG_FILE")"
  cp "$PROJECT_DIR/config.example.json" "$CONFIG_FILE"
else
  log "Keeping existing config"
fi

if command -v codex >/dev/null 2>&1; then
  if codex login status >/dev/null 2>&1; then
    log "Codex CLI login looks available"
  else
    log "Codex CLI exists, but login status failed. Run: codex login"
  fi
else
  log "codex command not found. Install and login Codex CLI before starting real traffic."
fi

if command -v claude >/dev/null 2>&1; then
  if claude auth status --text >/dev/null 2>&1; then
    log "Claude Code CLI login looks available"
  else
    log "Claude Code CLI exists, but auth status failed. Run: claude auth login"
  fi
else
  log "claude command not found. Install and login Claude Code CLI before using /agent claude."
fi

account_count="$("$VENV_PYTHON" - "$CONFIG_FILE" <<'PY'
import contextlib
import io
import sys
from wechat_codex_multi.config import load_config
from wechat_codex_multi.state import StateStore

with contextlib.redirect_stdout(io.StringIO()):
    config = load_config(sys.argv[1])
state = StateStore(config["stateDir"])
print(len(state.list_accounts()))
PY
)"

if [ "$SETUP_ACCOUNT" -eq 1 ] && [ "$account_count" = "0" ]; then
  log "No WeChat bot account found; starting interactive add-account flow"
  WECHAT_CODEX_MULTI_CONFIG="$CONFIG_FILE" "$VENV_PYTHON" -m wechat_codex_multi add-account
else
  log "WeChat bot accounts: $account_count"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

log "Writing launchd plist: $PLIST_FILE"
"$PYTHON_BIN" - "$PLIST_FILE" "$LABEL" "$VENV_PYTHON" "$PROJECT_DIR" "$CONFIG_FILE" "$LOG_DIR" <<'PY'
import plistlib
import sys

plist_file, label, python_bin, project_dir, config_file, log_dir = sys.argv[1:]
data = {
    "Label": label,
    "ProgramArguments": [
        python_bin,
        "-m",
        "wechat_codex_multi",
        "start",
    ],
    "WorkingDirectory": project_dir,
    "EnvironmentVariables": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "WECHAT_CODEX_MULTI_CONFIG": config_file,
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": f"{log_dir}/stdout.log",
    "StandardErrorPath": f"{log_dir}/stderr.log",
}
with open(plist_file, "wb") as fh:
    plistlib.dump(data, fh, sort_keys=False)
PY

domain="gui/$(id -u)"
service="$domain/$LABEL"

log "Reloading launchd service"
launchctl bootout "$service" >/dev/null 2>&1 || true
launchctl bootstrap "$domain" "$PLIST_FILE"
launchctl enable "$service" >/dev/null 2>&1 || true

if [ "$START_SERVICE" -eq 1 ]; then
  log "Starting service"
  launchctl kickstart -k "$service"
else
  log "Skipping service start because --no-start was provided"
fi

log "Deployment complete"
log "Status: launchctl print $service"
log "Logs: tail -f $LOG_DIR/stdout.log $LOG_DIR/stderr.log"
