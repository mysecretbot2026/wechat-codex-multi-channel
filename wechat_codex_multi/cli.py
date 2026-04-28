import argparse
import json
from pathlib import Path

from . import logging as log
from .config import PROJECT_DIR, load_config
from .login import login_with_qr
from .media_tool import run_media_generator
from .service import MultiWechatCodexService
from .state import StateStore


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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wechat-codex-multi")
    parser.add_argument("--config", help="配置文件路径，默认 ./config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-account", help="扫码新增一个微信 Bot 账号")
    p.set_defaults(func=add_account)

    p = sub.add_parser("start", help="启动多账号微信 Codex 服务")
    p.set_defaults(func=start)

    p = sub.add_parser("status", help="查看本地状态")
    p.set_defaults(func=status)

    p = sub.add_parser("media-generate", help="调用配置的媒体生成器")
    p.add_argument("name")
    p.add_argument("prompt")
    p.set_defaults(func=media_generate)

    args = parser.parse_args(argv)
    args.func(args)
