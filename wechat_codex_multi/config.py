import json
import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_DIR / "config.json"


DEFAULT_CONFIG = {
    "stateDir": "~/.wechat-codex-multi",
    "defaultAgent": "codex",
    "wechat": {
        "baseUrl": "https://ilinkai.weixin.qq.com",
        "botType": "3",
        "routeTag": None,
    },
    "codex": {
        "bin": "codex",
        "workingDirectory": ".",
        "model": "",
        "reasoningEffort": "",
        "modelOptions": [],
        "modelDiscoveryTimeoutSeconds": 30,
        "timeoutMs": 7200_000,
        "bypassApprovalsAndSandbox": True,
        "defaultAccount": "main",
        "accounts": [
            {
                "name": "main",
                "codexHome": "~/.codex",
            }
        ],
        "extraPrompt": "",
    },
    "concurrency": {
        "maxWorkers": 4,
        "commandWorkers": 2,
        "perConversationSerial": True,
    },
    "state": {
        "saveDebounceMs": 1000,
    },
    "media": {
        "enabled": True,
        "maxFileBytes": 52_428_800,
        "maxConcurrentTransfers": 1,
        "generators": [],
    },
    "allowedUsers": [],
    "adminUsers": [],
    "textChunkLimit": 4000,
    "logLevel": "INFO",
}


def expand_path(value):
    return str(Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve())


def deep_merge(base, override):
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path=None):
    from .codex_accounts import normalize_codex_accounts

    config_file = Path(path or os.environ.get("WECHAT_CODEX_MULTI_CONFIG") or DEFAULT_CONFIG_FILE)
    loaded = {}
    if config_file.exists():
        loaded = json.loads(config_file.read_text(encoding="utf-8"))
    config = deep_merge(DEFAULT_CONFIG, loaded)
    config["configFile"] = str(config_file)
    config["stateDir"] = expand_path(config["stateDir"])
    config["codex"]["workingDirectory"] = expand_path(config["codex"]["workingDirectory"])
    normalize_codex_accounts(config)
    return config
