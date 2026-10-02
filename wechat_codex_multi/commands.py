"""Local command vocabulary. No model is needed to interpret these commands."""

from difflib import get_close_matches


COMMANDS = {
    "/help", "/menu", "/choose", "/status", "/active", "/accounts", "/users", "/user",
    "/ws", "/sessions", "/session", "/desktop", "/d-projects", "/d-project", "/d-sessions",
    "/d-session", "/d-account", "/new-project", "/n-p", "/d-new-project", "/d-n-p", "/d-p-n",
    "/usage", "/agents", "/agent", "/account", "/codex", "/codex-use", "/codex-accounts",
    "/codex-login", "/codex-logout", "/claude", "/claude-accounts", "/model", "/models", "/runner", "/login",
    "/restart", "/cancel", "/interrupt", "/reset", "/cwd", "/guide", "/update", "/codex-update",
    "/d-update", "/tasks", "/result", "/resend", "/notify",
}

ALIASES = {
    "/菜单": "/menu", "/帮助": "/help", "/选择": "/choose", "/状态": "/status",
    "/项目": "/ws", "/切换项目": "/ws use", "/会话": "/sessions", "/切换会话": "/session use",
    "/桌面项目": "/d-projects", "/桌面会话": "/d-sessions", "/切换桌面会话": "/d-session use",
    "/任务": "/tasks", "/结果": "/result", "/重发": "/resend", "/提醒": "/notify",
    "/停止": "/interrupt", "/取消": "/cancel", "/重置": "/reset", "/补充": "/guide",
    "/模型": "/model", "/账号": "/account", "/用量": "/usage", "/目录": "/cwd",
}
BARE_COMMANDS = {"菜单": "/menu", "帮助": "/help", "状态": "/status"}


def normalize_command(text):
    value = str(text or "").strip()
    if value in BARE_COMMANDS:
        return BARE_COMMANDS[value]
    if value.startswith("／"):
        value = "/" + value[1:]
    parts = value.split(maxsplit=1)
    login_parts = value.split()
    if len(login_parts) >= 3 and login_parts[0] == "/login" and any("@" in part for part in login_parts[2:]):
        return "/codex-login " + " ".join(login_parts[1:])
    if parts and parts[0] in ALIASES:
        return ALIASES[parts[0]] + (" " + parts[1] if len(parts) > 1 else "")
    return value


def unknown_command_message(text):
    first = str(text or "").strip().split(maxsplit=1)[0]
    matches = get_close_matches(first, sorted(COMMANDS | set(ALIASES)), n=2, cutoff=0.55)
    hint = "\n你可能想用：" + "、".join(matches) if matches else ""
    return f"未知命令：{first}。命令不会作为引导处理，也不会交给 Agent。{hint}\n发送 菜单 或 /help 查看可用操作。"
