import datetime as _dt
import os
import sys


LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_level = LEVELS.get(os.environ.get("LOG_LEVEL", "INFO").upper(), 20)


def configure(level):
    global _level
    _level = LEVELS.get(str(level or "INFO").upper(), 20)


def log(level, message):
    if LEVELS.get(level, 20) < _level:
        return
    ts = _dt.datetime.now().isoformat(timespec="seconds")
    sys.stderr.write(f"[{ts}] [{level}] {message}\n")
    sys.stderr.flush()


def debug(message):
    log("DEBUG", message)


def info(message):
    log("INFO", message)


def warn(message):
    log("WARN", message)


def error(message):
    log("ERROR", message)
