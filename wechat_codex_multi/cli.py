import argparse
import json
import os
from pathlib import Path

from . import logging as log
from .config import PROJECT_DIR, load_config
from .codex_accounts import default_codex_account
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
    account = login_with_qr(
        base_url=config["wechat"]["baseUrl"],
        bot_type=config["wechat"]["botType"],
        route_tag=config["wechat"].get("routeTag"),
        project_dir=PROJECT_DIR,
    )
    state = StateStore(config["stateDir"])
    state.upsert_account(account)
    ensure_account_session(state, config, account)
    print("\n微信账号已保存。")
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
    p.set_defaults(func=add_account)

    p = sub.add_parser("start", help="启动多账号微信 CLI Agent 服务")
    p.set_defaults(func=start)

    p = sub.add_parser("status", help="查看本地状态")
    p.set_defaults(func=status)

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
