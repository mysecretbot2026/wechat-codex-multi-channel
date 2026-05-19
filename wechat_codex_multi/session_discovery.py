import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


COMMAND_TAG_RE = re.compile(r"<command-[^>]+>.*?</command-[^>]+>", re.DOTALL)
SPACE_RE = re.compile(r"\s+")


def expand_home(path):
    return Path(path or "~").expanduser().resolve()


def clean_title(value, fallback="untitled", limit=90):
    text = str(value or "").strip()
    if "用户消息：" in text:
        text = text.split("用户消息：", 1)[1].strip()
    text = COMMAND_TAG_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip()
    if not text or text == "No prompt":
        text = fallback
    return text[: limit - 1] + "…" if len(text) > limit else text


def short_session_id(session_id, size=12):
    return str(session_id or "")[:size] or "-"


def _parse_iso_epoch(value):
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def format_session_time(epoch):
    try:
        value = int(epoch or 0)
    except Exception:
        value = 0
    if value <= 0:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _codex_rows_from_sqlite(db_path, limit, include_archived=False):
    if not db_path.exists():
        return []
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=1)
    con.row_factory = sqlite3.Row
    try:
        archived_clause = "" if include_archived else "where archived = 0"
        rows = con.execute(
            f"""
            select id, title, cwd, source, created_at, updated_at, archived
            from threads
            {archived_clause}
            order by updated_at desc
            limit ?
            """,
            (int(limit or 50),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _codex_rows_from_index(codex_home, limit):
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return []
    rows = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        rows.append(
            {
                "id": item.get("id") or "",
                "title": item.get("thread_name") or "",
                "cwd": "",
                "source": "index",
                "created_at": 0,
                "updated_at": _parse_iso_epoch(item.get("updated_at")),
                "archived": 0,
            }
        )
    rows.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return rows[: int(limit or 50)]


def list_codex_sessions(codex_account, limit=20, include_archived=False):
    codex_home = expand_home((codex_account or {}).get("codexHome") or "~/.codex")
    rows = []
    try:
        rows = _codex_rows_from_sqlite(codex_home / "state_5.sqlite", limit, include_archived=include_archived)
    except Exception:
        rows = []
    if not rows:
        rows = _codex_rows_from_index(codex_home, limit)
    account_name = (codex_account or {}).get("name") or "main"
    sessions = []
    for row in rows:
        session_id = row.get("id") or ""
        if not session_id:
            continue
        sessions.append(
            {
                "agent": "codex",
                "account": account_name,
                "sessionId": session_id,
                "title": clean_title(row.get("title")),
                "cwd": row.get("cwd") or "",
                "source": row.get("source") or "",
                "createdAt": int(row.get("created_at") or 0),
                "updatedAt": int(row.get("updated_at") or 0),
            }
        )
    return sessions


def _claude_sessions_from_meta(base_dir):
    sessions = {}
    meta_dir = base_dir / "usage-data" / "session-meta"
    if not meta_dir.exists():
        return sessions
    for path in meta_dir.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        session_id = item.get("session_id") or path.stem
        if not session_id:
            continue
        start_epoch = _parse_iso_epoch(item.get("start_time"))
        sessions[session_id] = {
            "sessionId": session_id,
            "title": clean_title(item.get("first_prompt")),
            "cwd": item.get("project_path") or "",
            "createdAt": start_epoch,
            "updatedAt": start_epoch or int(path.stat().st_mtime),
        }
    return sessions


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return ""


def _merge_claude_project_logs(base_dir, sessions):
    projects_dir = base_dir / "projects"
    if not projects_dir.exists():
        return
    for path in projects_dir.rglob("*.jsonl"):
        session_id = path.stem
        entry = sessions.setdefault(
            session_id,
            {
                "sessionId": session_id,
                "title": "",
                "cwd": "",
                "createdAt": 0,
                "updatedAt": int(path.stat().st_mtime),
            },
        )
        first_user = ""
        first_epoch = 0
        last_epoch = 0
        cwd = ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:
                continue
            timestamp = _parse_iso_epoch(item.get("timestamp"))
            if timestamp:
                last_epoch = max(last_epoch, timestamp)
            if item.get("cwd") and not cwd:
                cwd = item.get("cwd") or ""
            if item.get("type") == "user" and not first_user:
                first_user = _content_text((item.get("message") or {}).get("content"))
                first_epoch = timestamp
        if first_user and entry.get("title") in {"", "untitled"}:
            entry["title"] = clean_title(first_user)
        if cwd and not entry.get("cwd"):
            entry["cwd"] = cwd
        if first_epoch and not entry.get("createdAt"):
            entry["createdAt"] = first_epoch
        if last_epoch:
            entry["updatedAt"] = max(int(entry.get("updatedAt") or 0), last_epoch)


def list_claude_sessions(claude_account, limit=20):
    config_dir = (claude_account or {}).get("claudeConfigDir") or "~/.claude"
    base_dir = expand_home(config_dir)
    sessions = _claude_sessions_from_meta(base_dir)
    _merge_claude_project_logs(base_dir, sessions)
    account_name = (claude_account or {}).get("name") or "main"
    result = []
    for item in sessions.values():
        session_id = item.get("sessionId") or ""
        if not session_id:
            continue
        result.append(
            {
                "agent": "claude",
                "account": account_name,
                "sessionId": session_id,
                "title": clean_title(item.get("title")),
                "cwd": item.get("cwd") or "",
                "source": "local",
                "createdAt": int(item.get("createdAt") or 0),
                "updatedAt": int(item.get("updatedAt") or 0),
            }
        )
    result.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
    return result[: int(limit or 20)]


def sort_sessions(sessions):
    return sorted(sessions, key=lambda item: item.get("updatedAt") or 0, reverse=True)
