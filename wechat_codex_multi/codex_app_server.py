import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import logging as log
from .codex_accounts import default_codex_account, resolve_session_codex_account
from .codex_cli import CodexCancelled
from .codex_models import resolve_session_model


class JsonRpcError(RuntimeError):
    pass


class AppTurnState:
    def __init__(self, conversation_key, thread_id=""):
        self.conversation_key = conversation_key
        self.thread_id = thread_id or ""
        self.active_turn_id = ""
        self.running = False
        self.cancelled = False
        self.completed = threading.Event()
        self.item_order = []
        self.item_text = {}
        self.messages = []
        self.error = ""
        self.status = ""
        self.lock = threading.RLock()

    def start_turn(self, turn_id):
        with self.lock:
            self.active_turn_id = turn_id
            self.running = True
            self.cancelled = False
            self.completed.clear()
            self.item_order = []
            self.item_text = {}
            self.messages = []
            self.error = ""
            self.status = "inProgress"

    def handle_agent_delta(self, item_id, delta):
        if not item_id or not isinstance(delta, str):
            return
        with self.lock:
            if item_id not in self.item_order:
                self.item_order.append(item_id)
            self.item_text[item_id] = self.item_text.get(item_id, "") + delta

    def handle_completed_item(self, item):
        item = item if isinstance(item, dict) else {}
        if item.get("type") != "agentMessage":
            return
        text = item.get("text")
        if not isinstance(text, str) or not text:
            return
        item_id = item.get("id")
        with self.lock:
            if item_id:
                if item_id not in self.item_order:
                    self.item_order.append(item_id)
                self.item_text[item_id] = text
            else:
                self.messages.append(text)

    def finish(self, status="", error=""):
        with self.lock:
            self.status = status or ""
            self.error = error or ""
            self.running = False
            self.active_turn_id = ""
            self.completed.set()

    def text(self):
        with self.lock:
            parts = []
            for item_id in self.item_order:
                value = self.item_text.get(item_id, "").strip()
                if value:
                    parts.append(value)
            parts.extend(m.strip() for m in self.messages if isinstance(m, str) and m.strip())
            return "\n".join(parts).strip()


class AppServerProcess:
    def __init__(self, bin_path, codex_home=""):
        self.bin_path = bin_path
        self.codex_home = codex_home or ""
        self.process = None
        self.next_id = 1
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.contexts_by_thread = {}
        self.contexts_lock = threading.RLock()
        self.closed = False
        self.reader_thread = None

    def start(self):
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home
        self.process = subprocess.Popen(
            [self.bin_path, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        self.closed = False
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "wechat-codex-multi-channel", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
            timeout_s=15,
        )

    def close(self):
        self.closed = True
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def _next_request_id(self):
        with self.pending_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

    def request(self, method, params=None, timeout_s=120):
        self.start() if method != "initialize" else None
        request_id = self._next_request_id()
        event = threading.Event()
        slot = {"event": event, "response": None}
        with self.pending_lock:
            self.pending[request_id] = slot
        payload = {"id": request_id, "method": method, "params": params or {}}
        try:
            assert self.process is not None and self.process.stdin is not None
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except Exception as err:
            with self.pending_lock:
                self.pending.pop(request_id, None)
            raise RuntimeError(f"app-server request failed: {err}") from err
        if not event.wait(timeout_s):
            with self.pending_lock:
                self.pending.pop(request_id, None)
            raise TimeoutError(f"app-server request timeout: {method}")
        response = slot.get("response") or {}
        if response.get("error"):
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise JsonRpcError(message or json.dumps(error, ensure_ascii=False))
        return response.get("result")

    def register_context(self, context):
        if not context.thread_id:
            return
        with self.contexts_lock:
            self.contexts_by_thread[context.thread_id] = context

    def unregister_context(self, context):
        with self.contexts_lock:
            if self.contexts_by_thread.get(context.thread_id) is context:
                self.contexts_by_thread.pop(context.thread_id, None)

    def context_for_thread(self, thread_id):
        with self.contexts_lock:
            return self.contexts_by_thread.get(thread_id)

    def _read_loop(self):
        try:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message:
                    with self.pending_lock:
                        slot = self.pending.pop(message["id"], None)
                    if slot:
                        slot["response"] = message
                        slot["event"].set()
                    continue
                self._handle_notification(message)
        finally:
            self.closed = True
            with self.pending_lock:
                pending = list(self.pending.values())
                self.pending.clear()
            for slot in pending:
                slot["response"] = {"error": {"message": "app-server stopped"}}
                slot["event"].set()

    def _stderr_loop(self):
        try:
            assert self.process is not None and self.process.stderr is not None
            for line in self.process.stderr:
                if line.strip():
                    log.warn(f"[app-server] {line.strip()}")
        except Exception:
            pass

    def _handle_notification(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId")
        context = self.context_for_thread(thread_id) if thread_id else None
        if not context:
            return
        turn_id = params.get("turnId")
        with context.lock:
            if context.active_turn_id and turn_id and turn_id != context.active_turn_id:
                return
        if method == "item/agentMessage/delta":
            context.handle_agent_delta(params.get("itemId"), params.get("delta"))
            return
        if method == "item/completed":
            context.handle_completed_item(params.get("item") or {})
            return
        if method == "turn/completed":
            turn = params.get("turn") or {}
            error = turn.get("error")
            if isinstance(error, dict):
                error = error.get("message") or json.dumps(error, ensure_ascii=False)
            context.finish(turn.get("status") or "", error or "")
            return
        if method == "turn/error":
            error = params.get("error")
            if isinstance(error, dict):
                error = error.get("message") or json.dumps(error, ensure_ascii=False)
            context.finish("failed", error or "turn failed")


class CodexAppServerRunner:
    def __init__(self, config, state_store):
        self.config = config
        self.state = state_store
        self.timeout_ms = int(config["codex"].get("timeoutMs") or 1200_000)
        self.model = str(config["codex"].get("model") or "").strip()
        self.reasoning_effort = str(config["codex"].get("reasoningEffort") or "").strip()
        self.bin = str(config["codex"].get("bin") or "codex")
        self.bypass = bool(config["codex"].get("bypassApprovalsAndSandbox", True))
        self.servers = {}
        self.contexts = {}
        self.lock = threading.RLock()

    def _resolve_bin(self):
        return shutil.which(self.bin) or self.bin

    def _server_key(self, codex_account):
        return codex_account.get("codexHome") or "__default__"

    def _server_for_account(self, codex_account):
        key = self._server_key(codex_account)
        with self.lock:
            server = self.servers.get(key)
            if not server:
                server = AppServerProcess(self._resolve_bin(), codex_home=codex_account.get("codexHome") or "")
                self.servers[key] = server
            server.start()
            return server

    def _context(self, conversation_key, thread_id=""):
        with self.lock:
            context = self.contexts.get(conversation_key)
            if not context:
                context = AppTurnState(conversation_key, thread_id=thread_id)
                self.contexts[conversation_key] = context
            elif thread_id and context.thread_id != thread_id:
                context.thread_id = thread_id
            return context

    def _instructions(self):
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
        media_generators = self.config.get("media", {}).get("generators") or []
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
        return "\n".join(instructions)

    def _thread_params(self, cwd, model="", reasoning_effort=""):
        params = {
            "cwd": str(cwd),
            "baseInstructions": self._instructions(),
            "developerInstructions": "",
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        if self.bypass:
            params["approvalPolicy"] = "never"
            params["sandbox"] = "danger-full-access"
        return params

    def _turn_params(self, thread_id, cwd, user_message, model="", reasoning_effort=""):
        params = {
            "threadId": thread_id,
            "cwd": str(cwd),
            "input": [{"type": "text", "text": user_message}],
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        if self.bypass:
            params["approvalPolicy"] = "never"
            params["sandboxPolicy"] = {"type": "dangerFullAccess"}
        return params

    def _ensure_thread(self, server, context, conversation_key, session, cwd, model, reasoning_effort, codex_account_name):
        thread_id = session.get("codexThreadId") or ""
        if not thread_id:
            context.thread_id = ""
            server.unregister_context(context)
        if thread_id:
            context.thread_id = thread_id
            server.register_context(context)
            try:
                server.request(
                    "thread/resume",
                    dict(self._thread_params(cwd, model, reasoning_effort), threadId=thread_id, excludeTurns=True),
                    timeout_s=30,
                )
                return thread_id
            except Exception as err:
                log.warn(f"[app-server] resume failed conversation={conversation_key}: {err}")
                self.state.reset_session(conversation_key)
                context.thread_id = ""
                server.unregister_context(context)
        result = server.request("thread/start", self._thread_params(cwd, model, reasoning_effort), timeout_s=30)
        thread = (result or {}).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise RuntimeError("app-server did not return thread id")
        context.thread_id = thread_id
        server.register_context(context)
        self.state.update_session(
            conversation_key,
            codexThreadId=thread_id,
            cwd=cwd,
            codexAccount=codex_account_name,
            codexModel=model,
            codexReasoningEffort=reasoning_effort,
        )
        return thread_id

    def is_running(self, conversation_key):
        with self.lock:
            context = self.contexts.get(conversation_key)
        return bool(context and context.running)

    def run(self, conversation_key, user_message, retry_on_resume_error=True):
        default_cwd = self.config["codex"]["workingDirectory"]
        session = self.state.get_session(conversation_key, default_cwd, default_codex_account(self.config))
        cwd = session.get("cwd") or default_cwd
        codex_account = resolve_session_codex_account(self.config, session)
        codex_account_name = codex_account.get("name") or default_codex_account(self.config)
        model_selection = resolve_session_model(self.config, session)
        selected_model = model_selection.get("model") or self.model
        selected_reasoning = model_selection.get("reasoningEffort") or self.reasoning_effort
        server = self._server_for_account(codex_account)
        context = self._context(conversation_key, session.get("codexThreadId") or "")
        thread_id = self._ensure_thread(
            server,
            context,
            conversation_key,
            session,
            cwd,
            selected_model,
            selected_reasoning,
            codex_account_name,
        )
        log.info(
            f"[app-server] start turn conversation={conversation_key} account={codex_account_name} "
            f"model={selected_model or 'default'} reasoning={selected_reasoning or 'default'} cwd={cwd}"
        )
        result = server.request(
            "turn/start",
            self._turn_params(thread_id, cwd, user_message, selected_model, selected_reasoning),
            timeout_s=30,
        )
        turn = (result or {}).get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id:
            raise RuntimeError("app-server did not return turn id")
        context.start_turn(turn_id)
        if not context.completed.wait(self.timeout_ms / 1000):
            self.cancel(conversation_key)
            raise RuntimeError(f"Codex 在 {self.timeout_ms // 1000} 秒内没有返回结果")
        if context.cancelled or context.status == "interrupted":
            raise CodexCancelled("Codex 已取消")
        if context.status and context.status != "completed":
            raise RuntimeError(context.error or f"app-server turn 状态异常: {context.status}")
        self.state.update_session(
            conversation_key,
            codexThreadId=thread_id,
            cwd=cwd,
            codexAccount=codex_account_name,
            codexModel=selected_model,
            codexReasoningEffort=selected_reasoning,
        )
        return context.text()

    def steer(self, conversation_key, user_message):
        default_cwd = self.config["codex"]["workingDirectory"]
        session = self.state.get_session(conversation_key, default_cwd, default_codex_account(self.config))
        codex_account = resolve_session_codex_account(self.config, session)
        if not session.get("codexThreadId"):
            return False
        server = self._server_for_account(codex_account)
        context = self._context(conversation_key, session.get("codexThreadId") or "")
        with context.lock:
            if not context.running or not context.thread_id or not context.active_turn_id:
                return False
            thread_id = context.thread_id
            turn_id = context.active_turn_id
        try:
            server.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": user_message}],
                },
                timeout_s=15,
            )
            log.info(f"[app-server] steered conversation={conversation_key} turn={turn_id}")
            return True
        except Exception as err:
            log.warn(f"[app-server] steer failed conversation={conversation_key}: {err}")
            return False

    def cancel(self, conversation_key):
        default_cwd = self.config["codex"]["workingDirectory"]
        session = self.state.get_session(conversation_key, default_cwd, default_codex_account(self.config))
        codex_account = resolve_session_codex_account(self.config, session)
        server = self._server_for_account(codex_account)
        context = self._context(conversation_key, session.get("codexThreadId") or "")
        killed = False
        with context.lock:
            thread_id = context.thread_id
            turn_id = context.active_turn_id
            context.cancelled = True
        if thread_id and turn_id:
            try:
                server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout_s=10)
                killed = True
            except Exception as err:
                log.warn(f"[app-server] interrupt failed conversation={conversation_key}: {err}")
        self.state.reset_session(conversation_key)
        with context.lock:
            context.thread_id = ""
            context.active_turn_id = ""
        context.finish("interrupted", "")
        return killed

    def terminate_all(self):
        with self.lock:
            servers = list(self.servers.values())
            self.servers.clear()
        for server in servers:
            server.close()
