import os
from pathlib import Path


DEFAULT_CLAUDE_ACCOUNT = "main"


def expand_path(value):
    return str(Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve())


def normalize_claude_accounts(config):
    claude = config.setdefault("claude", {})
    accounts = []
    seen = set()
    raw_accounts = claude.get("accounts") or []
    if not raw_accounts:
        raw_accounts = [
            {
                "name": claude.get("defaultAccount") or DEFAULT_CLAUDE_ACCOUNT,
                "claudeConfigDir": claude.get("claudeConfigDir") or "",
            }
        ]
    for raw in raw_accounts:
        account = dict(raw or {})
        name = str(account.get("name") or "").strip()
        if not name:
            continue
        if name in seen:
            raise RuntimeError(f"重复的 Claude 账号名: {name}")
        seen.add(name)
        account["name"] = name
        raw_config_dir = str(account.get("claudeConfigDir") or "").strip()
        account["claudeConfigDir"] = expand_path(raw_config_dir) if raw_config_dir else ""
        accounts.append(account)
    if not accounts:
        accounts.append({"name": DEFAULT_CLAUDE_ACCOUNT, "claudeConfigDir": ""})
    default_account = str(claude.get("defaultAccount") or accounts[0]["name"]).strip()
    if default_account not in {account["name"] for account in accounts}:
        default_account = accounts[0]["name"]
    claude["accounts"] = accounts
    claude["defaultAccount"] = default_account
    return config


def list_claude_accounts(config):
    return list((config.get("claude") or {}).get("accounts") or [])


def default_claude_account(config):
    return (config.get("claude") or {}).get("defaultAccount") or DEFAULT_CLAUDE_ACCOUNT


def claude_account_names(config):
    return [account["name"] for account in list_claude_accounts(config)]


def find_claude_account(config, selector):
    value = str(selector or "").strip()
    accounts = list_claude_accounts(config)
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


def get_claude_account(config, name=None):
    target = name or default_claude_account(config)
    for account in list_claude_accounts(config):
        if account.get("name") == target:
            return dict(account)
    for account in list_claude_accounts(config):
        if account.get("name") == default_claude_account(config):
            return dict(account)
    accounts = list_claude_accounts(config)
    return dict(accounts[0]) if accounts else {"name": DEFAULT_CLAUDE_ACCOUNT, "claudeConfigDir": ""}


def resolve_session_claude_account(config, session):
    name = (session or {}).get("claudeAccount") or default_claude_account(config)
    valid_names = set(claude_account_names(config))
    if name not in valid_names:
        name = default_claude_account(config)
    return get_claude_account(config, name)


def adjacent_claude_account(config, current_name, step):
    accounts = list_claude_accounts(config)
    if not accounts:
        return None
    names = [account["name"] for account in accounts]
    try:
        index = names.index(current_name)
    except ValueError:
        index = 0
    return dict(accounts[(index + step) % len(accounts)])
