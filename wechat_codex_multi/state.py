import json
import os
import threading
import time
from pathlib import Path


class StateStore:
    def __init__(self, state_dir, save_debounce_ms=0):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.state_dir / "state.json"
        self.lock = threading.RLock()
        self.save_debounce_ms = int(save_debounce_ms or 0)
        self.save_timer = None
        self.save_pending = False
        self.state = {
            "accounts": [],
            "sessions": {},
            "contextTokens": {},
        }
        self.load()

    def load(self):
        with self.lock:
            if not self.file.exists():
                return self.state
            try:
                loaded = json.loads(self.file.read_text(encoding="utf-8"))
            except Exception:
                return self.state
            self.state["accounts"] = list(loaded.get("accounts") or [])
            self.state["sessions"] = dict(loaded.get("sessions") or {})
            self.state["contextTokens"] = dict(loaded.get("contextTokens") or {})
            return self.state

    def _write_locked(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.file)
        self.save_pending = False

    def save(self, debounce=False):
        with self.lock:
            if not debounce or self.save_debounce_ms <= 0:
                if self.save_timer:
                    self.save_timer.cancel()
                    self.save_timer = None
                self._write_locked()
                return
            self.save_pending = True
            if self.save_timer:
                return
            self.save_timer = threading.Timer(self.save_debounce_ms / 1000, self.flush)
            self.save_timer.daemon = True
            self.save_timer.start()

    def flush(self):
        with self.lock:
            if self.save_timer:
                self.save_timer.cancel()
                self.save_timer = None
            if self.save_pending:
                self._write_locked()

    def list_accounts(self):
        with self.lock:
            return [dict(a) for a in self.state["accounts"]]

    def upsert_account(self, account):
        with self.lock:
            account = dict(account)
            account.setdefault("getUpdatesBuf", "")
            next_accounts = []
            for existing in self.state["accounts"]:
                same_account = existing.get("accountId") == account.get("accountId")
                same_user = account.get("userId") and existing.get("userId") == account.get("userId")
                if not same_account and not same_user:
                    next_accounts.append(existing)
            next_accounts.append(account)
            self.state["accounts"] = next_accounts
            self.save()

    def update_account(self, account_id, **updates):
        with self.lock:
            changed = False
            for account in self.state["accounts"]:
                if account.get("accountId") == account_id:
                    changed = any(account.get(key) != value for key, value in updates.items())
                    if not changed:
                        return False
                    account.update(updates)
                    break
            else:
                return False
            self.save(debounce=True)
            return True

    def conversation_key(self, account_id, user_id):
        return f"{account_id}:{user_id}"

    def get_session(self, conversation_key, default_cwd, default_codex_account=""):
        with self.lock:
            sessions = self.state["sessions"]
            if conversation_key not in sessions:
                sessions[conversation_key] = {
                    "cwd": default_cwd,
                    "codexThreadId": "",
                    "codexAccount": default_codex_account,
                    "lastActive": int(time.time() * 1000),
                }
                self.save(debounce=True)
            return dict(sessions[conversation_key])

    def update_session(self, conversation_key, **updates):
        with self.lock:
            session = self.state["sessions"].setdefault(conversation_key, {})
            changed = any(session.get(key) != value for key, value in updates.items())
            if not changed:
                return False
            session.update(updates)
            session["lastActive"] = int(time.time() * 1000)
            self.save(debounce=True)
            return True

    def reset_session(self, conversation_key):
        with self.lock:
            session = self.state["sessions"].setdefault(conversation_key, {})
            if session.get("codexThreadId") == "":
                return False
            session["codexThreadId"] = ""
            session["lastActive"] = int(time.time() * 1000)
            self.save()
            return True

    def set_context_token(self, account_id, user_id, context_token):
        with self.lock:
            key = self.conversation_key(account_id, user_id)
            if self.state["contextTokens"].get(key) == context_token:
                return False
            self.state["contextTokens"][key] = context_token
            self.save(debounce=True)
            return True

    def get_context_token(self, account_id, user_id):
        with self.lock:
            return self.state["contextTokens"].get(self.conversation_key(account_id, user_id))
