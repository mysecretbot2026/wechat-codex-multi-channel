import json
import os
import select
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def read_codex_usage(codex_bin="codex", timeout_s=15, codex_home=""):
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
    binary = shutil.which(codex_bin) or codex_bin
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = str(Path(os.path.expandvars(os.path.expanduser(str(codex_home)))).resolve())
    subprocess.run(
        [binary, "login", "status"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(5, timeout_s),
        check=True,
    )


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
    binary = shutil.which(codex_bin) or codex_bin
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = str(Path(os.path.expandvars(os.path.expanduser(str(codex_home)))).resolve())
    messages = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "wechat-codex-multi", "title": None, "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/rateLimits/read", "params": None},
    ]
    payload = "".join(json.dumps(message, separators=(",", ":")) + "\n" for message in messages)
    process = subprocess.Popen(
        [binary, "app-server", "--listen", "stdio://"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdin is not None
    process.stdin.write(payload)
    process.stdin.flush()
    deadline = time.time() + timeout_s
    stderr_chunks = []
    try:
        while time.time() < deadline:
            streams = [s for s in (process.stdout, process.stderr) if s is not None]
            readable, _, _ = select.select(streams, [], [], 0.5)
            for stream in readable:
                line = stream.readline()
                if not line:
                    continue
                if stream is process.stderr:
                    stderr_chunks.append(line)
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == 2:
                    if "error" in message:
                        raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                    return message.get("result") or {}
            if process.poll() is not None:
                break
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    error = "".join(stderr_chunks).strip()
    raise RuntimeError(error or "Codex 没有返回用量信息")


def format_codex_usage(usage):
    rate_limits = (usage or {}).get("rateLimits") or {}
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
        rate_limits = usage.get("rateLimits") or {}
        _append_codex_usage_lines(lines, rate_limits, usage.get("account") or {})
    return "\n".join(lines)


def _append_codex_usage_lines(lines, rate_limits, account=None):
    account_line = _format_usage_account(account or {})
    if account_line:
        lines.append(f"登录账号：{account_line}")
    plan = rate_limits.get("planType")
    if plan:
        lines.append(f"套餐：{plan}")
    _append_window(lines, "5 小时窗口", rate_limits.get("primary"))
    _append_window(lines, "周窗口", rate_limits.get("secondary"))
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


def _append_window(lines, label, window):
    if not window:
        lines.append(f"{label}：无数据")
        return
    used = window.get("usedPercent")
    duration = window.get("windowDurationMins")
    resets_at = window.get("resetsAt")
    lines.append(f"{label}：已用 {used}%")
    if duration:
        lines.append(f"窗口长度：{duration} 分钟")
    if resets_at:
        lines.append(f"重置时间：{datetime.fromtimestamp(int(resets_at)).strftime('%Y-%m-%d %H:%M:%S')}")


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
