"""Browse local Codex projects and manage their threads through App Server."""

import sqlite3
from pathlib import Path

from .session_discovery import clean_title


SOURCE_KINDS = ["cli", "vscode", "exec", "appServer"]


def _is_unmaterialized_thread_error(error):
    message = str(error).lower()
    return ("not materialized yet" in message or "list_turns is not supported yet" in message
            or "no rollout found" in message)


def _connect_readonly(path):
    path = Path(path).expanduser()
    if not path.is_file():
        return None
    uri = path.resolve().as_uri()
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        db = None
        try:
            db = sqlite3.connect(uri + suffix, uri=True, timeout=2)
            db.execute("pragma schema_version").fetchone()
            return db
        except sqlite3.OperationalError:
            if db is not None:
                db.close()
    raise sqlite3.OperationalError(f"unable to open local Codex database: {path}")


def project_for_thread(thread, projects):
    known_ids = {project["id"] for project in projects}
    if thread.get("projectId") in known_ids:
        return thread["projectId"]
    cwd = str(thread.get("cwd") or "").rstrip("/")
    best = None
    best_length = -1
    for project in projects:
        for root in project.get("roots") or []:
            root = str(root).rstrip("/")
            if root and (cwd == root or cwd.startswith(root + "/")) and len(root) > best_length:
                best, best_length = project["id"], len(root)
    return best or "_ungrouped"


class DesktopCodexCatalog:
    def __init__(self, runner):
        self.runner = runner

    def projects(self, account):
        server = self.runner._server_for_account(account)
        cursor = None
        projects = []
        for _ in range(100):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = server.request("project/list", params, timeout_s=30) or {}
            for item in result.get("data") or []:
                projects.append({
                    "id": item["id"],
                    "name": item.get("name") or "未命名项目",
                    "roots": [root["path"] for root in item.get("roots") or [] if root.get("path")],
                })
            cursor = result.get("nextCursor")
            if not cursor:
                return projects
        raise RuntimeError("Codex 项目超过 10000 个，请缩小查询范围")

    def create_project(self, account, name, cwd, idempotency_key):
        result = self.runner.request_for_account(account, "project/create", {
            "name": name,
            "roots": [{"path": cwd}],
            "idempotencyKey": idempotency_key,
        }, timeout_s=30) or {}
        item = result.get("project") or {}
        if not item.get("id"):
            raise RuntimeError("Codex 未返回新项目 ID")
        return {
            "id": item["id"],
            "name": item.get("name") or name,
            "roots": [root["path"] for root in item.get("roots") or [] if root.get("path")],
        }

    @staticmethod
    def _desktop_metadata(home):
        db = _connect_readonly(home / "sqlite" / "codex-dev.db")
        if db is None:
            return {}
        try:
            rows = db.execute(
                "select thread_id, display_title, project_id from local_thread_catalog "
                "where source_kind != 'chatgpt'"
            ).fetchall()
            return {thread_id: {"title": title, "projectId": project_id}
                    for thread_id, title, project_id in rows}
        except sqlite3.Error:
            return {}
        finally:
            db.close()

    @staticmethod
    def _last_turn_statuses(home, thread_ids):
        db = _connect_readonly(home / "thread_history_1.sqlite")
        if db is None:
            return {}
        statuses = {}
        try:
            for thread_id in thread_ids:
                row = db.execute(
                    "select status from thread_turns where thread_id = ? "
                    "order by rollout_ordinal desc limit 1", (thread_id,)
                ).fetchone()
                if row:
                    statuses[thread_id] = row[0]
        except sqlite3.Error:
            return statuses
        finally:
            db.close()
        return statuses

    def threads(self, account, archived=False):
        server = self.runner._server_for_account(account)
        cursor = None
        raw = []
        for _ in range(100):
            params = {
                "limit": 100,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "sourceKinds": SOURCE_KINDS,
                "archived": bool(archived),
                "useStateDbOnly": True,
            }
            if cursor:
                params["cursor"] = cursor
            result = server.request("thread/list", params, timeout_s=30) or {}
            raw.extend(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        else:
            raise RuntimeError("Codex 会话超过 10000 条，请缩小查询范围")
        home = Path(account.get("codexHome") or "~/.codex").expanduser()
        metadata = self._desktop_metadata(home)
        statuses = self._last_turn_statuses(home, [item.get("id") for item in raw if item.get("id")])
        result = []
        for item in raw:
            thread_id = item.get("id")
            if not thread_id:
                continue
            catalog_item = metadata.get(thread_id) or {}
            result.append({
                "id": thread_id,
                "title": clean_title(item.get("name") or catalog_item.get("title") or item.get("preview"), limit=70),
                "cwd": item.get("cwd") or "",
                "projectId": item.get("projectId") or catalog_item.get("projectId") or "",
                "source": item.get("source") or "",
                "createdAt": item.get("createdAt") or 0,
                "updatedAt": item.get("updatedAt") or 0,
                "archived": bool(archived),
                "runtimeStatus": (item.get("status") or {}).get("type") or "unknown",
                "lastTurnStatus": statuses.get(thread_id) or "unknown",
            })
        return result

    def read(self, account, thread_id, include_turns=False):
        result = self.runner.request_for_account(
            account, "thread/read", {"threadId": thread_id, "includeTurns": bool(include_turns)}, timeout_s=40
        ) or {}
        return result.get("thread") or {}

    def latest_result(self, account, thread_id):
        try:
            thread = self.read(account, thread_id, include_turns=True)
        except Exception as error:
            if _is_unmaterialized_thread_error(error):
                return {"status": "unknown", "text": "", "turnId": ""}
            raise
        turns = thread.get("turns") or []
        if not turns:
            return {"status": "unknown", "text": "", "turnId": ""}
        turn = turns[-1]
        final_text = ""
        for item in turn.get("items") or []:
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                value = item.get("text")
                if isinstance(value, str) and value.strip():
                    final_text = value.strip()
        return {
            "status": turn.get("status") or "unknown",
            "text": final_text,
            "turnId": turn.get("id") or "",
        }

    def last_turn_status(self, account, thread_id):
        try:
            result = self.runner.request_for_account(
                account, "thread/turns/list",
                {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"},
                timeout_s=30,
            ) or {}
        except Exception as error:
            if _is_unmaterialized_thread_error(error):
                return "unknown"
            raise
        turns = result.get("data") or []
        return (turns[0].get("status") if turns else None) or "unknown"

    def archive(self, account, thread_id):
        return self.runner.request_for_account(account, "thread/archive", {"threadId": thread_id})

    def unarchive(self, account, thread_id):
        return self.runner.request_for_account(account, "thread/unarchive", {"threadId": thread_id})

    def delete(self, account, thread_id):
        return self.runner.request_for_account(account, "thread/delete", {"threadId": thread_id})
