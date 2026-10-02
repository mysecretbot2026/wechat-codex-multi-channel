"""Register additional Codex accounts without replacing live account identities."""

import contextlib
import copy
import fcntl
import json
import os
import re
import threading
import uuid
from pathlib import Path

from .codex_accounts import expand_path, normalize_codex_accounts


class CodexAccountRegistry:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.file = Path(config["configFile"]).expanduser().resolve() if config.get("configFile") else None
        self.signature = None

    @contextlib.contextmanager
    def _file_lock(self):
        if self.file is None:
            raise ValueError("未配置 configFile，无法自动保存新账号。")
        self.file.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.file.with_name("." + self.file.name + ".accounts.lock")
        with lock_file.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self):
        if not self.file.exists():
            return {"codex": {"accounts": copy.deepcopy(self.config["codex"]["accounts"])}}
        loaded = json.loads(self.file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("codex", {}), dict):
            raise ValueError("账号配置格式无效，未修改配置或当前账号列表。")
        return loaded

    def _additions(self, loaded):
        raw = loaded.get("codex", {}).get("accounts")
        if raw is None:
            return []
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("codex.accounts 必须是账号对象数组。")
        candidate = {"codex": copy.deepcopy(loaded["codex"])}
        normalize_codex_accounts(candidate)
        existing = {item["name"]: item for item in self.config["codex"]["accounts"]}
        homes = {expand_path(item["codexHome"]): item["name"] for item in existing.values()}
        additions = []
        for item in candidate["codex"]["accounts"]:
            name, home = item["name"], item["codexHome"]
            if name in existing:
                if expand_path(existing[name]["codexHome"]) != home:
                    raise ValueError(f"账号 {name} 的目录修改需要重启；当前运行身份保持不变。")
            else:
                if home in homes:
                    raise ValueError(f"账号 {name} 与 {homes[home]} 使用同一目录，请为新账号配置独立目录。")
                homes[home] = name
                additions.append(item)
        return additions

    def _signature(self):
        if not self.file or not self.file.exists():
            return None
        stat = self.file.stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    def refresh(self):
        if not self.file:
            return []
        with self.lock:
            signature = self._signature()
            if signature is None or signature == self.signature:
                return []
            with self._file_lock():
                additions = self._additions(self._read())
                # Shared runner configs retain the same list object.
                self.config["codex"]["accounts"].extend(additions)
                self.signature = self._signature()
                return additions

    def register(self, name):
        value = str(name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) or value.isdigit():
            raise ValueError("新账号名需为 1–64 位字母、数字、下划线、点或短横线，且不能是纯数字或路径。")
        if value.lower() in {"status", "cancel", "next", "prev", "previous", "n", "p"}:
            raise ValueError("新账号名不能使用 status、cancel、next、prev 等命令保留字。")
        with self.lock, self._file_lock():
            loaded = self._read()
            original_signature = self._signature()
            additions = self._additions(loaded)
            known = {item["name"]: item for item in self.config["codex"]["accounts"] + additions}
            if value in known:
                self.config["codex"]["accounts"].extend(additions)
                self.signature = self._signature()
                return dict(known[value]), False
            root = expand_path(self.config["codex"].get("accountsDirectory") or "~/.codex-accounts")
            home = (Path(root) / value).resolve()
            try:
                home.relative_to(Path(root))
            except ValueError as error:
                raise ValueError("新账号目录不能通过符号链接指向账号根目录之外。") from error
            if any(expand_path(item["codexHome"]) == str(home) for item in known.values()):
                raise ValueError("新账号目录已被其他配置账号使用。")
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            account = {"name": value, "codexHome": str(home)}
            codex = loaded.setdefault("codex", {})
            accounts = codex.get("accounts")
            if accounts is None:
                accounts = copy.deepcopy(self.config["codex"]["accounts"])
                codex["accounts"] = accounts
            # Retain live identities removed manually until a service restart.
            for item in self.config["codex"]["accounts"]:
                if not any(raw.get("name") == item["name"] for raw in accounts):
                    accounts.append(dict(item))
            accounts.append(account)
            temporary = self.file.with_name("." + self.file.name + ".tmp-" + uuid.uuid4().hex)
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(loaded, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.file.exists():
                    os.chmod(temporary, self.file.stat().st_mode & 0o777)
                if self._signature() != original_signature:
                    raise RuntimeError("写入期间配置文件发生变化，未覆盖配置，请重试。")
                os.replace(temporary, self.file)
            finally:
                temporary.unlink(missing_ok=True)
            self.config["codex"]["accounts"].extend(additions + [account])
            self.signature = self._signature()
            return dict(account), True
