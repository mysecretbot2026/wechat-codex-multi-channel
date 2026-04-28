import os
from pathlib import Path


DEFAULT_CODEX_ACCOUNT = "main"


def expand_path(value):
    return str(Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve())


def normalize_codex_accounts(config):
    codex = config.setdefault("codex", {})
    accounts = []
    seen = set()
    raw_accounts = codex.get("accounts") or []
    if not raw_accounts:
        raw_accounts = [
            {
                "name": codex.get("defaultAccount") or DEFAULT_CODEX_ACCOUNT,
                "codexHome": codex.get("codexHome") or "~/.codex",
            }
        ]
    for raw in raw_accounts:
        account = dict(raw or {})
        name = str(account.get("name") or "").strip()
        if not name:
            continue
        if name in seen:
            raise RuntimeError(f"重复的 Codex 账号名: {name}")
        seen.add(name)
        account["name"] = name
        account["codexHome"] = expand_path(account.get("codexHome") or "~/.codex")
        accounts.append(account)
    if not accounts:
        accounts.append({"name": DEFAULT_CODEX_ACCOUNT, "codexHome": expand_path("~/.codex")})
    default_account = str(codex.get("defaultAccount") or accounts[0]["name"]).strip()
    if default_account not in {account["name"] for account in accounts}:
        default_account = accounts[0]["name"]
    codex["accounts"] = accounts
    codex["defaultAccount"] = default_account
    return config


def list_codex_accounts(config):
    return list((config.get("codex") or {}).get("accounts") or [])


def default_codex_account(config):
    return (config.get("codex") or {}).get("defaultAccount") or DEFAULT_CODEX_ACCOUNT


def codex_account_names(config):
    return [account["name"] for account in list_codex_accounts(config)]


def find_codex_account(config, selector):
    value = str(selector or "").strip()
    accounts = list_codex_accounts(config)
    if not value:
        return None
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(accounts):
            return dict(accounts[index])
    for account in accounts:
        if account.get("name") == value:
            return dict(account)
    lowered = value.lower()
    matches = [account for account in accounts if str(account.get("name") or "").lower().startswith(lowered)]
    if len(matches) == 1:
        return dict(matches[0])
    return None


def get_codex_account(config, name=None):
    target = name or default_codex_account(config)
    for account in list_codex_accounts(config):
        if account.get("name") == target:
            return dict(account)
    for account in list_codex_accounts(config):
        if account.get("name") == default_codex_account(config):
            return dict(account)
    accounts = list_codex_accounts(config)
    return dict(accounts[0]) if accounts else {"name": DEFAULT_CODEX_ACCOUNT, "codexHome": expand_path("~/.codex")}


def resolve_session_codex_account(config, session):
    name = (session or {}).get("codexAccount") or default_codex_account(config)
    valid_names = set(codex_account_names(config))
    if name not in valid_names:
        name = default_codex_account(config)
    return get_codex_account(config, name)


def adjacent_codex_account(config, current_name, step):
    accounts = list_codex_accounts(config)
    if not accounts:
        return None
    names = [account["name"] for account in accounts]
    try:
        index = names.index(current_name)
    except ValueError:
        index = 0
    return dict(accounts[(index + step) % len(accounts)])
