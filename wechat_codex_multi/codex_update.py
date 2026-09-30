"""Local Codex updates. No model calls or machine-specific skill dependencies."""

import contextlib
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import logging as log
from .native_updater import trigger_builtin_update


DESKTOP_DMG_URL = "https://persistent.oaistatic.com/codex-app-prod/ChatGPT.dmg"
DESKTOP_BUNDLE_ID = "com.openai.codex"
OPENAI_TEAM_ID = "2DC432GLL2"
_UPDATE_CONTEXT = threading.local()
UPDATE_HELP = (
    "本机更新命令（微信中仅 adminUsers 可用，不调用模型，不消耗 token）：\n"
    "/update cli 或 /codex-update 更新已安装的 Codex CLI\n"
    "/update desktop 或 /d-update 调用桌面 App 自带的“检查更新”（无需先退出）\n"
    "/update cli check 或 /update desktop check 预览已安装版本和更新方式（不查询最新版本，不安装）\n"
    "/update status 查看两种程序的最近更新结果及日志\n"
    "/update cli status 或 /update desktop status 查看指定程序的结果\n"
    "桌面内置更新仅支持 macOS：需先打开 App，给运行服务的 Python 或终端授予辅助功能权限，再重启服务。\n"
    "触发检查不代表安装完成；下载、安装和重启按 App 提示操作，之后用 /update status 查询结果。\n"
    "支持应用标识 com.openai.codex 的 Codex.app 或 ChatGPT.app；普通独立 ChatGPT App 暂不支持。\n"
    "特殊安装路径、其他菜单语言及更新命令可通过本机 updates 配置适配。"
)


def parse_update_command(text):
    parts = text.strip().split()
    if not parts or parts[0] not in {"/update", "/codex-update", "/d-update"}:
        return None
    if parts[0] != "/update":
        parts = ["/update", "cli" if parts[0] == "/codex-update" else "desktop"] + parts[1:]
    if len(parts) == 1 or parts[1:] == ["help"]:
        return "", "help"
    if parts[1:] == ["status"]:
        return "", "status"
    if len(parts) in {2, 3} and parts[1] in {"cli", "desktop"}:
        action = parts[2] if len(parts) == 3 else "run"
        if action in {"run", "check", "status"} or (parts[1] == "desktop" and action in {"native", "installer"}):
            return parts[1], action
    raise ValueError(UPDATE_HELP)


def _capture(argv, timeout=20, optional=False):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=timeout)
    if result.returncode and not optional:
        raise RuntimeError(f"命令失败：{shlex.join(argv)}\n{result.stderr.strip()[:1000]}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _argv(value, key):
    if not value:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f"updates.{key} 必须是非空字符串组成的 argv 数组。")
    return [os.path.expandvars(os.path.expanduser(v)) for v in value]


def _bundle_info(path):
    return plistlib.loads((Path(path) / "Contents/Info.plist").read_bytes())


def desktop_app_path(config):
    explicit = (config.get("updates") or {}).get("desktopAppPath")
    if explicit:
        path = Path(os.path.expandvars(os.path.expanduser(explicit))).resolve()
        if _bundle_info(path).get("CFBundleIdentifier") != DESKTOP_BUNDLE_ID:
            raise RuntimeError(f"不是桌面 Codex App（{DESKTOP_BUNDLE_ID}）：{path}")
        return path
    desktop_bin = (config.get("codex") or {}).get("desktopBin") or ""
    if desktop_bin:
        parents = [p for p in Path(desktop_bin).expanduser().resolve().parents if p.suffix == ".app"]
        for path in reversed(parents):
            if _bundle_info(path).get("CFBundleIdentifier") == DESKTOP_BUNDLE_ID:
                return path
    candidates = []
    for root in [Path.home() / "Applications", Path("/Applications")]:
        for name in ["ChatGPT.app", "Codex.app"]:
            path = root / name
            try:
                if _bundle_info(path).get("CFBundleIdentifier") == DESKTOP_BUNDLE_ID:
                    candidates.append(path)
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise RuntimeError("检测到多个桌面 Codex App，请配置 updates.desktopAppPath。")
    raise RuntimeError("未找到已安装的桌面 Codex App，请配置 updates.desktopAppPath。")


@dataclass
class UpdatePlan:
    target: str
    method: str
    path: str
    before: str
    commands: list = field(default_factory=list)
    app_path: str = ""
    version_command: list = field(default_factory=list)

    def describe(self):
        commands = "\n".join(shlex.join(argv) for argv in self.commands)
        if self.method == "dmg":
            commands = f"下载官方安装包：{DESKTOP_DMG_URL}\n验证 OpenAI 签名后替换 App（须先退出桌面应用）"
        elif self.method == "native":
            commands = "调用 App 内置的“检查更新”菜单，无需先退出；macOS 需辅助功能权限。最终安装按 App 提示进行。"
        return (f"{'Codex CLI' if self.target == 'cli' else '桌面 Codex App'}\n"
                f"当前版本：{self.before}\n路径：{self.path}\n更新方式：{self.method}\n{commands}")

    def version(self):
        if self.version_command:
            value = _capture(self.version_command)
            if not value:
                raise RuntimeError("更新后的版本命令没有返回版本，无法确认更新成功。")
            return value
        info = _bundle_info(self.app_path)
        if info.get("CFBundleIdentifier") != DESKTOP_BUNDLE_ID:
            raise RuntimeError("更新后的应用标识与桌面 Codex 不匹配，无法确认更新成功。")
        return f"{info['CFBundleShortVersionString']} ({info['CFBundleVersion']})"


def _desktop_is_running(app_path):
    executable = _bundle_info(app_path).get("CFBundleExecutable")
    if not executable:
        raise RuntimeError("App 缺少 CFBundleExecutable。")
    processes = _capture(["/bin/ps", "-axo", "command="])
    main = str(Path(app_path) / "Contents/MacOS" / executable)
    # Checking the full executable path avoids matching an unrelated ChatGPT app.
    return any(line.strip() == main or line.strip().startswith(main + " ")
               for line in processes.splitlines())


def _desktop_mode(config, override=""):
    mode = str(override or (config.get("updates") or {}).get("desktopMethod") or "native").strip().lower()
    if mode not in {"auto", "native", "installer"}:
        raise ValueError("updates.desktopMethod 必须是 auto、native 或 installer。")
    return mode


def make_update_plan(config, target, desktop_method=""):
    options = config.get("updates") or {}
    if target not in {"cli", "desktop"}:
        raise ValueError("更新目标必须是 cli 或 desktop。")
    if desktop_method and target != "desktop":
        raise ValueError("更新方式仅适用于 desktop。")
    custom = [] if target == "desktop" and desktop_method == "native" else _argv(options.get(f"{target}Command"), f"{target}Command")
    if target == "cli":
        configured = (config.get("codex") or {}).get("bin") or "codex"
        configured = os.path.expandvars(os.path.expanduser(configured))
        found = shutil.which(configured)
        if not found:
            raise RuntimeError(f"未找到 Codex CLI：{configured}。更新命令只更新已安装程序。")
        path = Path(found).absolute()
        real = path.resolve()
        before = _capture([str(path), "--version"])
        if not before:
            raise RuntimeError("Codex CLI 没有返回版本。")
        plan = UpdatePlan(target, "custom", str(path), before, version_command=[str(path), "--version"])
        if custom:
            plan.commands = [custom]
            return plan
        npm = shutil.which("npm")
        if npm:
            root = _capture([npm, "root", "-g"], optional=True)
            package = Path(root) / "@openai/codex" if root else None
            if package and package.resolve() in real.parents:
                plan.method = "npm"
                plan.commands = [[npm, "install", "-g", "@openai/codex@latest"]]
                return plan
        brew = shutil.which("brew")
        if brew:
            for kind, directory in [("--cask", "Caskroom"), ("--formula", "Cellar")]:
                if directory in real.parts and "codex" in real.parts:
                    if _capture([brew, "list", kind, "--versions", "codex"], optional=True):
                        plan.method = f"homebrew {kind}"
                        plan.commands = [[brew, "update"], [brew, "upgrade", kind, "codex"]]
                        return plan
        raise RuntimeError("无法确定当前 Codex CLI 的安装来源；请在本机配置 updates.cliCommand（argv 数组）。不会另装一份 CLI。")

    version_command = _argv(options.get("desktopVersionCommand"), "desktopVersionCommand")
    if sys.platform != "darwin":
        if desktop_method == "native":
            raise RuntimeError("内置更新菜单目前支持 macOS。")
        if not custom or not version_command:
            raise RuntimeError("桌面自动更新目前内置支持 macOS。其他系统请配置 updates.desktopCommand 和 desktopVersionCommand。")
        return UpdatePlan(target, "custom", version_command[0], _capture(version_command),
                          commands=[custom], version_command=version_command)
    app = desktop_app_path(config)
    plan = UpdatePlan(target, "dmg", str(app), "", app_path=str(app), version_command=version_command)
    plan.before = plan.version()
    if custom:
        plan.method, plan.commands = "custom", [custom]
        return plan
    mode = _desktop_mode(config, desktop_method)
    if mode == "native" or (mode == "auto" and _desktop_is_running(app)):
        plan.method = "native"
        return plan
    brew = shutil.which("brew")
    if brew:
        for cask in ["codex-app", "chatgpt"]:
            # Only use Homebrew when its installed cask owns this exact bundle.
            if not _capture([brew, "list", "--cask", "--versions", cask], optional=True):
                continue
            value = _capture([brew, "info", "--json=v2", "--cask", cask], optional=True)
            if not value:
                continue
            data = json.loads(value)
            for item in data.get("casks") or []:
                if not item.get("installed"):
                    continue
                # Historical chatgpt casks distribute a different application.
                if cask == "chatgpt" and "/codex-app-prod/" not in (item.get("url") or ""):
                    continue
                for artifact in item.get("artifacts") or []:
                    for entry in artifact.get("app") or []:
                        if isinstance(entry, str) and Path(entry).name == app.name:
                            appdir = Path(item.get("appdir") or "/Applications")
                            if (appdir / app.name).resolve() == app.resolve():
                                plan.method = "homebrew --cask"
                                plan.commands = [[brew, "update"], [brew, "upgrade", "--cask", "--greedy", cask]]
                                return plan
    return plan


def _run_logged(argv, output, timeout):
    output.write(f"$ {shlex.join(argv)}\n")
    output.flush()
    inherited = {}
    lock_fd = getattr(_UPDATE_CONTEXT, "lock_fd", None)
    if os.name != "nt" and lock_fd is not None:
        # A package manager can outlive a service restart. It must keep the lock
        # until it exits, preventing another update from overlapping its writes.
        inherited["pass_fds"] = (lock_fd,)
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                               stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"), **inherited)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise RuntimeError(f"更新命令超时（{timeout} 秒）：{argv[0]}")
    if code:
        raise RuntimeError(f"更新命令退出码 {code}：{argv[0]}；详情见日志。")


def _replace_bundle(staged, app, backup):
    app.rename(backup)
    try:
        staged.rename(app)
    except BaseException:
        backup.rename(app)
        raise


def _update_dmg(plan, output, timeout):
    app = Path(plan.app_path)
    if _desktop_is_running(app):
        raise RuntimeError("请先完全退出桌面 Codex / ChatGPT App，然后重新执行更新命令。")
    if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
        raise RuntimeError(f"没有 App 目录写权限：{app}；请在本机调整安装位置或权限。")
    with tempfile.TemporaryDirectory(prefix="codex-update-") as temp:
        dmg, mount = Path(temp) / "ChatGPT.dmg", Path(temp) / "mount"
        mount.mkdir()
        _run_logged(["/usr/bin/curl", "--fail", "--location", "--retry", "2", "--proto", "=https",
                     "--proto-redir", "=https", "--output", str(dmg), DESKTOP_DMG_URL], output, timeout)
        mounted = False
        try:
            _run_logged(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(dmg)], output, timeout)
            mounted = True
            candidates = [p for p in mount.glob("*.app")
                          if _bundle_info(p).get("CFBundleIdentifier") == DESKTOP_BUNDLE_ID]
            if len(candidates) != 1:
                raise RuntimeError("官方安装包中未找到唯一的桌面 Codex App。")
            source = candidates[0]
            requirement = (f'=anchor apple generic and identifier "{DESKTOP_BUNDLE_ID}" '
                           f'and certificate leaf[subject.OU] = "{OPENAI_TEAM_ID}"')
            _run_logged(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(source)], output, timeout)
            _run_logged(["/usr/sbin/spctl", "--assess", "--type", "execute", str(source)], output, timeout)
            source_info, current_info = _bundle_info(source), _bundle_info(app)
            old_build, new_build = current_info["CFBundleVersion"], source_info["CFBundleVersion"]
            if not str(old_build).isdigit() or not str(new_build).isdigit():
                raise RuntimeError("无法比较 App 构建号，未替换现有应用。")
            if int(new_build) <= int(old_build):
                output.write("已安装相同或更新的构建，保持当前 App。\n")
                return
            # Stage beside the app so the final rename is on the same filesystem.
            with tempfile.TemporaryDirectory(prefix=".codex-update-", dir=str(app.parent)) as staging:
                staged = Path(staging) / app.name
                _run_logged(["/usr/bin/ditto", str(source), str(staged)], output, timeout)
                _run_logged(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(staged)], output, timeout)
                if _desktop_is_running(app):
                    raise RuntimeError("下载期间桌面 App 被打开，未替换。请退出后重新更新。")
                backup = app.with_name(f".{app.stem}.backup-{uuid.uuid4().hex}.app")
                output.write(f"回退副本：{backup}\n")
                output.flush()
                _replace_bundle(staged, app, backup)
                try:
                    # Verify the installed copy before removing the old bundle.
                    plan.version()
                except BaseException:
                    app.rename(staged)
                    backup.rename(app)
                    raise
                shutil.rmtree(backup)
        finally:
            if mounted:
                _run_logged(["/usr/bin/hdiutil", "detach", str(mount)], output, timeout)


def format_update_result(record):
    label = "Codex CLI" if record.get("target") == "cli" else "桌面 Codex App"
    status = {"running": "正在更新", "requested": "已触发内置更新", "success": "更新完成", "failed": "更新失败"}.get(record.get("status"), "状态未知")
    lines = [f"{label}：{status}"]
    if record.get("before"):
        lines.append(f"更新前：{record['before']}")
    if record.get("after"):
        lines.append(f"更新后：{record['after']}")
        if record.get("after") == record.get("before"):
            lines.append("版本未变化；更新命令已成功执行。")
    if record.get("error"):
        lines.append(record["error"])
    if record.get("warning"):
        lines.append(record["warning"])
    if record.get("log"):
        lines.append(f"日志：{record['log']}")
    if record.get("status") == "requested":
        lines.append("已调用 App 的“检查更新”；尚未确认安装完成。请查看 App 提示，最终安装可能需要重启 App。")
    if record.get("status") == "success":
        lines.append("后续新进程使用更新后的版本；已打开的客户端需重启。")
    return "\n".join(lines)


class CodexUpdateManager:
    def __init__(self, config):
        self.config = config
        self.directory = Path(config["stateDir"]) / "updates"

    def _lock(self, shared=False, name="update.lock"):
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = (self.directory / name).open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Codex 更新或任务正在运行，请稍后重试。")
        return handle

    @contextlib.contextmanager
    def agent_run(self):
        with self._lock(shared=True):
            yield

    @contextlib.contextmanager
    def _inherit_lock(self, handle):
        previous = getattr(_UPDATE_CONTEXT, "lock_fd", None)
        _UPDATE_CONTEXT.lock_fd = handle.fileno()
        try:
            yield
        finally:
            _UPDATE_CONTEXT.lock_fd = previous

    def _write(self, record):
        destination = self.directory / f"{record['target']}.json"
        temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    def status(self, target=""):
        records = []
        for name in ([target] if target else ["cli", "desktop"]):
            path = self.directory / f"{name}.json"
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("status") == "requested" and record.get("appPath"):
                    try:
                        info = _bundle_info(record["appPath"])
                        before_build = str(record.get("buildBefore") or "")
                        current_build = str(info.get("CFBundleVersion") or "")
                        if (info.get("CFBundleIdentifier") == DESKTOP_BUNDLE_ID and before_build.isdigit()
                                and current_build.isdigit() and int(current_build) > int(before_build)):
                            record.update(status="success", after=f"{info['CFBundleShortVersionString']} ({current_build})",
                                          finishedAt=time.time())
                            self._write(record)
                    except (OSError, ValueError, plistlib.InvalidFileException):
                        pass
                if record.get("status") == "running":
                    try:
                        handle = self._lock(shared=True, name="native-update.lock" if record.get("method") == "native" else "update.lock")
                    except RuntimeError:
                        pass
                    else:
                        handle.close()
                        record["status"] = "failed"
                        record["error"] = "更新进程已结束，未记录成功结果。请检查日志后重试。"
                records.append(format_update_result(record))
        return "\n\n".join(records) or "尚无更新记录。"

    def _prepare(self, target, desktop_method=""):
        if target not in {"cli", "desktop"}:
            raise ValueError("更新目标必须是 cli 或 desktop。")
        if desktop_method and target != "desktop":
            raise ValueError("更新方式仅适用于 desktop。")
        if desktop_method == "native" and sys.platform != "darwin":
            raise RuntimeError("内置更新菜单目前支持 macOS。")
        mode = ""
        if target == "desktop" and sys.platform == "darwin" and (desktop_method == "native" or not (self.config.get("updates") or {}).get("desktopCommand")):
            selected = _desktop_mode(self.config, desktop_method)
            mode = "native" if (selected == "native" or
                                (selected == "auto" and _desktop_is_running(desktop_app_path(self.config)))) else "installer"
        # A menu request does not replace files or restart the App. It can run
        # alongside Codex tasks; only the installer needs the task exclusion lock.
        handle = self._lock(name="native-update.lock" if mode == "native" else "update.lock")
        record = {"target": target, "status": "running", "startedAt": time.time(), "pid": os.getpid(),
                  "log": str(self.directory / f"{target}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.log")}
        if mode or desktop_method:
            record["desktopMethod"] = mode or desktop_method
            if mode == "native":
                record["method"] = "native"
        try:
            self._write(record)
        except BaseException:
            handle.close()
            raise
        return handle, record

    def _execute(self, record):
        with Path(record["log"]).open("w", encoding="utf-8", buffering=1) as output:
            try:
                plan = make_update_plan(self.config, record["target"], record.get("desktopMethod") or "")
                record.update(before=plan.before, method=plan.method)
                self._write(record)
                output.write(plan.describe() + "\n")
                timeout = max(1, int((self.config.get("updates") or {}).get("timeoutSeconds") or 900))
                if plan.method == "native":
                    info = _bundle_info(plan.app_path)
                    menu = trigger_builtin_update(plan.app_path, DESKTOP_BUNDLE_ID,
                                                  (self.config.get("updates") or {}).get("desktopUpdateMenuTitles") or None)
                    record.update(status="requested", appPath=plan.app_path,
                                  buildBefore=str(info["CFBundleVersion"]), nativeMenu=menu)
                    output.write(f"已触发内置菜单：{menu}\n")
                elif plan.target == "desktop" and sys.platform == "darwin" and _desktop_is_running(plan.app_path):
                    raise RuntimeError("请先完全退出桌面 Codex / ChatGPT App，然后重新执行更新命令。")
                if plan.method == "dmg":
                    _update_dmg(plan, output, timeout)
                elif plan.method != "native":
                    for argv in plan.commands:
                        _run_logged(argv, output, timeout)
                if plan.method != "native":
                    record.update(after=plan.version(), status="success")
            except Exception as err:
                record.update(status="failed", error=str(err))
            record["finishedAt"] = time.time()
            output.write(format_update_result(record) + "\n")
        self._write(record)
        return dict(record)

    def run(self, target, desktop_method=""):
        handle, record = self._prepare(target, desktop_method)
        with handle, self._inherit_lock(handle):
            return self._execute(record)

    def start(self, target, on_started, on_complete, on_updated=None, desktop_method=""):
        handle, record = self._prepare(target, desktop_method)

        def worker():
            try:
                try:
                    on_started(format_update_result(record))
                except Exception as err:
                    log.warn(f"update start notification failed: {err}")
                with self._inherit_lock(handle):
                    result = self._execute(record)
                if result["status"] == "success" and on_updated:
                    try:
                        on_updated()
                    except Exception as err:
                        result["warning"] = f"程序已更新，但刷新本地 App Server 失败，请重启微信服务：{err}"
                        self._write(result)
            finally:
                handle.close()
            try:
                on_complete(result)
            except Exception as err:
                log.warn(f"update completion notification failed: {err}")

        thread = threading.Thread(target=worker, name="codex-update", daemon=True)
        try:
            thread.start()
        except BaseException:
            handle.close()
            raise
        return thread
