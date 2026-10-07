import json
import math
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .codex_app_server import AppServerProcess
from .codex_runtime import resolve_codex_bin


def read_codex_usage(codex_bin="codex", timeout_s=15, codex_home="", prefer_app_server=False):
    if prefer_app_server:
        # Desktop credentials may live in the OS keychain. A fresh process reads
        # the selected home's current login without reusing a catalog auth cache.
        return _read_codex_usage_app_server(codex_bin=codex_bin, timeout_s=timeout_s, codex_home=codex_home)
    try:
        return _read_codex_usage_backend(codex_bin=codex_bin, timeout_s=timeout_s, codex_home=codex_home)
    except Exception as backend_err:
        try:
            return _read_codex_usage_app_server(codex_bin=codex_bin, timeout_s=timeout_s, codex_home=codex_home)
        except Exception as app_server_err:
            raise RuntimeError(f"{backend_err}; app-server fallback failed: {app_server_err}") from app_server_err


def _read_codex_usage_backend(codex_bin="codex", timeout_s=15, codex_home=""):
    auth = _load_chatgpt_auth(codex_home)
    try:
        return _request_chatgpt_usage(auth["accessToken"], timeout_s=timeout_s)
    except urllib.error.HTTPError as err:
        if err.code not in {401, 403}:
            raise
        _refresh_codex_auth(codex_bin=codex_bin, timeout_s=timeout_s, codex_home=codex_home)
        auth = _load_chatgpt_auth(codex_home)
        return _request_chatgpt_usage(auth["accessToken"], timeout_s=timeout_s)


def _load_chatgpt_auth(codex_home=""):
    home = Path(os.path.expandvars(os.path.expanduser(str(codex_home or os.environ.get("CODEX_HOME") or "~/.codex")))).resolve()
    path = home / "auth.json"
    if not path.exists():
        raise RuntimeError(f"未找到 Codex 登录文件: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("auth_mode") != "chatgpt":
        raise RuntimeError("当前 Codex 登录方式不是 ChatGPT，无法直接读取用量")
    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("Codex 登录文件缺少 access_token")
    return {"accessToken": access_token}


def _refresh_codex_auth(codex_bin="codex", timeout_s=15, codex_home=""):
    home = str(Path(os.path.expandvars(os.path.expanduser(str(codex_home)))).resolve()) if codex_home else ""
    server = AppServerProcess(resolve_codex_bin(codex_bin), codex_home=home)
    try:
        info = server.request("account/read", {"refreshToken": True}, timeout_s=timeout_s) or {}
        if not info.get("account"):
            raise RuntimeError("所选 Codex 账号未登录，无法刷新令牌")
    finally:
        server.close()


def _request_chatgpt_usage(access_token, timeout_s=15):
    request = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "wechat-codex-multi-channel",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return _normalize_chatgpt_usage(json.loads(response.read().decode("utf-8")))


def _normalize_chatgpt_usage(data):
    rate_limit = data.get("rate_limit") or {}
    credits = data.get("credits") or {}
    return {
        "account": {
            "userId": data.get("user_id") or "",
            "accountId": data.get("account_id") or "",
            "email": data.get("email") or "",
        },
        "rateLimits": {
            "planType": data.get("plan_type") or "",
            "primary": _normalize_chatgpt_window(rate_limit.get("primary_window")),
            "secondary": _normalize_chatgpt_window(rate_limit.get("secondary_window")),
            "credits": {
                "hasCredits": bool(credits.get("has_credits")),
                "balance": credits.get("balance", "0"),
                "unlimited": bool(credits.get("unlimited")),
            },
            "rateLimitReachedType": data.get("rate_limit_reached_type") or ("rate_limit_reached" if rate_limit.get("limit_reached") else ""),
        },
    }


def _normalize_chatgpt_window(window):
    if not window:
        return None
    duration_seconds = window.get("limit_window_seconds")
    duration_mins = int(duration_seconds / 60) if isinstance(duration_seconds, (int, float)) else None
    return {
        "usedPercent": window.get("used_percent"),
        "windowDurationMins": duration_mins,
        "resetsAt": window.get("reset_at"),
    }


def _read_codex_usage_app_server(codex_bin="codex", timeout_s=15, codex_home=""):
    binary = resolve_codex_bin(codex_bin, prefer_desktop=True)
    home = str(Path(os.path.expandvars(os.path.expanduser(str(codex_home)))).resolve()) if codex_home else ""
    server = AppServerProcess(binary, codex_home=home)
    try:
        info = server.request("account/read", {"refreshToken": False}, timeout_s=timeout_s) or {}
        account = info.get("account") or {}
        if not account:
            raise RuntimeError("所选 Codex 账号未登录，无法查询套餐用量")
        if account.get("type") == "apiKey":
            raise RuntimeError("所选 Codex 账号使用 API Key，无法查询 ChatGPT 套餐用量")
        usage = dict(server.request("account/rateLimits/read", timeout_s=timeout_s) or {})
        usage["account"] = account
        return usage
    finally:
        server.close()


def format_codex_usage(usage):
    rate_limits = _codex_rate_limits(usage or {})
    lines = ["Codex 用量："]
    _append_codex_usage_lines(lines, rate_limits, (usage or {}).get("account") or {})
    return "\n".join(lines)


def format_codex_usage_all(results):
    lines = ["Codex 全部账号用量："]
    for result in results:
        account = result.get("account") or {}
        name = account.get("name") or "unknown"
        codex_home = account.get("codexHome") or ""
        lines.append("")
        lines.append(f"[{name}]")
        if codex_home:
            lines.append(f"codexHome: {codex_home}")
        error = result.get("error")
        if error:
            lines.append(f"读取失败：{error}")
            continue
        usage = result.get("usage") or {}
        rate_limits = _codex_rate_limits(usage)
        _append_codex_usage_lines(lines, rate_limits, usage.get("account") or {})
    return "\n".join(lines)


def _codex_rate_limits(usage):
    legacy = usage.get("rateLimits") or {}
    buckets = usage.get("rateLimitsByLimitId") or {}
    if buckets.get("codex"):
        # The default single-bucket view can refer to a different metered model.
        base = legacy if legacy.get("limitId") in {None, "codex"} else {}
        return dict(base, **buckets["codex"])
    return legacy


def _append_codex_usage_lines(lines, rate_limits, account=None):
    account_line = _format_usage_account(account or {})
    if account_line:
        lines.append(f"登录账号：{account_line}")
    plan = rate_limits.get("planType") or (account or {}).get("planType")
    if plan:
        lines.append(f"套餐：{plan}")
    is_pro = str(plan or "").strip().lower() == "pro"
    if is_pro:
        lines.append("5 小时限制：不适用（Pro）")
    shown = False
    for key, fallback in (("primary", "主窗口"), ("secondary", "次窗口")):
        window = rate_limits.get(key)
        if not window:
            continue
        duration = _finite_number(window.get("windowDurationMins"))
        if is_pro and duration == 300:
            # Older snapshots can still contain the retired Pro five-hour slot.
            continue
        if str(plan or "").lower() == "plus" and not duration:
            fallback = "5 小时窗口" if key == "primary" else "周窗口"
        _append_window(lines, _window_label(duration, fallback), window)
        shown = True
    if not shown:
        lines.append("用量窗口：无数据")
    credits = rate_limits.get("credits") or {}
    if credits:
        lines.append(
            "credits："
            + f"hasCredits={str(bool(credits.get('hasCredits'))).lower()} "
            + f"balance={credits.get('balance', '0')} "
            + f"unlimited={str(bool(credits.get('unlimited'))).lower()}"
        )
    reached = rate_limits.get("rateLimitReachedType")
    if reached:
        lines.append(f"限额状态：{reached}")


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _window_label(duration, fallback):
    if not duration or duration <= 0:
        return fallback
    if duration == 10080:
        return "周窗口"
    if duration % 1440 == 0:
        return f"{duration / 1440:g} 天窗口"
    if duration % 60 == 0:
        return f"{duration / 60:g} 小时窗口"
    return f"{duration:g} 分钟窗口"


def _append_window(lines, label, window):
    used = _finite_number(window.get("usedPercent"))
    duration = _finite_number(window.get("windowDurationMins"))
    resets_at = window.get("resetsAt")
    lines.append(f"{label}：已用 {used:g}%" if used is not None else f"{label}：已用比例无数据")
    if duration and duration > 0:
        lines.append(f"窗口长度：{duration:g} 分钟")
    if resets_at:
        try:
            reset_time = datetime.fromtimestamp(int(resets_at)).strftime('%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError, OverflowError, OSError):
            reset_time = "未知"
        lines.append(f"重置时间：{reset_time}")


def _format_usage_account(account):
    email = account.get("email") or ""
    account_id = account.get("accountId") or account.get("userId") or ""
    if email and account_id:
        return f"{email} ({_short_account_id(account_id)})"
    if email:
        return email
    if account_id:
        return _short_account_id(account_id)
    return ""


def _short_account_id(value):
    text = str(value or "")
    if len(text) <= 16:
        return text
    return f"{text[:8]}...{text[-6:]}"
