"""Locate local Codex installations and explain authentication failures."""

import os
import shlex
import shutil
from pathlib import Path


_BUNDLE_BINARIES = (
    "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
    "Contents/Resources/codex",
)


def _executable(path):
    return path.is_file() and os.access(path, os.X_OK)


def _desktop_candidates(app_path=""):
    apps = [Path(os.path.expandvars(os.path.expanduser(app_path)))] if app_path else []
    for directory in (Path("/Applications"), Path.home() / "Applications"):
        apps.extend(directory / name for name in ("ChatGPT.app", "Codex.app"))
    return [app / relative for app in apps for relative in _BUNDLE_BINARIES]


def find_desktop_codex_bin(app_path=""):
    return next((str(path) for path in _desktop_candidates(app_path) if _executable(path)), "")


def desktop_codex_bin(config):
    codex = config.get("codex") or {}
    return (codex.get("desktopBin")
            or find_desktop_codex_bin((config.get("updates") or {}).get("desktopAppPath") or "")
            or codex.get("bin") or "codex")


def _legacy_bundle_path(value):
    path = Path(value)
    return any(str(path).endswith(f"/{name}/{relative}")
               for name in ("ChatGPT.app", "Codex.app") for relative in _BUNDLE_BINARIES)


def resolve_codex_bin(codex_bin="codex", prefer_desktop=False):
    value = os.path.expandvars(os.path.expanduser(str(codex_bin or "codex").strip()))
    found = shutil.which(value)
    if found:
        return str(Path(found).absolute())
    # Old app paths can move after a desktop update. Other explicit binaries
    # must not silently switch to a different installation.
    automatic = value == "codex" or _legacy_bundle_path(value)
    if automatic:
        bundled = find_desktop_codex_bin()
        cli = shutil.which("codex")
        if prefer_desktop and bundled:
            return bundled
        if cli:
            return cli
        for prefix in (Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
                       Path.home() / ".local/bin", Path.home() / ".npm-global/bin"):
            candidate = prefix / "codex"
            if _executable(candidate):
                return str(candidate)
        if bundled:
            return bundled
    raise FileNotFoundError(
        f"找不到可执行的 Codex 程序：{value}。请安装 Codex CLI "
        "（npm install -g @openai/codex），或在 config.json 的 codex.bin / "
        "codex.desktopBin 中填写本机实际可执行路径，然后重启服务。"
    )


def is_workspace_auth_error(error):
    value = str(error or "").lower()
    return "workspace routing discovery" in value and ("unauthorized" in value or "401" in value)


def is_codex_auth_error(error):
    value = str(error or "").lower()
    return (is_workspace_auth_error(error)
            or "refresh token" in value
            or "refresh_token" in value
            or "invalid_grant" in value
            or "not logged in" in value
            or ("unauthorized" in value and "401" in value))


def auth_error_message(error, codex_home="", account_name="", codex_bin="codex"):
    lines = [f"Codex 授权失效或未登录：{error}"]
    if account_name:
        lines.append(f"账号：{account_name}")
        lines.append(f"微信管理员重新登录：/codex-login {account_name}")
    if codex_home:
        lines.append(f"CODEX_HOME：{codex_home}")
        lines.append("本机重新登录：CODEX_HOME=" + shlex.quote(str(codex_home))
                     + " " + shlex.quote(str(codex_bin)) + " login --device-auth")
    lines.append("请在这台机器的对应账号目录重新授权；codex login status 只检查本地登录状态。")
    return "\n".join(lines)


class CodexAuthError(RuntimeError):
    def __init__(self, error, codex_home="", account_name="", codex_bin="codex"):
        super().__init__(auth_error_message(error, codex_home, account_name, codex_bin))
