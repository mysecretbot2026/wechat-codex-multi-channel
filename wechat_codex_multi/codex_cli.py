import json
import os
import signal
import shutil
import subprocess
import threading
from pathlib import Path

from . import logging as log
from .codex_accounts import default_codex_account, resolve_session_codex_account
from .codex_models import resolve_session_model


class CodexCancelled(RuntimeError):
    pass


class CodexAccumulator:
    def __init__(self, thread_id="", codex_home=""):
        self.thread_id = thread_id or ""
        self.codex_home = str(Path(codex_home or "~/.codex").expanduser())
        self.item_order = []
        self.item_text = {}
        self.messages = []
        self.errors = []

    def handle(self, event):
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                self.thread_id = thread_id
            return
        if event_type in {"item.started", "item.delta", "item.completed"}:
            self._handle_item(event_type, event.get("item") or event)
            return
        if event_type in {"turn.failed", "error"}:
            message = self._extract_error(event)
            if message:
                self.errors.append(message)
            return
        if event_type == "image_generation_end":
            self._handle_image_generation(event)

    def _handle_item(self, event_type, item):
        item = item if isinstance(item, dict) else {}
        item_type = item.get("type") or item.get("item_type") or item.get("itemType")
        if item_type not in (None, "", "agent_message"):
            return
        item_id = item.get("id") or item.get("item_id") or item.get("itemId")
        if isinstance(item_id, str) and item_id and item_id not in self.item_order:
            self.item_order.append(item_id)
        if event_type == "item.delta":
            delta = item.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                self.item_text[item_id] = self.item_text.get(item_id, "") + delta
            return
        text = item.get("text")
        if isinstance(text, str) and text:
            if isinstance(item_id, str) and item_id:
                self.item_text[item_id] = text
            else:
                self.messages.append(text)

    def text(self):
        parts = []
        for item_id in self.item_order:
            value = self.item_text.get(item_id, "").strip()
            if value:
                parts.append(value)
        parts.extend(m.strip() for m in self.messages if isinstance(m, str) and m.strip())
        return "\n".join(parts).strip()

    def _handle_image_generation(self, event):
        call_id = event.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        if not self.thread_id:
            return
        path = Path(self.codex_home) / "generated_images" / self.thread_id / f"{call_id}.png"
        self.messages.append(f"[[send_image:{path}]]")

    @staticmethod
    def _extract_error(event):
        for key in ("message", "error", "stderr"):
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


class CodexCliRunner:
    def __init__(self, config, state_store):
        self.config = config
        self.state = state_store
        self.timeout_ms = int(config["codex"].get("timeoutMs") or 1200_000)
        self.model = str(config["codex"].get("model") or "").strip()
        self.reasoning_effort = str(config["codex"].get("reasoningEffort") or "").strip()
        self.bin = str(config["codex"].get("bin") or "codex")
        self.bypass = bool(config["codex"].get("bypassApprovalsAndSandbox", True))
        self.processes = set()
        self.process_by_conversation = {}
        self.cancelled_conversations = set()
        self.processes_lock = threading.Lock()

    def _resolve_bin(self):
        return shutil.which(self.bin) or self.bin

    def _base_args(self, cwd, model="", reasoning_effort=""):
        args = [self._resolve_bin(), "-C", str(cwd)]
        if self.bypass:
            args.append("--dangerously-bypass-approvals-and-sandbox")
        selected_model = str(model or self.model or "").strip()
        selected_reasoning = str(reasoning_effort or self.reasoning_effort or "").strip()
        if selected_model:
            args.extend(["-m", selected_model])
        if selected_reasoning:
            args.extend(["-c", f'model_reasoning_effort="{selected_reasoning}"'])
        return args

    def _build_prompt(self, user_message, fresh_session, media_generators):
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
            "如果 Codex 生成图片后输出 Saved to: file:///Users/.../image.png，也可以直接保留这个 file:// 路径，微信通道会自动发送。",
            "这些标记会被微信通道解析并发送，用户不会看到标记文本。",
            "",
            "如果需要调用可配置媒体生成器，可在 shell 中运行：",
            "python3 -m wechat_codex_multi media-generate <name> <prompt>",
            "命令会输出生成文件路径。然后使用对应 send_image/send_video/send_file 标记发送。",
        ]
        if media_generators:
            instructions.append("当前已配置媒体生成器：")
            for gen in media_generators:
                name = gen.get("name")
                kind = gen.get("kind")
                desc = gen.get("description") or ""
                instructions.append(f"- {name} ({kind}) {desc}".strip())
        extra = str(self.config["codex"].get("extraPrompt") or "").strip()
        if extra:
            instructions.extend(["", extra])
        if fresh_session:
            return "\n".join(instructions + ["", f"用户消息：{user_message}"])
        return user_message

    @staticmethod
    def _is_transient_resume_error(error):
        value = str(error or "").lower()
        return (
            "stream disconnected before completion" in value
            or "broken pipe" in value
            or "failed to send websocket request" in value
            or "reconnecting..." in value
        )

    @staticmethod
    def _is_rollout_record_error(error):
        value = str(error or "").lower()
        return "failed to record rollout items" in value and "thread" in value and "not found" in value

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

    def cancel(self, conversation_key, reset_session=True):
        with self.processes_lock:
            process = self.process_by_conversation.get(conversation_key)
            self.cancelled_conversations.add(conversation_key)
        if process and process.poll() is None:
            self._terminate_process(process)
            if reset_session:
                self.state.reset_session(conversation_key)
            return True
        if reset_session:
            self.state.reset_session(conversation_key)
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
        default_cwd = self.config["codex"]["workingDirectory"]
        session = self.state.get_session(conversation_key, default_cwd, default_codex_account(self.config))
        cwd = session.get("cwd") or default_cwd
        codex_account = resolve_session_codex_account(self.config, session)
        codex_account_name = codex_account.get("name") or default_codex_account(self.config)
        codex_home = codex_account.get("codexHome") or ""
        model_selection = resolve_session_model(self.config, session)
        selected_model = model_selection.get("model") or ""
        selected_reasoning = model_selection.get("reasoningEffort") or ""
        existing_thread_id = session.get("codexThreadId") or ""
        fresh = not existing_thread_id
        args = self._base_args(cwd, selected_model, selected_reasoning) + ["exec"]
        if existing_thread_id:
            args.extend(["resume", existing_thread_id])
        prompt = self._build_prompt(
            user_message,
            fresh,
            self.config.get("media", {}).get("generators") or [],
        )
        args.extend(["--skip-git-repo-check", "--json", prompt])

        log.info(
            f"[codex] start conversation={conversation_key} account={codex_account_name} "
            f"model={selected_model or 'default'} reasoning={selected_reasoning or 'default'} "
            f"resume={bool(existing_thread_id)} cwd={cwd}"
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        if codex_home:
            env["CODEX_HOME"] = codex_home
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
        accumulator = CodexAccumulator(existing_thread_id, codex_home=codex_home)
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
                self.state.reset_session(conversation_key)
                raise RuntimeError(f"Codex 在 {self.timeout_ms // 1000} 秒内没有返回结果")
            except BaseException:
                self._terminate_process(process)
                raise
            t1.join(timeout=2)
            t2.join(timeout=2)
            if self._consume_cancelled(conversation_key):
                raise CodexCancelled("Codex 已取消")
            if stdout_errors:
                raise RuntimeError(str(stdout_errors[-1]))
            if accumulator.thread_id:
                self.state.update_session(
                    conversation_key,
                    codexThreadId=accumulator.thread_id,
                    cwd=cwd,
                    codexAccount=codex_account_name,
                    codexModel=selected_model,
                    codexReasoningEffort=selected_reasoning,
                )
            text = accumulator.text()
            if return_code == 0 and text:
                return text
            stderr = "".join(stderr_chunks).strip()
            error = text or (accumulator.errors[-1] if accumulator.errors else "") or stderr
            if text and self._is_rollout_record_error(stderr or error):
                log.warn(f"[codex] ignoring rollout record error after content was produced conversation={conversation_key}")
                return text
            if existing_thread_id and retry_on_resume_error and self._is_transient_resume_error(error):
                log.warn(f"[codex] resume failed; resetting thread and retrying conversation={conversation_key}")
                self.state.reset_session(conversation_key)
                return self.run(conversation_key, user_message, retry_on_resume_error=False)
            raise RuntimeError(error or f"codex 返回非零退出码: {return_code}")
        finally:
            self._unregister_process(conversation_key, process)
