import argparse
import json
import os
from pathlib import Path

from . import logging as log
from .config import PROJECT_DIR, load_config
from .codex_accounts import default_codex_account
from .claude_usage import format_claude_admin_usage, read_claude_admin_usage
from .login import login_with_qr
from .media_outbox import queue_media
from .media_tool import run_media_generator
from .service import MultiWechatCodexService
from .state import StateStore


def ensure_account_session(state, config, account):
    account_id = (account or {}).get("accountId")
    user_id = (account or {}).get("userId")
    if not account_id or not user_id:
        return False
    state.get_session(
        state.conversation_key(account_id, user_id),
        config["codex"]["workingDirectory"],
        default_codex_account(config),
        config.get("defaultAgent") or "codex",
    )
    return True


def add_account(args):
    config = load_config(args.config)
    log.configure(config.get("logLevel"))
    state = StateStore(config["stateDir"])
    nickname = str(args.nickname or "").strip()
    if nickname:
        error = StateStore.validate_account_nickname(nickname)
        if error:
            raise RuntimeError(error)
        if not state.account_nickname_available(nickname):
            raise RuntimeError(f"用户昵称已存在: {nickname}")
    account = login_with_qr(
        base_url=config["wechat"]["baseUrl"],
        bot_type=config["wechat"]["botType"],
        route_tag=config["wechat"].get("routeTag"),
        project_dir=PROJECT_DIR,
    )
    if nickname:
        account["nickname"] = nickname
    account = state.upsert_account(account)
    ensure_account_session(state, config, account)
    print("\n微信账号已保存。")
    print(f"nickname: {account.get('nickname')}")
    print(f"accountId: {account['accountId']}")
    print(f"state: {state.file}")


def start(args):
    config = load_config(args.config)
    log.configure(config.get("logLevel"))
    service = MultiWechatCodexService(config)
    service.start()


def status(args):
    config = load_config(args.config)
    state = StateStore(config["stateDir"])
    data = {
        "configFile": config["configFile"],
        "stateFile": str(state.file),
        "accounts": [
            {
                "accountId": a.get("accountId"),
                "nickname": a.get("nickname"),
                "baseUrl": a.get("baseUrl"),
                "userId": a.get("userId"),
                "hasToken": bool(a.get("token")),
                "hasGetUpdatesBuf": bool(a.get("getUpdatesBuf")),
            }
            for a in state.list_accounts()
        ],
        "sessionCount": len(state.state.get("sessions") or {}),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))


def delete_account(args):
    config = load_config(args.config)
    state = StateStore(config["stateDir"])
    deleted = state.delete_account(args.selector)
    if not deleted:
        raise RuntimeError(f"没有找到用户: {args.selector}")
    print(f"已删除用户: {deleted.get('nickname')}")
    print(f"accountId: {deleted.get('accountId')}")
    print(f"state: {state.file}")


def rename_account(args):
    config = load_config(args.config)
    state = StateStore(config["stateDir"])
    renamed = state.rename_account(args.selector, args.nickname)
    if not renamed:
        raise RuntimeError(f"没有找到用户: {args.selector}")
    print(f"已修改用户昵称: {args.selector} -> {renamed.get('nickname')}")
    print(f"accountId: {renamed.get('accountId')}")
    print(f"state: {state.file}")


def claude_usage(args):
    config = load_config(args.config)
    claude = config.get("claude") or {}
    usage = read_claude_admin_usage(
        api_key=args.key or "",
        days=args.days,
        timeout_s=int(claude.get("adminUsageTimeoutSeconds") or 60),
        keychain_service=claude.get("adminKeychainService") or "",
    )
    print(format_claude_admin_usage(usage))


def media_generate(args):
    print(run_media_generator(args.name, args.prompt))


def media_send(args):
    outbox = args.outbox or os.environ.get("WECHAT_CODEX_MULTI_MEDIA_OUTBOX")
    if not outbox:
        raise RuntimeError("缺少媒体 outbox。请在 Agent 会话中使用，或显式传 --outbox。")
    actions = queue_media(outbox, args.paths, kind=args.kind or "")
    for action in actions:
        print(f"queued {action['kind']}: {action['path']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wechat-codex-multi")
    parser.add_argument("--config", help="配置文件路径，默认 ./config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-account", help="扫码新增一个微信 Bot 账号")
    p.add_argument("nickname", nargs="?", help="用户昵称，默认自动生成 用户1、用户2 ...")
    p.set_defaults(func=add_account)

    p = sub.add_parser("delete-account", help="按昵称、accountId 或编号删除微信用户")
    p.add_argument("selector")
    p.set_defaults(func=delete_account)

    p = sub.add_parser("delete-user", help="按昵称、accountId 或编号删除微信用户")
    p.add_argument("selector")
    p.set_defaults(func=delete_account)

    p = sub.add_parser("rename-account", help="修改微信用户昵称")
    p.add_argument("selector")
    p.add_argument("nickname")
    p.set_defaults(func=rename_account)

    p = sub.add_parser("rename-user", help="修改微信用户昵称")
    p.add_argument("selector")
    p.add_argument("nickname")
    p.set_defaults(func=rename_account)

    p = sub.add_parser("start", help="启动多账号微信 CLI Agent 服务")
    p.set_defaults(func=start)

    p = sub.add_parser("status", help="查看本地状态")
    p.set_defaults(func=status)

    p = sub.add_parser("claude-usage", help="通过 Anthropic Admin API 查看 Claude API 用量")
    p.add_argument("--days", type=int, default=7, help="查询最近 N 天，接口日粒度最大 31 天")
    p.add_argument("--key", default="", help="直接传入 Anthropic Admin API Key；默认读取环境变量或 macOS Keychain")
    p.set_defaults(func=claude_usage)

    p = sub.add_parser("media-generate", help="调用配置的媒体生成器")
    p.add_argument("name")
    p.add_argument("prompt")
    p.set_defaults(func=media_generate)

    p = sub.add_parser("media-send", help="登记本地媒体文件，当前 Agent 任务结束后由微信发送")
    p.add_argument("paths", nargs="+", help="本地媒体文件绝对路径")
    p.add_argument("--kind", choices=["image", "video", "file"], default="", help="媒体类型，默认按扩展名判断")
    p.add_argument("--outbox", default="", help="媒体 outbox 路径；Agent 会话中通常由环境变量提供")
    p.set_defaults(func=media_send)

    args = parser.parse_args(argv)
    args.func(args)
