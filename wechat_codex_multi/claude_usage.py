import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path


def read_claude_usage(claude_bin="claude", timeout_s=2, claude_config_dir="", permission_mode=""):
    stats = _read_claude_stats_cache(claude_config_dir)
    try:
        subscription = _read_claude_subscription_usage(
            claude_bin=claude_bin,
            timeout_s=timeout_s,
            claude_config_dir=claude_config_dir,
            permission_mode=permission_mode,
        )
    except Exception as err:
        subscription = {"text": "", "error": str(err)}
    return {
        "subscription": subscription,
        "stats": stats,
    }


def _read_claude_subscription_usage(claude_bin="claude", timeout_s=15, claude_config_dir="", permission_mode=""):
    binary = shutil.which(claude_bin) or claude_bin
    env = os.environ.copy()
    if claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(os.path.expandvars(os.path.expanduser(str(claude_config_dir)))).resolve())
    args = [binary, "-p", "--verbose", "--output-format", "stream-json"]
    if permission_mode:
        args.extend(["--permission-mode", str(permission_mode)])
    args.append("/usage")
    process = subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    text = ""
    is_error = False
    stderr_chunks = []
    stdout_buffer = ""
    stderr_buffer = ""
    deadline = time.time() + timeout_s
    timed_out = False

    def handle_stdout_line(line):
        nonlocal text, is_error
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if event.get("type") == "result":
            if event.get("is_error"):
                is_error = True
            result = event.get("result")
            if isinstance(result, str) and result.strip():
                text = result.strip()
            if is_error:
                raise RuntimeError(text or "claude /usage 返回错误")
            return {"text": text or "Claude 没有返回用量信息"}
        if event.get("type") == "assistant" and not text:
            text = _assistant_text(event.get("message") or {})
        return None

    try:
        while time.time() < deadline:
            streams = [s for s in (process.stdout, process.stderr) if s is not None]
            readable, _, _ = select.select(streams, [], [], 0.5)
            for stream in readable:
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    continue
                if stream is process.stderr:
                    stderr_buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in stderr_buffer:
                        line, stderr_buffer = stderr_buffer.split("\n", 1)
                        stderr_chunks.append(line)
                    continue
                stdout_buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in stdout_buffer:
                    line, stdout_buffer = stdout_buffer.split("\n", 1)
                    result = handle_stdout_line(line)
                    if result:
                        return result
            if process.poll() is not None:
                break
        if stdout_buffer.strip():
            result = handle_stdout_line(stdout_buffer.strip())
            if result:
                return result
    finally:
        if process.poll() is None:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
    return_code = process.poll()
    if timed_out:
        raise TimeoutError(f"Claude /usage 未在 {timeout_s} 秒内返回完整结果")
    if is_error:
        raise RuntimeError(text or "".join(stderr_chunks).strip() or "claude /usage 返回错误")
    if return_code not in (0, None):
        raise RuntimeError(text or "".join(stderr_chunks).strip() or f"claude /usage 退出码 {return_code}")
    if text:
        return {"text": text}
    raise TimeoutError("Claude 没有返回用量信息")


def _assistant_text(message):
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts).strip()


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
    subscription = (usage or {}).get("subscription") or {}
    if subscription.get("text"):
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
