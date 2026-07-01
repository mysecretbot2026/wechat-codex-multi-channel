import argparse
import os

from wechat_codex_multi.media_outbox import queue_media
from wechat_codex_multi.media_tool import run_media_generator


def media_generate(args):
    print(run_media_generator(args.name, args.prompt))


def media_send(args):
    outbox = (
        args.outbox
        or os.environ.get("LOCAL_AGENT_MEDIA_OUTBOX")
        or os.environ.get("WECHAT_CODEX_MULTI_MEDIA_OUTBOX")
    )
    if not outbox:
        raise RuntimeError("缺少媒体 outbox。请在 Agent 会话中使用，或显式传 --outbox。")
    actions = queue_media(outbox, args.paths, kind=args.kind or "")
    for action in actions:
        print(f"queued {action['kind']}: {action['path']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="local-agent-tools")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("media-generate", help="调用配置的媒体生成器")
    p.add_argument("name")
    p.add_argument("prompt")
    p.set_defaults(func=media_generate)

    p = sub.add_parser("media-send", help="登记本地媒体文件，当前 Agent 任务结束后发送")
    p.add_argument("paths", nargs="+", help="本地媒体文件绝对路径")
    p.add_argument("--kind", choices=["image", "video", "file"], default="", help="媒体类型，默认按扩展名判断")
    p.add_argument("--outbox", default="", help="媒体 outbox 路径；Agent 会话中通常由环境变量提供")
    p.set_defaults(func=media_send)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
