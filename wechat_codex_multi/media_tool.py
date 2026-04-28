import subprocess
import sys

from .config import load_config


def run_media_generator(name, prompt):
    config = load_config()
    generators = config.get("media", {}).get("generators") or []
    target = next((g for g in generators if g.get("name") == name), None)
    if not target:
        raise RuntimeError(f"未找到媒体生成器: {name}")
    command = str(target.get("command") or "").strip()
    if not command:
        raise RuntimeError(f"媒体生成器 {name} 未配置 command")
    completed = subprocess.run(
        command,
        input=prompt,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(target.get("timeoutSeconds") or 1800),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"生成器退出码 {completed.returncode}")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("生成器没有输出文件路径")
    return lines[-1]


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    if len(argv) < 2:
        print("usage: python3 -m wechat_codex_multi media-generate <name> <prompt>", file=sys.stderr)
        raise SystemExit(2)
    print(run_media_generator(argv[0], " ".join(argv[1:])))
