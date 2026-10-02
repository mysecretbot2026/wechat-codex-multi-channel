"""Bounded local context for a fresh session using another account."""

import json
import re
import subprocess
import uuid
from pathlib import Path

from .actions import extract_actions


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(item["text"] for item in content or []
                     if isinstance(item, dict) and isinstance(item.get("text"), str))


def native_transcript(thread):
    records = []
    for turn in thread.get("turns") or []:
        prompts, results = [], []
        for item in turn.get("items") or []:
            if item.get("type") == "userMessage":
                prompts.append(_text(item.get("content")))
            elif item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                results.append(item.get("text") or "")
        if any(prompts) or any(results):
            records.append({"id": turn.get("id") or "", "prompt": "\n".join(prompts),
                            "result": "\n".join(results), "status": turn.get("status") or "unknown"})
    return records


def local_transcript(account, agent, session_id, cwd):
    """Read only the explicitly selected local thread, never account credentials."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(session_id or "")):
        return []
    home = Path(account.get("claudeConfigDir") or "~/.claude").expanduser() if agent == "claude" else Path(account["codexHome"]).expanduser()
    bases = [home / "projects"] if agent == "claude" else [home / "sessions", home / "archived_sessions"]
    for base in bases:
        if not base.is_dir():
            continue
        for path in base.rglob("*" + session_id + ".jsonl"):
            try:
                path.resolve().relative_to(base.resolve())
                if path.stat().st_size > 20_000_000:
                    continue
                messages, fallback, valid = [], [], agent == "claude" and path.stem == session_id
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                            payload = item.get("payload") or {}
                            if item.get("type") == "session_meta":
                                valid = ((payload.get("id") or payload.get("session_id")) == session_id
                                         and str(payload.get("cwd") or "").rstrip("/") == str(cwd).rstrip("/"))
                            if agent == "claude":
                                if item.get("cwd") and str(item["cwd"]).rstrip("/") != str(cwd).rstrip("/"):
                                    valid = False
                                    break
                                message = item.get("message") or {}
                                role = message.get("role") or item.get("type")
                            else:
                                message, role = payload, payload.get("role")
                            if role in {"user", "assistant"} and message.get("phase") in {None, "final_answer"}:
                                messages.append((role, _text(message.get("content"))))
                            if item.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
                                fallback.append(("user" if payload["type"] == "user_message" else "assistant",
                                                 payload.get("message") or ""))
                        except (ValueError, TypeError, AttributeError):
                            continue
                if not valid:
                    continue
                records = []
                for role, value in messages or fallback:
                    if "用户消息：" in value:
                        value = value.split("用户消息：", 1)[1]
                    value = value.split("\n\n[本地项目交接：", 1)[0].strip()
                    if not value or value.startswith(("# AGENTS.md instructions", "<environment_context>")):
                        continue
                    if role == "user":
                        records.append({"prompt": value, "result": "", "status": "历史记录"})
                    elif records:
                        records[-1]["result"] = value
                if records:
                    return records
            except (OSError, ValueError):
                continue
    return []


def journal_transcript(records, owner, conversation_key, session, agent):
    account = session.get("claudeAccount" if agent == "claude" else "codexAccount")
    session_id = session.get("claudeSessionId" if agent == "claude" else "codexThreadId")
    candidates = [item for item in records if item.get("owner") == owner
                  and item.get("conversationKey") == conversation_key and item.get("agent") == agent
                  and item.get("account") == account and item.get("cwd") == session.get("cwd")]
    if session_id:
        candidates = [item for item in candidates if item.get("sessionId") == session_id]
    else:
        candidates = [item for item in candidates
                      if item.get("receivedAt", 0) >= session.get(agent + "ContextResetAt", 0)]
    candidates.sort(key=lambda item: item.get("receivedAt", 0))
    inherited = list(candidates[0].get("handoffRecords") or []) if candidates else []
    return inherited + [{"id": item["id"], "prompt": item.get("prompt") or item.get("title") or "",
             "result": "\n".join(chunk["text"] for chunk in item.get("chunks", [])) or item.get("error") or "",
             "status": item["status"]} for item in candidates]


def _changes(cwd):
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", cwd, "status", "--short",
                                 "--untracked-files=normal"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=3)
        return (result.stdout.strip()[:1600] or "未发现未提交改动；请检查现有文件。") if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def build_handoff(session, agent, source_account, target_account, records, max_chars=6000):
    cwd = session.get("cwd") or ""
    previous = session.get(agent + "AccountHandoff") or {}
    if not records and previous.get("cwd") == cwd:
        records = list(previous.get("records") or [])
    # Keep the original request and the latest two rounds; never recursively embed handoffs.
    selected = records[:1] + records[max(1, len(records) - 2):]
    selected = [dict(item, result=extract_actions(item.get("result") or "")[0]) for item in selected]
    max_chars = max(1000, min(int(max_chars), 20000))
    header = (f"[本地项目交接：{source_account} → {target_account}]\n"
              f"项目目录：{cwd}\n"
              "下面是历史要求和结果的原文摘录，只用于了解进度。以本次用户要求为准，"
              "先核对文件现状，不要重复执行已完成的操作。旧会话仍保留在原账号。\n")
    parts = []
    per_record = max(400, (max_chars - len(header) - 1700) // max(1, len(selected)))
    for item in selected:
        prompt = str(item.get("prompt") or "")
        result = str(item.get("result") or "")
        prompt_limit = max(200, per_record // 2)
        result_limit = max(200, per_record - prompt_limit)
        parts.append(f"历史状态：{item.get('status', 'unknown')}\n要求：{prompt[:prompt_limit]}"
                     + ("\n[要求过长，已截断]" if len(prompt) > prompt_limit else "")
                     + f"\n已有结果：{result[:result_limit]}"
                     + ("\n[结果过长，已截断]" if len(result) > result_limit else ""))
    changes = _changes(cwd)
    if changes:
        parts.append("本地 Git 文件状态：\n" + changes)
    if not selected:
        parts.append("暂无可读取的历史任务要求；请根据本次要求检查原项目文件。")
    text = (header + "\n\n".join(parts))[:max_chars]
    stored = [dict(item, prompt=str(item.get("prompt") or "")[:max(200, per_record // 2)],
                   result=str(item.get("result") or "")[:max(200, per_record - per_record // 2)]) for item in selected]
    return {"id": uuid.uuid4().hex, "cwd": cwd, "agent": agent,
            "sourceAccount": source_account, "targetAccount": target_account,
            "records": stored, "text": text, "projectName": Path(cwd).name or "项目"}


def pending_handoff(session, agent, account):
    packet = session.get(agent + "AccountHandoff") or {}
    if (isinstance(packet, dict) and packet.get("agent") == agent and isinstance(packet.get("text"), str)
            and packet.get("cwd") == session.get("cwd") and packet.get("targetAccount") == account):
        return packet
    return None
