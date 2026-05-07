import json
import os
import signal
import shutil
import subprocess
import threading
from pathlib import Path

from . import logging as log
from .claude_accounts import default_claude_account, resolve_session_claude_account
from .claude_models import resolve_session_claude_model
from .codex_cli import CodexCancelled


class ClaudeAccumulator:
    def __init__(self, session_id=""):
        self.session_id = session_id or ""
        self.messages = []
        self.final_result = ""
        self.errors = []

    def handle(self, event):
        event_type = event.get("type")
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        if event_type == "system":
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
            return
        if event_type == "assistant":
            self._handle_assistant(event.get("message") or {})
            return
        if event_type == "result":
            self._handle_result(event)
            return
        if event_type == "error":
            message = self._extract_error(event)
            if message:
                self.errors.append(message)

    def _handle_assistant(self, message):
        content = message.get("content")
        if not isinstance(content, list):
            return
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "".join(parts).strip()
        if text:
            self.messages.append(text)

    def _handle_result(self, event):
        result = event.get("result")
        if isinstance(result, str) and result.strip():
            self.final_result = result.strip()
        if event.get("is_error"):
            message = self._extract_error(event)
            if message:
                self.errors.append(message)

    def text(self):
        if self.final_result:
            return self.final_result
        return "\n".join(m.strip() for m in self.messages if isinstance(m, str) and m.strip()).strip()

    @staticmethod
    def _extract_error(event):
        for key in ("message", "error", "result", "stderr", "api_error_status", "terminal_reason"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        error = event.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "stderr"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return json.dumps(event, ensure_ascii=False)


class ClaudeCliRunner:
    def __init__(self, config, state_store):
        self.config = config
        self.state = state_store
        claude = config.get("claude") or {}
        self.timeout_ms = int(claude.get("timeoutMs") or 1200_000)
        self.model = str(claude.get("model") or "").strip()
        self.effort = str(claude.get("effort") or "").strip()
        self.bin = str(claude.get("bin") or "claude")
        self.permission_mode = str(claude.get("permissionMode") or "").strip()
        self.processes = set()
        self.process_by_conversation = {}
        self.cancelled_conversations = set()
        self.processes_lock = threading.Lock()

    def _resolve_bin(self):
        return shutil.which(self.bin) or self.bin

    def _default_cwd(self):
        claude_cwd = (self.config.get("claude") or {}).get("workingDirectory") or ""
        return claude_cwd or self.config["codex"]["workingDirectory"]

    def _base_args(self, model="", effort="", session_id=""):
        args = [
            self._resolve_bin(),
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
        ]
        if self.permission_mode:
            args.extend(["--permission-mode", self.permission_mode])
        selected_model = str(model or self.model or "").strip()
        selected_effort = str(effort or self.effort or "").strip()
        if selected_model:
            args.extend(["--model", selected_model])
        if selected_effort:
            args.extend(["--effort", selected_effort])
        system_prompt = self._system_prompt()
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])
        if session_id:
            args.extend(["--resume", session_id])
        return args

    def _system_prompt(self):
        instructions = [
            "你通过微信与用户交流。",
            "默认用中文回复，除非用户明确使用其他语言。",
            "回复尽量直接、简洁、可执行。",
            "微信不渲染 Markdown，尽量输出纯文本。",
            "",
            "你可以生成本地图片、文件或视频，然后让微信通道发送。",
            "发送本地媒体时，在最终回复中单独写 [[send_image:/真实绝对路径]]、[[send_file:/真实绝对路径]] 或 [[send_video:/真实绝对路径]]。",
            "媒体标记路径必须是真实存在的本地绝对路径。",
            "不要原样输出占位路径，例如 /absolute/path/to/image.png、/Users/bot/.../xxx.png 或 真实绝对路径。",
            "如果生成图片后输出 Saved to: file:///Users/.../image.png，也可以直接保留这个 file:// 路径，微信通道会自动发送。",
            "这些标记会被微信通道解析并发送，用户不会看到标记文本。",
            "",
            "如果需要调用可配置媒体生成器，可在 shell 中运行：",
            "python3 -m wechat_codex_multi media-generate <name> <prompt>",
            "命令会输出生成文件路径。然后使用对应 send_image/send_video/send_file 标记发送。",
        ]
        media_generators = self.config.get("media", {}).get("generators") or []
        if media_generators:
            instructions.append("当前已配置媒体生成器：")
            for gen in media_generators:
                name = gen.get("name")
                kind = gen.get("kind")
                desc = gen.get("description") or ""
                instructions.append(f"- {name} ({kind}) {desc}".strip())
        extra = str((self.config.get("claude") or {}).get("extraPrompt") or "").strip()
        if extra:
            instructions.extend(["", extra])
        return "\n".join(instructions)

    @staticmethod
    def _is_resume_error(error):
        value = str(error or "").lower()
        return "resume" in value or ("session" in value and "not found" in value)

    def _register_process(self, conversation_key, process):
        with self.processes_lock:
            self.processes.add(process)
            self.process_by_conversation[conversation_key] = process

    def _unregister_process(self, conversation_key, process):
        with self.processes_lock:
            self.processes.discard(process)
            if self.process_by_conversation.get(conversation_key) is process:
                self.process_by_conversation.pop(conversation_key, None)

    def is_running(self, conversation_key):
        with self.processes_lock:
            process = self.process_by_conversation.get(conversation_key)
        return bool(process and process.poll() is None)

    def cancel(self, conversation_key):
        with self.processes_lock:
            process = self.process_by_conversation.get(conversation_key)
            self.cancelled_conversations.add(conversation_key)
        if process and process.poll() is None:
            self._terminate_process(process)
            self.state.reset_session(conversation_key, agent="claude")
            return True
        self.state.reset_session(conversation_key, agent="claude")
        return False

    def _consume_cancelled(self, conversation_key):
        with self.processes_lock:
            if conversation_key in self.cancelled_conversations:
                self.cancelled_conversations.discard(conversation_key)
                return True
        return False

    def terminate_all(self):
        with self.processes_lock:
            processes = list(self.processes)
        for process in processes:
            self._terminate_process(process)

    @staticmethod
    def _terminate_process(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=5)
        except Exception:
            pass

    def run(self, conversation_key, user_message, retry_on_resume_error=True):
        default_cwd = self._default_cwd()
        session = self.state.get_session(conversation_key, default_cwd)
        cwd = session.get("cwd") or default_cwd
        claude_account = resolve_session_claude_account(self.config, session)
        claude_account_name = claude_account.get("name") or default_claude_account(self.config)
        claude_config_dir = claude_account.get("claudeConfigDir") or ""
        model_selection = resolve_session_claude_model(self.config, session)
        selected_model = model_selection.get("model") or ""
        selected_effort = model_selection.get("effort") or ""
        existing_session_id = session.get("claudeSessionId") or ""
        args = self._base_args(selected_model, selected_effort, existing_session_id)
        args.append(user_message)

        log.info(
            f"[claude] start conversation={conversation_key} account={claude_account_name} "
            f"model={selected_model or 'default'} effort={selected_effort or 'default'} "
            f"resume={bool(existing_session_id)} cwd={cwd}"
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        if claude_config_dir:
            env["CLAUDE_CONFIG_DIR"] = claude_config_dir
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            bufsize=1,
            start_new_session=True,
        )
        self._register_process(conversation_key, process)
        accumulator = ClaudeAccumulator(existing_session_id)
        stderr_chunks = []
        stdout_errors = []

        def read_stdout():
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        accumulator.handle(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
            except Exception as err:
                stdout_errors.append(err)

        def read_stderr():
            try:
                assert process.stderr is not None
                for line in process.stderr:
                    stderr_chunks.append(line)
            except Exception:
                pass

        t1 = threading.Thread(target=read_stdout, daemon=True)
        t2 = threading.Thread(target=read_stderr, daemon=True)
        t1.start()
        t2.start()
        try:
            try:
                return_code = process.wait(timeout=self.timeout_ms / 1000)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                self.state.reset_session(conversation_key, agent="claude")
                raise RuntimeError(f"Claude 在 {self.timeout_ms // 1000} 秒内没有返回结果")
            except BaseException:
                self._terminate_process(process)
                raise
            t1.join(timeout=2)
            t2.join(timeout=2)
            if self._consume_cancelled(conversation_key):
                raise CodexCancelled("Claude 已取消")
            if stdout_errors:
                raise RuntimeError(str(stdout_errors[-1]))
            if accumulator.session_id:
                self.state.update_session(
                    conversation_key,
                    claudeSessionId=accumulator.session_id,
                    cwd=cwd,
                    claudeAccount=claude_account_name,
                    claudeModel=selected_model,
                    claudeEffort=selected_effort,
                )
            text = accumulator.text()
            if return_code == 0 and text and not accumulator.errors:
                return text
            stderr = "".join(stderr_chunks).strip()
            error = text or (accumulator.errors[-1] if accumulator.errors else "") or stderr
            if existing_session_id and retry_on_resume_error and self._is_resume_error(error):
                log.warn(f"[claude] resume failed; resetting session and retrying conversation={conversation_key}")
                self.state.reset_session(conversation_key, agent="claude")
                return self.run(conversation_key, user_message, retry_on_resume_error=False)
            raise RuntimeError(error or f"claude 返回非零退出码: {return_code}")
        finally:
            self._unregister_process(conversation_key, process)
