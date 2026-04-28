import json
import os
import select
import shutil
import subprocess
import time
from datetime import datetime


def read_codex_usage(codex_bin="codex", timeout_s=15, codex_home=""):
    binary = shutil.which(codex_bin) or codex_bin
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = codex_home
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
    return "\n".join(lines)


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
