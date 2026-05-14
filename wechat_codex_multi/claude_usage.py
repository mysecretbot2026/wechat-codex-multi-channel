import json
import os
import shutil
import subprocess
from pathlib import Path


def read_claude_usage(claude_bin="claude", timeout_s=2, claude_config_dir="", permission_mode=""):
    stats = _read_claude_stats_cache(claude_config_dir)
    auth = read_claude_auth_status(
        claude_bin=claude_bin,
        timeout_s=timeout_s,
        claude_config_dir=claude_config_dir,
    )
    subscription = {"text": auth.get("text") or ""}
    if auth.get("error"):
        subscription["error"] = auth.get("error")
    return {
        "auth": auth,
        "subscription": subscription,
        "stats": stats,
    }


def read_claude_auth_status(claude_bin="claude", timeout_s=5, claude_config_dir=""):
    binary = shutil.which(claude_bin) or claude_bin
    env = _claude_env(claude_config_dir)
    data = {}
    text = ""
    try:
        raw = _run_claude_auth_status(binary, env, timeout_s, as_text=False)
        data = json.loads(raw) if raw.strip() else {}
    except Exception as err:
        data = {"error": str(err)}
    try:
        text = _run_claude_auth_status(binary, env, timeout_s, as_text=True).strip()
    except Exception as err:
        if not data.get("error"):
            data["error"] = str(err)
    parsed = _parse_auth_status_text(text)
    result = {
        "loggedIn": bool(data.get("loggedIn") or parsed.get("loggedIn")),
        "authMethod": data.get("authMethod") or parsed.get("authMethod") or "",
        "apiProvider": data.get("apiProvider") or "",
        "apiKeySource": data.get("apiKeySource") or "",
        "email": data.get("email") or parsed.get("email") or "",
        "orgId": data.get("orgId") or "",
        "orgName": data.get("orgName") or parsed.get("orgName") or "",
        "subscriptionType": data.get("subscriptionType") or "",
        "text": text,
    }
    if data.get("error"):
        result["error"] = data.get("error")
    return result


def _run_claude_auth_status(binary, env, timeout_s, as_text=False):
    args = [binary, "auth", "status"]
    if as_text:
        args.append("--text")
    process = subprocess.run(
        args,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        check=False,
    )
    output = (process.stdout or "").strip()
    error = (process.stderr or "").strip()
    if process.returncode != 0 and not output:
        raise RuntimeError(error or f"claude auth status 退出码 {process.returncode}")
    return output or error


def _parse_auth_status_text(text):
    result = {}
    value = str(text or "").strip()
    if not value:
        return result
    lowered = value.lower()
    if "not logged in" in lowered:
        result["loggedIn"] = False
    for line in value.splitlines():
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        field = key.strip().lower()
        item = raw.strip()
        if field == "email":
            result["email"] = item
            result["loggedIn"] = True
        elif field == "organization":
            result["orgName"] = item
        elif field == "login method":
            result["authMethod"] = item
            result["loggedIn"] = True
    return result


def _claude_env(claude_config_dir=""):
    env = os.environ.copy()
    if claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(os.path.expandvars(os.path.expanduser(str(claude_config_dir)))).resolve())
    return env


def _read_claude_stats_cache(claude_config_dir=""):
    path = _stats_cache_path(claude_config_dir)
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as err:
        return {"path": str(path), "exists": True, "error": str(err)}
    return {
        "path": str(path),
        "exists": True,
        "lastComputedDate": data.get("lastComputedDate") or "",
        "totalSessions": data.get("totalSessions"),
        "totalMessages": data.get("totalMessages"),
        "modelUsage": data.get("modelUsage") or {},
    }


def _stats_cache_path(claude_config_dir=""):
    if claude_config_dir:
        root = Path(os.path.expandvars(os.path.expanduser(str(claude_config_dir)))).resolve()
    else:
        root = Path("~/.claude").expanduser().resolve()
    return root / "stats-cache.json"


def format_claude_usage(usage, account=None):
    lines = ["Claude 用量："]
    _append_claude_usage_lines(lines, usage, account or {})
    return "\n".join(lines)


def format_claude_usage_all(results):
    lines = ["Claude 全部账号用量："]
    for result in results:
        account = result.get("account") or {}
        name = account.get("name") or "unknown"
        config_dir = account.get("claudeConfigDir") or ""
        lines.append("")
        lines.append(f"[{name}]")
        lines.append(f"claudeConfigDir: {config_dir or '(default)'}")
        error = result.get("error")
        if error:
            lines.append(f"读取失败：{error}")
            continue
        _append_claude_usage_lines(lines, result.get("usage") or {}, account)
    return "\n".join(lines)


def _append_claude_usage_lines(lines, usage, account):
    config_dir = account.get("claudeConfigDir") or ""
    if config_dir:
        lines.append(f"登录配置：{config_dir}")
    else:
        lines.append("登录配置：默认 Claude Code 登录态")
    auth = (usage or {}).get("auth") or {}
    if auth:
        lines.append(f"登录状态：{'已登录' if auth.get('loggedIn') else '未登录'}")
        if auth.get("email"):
            lines.append(f"邮箱：{auth.get('email')}")
        if auth.get("orgName"):
            lines.append(f"组织：{auth.get('orgName')}")
        if auth.get("authMethod"):
            lines.append(f"认证方式：{auth.get('authMethod')}")
        if auth.get("apiProvider"):
            lines.append(f"API Provider：{auth.get('apiProvider')}")
        if auth.get("apiKeySource"):
            lines.append(f"API Key 来源：{auth.get('apiKeySource')}")
        if auth.get("error"):
            lines.append(f"登录状态读取失败：{auth.get('error')}")
    subscription = (usage or {}).get("subscription") or {}
    if subscription.get("text") and not auth:
        lines.append(f"订阅状态：{subscription.get('text')}")
    if subscription.get("error"):
        lines.append(f"订阅状态读取失败：{subscription.get('error')}")
    stats = (usage or {}).get("stats") or {}
    if not stats.get("exists"):
        lines.append(f"本地模型统计：无缓存 ({stats.get('path') or '-'})")
        return
    if stats.get("error"):
        lines.append(f"本地模型统计读取失败：{stats.get('error')}")
        return
    if stats.get("lastComputedDate"):
        lines.append(f"本地模型统计日期：{stats.get('lastComputedDate')}")
    if stats.get("totalSessions") is not None:
        lines.append(f"sessions: {stats.get('totalSessions')}")
    if stats.get("totalMessages") is not None:
        lines.append(f"messages: {stats.get('totalMessages')}")
    model_usage = stats.get("modelUsage") or {}
    if not model_usage:
        lines.append("模型用量：无数据")
        return
    lines.append("模型用量：")
    for model, item in sorted(model_usage.items()):
        item = item or {}
        lines.append(
            f"- {model}: "
            f"input={item.get('inputTokens', 0)} "
            f"output={item.get('outputTokens', 0)} "
            f"cacheRead={item.get('cacheReadInputTokens', 0)} "
            f"cacheCreate={item.get('cacheCreationInputTokens', 0)} "
            f"webSearch={item.get('webSearchRequests', 0)} "
            f"cost=${item.get('costUSD', 0)}"
        )
