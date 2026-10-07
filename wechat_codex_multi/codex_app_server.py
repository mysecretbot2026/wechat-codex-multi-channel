import json
import os
import subprocess
import threading
from pathlib import Path

from . import logging as log
from .codex_accounts import default_codex_account, resolve_session_codex_account
from .codex_cli import CodexCancelled
from .codex_models import resolve_session_model
from .codex_runtime import is_codex_auth_error, is_workspace_auth_error, resolve_codex_bin
from .prompting import prompt_version
from .session_discovery import clean_title


class JsonRpcError(RuntimeError):
    pass


class AppTurnState:
    def __init__(self, conversation_key, thread_id=""):
        self.conversation_key = conversation_key
        self.thread_id = thread_id or ""
        self.active_turn_id = ""
        self.running = False
        self.cancelled = False
        self.reset_on_cancel = False
        self.model = ""
        self.reasoning_effort = ""
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
            self.reset_on_cancel = False
            self.model = ""
            self.reasoning_effort = ""
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
        self.write_lock = threading.Lock()
        self.contexts_by_thread = {}
        self.contexts_lock = threading.RLock()
        self.closed = False
        self.reader_thread = None
        self.lifecycle_lock = threading.RLock()
        self.auth_refresh_lock = threading.RLock()

    def start(self):
        with self.lifecycle_lock:
            self._start()

    def _start(self):
        if self.closed:
            raise RuntimeError("app-server stopped")
        if self.process:
            if self.process.poll() is None:
                return
            self.closed = True
            raise RuntimeError("app-server stopped")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home
        self.process = subprocess.Popen(
            [resolve_codex_bin(self.bin_path), "app-server", "--listen", "stdio://"],
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
                "clientInfo": {"name": "local-agent-bridge", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
            timeout_s=15,
        )
        self.notify("initialized")

    def notify(self, method, params=None):
        payload = {"method": method, "params": params or {}}
        assert self.process is not None and self.process.stdin is not None
        with self.write_lock:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def close(self):
        with self.lifecycle_lock:
            self._close()

    def _close(self):
        self.closed = True
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
        if process:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

    def _next_request_id(self):
        with self.pending_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

    def request(self, method, params=None, timeout_s=120):
        try:
            return self._request_once(method, params, timeout_s)
        except JsonRpcError as err:
            # Routing discovery rejects the request before model/tool execution.
            # Retry this specific preflight failure once; never replay a failed
            # turn or an arbitrary authentication error after tools may have run.
            if (not is_workspace_auth_error(err) or method == "initialize"
                    or (method == "account/read" and (params or {}).get("refreshToken"))):
                raise
            with self.auth_refresh_lock:
                try:
                    info = self._request_once("account/read", {"refreshToken": True}, timeout_s) or {}
                    if not info.get("account"):
                        raise RuntimeError("Codex not logged in; cannot refresh workspace routing credentials")
                except Exception as refresh_error:
                    raise JsonRpcError(f"{err}; 令牌刷新失败：{refresh_error}") from refresh_error
                return self._request_once(method, params, timeout_s)

    def _request_once(self, method, params=None, timeout_s=120):
        self.start() if method != "initialize" else None
        request_id = self._next_request_id()
        event = threading.Event()
        slot = {"event": event, "response": None}
        with self.pending_lock:
            self.pending[request_id] = slot
        payload = {"id": request_id, "method": method, "params": params or {}}
        try:
            assert self.process is not None and self.process.stdin is not None
            with self.write_lock:
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
            # A reset may already have changed context.thread_id.
            for thread_id, registered in list(self.contexts_by_thread.items()):
                if registered is context:
                    self.contexts_by_thread.pop(thread_id, None)

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
                if "id" in message and "method" in message:
                    # Approval and elicitation requests need an interactive client.
                    # Fail promptly instead of leaving a WeChat turn waiting forever.
                    try:
                        assert self.process.stdin is not None
                        with self.write_lock:
                            self.process.stdin.write(json.dumps({
                                "id": message["id"],
                                "error": {"code": -32000, "message": "微信通道暂不支持交互式审批"},
                            }, ensure_ascii=False) + "\n")
                            self.process.stdin.flush()
                    except Exception:
                        pass
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
            with self.contexts_lock:
                contexts = list(self.contexts_by_thread.values())
            for context in contexts:
                with context.lock:
                    if context.running:
                        context.finish("failed", "app-server stopped")

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
        self.preserve_existing_instructions = bool(config["codex"].get("preserveExistingInstructions", False))
        self.servers = {}
        # Catalog requests never resume threads. Each running conversation gets
        # its own process, closed after the turn to release Codex's writer lock.
        # Unsubscribe alone can retain the writer for a 30-minute grace period.
        self.run_servers = {}
        self.contexts = {}
        self.lock = threading.RLock()

    def _resolve_bin(self):
        return resolve_codex_bin(self.bin, prefer_desktop=self.preserve_existing_instructions)

    def _server_key(self, codex_account):
        return codex_account.get("codexHome") or "__default__"

    def _server_for_account(self, codex_account):
        key = self._server_key(codex_account)
        with self.lock:
            server = self.servers.get(key)
            if server and (server.closed or (server.process and server.process.poll() is not None)):
                server.close()
                self.servers.pop(key, None)
                server = None
            if not server:
                server = AppServerProcess(self._resolve_bin(), codex_home=codex_account.get("codexHome") or "")
                self.servers[key] = server
            try:
                server.start()
            except Exception:
                server.close()
                self.servers.pop(key, None)
                raise
            return server

    def request_for_account(self, codex_account, method, params=None, timeout_s=30):
        return self._server_for_account(codex_account).request(method, params, timeout_s=timeout_s)

    def close_account(self, codex_account):
        """Discard only this account's catalog process and cached credentials."""
        with self.lock:
            server = self.servers.pop(self._server_key(codex_account), None)
        if server:
            server.close()

    def _new_run_server(self, codex_account):
        return AppServerProcess(self._resolve_bin(), codex_home=codex_account.get("codexHome") or "")

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
            "默认用中文回复，除非用户明确使用其他语言。",
            "回复尽量直接、简洁、可执行。",
            "无法渲染 Markdown，尽量输出纯文本。",
            "",
            "你可以生成本地图片、文件或视频，然后让当前通道发送。",
            "发送本地媒体时，在最终回复中单独写 [[send_image:/真实绝对路径]]、[[send_file:/真实绝对路径]] 或 [[send_video:/真实绝对路径]]。",
            "媒体标记路径必须是真实存在的本地绝对路径。",
            "不要原样输出占位路径，例如 /absolute/path/to/image.png、/Users/bot/.../xxx.png 或 真实绝对路径。",
            "如果 Codex 生成图片后输出 Saved to: file:///Users/.../image.png，也可以直接保留这个 file:// 路径，当前通道会自动发送。",
            "这些标记会被当前通道解析并发送，用户不会看到标记文本。",
            "",
            "如果需要调用可配置媒体生成器，可在 shell 中运行：",
            "python3 -m local_agent_tools media-generate <name> <prompt>",
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

    def _prompt_version(self, instructions=None):
        return prompt_version(instructions if instructions is not None else self._instructions())

    def _thread_params(self, cwd, model="", reasoning_effort="", include_instructions=True, instructions=None,
                       project_id="", thread_source=""):
        params = {
            "cwd": str(cwd),
        }
        if include_instructions:
            params["baseInstructions"] = instructions if instructions is not None else self._instructions()
            params["developerInstructions"] = ""
        if model:
            params["model"] = model
        if reasoning_effort:
            # thread/start and thread/resume accept config overrides, not the
            # turn/start-only `effort` field.
            params["config"] = {"model_reasoning_effort": reasoning_effort}
        if project_id:
            params["projectId"] = project_id
        if thread_source:
            params["threadSource"] = thread_source
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

    def _ensure_thread(
        self,
        server,
        context,
        conversation_key,
        session,
        cwd,
        model,
        reasoning_effort,
        codex_account_name,
        current_prompt_version,
        instructions,
        initial_thread_name="",
    ):
        thread_id = session.get("codexThreadId") or ""
        inject_prompt = (not thread_id) or (
            not self.preserve_existing_instructions
            and session.get("codexAppServerPromptVersion") != current_prompt_version
        )
        if not thread_id:
            server.unregister_context(context)
            context.thread_id = ""
        if thread_id:
            context.thread_id = thread_id
            server.register_context(context)
            try:
                result = server.request(
                    "thread/resume",
                    dict(
                        self._thread_params(
                            cwd,
                            model,
                            reasoning_effort,
                            include_instructions=inject_prompt,
                            instructions=instructions,
                        ),
                        threadId=thread_id,
                        excludeTurns=True,
                    ),
                    timeout_s=30,
                )
                self._capture_model_settings(context, result, model, reasoning_effort)
                return thread_id
            except Exception as err:
                log.warn(f"[app-server] resume failed conversation={conversation_key}: {err}")
                server.unregister_context(context)
                if is_codex_auth_error(err):
                    raise
                if "active writer" in str(err).lower():
                    raise RuntimeError(
                        "所选会话正被桌面 Codex 或其他客户端占用（active writer）。"
                        "请先在占用端结束任务并关闭该会话，再回微信重试；"
                        "如果仍被占用，请退出占用端应用后重试，无需删除或归档会话。"
                        "已保留所选会话，不会另建会话。"
                    ) from err
                missing_pending_thread = (
                    session.get("desktopPendingThread")
                    and ("no rollout found" in str(err).lower() or "not materialized yet" in str(err).lower())
                )
                if self.preserve_existing_instructions and not missing_pending_thread:
                    raise RuntimeError(f"无法续聊所选桌面会话 {thread_id}: {err}") from err
                self.state.reset_session(conversation_key)
                context.thread_id = ""
        result = server.request(
            "thread/start",
            self._thread_params(cwd, model, reasoning_effort, include_instructions=True, instructions=instructions,
                                project_id=session.get("desktopProjectId") or "", thread_source="user"),
            timeout_s=30,
        )
        thread = (result or {}).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise RuntimeError("app-server did not return thread id")
        context.thread_id = thread_id
        self._capture_model_settings(context, result, model, reasoning_effort)
        server.register_context(context)
        self.state.update_session(
            conversation_key,
            codexThreadId=thread_id,
            cwd=cwd,
            codexAccount=codex_account_name,
            codexModel=context.model,
            codexReasoningEffort=context.reasoning_effort,
            codexAppServerPromptVersion=current_prompt_version,
        )
        thread_name = clean_title(initial_thread_name, fallback="", limit=80)
        if thread_name:
            try:
                # Codex Desktop assigns a name to its own new threads. Do the same for
                # threads created by this bridge so they enter session_index.jsonl and
                # are discoverable by the desktop catalog after refresh/reconciliation.
                server.request(
                    "thread/name/set", {"threadId": thread_id, "name": thread_name}, timeout_s=30
                )
            except Exception as err:
                # Naming is catalog metadata; it must not prevent the actual task from running.
                log.warn(f"[app-server] initial thread name failed conversation={conversation_key}: {err}")
        return thread_id

    @staticmethod
    def _capture_model_settings(context, result, model="", effort=""):
        result = result or {}
        thread = result.get("thread") or {}
        context.model = result.get("model") or thread.get("model") or model
        context.reasoning_effort = result.get("reasoningEffort", thread.get("reasoningEffort", effort)) or ""

    def is_running(self, conversation_key):
        with self.lock:
            context = self.contexts.get(conversation_key)
            return conversation_key in self.run_servers or bool(context and context.running)

    def active_runs(self):
        default_cwd = self.config["codex"]["workingDirectory"]
        with self.lock:
            contexts = [
                (conversation_key, context)
                for conversation_key, context in self.contexts.items()
                if context and (context.running or conversation_key in self.run_servers)
            ]
        runs = []
        for conversation_key, _context in sorted(contexts, key=lambda item: item[0]):
            session = self.state.get_session(conversation_key, default_cwd, default_codex_account(self.config))
            server = self.run_servers.get(conversation_key)
            process = getattr(server, "process", None) if server else None
            model_selection = resolve_session_model(self.config, session)
            runs.append(
                {
                    "agent": "codex",
                    "conversationKey": conversation_key,
                    "pid": getattr(process, "pid", None),
                    "model": _context.model or model_selection.get("model") or "",
                    "effort": _context.reasoning_effort or model_selection.get("reasoningEffort") or "",
                }
            )
        return runs

    def run(self, conversation_key, user_message, retry_on_resume_error=True):
        default_cwd = self.config["codex"]["workingDirectory"]
        session = self.state.get_session(conversation_key, default_cwd, default_codex_account(self.config))
        cwd = session.get("cwd") or default_cwd
        codex_account = resolve_session_codex_account(self.config, session)
        codex_account_name = codex_account.get("name") or default_codex_account(self.config)
        model_selection = resolve_session_model(self.config, session)
        selected_model = model_selection.get("model") or ""
        selected_reasoning = model_selection.get("reasoningEffort") or ""
        instructions = self._instructions()
        current_prompt_version = self._prompt_version(instructions)
        thread_id = session.get("codexThreadId") or ""
        with self.lock:
            if conversation_key in self.run_servers or any(
                thread_id and self.contexts[key].thread_id == thread_id
                and self._server_key({"codexHome": running_server.codex_home}) == self._server_key(codex_account)
                for key, running_server in self.run_servers.items()
            ):
                raise RuntimeError("该会话已有运行中任务或正在释放占用，请稍后重试。")
            context = self._context(conversation_key, thread_id)
            context.thread_id = thread_id
            server = self._new_run_server(codex_account)
            self.run_servers[conversation_key] = server
            context.start_turn("")
        try:
            server.start()
            if context.cancelled:
                raise CodexCancelled("Codex 已取消")
            thread_id = self._ensure_thread(
                server,
                context,
                conversation_key,
                session,
                cwd,
                selected_model,
                selected_reasoning,
                codex_account_name,
                current_prompt_version,
                instructions,
                user_message,
            )
            if context.cancelled:
                raise CodexCancelled("Codex 已取消")
            self.state.update_session(conversation_key, codexModel=context.model,
                                      codexReasoningEffort=context.reasoning_effort)
            log.info(
                f"[app-server] start turn conversation={conversation_key} account={codex_account_name} "
                f"model={context.model or selected_model or 'default'} "
                f"reasoning={context.reasoning_effort or selected_reasoning or 'default'} cwd={cwd}"
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
            pending_model = session.get("desktopModelOverride")
            if pending_model:
                with self.state.lock:
                    if self.state.get_session(
                        conversation_key, default_cwd, default_codex_account(self.config)
                    ).get("desktopModelOverride") == pending_model:
                        # Do not erase a new /model command received mid-turn.
                        # After acceptance, inherit future native changes again.
                        self.state.update_session(conversation_key, desktopModelOverride={})
            with context.lock:
                if context.running and not context.active_turn_id:
                    context.active_turn_id = turn_id
            if not context.completed.wait(self.timeout_ms / 1000):
                self.cancel(conversation_key, reset_session=False)
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
                codexModel=context.model,
                codexReasoningEffort=context.reasoning_effort,
                codexAppServerPromptVersion=current_prompt_version,
            )
            return context.text()
        except Exception as err:
            context.finish("interrupted" if context.cancelled else "failed", str(err))
            raise
        finally:
            # Closing only this turn's worker releases the native writer lock,
            # including when resume/start, cancellation or a timeout failed.
            # Never close the shared catalog process or another running thread.
            try:
                server.close()
            finally:
                server.unregister_context(context)
                with self.lock:
                    try:
                        with context.lock:
                            if context.reset_on_cancel:
                                self.state.reset_session(conversation_key)
                                context.thread_id = ""
                            context.running = False
                            context.active_turn_id = ""
                    finally:
                        self.run_servers.pop(conversation_key, None)

    def steer(self, conversation_key, user_message):
        with self.lock:
            server = self.run_servers.get(conversation_key)
            context = self.contexts.get(conversation_key)
        if not server or not context:
            return False
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

    def cancel(self, conversation_key, reset_session=True):
        with self.lock:
            server = self.run_servers.get(conversation_key)
            context = self.contexts.get(conversation_key)
            if context and server:
                with context.lock:
                    thread_id = context.thread_id
                    turn_id = context.active_turn_id
                    context.cancelled = True
                    context.reset_on_cancel = context.reset_on_cancel or reset_session
            else:
                context = None
            if reset_session:
                self.state.reset_session(conversation_key)
        if context is None:
            return False
        killed = False
        if thread_id and turn_id:
            try:
                server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout_s=10)
                killed = True
            except Exception as err:
                log.warn(f"[app-server] interrupt failed conversation={conversation_key}: {err}")
        with self.lock:
            if self.run_servers.get(conversation_key) is server:
                context.finish("interrupted", "")
        return killed

    def terminate_all(self):
        with self.lock:
            servers = list(self.servers.values()) + list(self.run_servers.values())
            self.servers.clear()
        for server in servers:
            server.close()
