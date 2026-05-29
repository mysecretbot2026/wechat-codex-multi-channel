import json
import os
import errno
import fcntl
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ANTHROPIC_ADMIN_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_ADMIN_VERSION = "2023-06-01"
DEFAULT_ADMIN_KEYCHAIN_SERVICE = "wechat-codex-multi.anthropic-admin-key"
ADMIN_KEY_ENV_NAMES = ("ANTHROPIC_ADMIN_KEY", "ANTHROPICADMINKEY", "ANTHROPIC_ADMIN_API_KEY")


def read_claude_usage(
    claude_bin="claude",
    timeout_s=30,
    claude_config_dir="",
    permission_mode="",
    cwd="",
    include_admin_usage=False,
    admin_usage_days=7,
    admin_usage_timeout_s=60,
    admin_keychain_service=DEFAULT_ADMIN_KEYCHAIN_SERVICE,
):
    usage = {
        "interactive": read_claude_usage_interactive(
            claude_bin=claude_bin,
            timeout_s=timeout_s,
            claude_config_dir=claude_config_dir,
            permission_mode=permission_mode,
            cwd=cwd,
        )
    }
    if include_admin_usage:
        usage["adminApi"] = read_claude_admin_usage(
            days=admin_usage_days,
            timeout_s=admin_usage_timeout_s,
            keychain_service=admin_keychain_service,
        )
    return usage


def read_claude_usage_interactive(
    claude_bin="claude",
    timeout_s=30,
    claude_config_dir="",
    permission_mode="",
    cwd="",
    stable_s=2.5,
    post_trust_delay_s=2.0,
    rows=40,
    cols=120,
):
    if os.name != "posix":
        return {"source": "interactive-pty", "error": "当前交互式 Claude 用量查询仅支持 macOS/Linux PTY。"}

    binary = shutil.which(claude_bin) or claude_bin
    env = _claude_env(claude_config_dir)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    # Keep the TUI launch minimal for compatibility; /usage does not need permission flags.
    args = [binary]

    master_fd = None
    slave_fd = None
    process = None
    renderer = _TerminalRenderer(cols=cols, rows=rows)
    start = time.monotonic()
    last_output_at = start
    usage_sent_at = None
    usage_sent_count = 0
    trust_sent = False
    trust_sent_at = None
    timed_out = False

    try:
        master_fd, slave_fd = pty.openpty()
        _set_pty_size(slave_fd, rows, cols)
        _set_nonblocking(master_fd)
        process = subprocess.Popen(
            args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=_resolve_cwd(cwd),
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = None

        while True:
            now = time.monotonic()
            if now - start >= timeout_s:
                timed_out = True
                break

            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master_fd, 8192)
                except OSError as err:
                    if err.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                renderer.feed(chunk)
                last_output_at = now
                screen_text = renderer.text()
                if not trust_sent and _is_trust_prompt(screen_text):
                    _write_pty(master_fd, "1\r")
                    trust_sent = True
                    trust_sent_at = now
                    renderer = _TerminalRenderer(cols=cols, rows=rows)
                    continue

            if usage_sent_at is None:
                screen_text = renderer.text()
                if trust_sent_at is not None and now - trust_sent_at < post_trust_delay_s:
                    continue
                if _is_trust_prompt(screen_text) and now - (trust_sent_at or start) < 8:
                    continue
                if now - start >= 1.5 or _looks_like_claude_ready(screen_text):
                    _write_pty(master_fd, "/usage\r")
                    usage_sent_at = now
                    usage_sent_count += 1
                    last_output_at = now
                    continue

            if usage_sent_at is not None and now - usage_sent_at >= max(0.5, min(2.0, stable_s)) and now - last_output_at >= stable_s:
                if not _looks_like_usage_result(renderer.text()) and usage_sent_count < 2:
                    _write_pty(master_fd, "/usage\r")
                    usage_sent_at = now
                    usage_sent_count += 1
                    last_output_at = now
                    continue
                break

            if process.poll() is not None and now - last_output_at >= 0.5:
                break

        screen_text = renderer.text()
        raw_text = renderer.raw_text()
        text = _extract_usage_panel(screen_text, raw_text)
        result = {
            "source": "interactive-pty",
            "text": text,
            "screenText": screen_text,
            "durationSeconds": round(time.monotonic() - start, 2),
            "timedOut": timed_out,
        }
        if timed_out:
            result["error"] = f"Claude /usage 交互式查询超时（{timeout_s} 秒）"
        elif _is_trust_prompt(text):
            result["text"] = ""
            result["error"] = "Claude TUI 仍停留在 trust folder 提示，未进入 /usage 输出。"
        elif not text:
            result["error"] = "未能从 Claude TUI 提取 /usage 输出。"
        return result
    except FileNotFoundError:
        return {"source": "interactive-pty", "error": f"找不到 Claude CLI: {claude_bin}"}
    except Exception as err:
        return {"source": "interactive-pty", "error": str(err)}
    finally:
        if process is not None:
            _kill_process_tree(process)
        for fd in (slave_fd, master_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


class _TerminalRenderer:
    def __init__(self, cols=120, rows=40):
        self._raw_parts = []
        self._screen = None
        self._stream = None
        try:
            import pyte

            self._screen = pyte.Screen(cols, rows)
            self._stream = pyte.Stream(self._screen)
        except Exception:
            self._screen = None
            self._stream = None

    def feed(self, chunk):
        text = chunk.decode("utf-8", errors="replace")
        self._raw_parts.append(text)
        if len(self._raw_parts) > 200:
            self._raw_parts = self._raw_parts[-200:]
        if self._stream is not None:
            self._stream.feed(text)

    def text(self):
        if self._screen is not None:
            return "\n".join(self._screen.display)
        return _strip_ansi(self.raw_text())[-20000:]

    def raw_text(self):
        return "".join(self._raw_parts)


def _set_pty_size(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except Exception:
        pass


def _set_nonblocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _write_pty(fd, text):
    try:
        os.write(fd, text.encode("utf-8"))
    except OSError:
        pass


def _resolve_cwd(cwd):
    if not cwd:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(str(cwd)))).resolve()
    if not path.exists():
        raise RuntimeError(f"工作目录不存在: {path}")
    if not path.is_dir():
        raise RuntimeError(f"不是目录: {path}")
    return str(path)


def _looks_like_claude_ready(text):
    lowered = str(text or "").lower()
    return "claude" in lowered and ("?" in lowered or ">" in lowered or "welcome" in lowered)


def _is_trust_prompt(text):
    lowered = str(text or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    return (
        "trust this folder" in lowered
        or "trust this project" in lowered
        or "quick safety check" in lowered
        or "trustthisfolder" in compact
        or "trustthisproject" in compact
        or "quicksafetycheck" in compact
    )


def _looks_like_usage_result(text):
    if _is_trust_prompt(text):
        return False
    lowered = str(text or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    markers = ("usage", "limit", "tokens", "cost", "resets", "用量", "token")
    return any(marker in lowered or marker in compact for marker in markers)


def _kill_process_tree(process, grace_s=1.0):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass
    try:
        process.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=grace_s)
    except Exception:
        pass


_ANSI_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _strip_ansi(text):
    return _ANSI_PATTERN.sub("", str(text or ""))


def _extract_usage_panel(screen_text, raw_text=""):
    text = _clean_terminal_text(screen_text)
    if not text:
        text = _clean_terminal_text(raw_text)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    lines = [line for line in lines if not _is_usage_command_echo(line)]
    if not lines:
        return ""
    first_usage = None
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "usage" in lowered or "用量" in lowered or "tokens" in lowered or "cost" in lowered:
            first_usage = index
            break
    if first_usage is not None:
        lines = lines[max(0, first_usage - 2):]
    return "\n".join(lines).strip()


def _is_usage_command_echo(line):
    return bool(re.match(r"^\s*[>$#❯›]*\s*/usage\s*$", str(line or "").strip()))


def _clean_terminal_text(text):
    value = _strip_ansi(text).replace("\r", "\n")
    cleaned = []
    for char in value:
        if char == "\n" or char == "\t" or ord(char) >= 32:
            cleaned.append(char)
    return "\n".join(line.rstrip() for line in "".join(cleaned).splitlines()).strip()


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


def _has_claude_admin_key(keychain_service=DEFAULT_ADMIN_KEYCHAIN_SERVICE):
    _key, source = resolve_claude_admin_key("", keychain_service=keychain_service)
    return bool(source)


def resolve_claude_admin_key(api_key="", keychain_service=DEFAULT_ADMIN_KEYCHAIN_SERVICE):
    value = str(api_key or "").strip()
    if value:
        return value, "argument"
    for name in ADMIN_KEY_ENV_NAMES:
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value, f"env:{name}"
    value = _read_macos_keychain_secret(keychain_service)
    if value:
        return value, f"keychain:{keychain_service}"
    return "", ""


def _read_macos_keychain_secret(service):
    if sys.platform != "darwin":
        return ""
    service = str(service or "").strip()
    if not service:
        return ""
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ.get("USER") or "", "-s", service, "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def read_claude_admin_usage(
    api_key="",
    days=7,
    timeout_s=60,
    keychain_service=DEFAULT_ADMIN_KEYCHAIN_SERVICE,
    now=None,
):
    key, source = resolve_claude_admin_key(api_key, keychain_service=keychain_service)
    if not key:
        return {
            "configured": False,
            "error": "未配置 Anthropic Admin Key。请设置 ANTHROPIC_ADMIN_KEY，或在 macOS Keychain 保存。",
        }
    requested_days = int(days or 7)
    usage_days = max(1, min(requested_days, 31))
    now = now or datetime.now(timezone.utc)
    end_date = (now.date() + timedelta(days=1))
    start_date = end_date - timedelta(days=usage_days)
    starting_at = f"{start_date.isoformat()}T00:00:00Z"
    ending_at = f"{end_date.isoformat()}T00:00:00Z"

    result = {
        "configured": True,
        "source": source,
        "days": usage_days,
        "requestedDays": requested_days,
        "startingAt": starting_at,
        "endingAt": ending_at,
    }
    try:
        result["organization"] = _anthropic_admin_get("/organizations/me", key, timeout_s=timeout_s)
    except Exception as err:
        result["organizationError"] = str(err)

    usage_params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": "1d",
        "group_by[]": ["model"],
        "limit": usage_days,
    }
    try:
        usage = _anthropic_admin_get_paginated(
            "/organizations/usage_report/messages",
            key,
            usage_params,
            timeout_s=timeout_s,
        )
        result["usage"] = usage
        result["summary"] = summarize_claude_admin_usage(usage)
    except Exception as err:
        result["error"] = str(err)
        return result

    try:
        cost = _anthropic_admin_get_paginated(
            "/organizations/cost_report",
            key,
            {
                "starting_at": starting_at,
                "ending_at": ending_at,
                "bucket_width": "1d",
                "limit": usage_days,
            },
            timeout_s=timeout_s,
        )
        result["cost"] = cost
        result["costSummary"] = summarize_claude_admin_cost(cost)
    except Exception as err:
        result["costError"] = str(err)
    return result


def _anthropic_admin_get_paginated(path, api_key, params=None, timeout_s=60):
    params = dict(params or {})
    merged = {"data": [], "has_more": False, "next_page": None}
    while True:
        page = _anthropic_admin_get(path, api_key, params, timeout_s=timeout_s)
        merged["data"].extend(page.get("data") or [])
        if not page.get("has_more"):
            merged["has_more"] = False
            merged["next_page"] = None
            return merged
        next_page = page.get("next_page")
        if not next_page:
            merged["has_more"] = True
            merged["next_page"] = next_page
            return merged
        params["page"] = next_page


def _anthropic_admin_get(path, api_key, params=None, timeout_s=60):
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{ANTHROPIC_ADMIN_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_ADMIN_VERSION,
            "content-type": "application/json",
            "User-Agent": "wechat-codex-multi-channel/0.1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic Admin API HTTP {err.code}: {body}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Anthropic Admin API 请求失败: {err}") from err


def summarize_claude_admin_usage(usage):
    totals = {
        "uncachedInputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreation5mInputTokens": 0,
        "cacheCreation1hInputTokens": 0,
        "outputTokens": 0,
        "webSearchRequests": 0,
        "models": {},
    }
    for bucket in (usage or {}).get("data") or []:
        for item in bucket.get("results") or []:
            model = item.get("model") or "unknown"
            model_totals = totals["models"].setdefault(
                model,
                {
                    "uncachedInputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreation5mInputTokens": 0,
                    "cacheCreation1hInputTokens": 0,
                    "outputTokens": 0,
                    "webSearchRequests": 0,
                },
            )
            uncached = int(item.get("uncached_input_tokens") or 0)
            cache_read = int(item.get("cache_read_input_tokens") or 0)
            output = int(item.get("output_tokens") or 0)
            cache_creation = item.get("cache_creation") or {}
            cache_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
            cache_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
            web_search = int((item.get("server_tool_use") or {}).get("web_search_requests") or 0)
            for target in (totals, model_totals):
                target["uncachedInputTokens"] += uncached
                target["cacheReadInputTokens"] += cache_read
                target["cacheCreation5mInputTokens"] += cache_5m
                target["cacheCreation1hInputTokens"] += cache_1h
                target["outputTokens"] += output
                target["webSearchRequests"] += web_search
    totals["inputTokens"] = (
        totals["uncachedInputTokens"]
        + totals["cacheReadInputTokens"]
        + totals["cacheCreation5mInputTokens"]
        + totals["cacheCreation1hInputTokens"]
    )
    totals["totalTokens"] = totals["inputTokens"] + totals["outputTokens"]
    for model_totals in totals["models"].values():
        model_totals["inputTokens"] = (
            model_totals["uncachedInputTokens"]
            + model_totals["cacheReadInputTokens"]
            + model_totals["cacheCreation5mInputTokens"]
            + model_totals["cacheCreation1hInputTokens"]
        )
        model_totals["totalTokens"] = model_totals["inputTokens"] + model_totals["outputTokens"]
    return totals


def summarize_claude_admin_cost(cost):
    summary = {"currency": "USD", "amountMinor": Decimal("0"), "amount": Decimal("0")}
    for bucket in (cost or {}).get("data") or []:
        for item in bucket.get("results") or []:
            try:
                amount_minor = Decimal(str(item.get("amount") or "0"))
            except (InvalidOperation, ValueError):
                continue
            summary["currency"] = item.get("currency") or summary["currency"]
            summary["amountMinor"] += amount_minor
    summary["amount"] = summary["amountMinor"] / Decimal("100")
    return summary


def format_claude_admin_usage(admin_usage):
    lines = ["Claude Admin API 用量："]
    _append_claude_admin_usage_lines(lines, admin_usage or {})
    return "\n".join(lines)


def _format_count(value):
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _append_claude_admin_usage_lines(lines, admin_usage):
    if not admin_usage:
        return
    if not admin_usage.get("configured"):
        if admin_usage.get("error"):
            lines.append(f"Admin API：{admin_usage.get('error')}")
        return
    if admin_usage.get("error"):
        lines.append(f"Admin API 读取失败：{admin_usage.get('error')}")
        return
    days = admin_usage.get("days") or 7
    requested_days = admin_usage.get("requestedDays") or days
    range_text = f"{admin_usage.get('startingAt')} ~ {admin_usage.get('endingAt')}"
    title = f"Admin API 最近 {days} 天"
    if requested_days != days:
        title += f"（已按接口上限从 {requested_days} 天裁剪）"
    lines.append(f"{title}:")
    organization = admin_usage.get("organization") or {}
    if organization:
        lines.append(f"组织：{organization.get('name') or organization.get('id')}")
    elif admin_usage.get("organizationError"):
        lines.append(f"组织读取失败：{admin_usage.get('organizationError')}")
    lines.append(f"范围：{range_text}")
    if admin_usage.get("source"):
        lines.append(f"Key 来源：{admin_usage.get('source')}")
    summary = admin_usage.get("summary") or {}
    if not summary:
        lines.append("Admin API 用量：无数据")
        return
    lines.append(
        "tokens: "
        f"input={_format_count(summary.get('inputTokens'))} "
        f"output={_format_count(summary.get('outputTokens'))} "
        f"total={_format_count(summary.get('totalTokens'))}"
    )
    lines.append(
        "cache: "
        f"read={_format_count(summary.get('cacheReadInputTokens'))} "
        f"create5m={_format_count(summary.get('cacheCreation5mInputTokens'))} "
        f"create1h={_format_count(summary.get('cacheCreation1hInputTokens'))}"
    )
    if summary.get("webSearchRequests"):
        lines.append(f"webSearchRequests: {summary.get('webSearchRequests')}")
    cost_summary = admin_usage.get("costSummary") or {}
    if cost_summary.get("amount") is not None:
        amount = cost_summary.get("amount")
        lines.append(f"cost: ${amount:.4f} {cost_summary.get('currency') or 'USD'}")
    elif admin_usage.get("costError"):
        lines.append(f"cost 读取失败：{admin_usage.get('costError')}")
    models = summary.get("models") or {}
    if models:
        lines.append("按模型：")
        for model, item in sorted(models.items(), key=lambda pair: pair[1].get("totalTokens", 0), reverse=True):
            lines.append(
                f"- {model}: "
                f"input={_format_count(item.get('inputTokens'))} "
                f"output={_format_count(item.get('outputTokens'))} "
                f"total={_format_count(item.get('totalTokens'))}"
            )


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
    interactive = (usage or {}).get("interactive") or {}
    if interactive.get("error"):
        lines.append(f"交互式 /usage 读取失败：{interactive.get('error')}")
        text = str(interactive.get("text") or "").strip()
        if text:
            lines.append(text)
    else:
        if interactive.get("durationSeconds") is not None:
            lines.append(f"查询方式：Claude TUI /usage（{interactive.get('durationSeconds')} 秒）")
        text = _beautify_claude_usage_text(interactive.get("text") or "")
        if text:
            lines.append(text)
        else:
            lines.append("交互式 /usage：无可显示内容")
    _append_claude_admin_usage_lines(lines, (usage or {}).get("adminApi") or {})


def _beautify_claude_usage_text(text):
    lines = []
    for raw_line in _clean_terminal_text(text).splitlines():
        beautified = _beautify_claude_usage_line(raw_line)
        if not beautified:
            continue
        starts_block = bool(_claude_usage_section_title(raw_line)) or beautified[0] in {
            "What's contributing to your limits usage?",
            "Last 24h",
            "Skills, subagents, plugins, and MCP servers",
            "Usage credits",
        }
        if starts_block and lines and lines[-1] != "":
            lines.append("")
        lines.extend(beautified)
    return "\n".join(lines).strip()


def _beautify_claude_usage_line(line):
    value = str(line or "").strip()
    if not value:
        return []
    compact = re.sub(r"\s+", "", value)
    lowered = compact.lower()
    if set(compact) <= {"─", "━", "═", "-"}:
        return []
    if lowered in {
        "statusconfigusagestats",
        "settingsstatusconfigusagestats",
        "refreshing…",
        "refreshing...",
        "esctocancel",
        "scanninglocalsessions…",
        "scanninglocalsessions...",
        "dtoday·wtoweek",
    }:
        return []
    if re.fullmatch(r"[█░▓▒]+[0-9]*", compact):
        return []
    section_title = _claude_usage_section_title(value)
    if section_title:
        return [section_title]
    if lowered == "session":
        return ["Session"]
    if lowered == "currentsession":
        return ["Current session"]
    if lowered.startswith("totalcost:"):
        return [f"  Total cost: {compact[len('Totalcost:'):] or '-'}"]
    if lowered.startswith("apiduration:"):
        return [f"  API duration: {compact[len('APIduration:'):] or '-'}"]
    if lowered.startswith("totalduration(api):"):
        return [f"  API duration: {compact[len('Totalduration(API):'):] or '-'}"]
    if lowered.startswith("wallduration:"):
        return [f"  Wall duration: {compact[len('Wallduration:'):] or '-'}"]
    if lowered.startswith("totalduration(wall):"):
        return [f"  Wall duration: {compact[len('Totalduration(wall):'):] or '-'}"]
    if lowered.startswith("codechanges:"):
        return [f"  Code changes: {value.split(':', 1)[1].strip() or '-'}"]
    if lowered.startswith("tokens:"):
        return [f"  Tokens: {value.split(':', 1)[1].strip() or '-'}"]
    match = re.match(r"^Totalcodechanges:(.+)linesadded,(.+)linesremoved$", compact, flags=re.IGNORECASE)
    if match:
        return [f"  Code changes: +{match.group(1)} / -{match.group(2)} lines"]
    match = re.match(r"^Usage:(.+)input,(.+)output,(.+)cacheread,(.+)cachewrite$", compact, flags=re.IGNORECASE)
    if match:
        return [
            "  Tokens: "
            f"input {match.group(1)}, "
            f"output {match.group(2)}, "
            f"cache read {match.group(3)}, "
            f"cache write {match.group(4)}"
        ]
    percent = _claude_usage_percent(value)
    if percent is not None:
        return [f"  {_claude_usage_bar(percent)} {_format_percent(percent)}% used"]
    reset_at = _claude_usage_reset_text(value)
    if reset_at is not None:
        return [f"  Resets {reset_at or '-'}"]
    if lowered.startswith("what'scontributingtoyourlimitsusage?"):
        return ["What's contributing to your limits usage?"]
    if lowered.startswith("approximate,basedonlocalsessionsonthismachine"):
        return ["  Approximate, based on local sessions on this machine"]
    if lowered.startswith("last24h·theseareindependentcharacteristicsofyourusage,notabreakdown"):
        return ["Last 24h", "  These are independent characteristics of your usage, not a breakdown"]
    if "last24h" in lowered and "nothingover10%" in lowered:
        return ["Last 24h", "  Nothing over 10% in this period"]
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)%ofyourusagewasat>(.+)context$", compact, flags=re.IGNORECASE)
    if match:
        return [f"  {match.group(1)}% of your usage was at >{match.group(2)} context"]
    if lowered.startswith("longersessionsaremoreexpensive"):
        return [f"  {_restore_claude_usage_sentence(value)}"]
    if lowered.startswith("whenswithingtonewtasks.") or lowered.startswith("whenswitchingtonewtasks."):
        return [f"  {_restore_claude_usage_sentence(value)}"]
    if lowered == "skills,subagents,plugins,andmcpservers":
        return ["Skills, subagents, plugins, and MCP servers"]
    if lowered.startswith("noattributiondatayet·accumulatesasyouuseclaude"):
        return ["  No attribution data yet · accumulates as you use Claude"]
    if lowered == "usagecredits":
        return ["Usage credits"]
    if lowered.startswith("usagecreditsareoff·/usage-creditstoturnthemon"):
        return ["  Usage credits are off · /usage-credits to turn them on"]
    if lowered.startswith("extrausageextrausagenotenabled"):
        return ["Extra usage", "  Not enabled. Use /extra-usage in Claude TUI to enable."]
    return [value]


def _claude_usage_section_title(line):
    compact = re.sub(r"\s+", "", str(line or "")).lower()
    if compact == "currentsession":
        return "Current session"
    if compact in {"currentweek", "currentweek(allmodels)"}:
        return "Current week (all models)"
    if compact == "currentweek(sonnetonly)":
        return "Current week (Sonnet only)"
    return ""


def _claude_usage_percent(line):
    compact = re.sub(r"\s+", "", str(line or ""))
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)%used", compact, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return max(0.0, min(100.0, float(match.group(1))))
    except ValueError:
        return None


def _claude_usage_reset_text(line):
    value = str(line or "").strip()
    match = re.match(r"^Resets\s*(.+)?$", value, flags=re.IGNORECASE)
    if not match:
        return None
    reset_at = (match.group(1) or "").strip()
    if not reset_at:
        return ""
    reset_at = re.sub(r"(?<!\s)\(", " (", reset_at)
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    reset_at = re.sub(rf"\b({months})(\d)", r"\1 \2", reset_at, flags=re.IGNORECASE)
    reset_at = re.sub(r",(?=\S)", ", ", reset_at)
    return re.sub(r"\s+", " ", reset_at).strip()


def _claude_usage_bar(percent, width=28):
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round((pct / 100.0) * width))
    if pct > 0 and filled == 0:
        filled = 1
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _format_percent(percent):
    value = float(percent)
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _restore_claude_usage_sentence(line):
    text = str(line or "").strip()
    replacements = {
        "yourusage": "your usage",
        "swithing": "switching",
        "whenswitching": "when switching",
        "switchingto": "switching to",
        "newtasks": "new tasks",
        "mid-task": "mid-task",
    }
    for old, new in replacements.items():
        text = re.sub(old, new, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
