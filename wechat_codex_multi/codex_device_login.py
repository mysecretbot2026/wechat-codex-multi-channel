"""Run Codex's device-code login without sending the request through an agent."""

import base64
import binascii
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DEVICE_URL = re.compile(r"https://auth\.openai\.com/[^\s]+")
_DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6})+\b")
_LOGIN_TIMEOUT_SECONDS = 16 * 60


def cached_email(codex_home):
    """Return the account email from Codex's local ID token, if available."""
    try:
        auth = json.loads((Path(codex_home) / "auth.json").read_text(encoding="utf-8"))
        token = (auth.get("tokens") or {}).get("id_token") or ""
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return str(claims.get("email") or "").strip()
    except (OSError, ValueError, IndexError, TypeError, binascii.Error):
        return ""


def login_status(codex_bin, codex_home, timeout=10):
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    binary = shutil.which(codex_bin) or codex_bin
    try:
        result = subprocess.run(
            [binary, "login", "status"], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


class CodexDeviceLoginManager:
    def __init__(self, codex_bin="codex", timeout_seconds=_LOGIN_TIMEOUT_SECONDS):
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._processes = {}

    def is_running(self, codex_home):
        with self._lock:
            process = self._processes.get(str(codex_home))
            return bool(process and process.poll() is None)

    def start(self, codex_home, on_code, on_done, expected_email=""):
        home = str(Path(codex_home).expanduser().resolve())
        env = os.environ.copy()
        env["CODEX_HOME"] = home
        binary = shutil.which(self.codex_bin) or self.codex_bin
        with self._lock:
            if home in self._processes:
                return False
            process = subprocess.Popen(
                [binary, "login", "--device-auth"], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, start_new_session=True,
            )
            self._processes[home] = process
        thread = threading.Thread(
            target=self._watch, args=(home, process, on_code, on_done, expected_email),
            daemon=True,
        )
        thread.start()
        return True

    def _watch(self, home, process, on_code, on_done, expected_email):
        details = {"url": "", "code": ""}
        code_sent = threading.Event()

        def read_output():
            try:
                for raw in process.stdout:
                    line = _ANSI.sub("", raw).strip()
                    if not details["url"]:
                        match = _DEVICE_URL.search(line)
                        if match:
                            details["url"] = match.group(0).rstrip(".,")
                    if not details["code"]:
                        match = _DEVICE_CODE.search(line)
                        if match:
                            details["code"] = match.group(0)
                    if details["url"] and details["code"] and not code_sent.is_set():
                        code_sent.set()
                        on_code(details["url"], details["code"])
            except Exception:
                pass

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        timed_out = False
        try:
            try:
                returncode = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
                returncode = process.wait()
            reader.join(timeout=2)
            if timed_out:
                message = "Codex 设备码已超时，请重新发起登录。"
            elif returncode != 0:
                message = "Codex 登录未完成或已取消。请重新发起登录；详情可查看 CODEX_HOME/log/codex-login.log。"
            else:
                logged_in, _ = login_status(self.codex_bin, home)
                email = cached_email(home)
                if not logged_in:
                    message = "Codex 登录进程已结束，但登录状态检查失败。请检查 CODEX_HOME。"
                elif expected_email and email and email.casefold() != expected_email.casefold():
                    message = f"Codex 已登录，但账号是 {email}，与期望的 {expected_email} 不符。"
                elif expected_email and not email:
                    message = f"Codex 已登录，但无法从本地缓存核对邮箱。请确认官方页面登录的是 {expected_email}。"
                else:
                    identity = f"账号：{email}" if email else "账号邮箱未能从本地缓存读取，请在官方页面核对。"
                    message = f"Codex 登录成功。{identity}\nCODEX_HOME: {home}"
        finally:
            with self._lock:
                if self._processes.get(home) is process:
                    self._processes.pop(home, None)
        on_done(message)

    def cancel(self, codex_home):
        with self._lock:
            process = self._processes.get(str(codex_home))
        if not process or process.poll() is not None:
            return False
        self._terminate(process)
        return True

    def stop(self):
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            self._terminate(process)

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
