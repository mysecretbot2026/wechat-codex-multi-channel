import concurrent.futures
import contextlib
import os
import re
import threading
import time
import uuid
from pathlib import Path

from . import logging as log
from .actions import execute_actions, extract_actions
from .agent_runner import AgentRunnerManager
from .agents import normalize_agent, resolve_session_agent
from .claude_accounts import (
    adjacent_claude_account,
    claude_account_names,
    default_claude_account,
    find_claude_account,
    get_claude_account,
    list_claude_accounts,
    resolve_session_claude_account,
)
from .claude_cli import ClaudeCliRunner
from .claude_models import (
    claude_model_options,
    find_claude_model_option,
    format_claude_model_option,
    resolve_session_claude_model,
)
from .claude_usage import (
    format_claude_admin_usage,
    format_claude_usage,
    format_claude_usage_all,
    read_claude_admin_usage,
    read_claude_auth_status,
    read_claude_usage,
)
from .codex_app_server import CodexAppServerRunner
from .codex_accounts import (
    adjacent_codex_account,
    codex_account_names,
    default_codex_account,
    find_codex_account,
    get_codex_account,
    list_codex_accounts,
    resolve_session_codex_account,
)
from .codex_cli import CodexCancelled, CodexCliRunner
from .codex_device_login import CodexDeviceLoginManager, cached_email, login_status
from .desktop_codex import DesktopCodexCatalog, project_for_thread
from .codex_models import find_model_option, format_model_option, model_options, resolve_session_model
from .codex_usage import format_codex_usage, format_codex_usage_all, read_codex_usage
from .codex_update import CodexUpdateManager, UPDATE_HELP, format_update_result, make_update_plan, parse_update_command
from .config import PROJECT_DIR
from .login import login_with_qr, render_qr_png
from .media import send_local_media
from .media_outbox import media_outbox_path, read_and_clear_media_outbox
from .session_discovery import (
    format_session_time,
    list_claude_sessions,
    list_codex_sessions,
    short_session_id,
    sort_sessions,
)
from .state import StateStore
from .util import markdown_to_plain_text, split_text
from .wechat import MESSAGE_TYPE_USER, TYPING_STATUS_CANCEL, TYPING_STATUS_TYPING, WechatClient, extract_text


class MultiWechatCodexService:
    WORKSPACE_NAME_RE = re.compile(r"^[^\W_][\w.-]{0,63}$")
    DESKTOP_RUN_MARKER = ":desktop-thread:"

    def __init__(self, config):
        self.config = config
        self.state = StateStore(
            config["stateDir"],
            save_debounce_ms=int(config.get("state", {}).get("saveDebounceMs") or 0),
        )
        self.codex = AgentRunnerManager(
            config,
            self.state,
            codex_factory=lambda cfg, state: self._create_codex_runner(cfg),
            claude_factory=lambda cfg, state: self._create_claude_runner(cfg),
            desktop_factory=lambda cfg, state: self._create_desktop_runner(cfg),
        )
        self.desktop = DesktopCodexCatalog(self.codex.runner_for("desktop"))
        self.codex_device_login = CodexDeviceLoginManager(config["codex"].get("bin") or "codex")
        self.updates = CodexUpdateManager(config)
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=int(config.get("concurrency", {}).get("maxWorkers") or 4)
        )
        self.command_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=int(config.get("concurrency", {}).get("commandWorkers") or 2)
        )
        self.media_semaphore = threading.Semaphore(
            int(config.get("media", {}).get("maxConcurrentTransfers") or 1)
        )
        self.stop_event = threading.Event()
        self.conversation_locks = {}
        self.conversation_locks_guard = threading.Lock()
        self.pending_guidance = {}
        self.pending_guidance_guard = threading.Lock()
        self.session_selection_cache = {}
        self.desktop_project_cache = {}
        self.desktop_thread_cache = {}
        self.desktop_account_selection = {}
        self.desktop_delete_pending = {}
        self.session_delete_pending = {}
        self.monitor_accounts = set()
        self.monitor_disabled_accounts = set()
        self._model_options = None
        self.desktop_model_options_cache = {}
        self._claude_model_options = None

    def _create_codex_runner(self, config):
        runner = str(config.get("codex", {}).get("runner") or "exec").strip().lower()
        if runner in {"app-server", "appserver", "server"}:
            return CodexAppServerRunner(config, self.state)
        return CodexCliRunner(config, self.state)

    def _create_claude_runner(self, config):
        return ClaudeCliRunner(config, self.state)

    def _create_desktop_runner(self, config):
        desktop_config = dict(config)
        desktop_config["codex"] = dict(config["codex"])
        bundled_bin = Path(
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/"
            "CodexCLI.app/Contents/MacOS/codex"
        )
        desktop_config["codex"].update(
            bin=config["codex"].get("desktopBin") or (
                str(bundled_bin) if bundled_bin.is_file() else config["codex"].get("bin") or "codex"
            ),
            bypassApprovalsAndSandbox=False,
            preserveExistingInstructions=True,
            model="",
            reasoningEffort="",
        )
        return CodexAppServerRunner(desktop_config, self.state)

    def _api_for_account(self, account):
        return WechatClient(
            base_url=account.get("baseUrl") or self.config["wechat"]["baseUrl"],
            token=account.get("token"),
            route_tag=self.config["wechat"].get("routeTag"),
        )

    def start(self):
        accounts = self.state.list_accounts()
        if not accounts:
            raise RuntimeError("没有微信账号。请先运行 python3 -m wechat_codex_multi add-account")
        log.info(f"starting {len(accounts)} account monitor(s), maxWorkers={self.config['concurrency']['maxWorkers']}")
        for account in accounts:
            self._ensure_account_session(account)
            self._start_account_monitor(account)
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("stopping service")
        finally:
            self.stop()

    def stop(self):
        self.stop_event.set()
        self.codex_device_login.stop()
        self.codex.terminate_all()
        self.command_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.state.flush()

    def _start_account_monitor(self, account):
        account_id = account["accountId"]
        if account_id in self.monitor_accounts:
            return
        self.monitor_disabled_accounts.discard(account_id)
        self.monitor_accounts.add(account_id)
        thread = threading.Thread(target=self._monitor_account, args=(account,), daemon=True)
        thread.start()

    def _monitor_account(self, account):
        account_id = account["accountId"]
        client = self._api_for_account(account)
        get_updates_buf = account.get("getUpdatesBuf") or ""
        log.info(f"monitor started accountId={account_id}")
        failures = 0
        try:
            while not self.stop_event.is_set() and account_id not in self.monitor_disabled_accounts:
                try:
                    response = client.get_updates(get_updates_buf)
                    if account_id in self.monitor_disabled_accounts:
                        break
                    failures = 0
                    if response.get("get_updates_buf"):
                        get_updates_buf = response["get_updates_buf"]
                        self.state.update_account(account_id, getUpdatesBuf=get_updates_buf)
                    for msg in response.get("msgs") or []:
                        if msg.get("message_type") != MESSAGE_TYPE_USER:
                            continue
                        self._submit_message(account, msg)
                except Exception as err:
                    failures += 1
                    log.error(f"monitor error accountId={account_id}: {err}")
                    time.sleep(30 if failures >= 3 else 2)
        finally:
            self.monitor_accounts.discard(account_id)
            log.info(f"monitor stopped accountId={account_id}")

    def _submit_message(self, account, msg):
        account_id = account["accountId"]
        user_id = msg.get("from_user_id")
        if not user_id:
            return
        context_token = msg.get("context_token")
        if context_token:
            self.state.set_context_token(account_id, user_id, context_token)
        text = extract_text(msg, download_media=False)
        if not text:
            return
        if not self._is_allowed(user_id):
            log.warn(f"user not allowed: {user_id}")
            return
        base_conversation_key = self.state.conversation_key(account_id, user_id)
        log.info(f"inbound accountId={account_id} user={user_id} conversation={base_conversation_key} len={len(text)}")
        if self._is_command(text) and not self._is_workspace_run_command(text):
            self.command_executor.submit(self._handle_message_safe, account, user_id, base_conversation_key, text, None)
        else:
            self.executor.submit(self._handle_message_safe, account, user_id, base_conversation_key, text, msg)

    def _conversation_lock(self, key):
        with self.conversation_locks_guard:
            if key not in self.conversation_locks:
                self.conversation_locks[key] = threading.Lock()
            return self.conversation_locks[key]

    def _handle_message_safe(self, account, user_id, base_conversation_key, text, msg=None):
        conversation_key = base_conversation_key
        try:
            conversation_key = self._conversation_key_for_text(base_conversation_key, text)
            if self._can_run_without_conversation_lock(text):
                self._handle_message(account, user_id, base_conversation_key, text, conversation_key)
                return
            if self.config.get("concurrency", {}).get("perConversationSerial", True):
                lock = self._conversation_lock(conversation_key)
                if not lock.acquire(blocking=False):
                    interrupt_action, interrupt_text = self._parse_interrupt_command(text)
                    agent = resolve_session_agent(self.config, self._get_session(conversation_key))
                    agent_label = self._agent_label(agent)
                    if text.strip() == "/reset":
                        killed = self._cancel_runner(conversation_key, reset_session=True)
                        if self.DESKTOP_RUN_MARKER in conversation_key:
                            self.state.reset_session(self._desktop_parent_key(conversation_key), agent="codex")
                            self.state.update_session(self._desktop_parent_key(conversation_key), desktopPendingId="")
                        self._clear_pending_guidance(conversation_key)
                        message = f"已取消正在运行的 {agent_label} 并重置当前工作区。" if killed else "已重置当前工作区。"
                        self._send_text(account, user_id, message)
                        return
                    if interrupt_action:
                        self._clear_pending_guidance(conversation_key)
                        killed = self._cancel_runner(conversation_key, reset_session=False)
                        if interrupt_text:
                            self._send_text(account, user_id, f"已中断当前 {agent_label} 任务，保留当前会话，稍后按新任务继续。")
                            self._schedule_after_lock(account, user_id, base_conversation_key, conversation_key, interrupt_text)
                        else:
                            message = f"已中断当前 {agent_label} 任务，已保留当前会话。" if killed else f"当前没有运行中的 {agent_label} 任务，当前会话保持不变。"
                            self._send_text(account, user_id, message)
                        return
                    if self._is_command(text) and not self._is_workspace_run_command(text):
                        self._send_text(account, user_id, "当前任务运行中，命令不会作为引导处理。可发送 /status、/usage、/interrupt、/reset 等命令。")
                        return
                    if msg is not None:
                        text = self._extract_message_text(account, user_id, msg)
                        if not text:
                            return
                    guidance_text = self._guidance_text(text)
                    if not guidance_text:
                        self._send_text(account, user_id, "补充引导不能为空。")
                        return
                    if hasattr(self.codex, "steer") and self.codex.steer(conversation_key, guidance_text):
                        self._send_text(account, user_id, f"已发送引导，{agent_label} 会在当前任务中调整方向。")
                        return
                    self._append_pending_guidance(conversation_key, guidance_text)
                    self._send_text(
                        account,
                        user_id,
                        "已收到补充引导。当前任务结束后会继续处理。\n"
                        "需要立刻放弃当前任务：/interrupt\n"
                        "需要中断并改做新任务：/interrupt <新任务>",
                    )
                    return
                try:
                    if msg is not None:
                        text = self._extract_message_text(account, user_id, msg)
                        if not text:
                            return
                    self._handle_message(account, user_id, base_conversation_key, text, conversation_key)
                finally:
                    lock.release()
            else:
                if msg is not None:
                    text = self._extract_message_text(account, user_id, msg)
                    if not text:
                        return
                self._handle_message(account, user_id, base_conversation_key, text, conversation_key)
        except CodexCancelled:
            log.info(f"handler cancelled conversation={base_conversation_key}")
        except Exception as err:
            log.error(f"handler error conversation={base_conversation_key}: {err}")
            if self.DESKTOP_RUN_MARKER in conversation_key:
                run_session = self._get_session(conversation_key)
                turn_id = ""
                try:
                    codex_account = resolve_session_codex_account(self.config, run_session)
                    turn_id = self.desktop.latest_result(codex_account, run_session.get("codexThreadId") or "")["turnId"]
                except Exception:
                    pass
                self.state.update_session(
                    conversation_key, desktopLastError=str(err), desktopLastErrorTurnId=turn_id,
                )
                with self._desktop_selection_lock(conversation_key):
                    if self._desktop_is_selected(conversation_key):
                        self._send_text(account, user_id, f"执行失败：{err}")
                return
            self._send_text(account, user_id, f"执行失败：{err}")

    def _extract_message_text(self, account, user_id, msg):
        media_dir = self.state.state_dir / "inbound_media" / account["accountId"] / user_id
        with self.media_semaphore:
            return extract_text(msg, media_dir=media_dir)

    @staticmethod
    def _is_command(text):
        command = text.strip()
        return command.startswith("/")

    @staticmethod
    def _workspace_run_parts(text):
        parts = text.strip().split(maxsplit=3)
        if len(parts) >= 2 and parts[0] == "/ws" and parts[1].lower() == "run":
            return parts
        return []

    @classmethod
    def _is_workspace_run_command(cls, text):
        return bool(cls._workspace_run_parts(text))

    def _conversation_key_for_text(self, base_conversation_key, text):
        parts = self._workspace_run_parts(text)
        if len(parts) >= 3:
            workspace_key = self.state.workspace_conversation_key(base_conversation_key, parts[2])
            return self._desktop_execution_key(workspace_key)
        active = self.state.get_active_workspace(base_conversation_key)
        workspace_key = self.state.workspace_conversation_key(base_conversation_key, active)
        first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        if not first.startswith("/") or first in {"/status", "/model", "/models", "/guide", "/interrupt", "/cancel", "/reset"}:
            return self._desktop_execution_key(workspace_key)
        return workspace_key

    @classmethod
    def _desktop_parent_key(cls, conversation_key):
        return conversation_key.split(cls.DESKTOP_RUN_MARKER, 1)[0]

    def _desktop_selection_lock(self, conversation_key):
        parent = self._desktop_parent_key(conversation_key)
        base_key = ":".join(parent.split(":", 2)[:2])
        return self._conversation_lock(f"{base_key}:desktop-selection")

    def _desktop_is_selected(self, run_key):
        parent = self._desktop_parent_key(run_key)
        run_session = self._get_session(run_key)
        selected = self._get_session(parent)
        if (selected.get("codexClient") != "desktop"
                or selected.get("codexThreadId") != run_session.get("codexThreadId")
                or selected.get("codexAccount") != run_session.get("codexAccount")):
            return False
        parts = parent.split(":", 2)
        if len(parts) < 2:
            return True
        base_key = ":".join(parts[:2])
        active = self.state.get_active_workspace(base_key)
        return self.state.workspace_conversation_key(base_key, active) == parent

    def _desktop_execution_key(self, workspace_key):
        if self.DESKTOP_RUN_MARKER in workspace_key:
            return workspace_key
        session = self._get_session(workspace_key)
        thread_id = session.get("codexThreadId") or ""
        if session.get("codexClient") != "desktop":
            return workspace_key
        account_name = session.get("codexAccount") or default_codex_account(self.config)
        if thread_id:
            run_key = f"{workspace_key}{self.DESKTOP_RUN_MARKER}{account_name}:{thread_id}"
        else:
            pending_id = session.get("desktopPendingId") or uuid.uuid4().hex
            if not session.get("desktopPendingId"):
                self.state.update_session(workspace_key, desktopPendingId=pending_id)
            run_key = f"{workspace_key}{self.DESKTOP_RUN_MARKER}{account_name}:pending-{pending_id}"
            thread_id = self._get_session(run_key).get("codexThreadId") or ""
        self.state.update_session(
            run_key, agent="codex", codexClient="desktop", codexThreadId=thread_id,
            codexAccount=account_name, cwd=session.get("cwd") or self.config["codex"]["workingDirectory"],
            desktopProjectId=session.get("desktopProjectId") or "",
            desktopPendingThread=bool(session.get("desktopPendingId")),
        )
        return run_key

    def _promote_pending_desktop_thread(self, run_key):
        if ":pending-" not in run_key:
            return
        parent = self._desktop_parent_key(run_key)
        selected = self._get_session(parent)
        pending_id = selected.get("desktopPendingId") or ""
        run_session = self._get_session(run_key)
        if (pending_id and run_key.endswith(f":pending-{pending_id}")
                and selected.get("codexClient") == "desktop"
                and not selected.get("codexThreadId")
                and run_session.get("codexThreadId")):
            self.state.update_session(parent, codexThreadId=run_session["codexThreadId"], desktopPendingId="")
            resolved_key = self._desktop_execution_key(parent)
            self.state.update_session(
                resolved_key, codexModel=run_session.get("codexModel") or "",
                codexReasoningEffort=run_session.get("codexReasoningEffort") or "",
                desktopModelOverride=run_session.get("desktopModelOverride") or {},
            )

    @staticmethod
    def _can_run_without_conversation_lock(text):
        command = text.strip()
        first = command.split()[0] if command else ""
        if first == "/ws":
            parts = command.split(maxsplit=2)
            return len(parts) < 2 or parts[1].lower() != "run"
        return first in {
            "/help",
            "/accounts",
            "/users",
            "/user",
            "/active",
            "/status",
            "/usage",
            "/agents",
            "/agent",
            "/account",
            "/codex-accounts",
            "/codex",
            "/codex-login",
            "/claude-accounts",
            "/claude",
            "/model",
            "/models",
            "/sessions",
            "/session",
            "/desktop",
            "/d-projects",
            "/d-sessions",
            "/d-session",
            "/d-account",
            "/new-project",
            "/n-p",
            "/d-new-project",
            "/d-n-p",
            "/d-p-n",
            "/d-project",
            "/cwd",
            "/runner",
            "/update",
            "/codex-update",
            "/d-update",
            "/restart",
        }

    @staticmethod
    def _agent_label(agent):
        return "Claude" if str(agent or "").lower() == "claude" else "Codex"

    @staticmethod
    def _account_nickname(account):
        return str((account or {}).get("nickname") or "").strip()

    def _format_accounts(self):
        accounts = self.state.list_accounts()
        if not accounts:
            return "没有已连接用户。"
        lines = ["已连接用户："]
        for index, item in enumerate(accounts, start=1):
            nickname = self._account_nickname(item) or f"用户{index}"
            user_id = item.get("userId") or "-"
            lines.append(f"{index}. {nickname} accountId={item.get('accountId')} userId={user_id}")
        lines.extend(
            [
                "",
                "添加：/login [昵称]",
                "改名：/user rename <当前昵称|accountId|编号> <新昵称>",
                "删除：/user delete <昵称|accountId|编号>",
            ]
        )
        return "\n".join(lines)

    def _handle_user_command(self, account, user_id, command):
        parts = command.split(maxsplit=3)
        if command == "/users" or command == "/user" or (len(parts) >= 2 and parts[1].lower() in {"list", "ls"}):
            self._send_text(account, user_id, self._format_accounts())
            return
        action = parts[1].lower() if len(parts) >= 2 else ""
        if action in {"rename", "nick", "nickname"}:
            if not self._is_admin(user_id):
                self._send_text(account, user_id, "只有 adminUsers 可以修改用户昵称。")
                return
            if len(parts) < 4:
                self._send_text(account, user_id, "用法：/user rename <当前昵称|accountId|编号> <新昵称>")
                return
            selector = parts[2].strip()
            nickname = parts[3].strip()
            try:
                renamed = self.state.rename_account(selector, nickname)
            except ValueError as err:
                self._send_text(account, user_id, str(err))
                return
            if not renamed:
                self._send_text(account, user_id, f"没有找到用户: {selector}")
                return
            self._send_text(
                account,
                user_id,
                f"已修改用户昵称: {selector} -> {renamed.get('nickname')}\naccountId: {renamed.get('accountId')}",
            )
            return
        if action in {"delete", "remove", "del", "rm"}:
            if not self._is_admin(user_id):
                self._send_text(account, user_id, "只有 adminUsers 可以删除用户。")
                return
            if len(parts) < 3:
                self._send_text(account, user_id, "用法：/user delete <昵称|accountId|编号>")
                return
            selector = parts[2].strip()
            target = self.state.find_account(selector)
            if not target:
                self._send_text(account, user_id, f"没有找到用户: {selector}")
                return
            reply_context_token = self.state.get_context_token(account["accountId"], user_id)
            account_id = target.get("accountId")
            prefix = f"{account_id}:"
            for run in self._active_runs():
                conversation_key = str(run.get("conversationKey") or "")
                if conversation_key.startswith(prefix):
                    self._cancel_runner(conversation_key, reset_session=True)
                    self._clear_pending_guidance(conversation_key)
            deleted = self.state.delete_account(account_id)
            if not deleted:
                self._send_text(account, user_id, f"没有找到用户: {selector}")
                return
            self.monitor_disabled_accounts.add(account_id)
            message = f"已删除用户: {deleted.get('nickname')}\naccountId: {account_id}"
            if account_id == account.get("accountId"):
                self._send_text_with_context_token(account, user_id, reply_context_token, message)
            else:
                self._send_text(account, user_id, message)
            return
        self._send_text(
            account,
            user_id,
            "\n".join(
                [
                    "用户命令：",
                    "/users 查看已连接用户",
                    "/login [昵称] 新增用户",
                    "/user rename <当前昵称|accountId|编号> <新昵称>",
                    "/user delete <昵称|accountId|编号>",
                ]
            ),
        )

    def _active_runs(self):
        if hasattr(self.codex, "active_runs"):
            return self.codex.active_runs()
        return []

    @staticmethod
    def _format_active_runs(runs):
        cleaned = []
        for run in runs or []:
            conversation_key = str(run.get("conversationKey") or run.get("conversation") or "").strip()
            if not conversation_key:
                continue
            agent = str(run.get("agent") or "codex").strip().lower() or "codex"
            cleaned.append(
                {
                    "agent": agent,
                    "conversationKey": conversation_key,
                    "pid": run.get("pid"),
                    "model": str(run.get("model") or "").strip(),
                    "effort": str(run.get("effort") or "").strip(),
                }
            )
        if not cleaned:
            return "当前没有用户正在交互中。"

        agent_order = ["claude", "codex"]
        agent_order.extend(sorted({item["agent"] for item in cleaned if item["agent"] not in agent_order}))
        lines = []
        for agent in agent_order:
            entries = sorted(
                [item for item in cleaned if item["agent"] == agent],
                key=lambda item: item["conversationKey"],
            )
            if not entries:
                continue
            if lines:
                lines.append("")
            lines.append(f"{agent} ({len(entries)} 个)：")
            for item in entries:
                details = []
                if item.get("pid") is not None:
                    details.append(f"pid={item.get('pid')}")
                if item.get("model"):
                    details.append(f"model={item.get('model')}")
                if item.get("effort"):
                    details.append(f"effort={item.get('effort')}")
                suffix = f" ({' '.join(details)})" if details else ""
                lines.append(f"- {item['conversationKey']}{suffix}")
        lines.append(f"共 {len(cleaned)} 个用户正在交互中")
        return "\n".join(lines)

    def _handle_message(self, account, user_id, base_conversation_key, text, conversation_key=None):
        conversation_key = conversation_key or base_conversation_key
        command = text.strip()
        if command and command.split(maxsplit=1)[0] in {"/update", "/codex-update", "/d-update"}:
            self._handle_update_command(account, user_id, command)
            return
        if command == "/help" or command == "/help all":
            self._send_text(account, user_id, self._help_text(account["accountId"], full=command.endswith(" all")))
            return
        if command == "/codex-login" or command.startswith("/codex-login "):
            self._handle_codex_login(account, user_id, conversation_key, command)
            return
        if command == "/accounts":
            self._send_text(account, user_id, self._format_accounts())
            return
        if command == "/users" or command == "/user" or command.startswith("/user "):
            self._handle_user_command(account, user_id, command)
            return
        if command == "/active":
            self._send_text(account, user_id, self._format_active_runs(self._active_runs()))
            return
        if command == "/ws" or command.startswith("/ws "):
            self._handle_workspace_command(account, user_id, base_conversation_key, conversation_key, command)
            return
        if command == "/status":
            conversation_key = self._desktop_execution_key(conversation_key)
            session = self._get_session(conversation_key)
            agent = resolve_session_agent(self.config, session)
            codex_account = resolve_session_codex_account(self.config, session)
            claude_account = resolve_session_claude_account(self.config, session)
            model_selection = self._current_codex_model(conversation_key, session)
            claude_model = resolve_session_claude_model(self.config, session)
            workspace_name = self._workspace_name_from_key(base_conversation_key, conversation_key)
            session_id = session.get("claudeSessionId") if agent == "claude" else session.get("codexThreadId")
            lines = [
                f"accountId: {account['accountId']}",
                f"conversation: {conversation_key}",
                f"workspace: {workspace_name}",
                f"cwd: {session.get('cwd')}",
                f"agent: {agent}",
                f"running: {str(self.codex.is_running(conversation_key)).lower()}",
                f"sessionId: {session_id or '-'}",
            ]
            if agent == "claude":
                lines.extend(
                    [
                        f"claudeAccount: {claude_account.get('name')}",
                        f"claudeConfigDir: {claude_account.get('claudeConfigDir')}",
                        f"claudeModel: {claude_model.get('model') or 'default'}",
                        f"effort: {claude_model.get('effort') or 'default'}",
                        f"claudeSessionId: {(session.get('claudeSessionId') or '')[:12]}",
                    ]
                )
                try:
                    auth = read_claude_auth_status(
                        self.config.get("claude", {}).get("bin") or "claude",
                        timeout_s=int(self.config.get("claude", {}).get("authStatusTimeoutSeconds") or 5),
                        claude_config_dir=claude_account.get("claudeConfigDir") or "",
                    )
                    lines.append(f"claudeLogin: {'logged in' if auth.get('loggedIn') else 'not logged in'}")
                    if auth.get("email"):
                        lines.append(f"claudeEmail: {auth.get('email')}")
                    if auth.get("orgName"):
                        lines.append(f"claudeOrg: {auth.get('orgName')}")
                    if auth.get("authMethod"):
                        lines.append(f"claudeAuthMethod: {auth.get('authMethod')}")
                    if auth.get("apiProvider"):
                        lines.append(f"claudeApiProvider: {auth.get('apiProvider')}")
                    if auth.get("apiKeySource"):
                        lines.append(f"claudeApiKeySource: {auth.get('apiKeySource')}")
                    if auth.get("error"):
                        lines.append(f"claudeAuthError: {auth.get('error')}")
                except Exception as err:
                    lines.append(f"claudeAuthError: {err}")
            else:
                lines.extend(
                    [
                        f"codexAccount: {codex_account.get('name')}",
                        f"codexHome: {codex_account.get('codexHome')}",
                        f"codexRunner: {'desktop-app-server' if session.get('codexClient') == 'desktop' else self.config.get('codex', {}).get('runner') or 'exec'}",
                        f"codexModel: {model_selection.get('model') or 'default'}",
                        f"reasoning: {model_selection.get('reasoningEffort') or 'default'}",
                        f"codexThreadId: {session.get('codexThreadId') or '-'}",
                    ]
                )
                if model_selection.get("source"):
                    lines.append(f"modelSource: {model_selection['source']}")
                if model_selection.get("warning"):
                    lines.append(f"modelWarning: {model_selection['warning']}")
            lines.append(
                "accounts: "
                + ", ".join(
                    f"{self._account_nickname(a) or a['accountId']}({a['accountId']})"
                    for a in self.state.list_accounts()
                )
            )
            self._send_text(account, user_id, "\n".join(lines))
            return
        if command == "/sessions" or command.startswith("/sessions "):
            self._handle_sessions_command(account, user_id, conversation_key, command[len("/sessions"):].strip())
            return
        for prefix in ("/new-project", "/n-p", "/d-new-project", "/d-n-p", "/d-p-n"):
            if command == prefix or command.startswith(prefix + " "):
                self._handle_new_project_command(
                    account, user_id, base_conversation_key, conversation_key,
                    command[len(prefix):].strip(), desktop=prefix.startswith("/d-"),
                )
                return
        if command == "/d-projects":
            self._handle_desktop_command(account, user_id, conversation_key, "projects")
            return
        if command == "/d-project" or command.startswith("/d-project "):
            self._handle_desktop_command(account, user_id, conversation_key,
                                         "project " + command[len("/d-project"):].strip())
            return
        if command == "/d-sessions" or command.startswith("/d-sessions "):
            arg = command[len("/d-sessions"):].strip()
            parts = arg.split(maxsplit=1)
            action = "archived" if parts and parts[0].lower() == "archived" else "chats"
            remainder = parts[1] if action == "archived" and len(parts) > 1 else ("" if action == "archived" else arg)
            self._handle_desktop_command(account, user_id, conversation_key, f"{action} {remainder}".strip())
            return
        if command == "/d-session" or command.startswith("/d-session "):
            self._handle_desktop_command(account, user_id, conversation_key, command[len("/d-session"):].strip())
            return
        if command == "/d-account" or command.startswith("/d-account "):
            self._handle_desktop_command(account, user_id, conversation_key, "account " + command[len("/d-account"):].strip())
            return
        if command == "/desktop" or command.startswith("/desktop "):
            self._handle_desktop_command(account, user_id, conversation_key, command[len("/desktop"):].strip())
            return
        if command == "/session" or command.startswith("/session "):
            self._handle_session_command(account, user_id, conversation_key, command[len("/session"):].strip())
            return
        usage_parts = command.split()
        if usage_parts and usage_parts[0] == "/usage":
            message = self._usage_text(conversation_key, usage_parts[1:])
            self._send_text(account, user_id, message)
            return
        if command == "/agents":
            self._send_text(account, user_id, self._format_agents(conversation_key))
            return
        if command == "/agent" or command.startswith("/agent "):
            selector = command[len("/agent"):].strip()
            self._handle_agent_switch(account, user_id, conversation_key, selector)
            return
        if command == "/account" or command.startswith("/account "):
            selector = command[len("/account"):].strip()
            self._handle_current_agent_account_switch(account, user_id, conversation_key, selector)
            return
        if command == "/codex-accounts":
            current = resolve_session_codex_account(self.config, self._get_session(conversation_key)).get("name")
            lines = ["Codex 账号："]
            for index, codex_account in enumerate(list_codex_accounts(self.config), start=1):
                marker = "*" if codex_account.get("name") == current else "-"
                lines.append(f"{marker} {index}. {codex_account.get('name')}")
                lines.append(f"   {codex_account.get('codexHome')}")
            lines.extend(
                [
                    "",
                    "切换：/codex <编号或名称>",
                    "例如：/codex 2 或 /codex backup",
                    "下一个：/codex next",
                ]
            )
            self._send_text(account, user_id, "\n".join(lines))
            return
        if command == "/codex" or command.startswith("/codex ") or command.startswith("/codex-use"):
            selector = command[len("/codex"):].strip() if command.startswith("/codex ") else command[len("/codex-use"):].strip()
            self._handle_codex_switch(account, user_id, conversation_key, selector)
            return
        if command == "/claude-accounts":
            current = resolve_session_claude_account(self.config, self._get_session(conversation_key)).get("name")
            lines = ["Claude 账号："]
            for index, claude_account in enumerate(list_claude_accounts(self.config), start=1):
                marker = "*" if claude_account.get("name") == current else "-"
                lines.append(f"{marker} {index}. {claude_account.get('name')}")
                lines.append(f"   {claude_account.get('claudeConfigDir')}")
            lines.extend(
                [
                    "",
                    "切换：/claude <编号或名称>",
                    "例如：/claude 2 或 /claude work",
                    "下一个：/claude next",
                ]
            )
            self._send_text(account, user_id, "\n".join(lines))
            return
        if command == "/claude" or command.startswith("/claude "):
            selector = command[len("/claude"):].strip()
            self._handle_claude_switch(account, user_id, conversation_key, selector)
            return
        if command == "/model" or command == "/models" or command.startswith("/model "):
            selector = command[len("/model"):].strip() if command.startswith("/model ") else ""
            self._handle_model_switch(account, user_id, conversation_key, selector, list_only=command == "/models")
            return
        if command == "/runner" or command.startswith("/runner "):
            selector = command[len("/runner"):].strip()
            self._handle_runner_switch(account, user_id, selector)
            return
        if command == "/login" or command.startswith("/login "):
            if not self._is_admin(user_id):
                self._send_text(account, user_id, "只有 adminUsers 可以通过微信触发 /login。")
                return
            nickname = command[len("/login"):].strip()
            if nickname:
                error = StateStore.validate_account_nickname(nickname)
                if error:
                    self._send_text(account, user_id, error)
                    return
                if not self.state.account_nickname_available(nickname):
                    self._send_text(account, user_id, f"用户昵称已存在: {nickname}")
                    return
            self._send_text(account, user_id, "正在生成新增 Bot 账号登录二维码，请在微信中扫码确认。")

            def send_qr(qr_content, _qr):
                try:
                    self._send_login_qr(account, user_id, qr_content)
                    self._send_text(account, user_id, "请扫描上方二维码并确认登录。")
                except Exception as err:
                    log.error(f"failed to send login qr account={account.get('accountId')} user={user_id}: {err}")
                    self._send_text(account, user_id, f"二维码图片发送失败，请到运行服务的终端扫描二维码。错误：{err}")

            new_account = login_with_qr(
                base_url=self.config["wechat"]["baseUrl"],
                bot_type=self.config["wechat"]["botType"],
                route_tag=self.config["wechat"].get("routeTag"),
                project_dir=PROJECT_DIR,
                on_qr=send_qr,
            )
            if nickname:
                new_account["nickname"] = nickname
            try:
                new_account = self.state.upsert_account(new_account)
            except ValueError as err:
                self._send_text(account, user_id, f"新增账号失败：{err}")
                return
            self._ensure_account_session(new_account)
            self._start_account_monitor(new_account)
            self._send_text(
                account,
                user_id,
                f"新增账号已连接: {new_account['accountId']}\n昵称: {new_account.get('nickname')}",
            )
            return
        if command == "/restart":
            if not self._is_admin(user_id):
                self._send_text(account, user_id, "只有 adminUsers 可以通过微信触发 /restart。")
                return
            self._send_text(account, user_id, "正在重启服务，稍后可发送 /status 确认。")
            self._schedule_restart()
            return
        interrupt_action, interrupt_text = self._parse_interrupt_command(command)
        if interrupt_action:
            self._clear_pending_guidance(conversation_key)
            agent = resolve_session_agent(self.config, self._get_session(conversation_key))
            agent_label = self._agent_label(agent)
            killed = self._cancel_runner(conversation_key, reset_session=False)
            if interrupt_text:
                self._send_text(account, user_id, "已中断当前工作区，保留当前会话，开始处理新任务。")
                self._run_codex_and_reply(account, user_id, conversation_key, interrupt_text)
            else:
                message = f"已中断当前 {agent_label} 任务，已保留当前会话。" if killed else "当前没有运行中的任务，当前会话保持不变。"
                self._send_text(account, user_id, message)
            return
        if command == "/reset":
            self._clear_pending_guidance(conversation_key)
            agent = resolve_session_agent(self.config, self._get_session(conversation_key))
            self.state.reset_session(conversation_key, agent=agent)
            self.state.update_session(conversation_key, desktopPendingId="")
            if self.DESKTOP_RUN_MARKER in conversation_key:
                self.state.reset_session(self._desktop_parent_key(conversation_key), agent="codex")
                self.state.update_session(self._desktop_parent_key(conversation_key), desktopPendingId="")
            self._send_text(account, user_id, f"已重置当前工作区 {self._agent_label(agent)} 会话。")
            return
        if command.startswith("/cwd"):
            arg = command[4:].strip()
            session = self._get_session(conversation_key)
            if not arg:
                self._send_text(account, user_id, f"当前 CWD: {session.get('cwd')}")
            else:
                cwd, error = self._resolve_cwd(arg, session.get("cwd"))
                if error:
                    self._send_text(account, user_id, error)
                    return
                workspace_name = self._workspace_name_from_key(base_conversation_key, conversation_key)
                if workspace_name != self.state.DEFAULT_WORKSPACE:
                    self.state.upsert_workspace(base_conversation_key, workspace_name, cwd)
                updates = {"cwd": cwd, "codexThreadId": "", "claudeSessionId": "", "desktopPendingId": ""}
                if session.get("codexClient") == "desktop":
                    codex_account = resolve_session_codex_account(self.config, session)
                    project_id = project_for_thread({"cwd": cwd}, self.desktop.projects(codex_account))
                    updates["desktopProjectId"] = "" if project_id == "_ungrouped" else project_id
                    updates["desktopPendingId"] = uuid.uuid4().hex
                else:
                    updates["desktopProjectId"] = ""
                self.state.update_session(conversation_key, **updates)
                self._send_text(account, user_id, f"已切换 CWD: {cwd}\n已重置当前工作区 Codex thread 和 Claude session。")
            return

        workspace_name = self._workspace_name_from_key(base_conversation_key, conversation_key)
        self.state.touch_workspace(base_conversation_key, workspace_name)
        self._run_codex_and_reply(account, user_id, conversation_key, text)
        self._run_pending_guidance(account, user_id, conversation_key)

    def _handle_update_command(self, account, user_id, command):
        if not self._is_admin(user_id):
            self._send_text(account, user_id, "只有 adminUsers 可以通过微信使用 Codex 更新命令。")
            return
        try:
            target, action = parse_update_command(command)
            if action == "help":
                self._send_text(account, user_id, UPDATE_HELP)
            elif action == "status":
                self._send_text(account, user_id, self.updates.status(target))
            elif action == "check":
                self._send_text(account, user_id, make_update_plan(self.config, target).describe())
            else:
                self.updates.start(
                    target,
                    on_started=lambda text: self._send_text(account, user_id, text),
                    on_complete=lambda result: self._send_text(account, user_id, format_update_result(result)),
                    on_updated=self._refresh_codex_after_update,
                    desktop_method=action if action in {"native", "installer"} else "",
                )
        except Exception as err:
            self._send_text(account, user_id, str(err))

    def _refresh_codex_after_update(self):
        # Close idle App Servers so subsequent requests launch the new binary.
        for agent in ["codex", "desktop"]:
            runner = self.codex.runners.get(agent)
            if runner:
                runner.terminate_all()
        self._model_options = None
        self.desktop_model_options_cache.clear()

    def _run_codex_and_reply(self, account, user_id, conversation_key, text):
        agent = resolve_session_agent(self.config, self._get_session(conversation_key))
        guard = self.updates.agent_run() if agent == "codex" else contextlib.nullcontext()
        with guard:
            self._run_codex_and_reply_locked(account, user_id, conversation_key, text)

    def _run_codex_and_reply_locked(self, account, user_id, conversation_key, text):
        if self.DESKTOP_RUN_MARKER in conversation_key:
            thread_id = self._get_session(conversation_key).get("codexThreadId") or ""
            running = self._desktop_running_context(thread_id) if thread_id else None
            if running and running.conversation_key != conversation_key:
                raise ValueError("该桌面会话已有运行中任务，请等待完成后再发新任务。")
            if thread_id and not running:
                run_session = self._get_session(conversation_key)
                codex_account = resolve_session_codex_account(self.config, run_session)
                if self.desktop.last_turn_status(codex_account, thread_id) == "inProgress":
                    raise ValueError("该桌面会话正在执行中，请等待完成后再发新任务。")
            self.state.update_session(conversation_key, desktopLastError="", desktopLastErrorTurnId="")
        stop_typing = self._start_typing_loop(account, user_id)
        try:
            result = self.codex.run(conversation_key, text)
        finally:
            stop_typing()
        turn_id = ""
        if self.DESKTOP_RUN_MARKER in conversation_key:
            with self._desktop_selection_lock(conversation_key):
                self._promote_pending_desktop_thread(conversation_key)
                if not self._desktop_is_selected(conversation_key):
                    return
                run_session = self._get_session(conversation_key)
                try:
                    codex_account = resolve_session_codex_account(self.config, run_session)
                    latest = self.desktop.latest_result(codex_account, run_session["codexThreadId"])
                    if latest["status"] == "completed" and latest["text"]:
                        result = latest["text"]
                        turn_id = latest["turnId"]
                except Exception as err:
                    log.warn(f"desktop latest result unavailable conversation={conversation_key}: {err}")
                if turn_id and self._get_session(conversation_key).get("desktopDeliveredTurnId") == turn_id:
                    return
                self._deliver_agent_output(account, user_id, conversation_key, result, turn_id=turn_id)
            return
        self._deliver_agent_output(account, user_id, conversation_key, result)

    def _deliver_agent_output(self, account, user_id, conversation_key, result, turn_id=""):
        cleaned, actions = extract_actions(result)
        session = self._get_session(conversation_key)
        already_delivered = bool(turn_id and session.get("desktopDeliveredTurnId") == turn_id)
        if already_delivered:
            actions = []
        else:
            actions.extend(read_and_clear_media_outbox(media_outbox_path(self.state.state_dir, conversation_key)))
        cleaned = markdown_to_plain_text(cleaned)
        if cleaned:
            self._send_text(account, user_id, cleaned)
        if actions:
            client = self._api_for_account(account)
            context_token = self.state.get_context_token(account["accountId"], user_id)
            if not context_token:
                raise RuntimeError("缺少 context_token，无法发送媒体")
            sent = execute_actions(
                client,
                user_id,
                context_token,
                actions,
                int(self.config.get("media", {}).get("maxFileBytes") or 52_428_800),
                transfer_semaphore=self.media_semaphore,
            )
            log.info(f"sent media conversation={conversation_key} count={len(sent)}")
        if turn_id and not already_delivered:
            self.state.update_session(conversation_key, desktopDeliveredTurnId=turn_id)

    def _cancel_runner(self, conversation_key, reset_session=True):
        try:
            return self.codex.cancel(conversation_key, reset_session=reset_session)
        except TypeError:
            return self.codex.cancel(conversation_key)

    def _discover_sessions(self, conversation_key, scope="", limit=20, archived=False):
        current = self._get_session(conversation_key)
        current_agent = resolve_session_agent(self.config, current)
        value = str(scope or "").strip().lower()
        if value in {"", "current"}:
            agents = [current_agent]
        elif value in {"all", "*"}:
            agents = ["codex", "claude"]
        elif value in {"codex", "claude"}:
            agents = [value]
        else:
            return None, f"未知 sessions 范围: {scope}\n用法：/sessions [codex|claude|all]"

        sessions = []
        if "codex" in agents:
            codex_account = resolve_session_codex_account(self.config, current)
            sessions.extend(list_codex_sessions(codex_account, limit=limit, archived_only=archived))
        if "claude" in agents:
            claude_account = resolve_session_claude_account(self.config, current)
            sessions.extend(list_claude_sessions(
                claude_account, limit=limit,
                archived_ids=self.state.archived_claude_ids(claude_account["name"]),
                archived_only=archived,
            ))
        return sort_sessions(sessions)[:limit], ""

    def _handle_sessions_command(self, account, user_id, conversation_key, scope):
        parts = str(scope or "").split()
        archived = "archived" in [part.lower() for part in parts]
        scope = " ".join(part for part in parts if part.lower() != "archived")
        sessions, error = self._discover_sessions(conversation_key, scope, limit=20, archived=archived)
        if error:
            self._send_text(account, user_id, error)
            return
        self.session_selection_cache[conversation_key] = sessions
        if not sessions:
            self._send_text(account, user_id, "没有找到归档会话。" if archived else "没有找到可恢复的 session。")
            return
        lines = ["归档 sessions（发送 /session unarchive 编号 恢复）：" if archived
                 else "可恢复 sessions（发送 /session use 编号 切换）："]
        for index, item in enumerate(sessions, 1):
            lines.append(
                f"{index}. {item['agent']}:{item.get('account') or '-'} {short_session_id(item.get('sessionId'))}"
            )
            lines.append(f"   time: {format_session_time(item.get('updatedAt'))}")
            if item.get("cwd"):
                lines.append(f"   cwd: {item.get('cwd')}")
            lines.append(f"   title: {item.get('title') or 'untitled'}")
        self._send_text(account, user_id, "\n".join(lines))

    @staticmethod
    def _session_help_text():
        return "\n".join(
            [
                "用法：",
                "/sessions [codex|claude|all] 查看可恢复 sessions",
                "/session use <编号|sessionId前缀> 切换当前工作区到指定 session",
                "/session new [codex|claude] 新建当前工作区会话",
                "/sessions archived [codex|claude|all] 查看归档会话",
                "/session archive|unarchive <编号> 归档或恢复（管理员）",
                "/session delete <编号|ID前缀> [更多编号或前缀] 预览批量删除，再加 confirm 确认（管理员）",
            ]
        )

    def _find_cached_session(self, conversation_key, selector):
        value = str(selector or "").strip()
        cached = list(self.session_selection_cache.get(conversation_key) or [])
        if value.isdigit() and cached:
            index = int(value) - 1
            if 0 <= index < len(cached):
                return cached[index], ""
            return None, f"编号超出范围: {value}"
        if value.isdigit():
            return None, "请先发送 /sessions 查看编号，再用 /session use <编号> 切换。"
        candidates = cached
        if not candidates:
            candidates, error = self._discover_sessions(conversation_key, "all", limit=100)
            if error:
                return None, error
        matches = [
            item
            for item in candidates
            if str(item.get("sessionId") or "").startswith(value)
            or short_session_id(item.get("sessionId")).startswith(value)
        ]
        if len(matches) == 1:
            return matches[0], ""
        if not matches:
            return None, f"没有找到 session: {value}\n请先发送 /sessions all 查看。"
        return None, f"匹配到多个 session: {value}\n请使用更长的 sessionId 前缀。"

    def _handle_session_command(self, account, user_id, conversation_key, arg):
        parts = str(arg or "").strip().split(maxsplit=1)
        if not parts:
            self._send_text(account, user_id, self._session_help_text())
            return
        action = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if action in {"use", "switch"}:
            if not rest:
                self._send_text(account, user_id, "用法：/session use <编号|sessionId前缀>")
                return
            if self.codex.is_running(conversation_key):
                self._send_text(account, user_id, "当前工作区有任务运行中，请等待结束或 /interrupt 后再切换 session。")
                return
            item, error = self._find_cached_session(conversation_key, rest)
            if error:
                self._send_text(account, user_id, error)
                return
            if item.get("archived"):
                self._send_text(account, user_id, "该会话已归档，请先 /session unarchive。")
                return
            updates = {"agent": item["agent"], "codexClient": "", "desktopPendingId": ""}
            if item.get("cwd"):
                updates["cwd"] = item["cwd"]
            if item["agent"] == "claude":
                updates.update(claudeSessionId=item["sessionId"], claudeAccount=item.get("account") or default_claude_account(self.config))
            else:
                updates.update(codexThreadId=item["sessionId"], codexAccount=item.get("account") or default_codex_account(self.config))
            self.state.update_session(conversation_key, **updates)
            self._send_text(
                account,
                user_id,
                f"已切换当前工作区到 {item['agent']} session: {short_session_id(item.get('sessionId'))}\n"
                f"title: {item.get('title') or 'untitled'}",
            )
            return
        if action == "new":
            if self.codex.is_running(conversation_key):
                self._send_text(account, user_id, "当前工作区有任务运行中，请等待结束或 /interrupt 后再新建 session。")
                return
            current = self._get_session(conversation_key)
            target = normalize_agent(rest or resolve_session_agent(self.config, current))
            if not target:
                self._send_text(account, user_id, "未知 Agent。可用：codex、claude")
                return
            self.state.reset_session(conversation_key, agent=target)
            self.state.update_session(conversation_key, agent=target, codexClient="", desktopPendingId="")
            self.session_selection_cache.pop(conversation_key, None)
            self._send_text(account, user_id, f"已新建当前工作区 {self._agent_label(target)} 会话。")
            return
        if action in {"archive", "unarchive", "delete"}:
            self._manage_cli_session(account, user_id, conversation_key, action, rest)
            return
        self._send_text(account, user_id, self._session_help_text())

    @staticmethod
    def _claude_session_files(claude_account, session_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id or ""):
            raise ValueError("无效的 Claude 会话 ID。")
        base = Path(claude_account.get("claudeConfigDir") or "~/.claude").expanduser().resolve()
        paths = list((base / "projects").glob(f"*/{session_id}.jsonl"))
        paths.append(base / "usage-data" / "session-meta" / f"{session_id}.json")
        existing = [path for path in paths if path.exists()]
        for path in existing:
            if base not in path.resolve().parents or not path.is_file():
                raise ValueError("Claude 会话文件路径异常，已停止删除。")
        return existing

    @staticmethod
    def _delete_claude_session_files(claude_account, session_id):
        existing = MultiWechatCodexService._claude_session_files(claude_account, session_id)
        for path in existing:
            path.unlink()
        return len(existing)

    def _session_operation_account(self, item, action):
        session_id = item["sessionId"]
        account_name = item["account"]
        if action != "unarchive":
            with self.state.lock:
                refs = [(key, value) for key, value in self.state.state["sessions"].items()
                        if value.get("codexThreadId" if item["agent"] == "codex" else "claudeSessionId") == session_id
                        and value.get("codexAccount" if item["agent"] == "codex" else "claudeAccount") in {None, "", account_name}]
            if any(self.codex.is_running(key) for key, _ in refs):
                raise ValueError("该会话正在运行，请先结束任务。")
        if item["agent"] == "codex":
            target_account = get_codex_account(self.config, account_name)
            if self._desktop_running_context(session_id):
                raise ValueError("该会话正在运行，请先结束任务。")
            if action != "unarchive" and self.desktop.last_turn_status(target_account, session_id) == "inProgress":
                raise ValueError("该会话可能正在运行，请先结束任务。")
        else:
            target_account = get_claude_account(self.config, account_name)
        return target_account

    def _delete_session_batch(self, account, user_id, conversation_key, rest, desktop=False):
        if not self._is_admin(user_id):
            raise ValueError("归档、恢复和删除仅限 adminUsers。")
        command = "/d-session delete" if desktop else "/session delete"
        list_command = "/d-sessions all" if desktop else "/sessions all"
        pending_cache = self.desktop_delete_pending if desktop else self.session_delete_pending
        selection_cache = self.desktop_thread_cache if desktop else self.session_selection_cache
        parts = rest.split()
        confirmed = bool(parts and parts[-1].lower() == "confirm")
        selectors = parts[:-1] if confirmed else parts
        if not selectors or any(value.lower() == "confirm" for value in selectors):
            raise ValueError(f"用法：{command} <编号|ID前缀> [更多编号或前缀] [confirm]。")
        if not confirmed:
            pending_cache.pop(conversation_key, None)

        # Resolve the whole batch before deleting anything. Deduplicate by full
        # identity so repeated numbers and ID prefixes never delete twice.
        items, identities = [], set()
        for selector in selectors:
            if desktop:
                item = dict(self._desktop_thread_selector(conversation_key, selector),
                            agent="codex")
                item["sessionId"] = item["id"]
            else:
                item, error = self._find_cached_session(conversation_key, selector)
                if error:
                    raise ValueError(error)
                item = dict(item)
            identity = (item["agent"], item["account"], item["sessionId"])
            if identity not in identities:
                identities.add(identity)
                items.append(item)
        if confirmed:
            pending = pending_cache.pop(conversation_key, None) or {}
            if (identities != set(pending.get("targets") or [])
                    or time.monotonic() > pending.get("expires", 0)):
                raise ValueError(f"删除确认已失效或所选会话发生变化；请重新发送 {command} <会话编号>。")

        targets = []
        for item in items:
            try:
                target_account = self._session_operation_account(item, "delete")
                if item["agent"] == "claude" and not self._claude_session_files(target_account, item["sessionId"]):
                    raise ValueError("没有找到可删除的 Claude 会话文件。")
            except Exception as error:
                raise ValueError(f"{item.get('title') or item['sessionId']}：{error}") from error
            targets.append((item, target_account))
        if not confirmed:
            pending_cache[conversation_key] = {"targets": list(identities), "expires": time.monotonic() + 60}
            lines = [f"即将永久删除 {len(items)} 条会话："]
            for item in items:
                lines.append(f"{item['agent']}:{item['account']} {item.get('title') or 'untitled'}\n{item['sessionId']}")
            if any(item["agent"] == "codex" for item in items):
                lines.append("Codex 派生子会话也会被删除。")
            lines.append(f"60 秒内发送 {command} {' '.join(selectors)} confirm 确认。")
            self._send_text(account, user_id, "\n".join(lines))
            return

        deleted = []
        try:
            for item, target_account in targets:
                try:
                    if item["agent"] == "codex":
                        self.desktop.delete(target_account, item["sessionId"])
                        self.state.clear_codex_thread(item["sessionId"], item["account"])
                    else:
                        if not self._delete_claude_session_files(target_account, item["sessionId"]):
                            raise ValueError("没有找到可删除的 Claude 会话文件。")
                        self.state.set_claude_archived(item["account"], item["sessionId"], False)
                        self.state.clear_claude_session(item["sessionId"], item["account"])
                except Exception as error:
                    if len(items) == 1:
                        raise
                    lines = [f"批量删除中断：已完成 {len(deleted)}/{len(items)} 条。"]
                    lines.extend(f"已永久删除：{value.get('title') or value['sessionId']}（{value['sessionId']}）" for value in deleted)
                    lines.append(f"处理失败：{item.get('title') or item['sessionId']}（{item['sessionId']}）：{error}")
                    lines.append(f"后续 {len(items) - len(deleted) - 1} 条未执行；请重新发送 {list_command} 查看后再删除。")
                    self._send_text(account, user_id, "\n".join(lines))
                    return
                deleted.append(item)
        finally:
            selection_cache.pop(conversation_key, None)
        if len(deleted) == 1:
            message = f"已永久删除：{deleted[0].get('title') or 'untitled'}"
        else:
            message = f"已永久删除 {len(deleted)} 条会话：\n" + "\n".join(
                f"{item.get('title') or 'untitled'}（{item['sessionId']}）" for item in deleted)
        self._send_text(account, user_id, message + f"\n请重新发送 {list_command} 查看最新编号。")

    def _manage_cli_session(self, account, user_id, conversation_key, action, rest):
        if action == "delete":
            self._delete_session_batch(account, user_id, conversation_key, rest)
            return
        if not self._is_admin(user_id):
            raise ValueError("归档、恢复和删除仅限 adminUsers。")
        parts = rest.split()
        if len(parts) != 1:
            raise ValueError(f"用法：/session {action} <编号|sessionId前缀>")
        item, error = self._find_cached_session(conversation_key, parts[0])
        if error:
            raise ValueError(error)
        session_id = item["sessionId"]
        account_name = item["account"]
        archived = bool(item.get("archived"))
        if action == "archive" and archived:
            raise ValueError("该会话已经归档。")
        if action == "unarchive" and not archived:
            raise ValueError("该会话未归档。")
        target_account = self._session_operation_account(item, action)
        if item["agent"] == "codex":
            if action == "archive":
                self.desktop.archive(target_account, session_id)
                self.state.clear_codex_thread(session_id, account_name)
            elif action == "unarchive":
                self.desktop.unarchive(target_account, session_id)
        else:
            if action == "archive":
                self.state.set_claude_archived(account_name, session_id, True)
                self.state.clear_claude_session(session_id, account_name)
            elif action == "unarchive":
                self.state.set_claude_archived(account_name, session_id, False)
        self.session_selection_cache.pop(conversation_key, None)
        self.session_delete_pending.pop(conversation_key, None)
        self._send_text(account, user_id, f"已{'归档' if action == 'archive' else '恢复'}：{item['title']}")

    def _desktop_account(self, conversation_key):
        session = self._get_session(conversation_key)
        name = (self.desktop_account_selection.get(conversation_key)
                or (session.get("codexAccount") if session.get("codexClient") == "desktop" else "")
                or default_codex_account(self.config))
        return get_codex_account(self.config, name)

    @staticmethod
    def _desktop_help_text():
        return "\n".join([
            "桌面 Codex 项目与会话：",
            "/d-p-n <目录> 创建桌面原生项目并切换；/d-new-project、/d-n-p 等价",
            "/d-account [账号] 选择本地 Codex 账号",
            "/d-projects 列桌面原生项目并编号",
            "/d-project use <编号> 切换桌面项目并准备新会话",
            "/d-sessions all 2 或 /d-sessions page 2 查看全部会话第 2 页",
            "/d-sessions <项目编号> [页码] 查看指定项目会话",
            "/d-sessions archived [all|项目编号] [页码] 列归档会话",
            "/d-session <编号> 快速切换；view|status|use <编号> 查看结果、状态或选中续聊",
            "/d-session new 在当前桌面项目准备新会话",
            "/d-session guide <编号> <内容> 引导本 Bot 发起的运行",
            "/d-session interrupt <编号> 打断本 Bot 发起的运行",
            "/d-session archive|unarchive <编号> 归档或恢复（管理员）",
            "/d-session delete <编号|ID前缀> [更多编号或前缀] 预览批量删除；再加 confirm 确认（管理员）",
            "/d-session off 退出桌面 App Server 路由",
            "旧版 /desktop 命令仍可使用。",
            "项目和会话存入桌面 Codex 的本地数据；任务结果同时回传微信。",
            "每次切换会话都会重发最近一次完整文字回答；运行中会同时提示状态。",
            "列表状态是最后保存的回合状态；桌面窗口实时运行状态无法由独立 App Server 确认。",
        ])

    def _desktop_projects(self, conversation_key, codex_account):
        projects = self.desktop.projects(codex_account)
        threads = self.desktop.threads(codex_account)
        counts = {project["id"]: 0 for project in projects}
        for item in threads:
            project_id = project_for_thread(item, projects)
            counts[project_id] = counts.get(project_id, 0) + 1
        if counts.get("_ungrouped"):
            projects.append({"id": "_ungrouped", "name": "未归类", "roots": []})
        for project in projects:
            project["count"] = counts.get(project["id"], 0)
        self.desktop_project_cache[conversation_key] = projects
        return projects

    def _desktop_project_selector(self, conversation_key, codex_account, selector):
        if not selector or selector.lower() in {"all", "*"}:
            return "all", ""
        projects = self.desktop_project_cache.get(conversation_key)
        if projects is None:
            projects = self._desktop_projects(conversation_key, codex_account)
        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(projects):
                return projects[index]["id"], projects[index]["name"]
            raise ValueError(f"项目编号超出范围：{selector}。先发送 /d-projects。")
        matches = [p for p in projects if selector == p["id"] or selector == p["name"]]
        if not matches:
            matches = [p for p in projects if selector.lower() in p["name"].lower()]
        if len(matches) != 1:
            raise ValueError("项目名称未找到或不唯一。先发送 /d-projects，使用编号。")
        return matches[0]["id"], matches[0]["name"]

    def _desktop_thread_selector(self, conversation_key, selector):
        value = str(selector or "").strip()
        if not value:
            raise ValueError("请提供会话编号；先发送 /d-sessions all。")
        cached = self.desktop_thread_cache.get(conversation_key) or []
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(cached):
                return cached[index]
            raise ValueError("会话编号无效；先发送 /d-sessions all 或 /d-sessions archived all。")
        matches = [item for item in cached if item["id"].startswith(value)]
        if len(matches) != 1:
            raise ValueError("会话 ID 前缀未找到或不唯一；先列会话并使用编号。")
        return matches[0]

    def _desktop_running_context(self, thread_id):
        if not thread_id:
            return None
        runner = self.desktop.runner
        with runner.lock:
            contexts = list(runner.contexts.values())
        for context in contexts:
            with context.lock:
                if context.thread_id == thread_id and context.running:
                    return context
        return None

    def _desktop_latest_error(self, run_key, latest):
        session = self._get_session(run_key)
        error = session.get("desktopLastError") or ""
        failed_turn_id = session.get("desktopLastErrorTurnId") or ""
        if error and (not failed_turn_id or not latest.get("turnId") or latest.get("turnId") == failed_turn_id):
            return error
        return ""

    def _send_desktop_selected_result(self, account, user_id, run_key, title, latest, running=False):
        lines = [f"已切换：{title}"]
        if running or latest["status"] == "inProgress":
            lines.append("正在执行中")
        else:
            last_error = self._desktop_latest_error(run_key, latest)
            if last_error:
                lines.append(f"最新任务失败：{last_error}")
            elif latest["status"] != "completed" or not latest.get("text"):
                lines.append(self._desktop_status_text({"lastTurnStatus": latest["status"]}))
        answer = latest.get("lastAnswerText") or latest.get("text") or ""
        if answer:
            cleaned, actions = extract_actions(answer)
            cleaned = markdown_to_plain_text(cleaned)
            label = ("最新结果" if latest["status"] == "completed"
                     and latest.get("lastAnswerTurnId", latest.get("turnId")) == latest.get("turnId")
                     and not running else "上一条完整回答")
            if cleaned:
                lines.extend([f"{label}：", cleaned])
            elif actions:
                lines.append(f"{label}仅包含媒体，切换时不重复发送附件。")
        self._send_text(account, user_id, "\n".join(lines))

    @staticmethod
    def _desktop_status_text(item, bot_running=False):
        if bot_running:
            return "本 Bot 运行中"
        labels = {
            "completed": "最近回合已完成",
            "failed": "最近回合失败",
            "interrupted": "最近回合已中断",
            "inProgress": "最近回合记录为进行中",
        }
        return labels.get(item.get("lastTurnStatus"), "最近回合状态未知")

    def _handle_desktop_command(self, account, user_id, conversation_key, arg):
        parts = str(arg or "").strip().split()
        action = parts[0].lower() if parts else "help"
        if len(parts) == 1 and action.isdigit():
            parts = ["use", action]
            action = "use"
        elif action in {"switch", "select"}:
            parts[0] = "use"
            action = "use"
        codex_account = self._desktop_account(conversation_key)
        if action in {"help", "?"}:
            self._send_text(account, user_id, self._desktop_help_text())
            return
        if action == "account":
            if len(parts) == 1:
                names = ", ".join(a["name"] for a in list_codex_accounts(self.config))
                self._send_text(account, user_id, f"当前桌面会话账号：{codex_account['name']}\n可选：{names}")
                return
            selected = find_codex_account(self.config, parts[1])
            if not selected:
                raise ValueError("未找到该 Codex 账号。")
            self.desktop_account_selection[conversation_key] = selected["name"]
            self.desktop_project_cache.pop(conversation_key, None)
            self.desktop_thread_cache.pop(conversation_key, None)
            self._send_text(account, user_id, f"桌面会话账号：{selected['name']}")
            return
        if action == "projects":
            projects = self._desktop_projects(conversation_key, codex_account)
            if not projects:
                self._send_text(account, user_id, "该账号没有本地 Codex 项目。")
                return
            lines = [f"桌面 Codex 项目（账号 {codex_account['name']}，共 {len(projects)} 个）："]
            for index, project in enumerate(projects, 1):
                lines.append(f"{index}. {project['name']}（{project['count']} 条会话）")
                if project.get("roots"):
                    lines.append("   " + ", ".join(project["roots"]))
            lines.append("发送 /d-sessions <项目编号> 查看会话。")
            self._send_text(account, user_id, "\n".join(lines))
            return
        if action == "project":
            if len(parts) != 3 or parts[1].lower() != "use":
                raise ValueError("用法：/d-project use <项目编号>。先发送 /d-projects。")
            project_id, _ = self._desktop_project_selector(conversation_key, codex_account, parts[2])
            project = next((p for p in self.desktop_project_cache[conversation_key]
                            if p["id"] == project_id), None)
            if not project or not project.get("roots"):
                raise ValueError("该项目没有本地目录，无法创建本地会话。")
            base_key = ":".join(self._desktop_parent_key(conversation_key).split(":", 2)[:2])
            with self._desktop_selection_lock(base_key):
                self.desktop_account_selection[base_key] = codex_account["name"]
                self.state.set_active_workspace(base_key, self.state.DEFAULT_WORKSPACE)
                self.state.update_session(
                    base_key, agent="codex", codexClient="desktop", codexThreadId="",
                    codexAccount=codex_account["name"], cwd=project["roots"][0],
                    desktopProjectId=project_id, desktopPendingId=uuid.uuid4().hex,
                    codexModel="", codexReasoningEffort="",
                )
            self._send_text(account, user_id,
                            f"已切换桌面项目：{project['name']}\n目录：{project['roots'][0]}\n发送第一条任务时创建会话。")
            return
        if action == "new":
            if len(parts) != 1:
                raise ValueError("用法：/d-session new；指定新目录请使用 /d-new-project <目录>。")
            if self.codex.is_running(conversation_key):
                raise ValueError("当前工作区有任务运行中，请先中断或等待完成。")
            with self._desktop_selection_lock(conversation_key):
                cwd = self._get_session(conversation_key).get("cwd") or self.config["codex"]["workingDirectory"]
                current = self._get_session(conversation_key)
                projects = self.desktop.projects(codex_account)
                project_id = current.get("desktopProjectId") or ""
                selected_project = next((p for p in projects if p["id"] == project_id), None)
                if not selected_project or not any(
                    cwd == root or cwd.startswith(root.rstrip("/") + "/")
                    for root in selected_project.get("roots") or []
                ):
                    project_id = project_for_thread({"cwd": cwd}, projects)
                    if project_id == "_ungrouped":
                        project_id = ""
                self.state.update_session(
                    conversation_key, agent="codex", codexClient="desktop",
                    codexThreadId="", codexAccount=codex_account["name"],
                    desktopProjectId=project_id,
                    codexModel="", codexReasoningEffort="", desktopPendingId=uuid.uuid4().hex,
                )
                self.desktop_thread_cache.pop(conversation_key, None)
            self._send_text(account, user_id,
                f"已准备新的桌面 Codex 会话。\n目录：{cwd}\n发送第一条任务时创建。")
            return
        if action in {"chats", "archived"}:
            selector = parts[1] if len(parts) > 1 else "all"
            page_text = parts[2] if len(parts) > 2 else "1"
            if selector.lower() == "page":
                if len(parts) != 3:
                    raise ValueError("用法：/d-sessions page <页码>。")
                selector = "all"
            if len(parts) > 3:
                raise ValueError("用法：/d-sessions [all|项目编号] [页码]，或 /d-sessions page <页码>。")
            if not page_text.isdigit() or int(page_text) < 1:
                raise ValueError("页码必须是正整数。")
            page = int(page_text)
            project_id, project_name = self._desktop_project_selector(conversation_key, codex_account, selector)
            projects = self.desktop_project_cache.get(conversation_key) or self._desktop_projects(conversation_key, codex_account)
            threads = self.desktop.threads(codex_account, archived=action == "archived")
            if project_id != "all":
                threads = [item for item in threads if project_for_thread(item, projects) == project_id]
            for item in threads:
                item["account"] = codex_account["name"]
            self.desktop_thread_cache[conversation_key] = threads
            if not threads:
                self._send_text(account, user_id, "没有找到会话。")
                return
            page_size = 20
            start = (page - 1) * page_size
            total_pages = (len(threads) + page_size - 1) // page_size
            if start >= len(threads):
                raise ValueError(f"页码超出范围；共 {total_pages} 页。")
            label = "归档会话" if action == "archived" else "会话"
            lines = [f"{project_name or '所有项目'}{label}（账号 {codex_account['name']}，共 {len(threads)} 条，第 {page}/{total_pages} 页）："]
            for index in range(start, min(start + page_size, len(threads))):
                item = threads[index]
                try:
                    item["lastTurnStatus"] = self.desktop.last_turn_status(codex_account, item["id"])
                except Exception:
                    item["lastTurnStatus"] = "unknown"
                status = self._desktop_status_text(item, bool(self._desktop_running_context(item["id"])))
                project_label = ""
                if project_id == "all":
                    matching = next((p for p in projects if p["id"] == project_for_thread(item, projects)), None)
                    project_label = f" [{matching['name'] if matching else '未归类'}]"
                lines.append(f"{index + 1}. {item['title']}{project_label} [{status}]")
                lines.append(f"   {item['id'][:12]}  {format_session_time(item['updatedAt'])}")
            list_command = "/d-sessions archived" if action == "archived" else "/d-sessions"
            if page > 1:
                lines.append(f"上一页：{list_command} {selector} {page - 1}")
            if page < total_pages:
                lines.append(f"下一页：{list_command} {selector} {page + 1}")
            lines.append("切换会话：/d-session <编号>；也可用 /d-session use <编号>。")
            self._send_text(account, user_id, "\n".join(lines))
            return
        if action == "off":
            selected = self._get_session(conversation_key)
            if (self.codex.is_running(conversation_key)
                    or (selected.get("codexClient") == "desktop"
                        and self._desktop_running_context(selected.get("codexThreadId") or ""))):
                raise ValueError("当前工作区有任务运行中，请先中断或等待完成。")
            self.state.update_session(conversation_key, codexClient="", desktopPendingId="")
            self._send_text(account, user_id, "已退出桌面 App Server 路由；当前 Codex 会话 ID 保留。")
            return
        if action not in {"view", "status", "use", "guide", "interrupt", "archive", "unarchive", "delete"}:
            raise ValueError(f"未知桌面会话操作：{action}。切换请用 /d-session <编号>；查看帮助发送 /d-session。")
        if action == "delete":
            self._delete_session_batch(account, user_id, conversation_key, " ".join(parts[1:]), desktop=True)
            return
        if len(parts) < 2:
            raise ValueError(f"用法：/d-session {action} <会话编号>。先发送 /d-sessions all。")
        item = self._desktop_thread_selector(conversation_key, parts[1])
        target_account = get_codex_account(self.config, item["account"])
        thread_id = item["id"]
        running = self._desktop_running_context(thread_id)
        if action == "status":
            item["lastTurnStatus"] = self.desktop.last_turn_status(target_account, thread_id)
            native_model = self.desktop.read(target_account, thread_id)
            lines = [f"{item['title']}", f"threadId: {thread_id}",
                     f"codexModel: {native_model.get('model') or '未记录'}",
                     f"reasoning: {native_model.get('reasoningEffort') or 'default'}",
                     f"状态: {self._desktop_status_text(item, bool(running))}",
                     f"最近更新: {format_session_time(item['updatedAt'])}",
                     "桌面窗口是否正在运行，当前连接无法确认。"]
            self._send_text(account, user_id, "\n".join(lines))
            return
        if action == "view":
            latest = self.desktop.latest_result(target_account, thread_id)
            if running or latest["status"] == "inProgress":
                message = "正在执行中"
            elif latest["status"] == "completed" and latest["text"]:
                message = markdown_to_plain_text(extract_actions(latest["text"])[0])
            else:
                message = self._desktop_status_text({"lastTurnStatus": latest["status"]})
            self._send_text(account, user_id, f"{item['title']}\n最新结果：\n{message}")
            return
        if action == "use":
            if self.codex.is_running(conversation_key):
                raise ValueError("当前工作区有任务运行中，请先中断或等待完成。")
            if item["archived"]:
                raise ValueError("该会话已归档，请先 /d-session unarchive。")
            with self._desktop_selection_lock(conversation_key):
                project_id = project_for_thread(item, self.desktop.projects(target_account))
                self.state.update_session(
                    conversation_key, agent="codex", codexClient="desktop", codexThreadId=thread_id,
                    codexAccount=target_account["name"], cwd=item["cwd"] or self._get_session(conversation_key).get("cwd"),
                    desktopProjectId="" if project_id == "_ungrouped" else project_id,
                    codexModel="", codexReasoningEffort="", desktopPendingId="",
                )
                run_key = self._desktop_execution_key(conversation_key)
                latest = self.desktop.latest_result(target_account, thread_id)
                running = self._desktop_running_context(thread_id)
                self._send_desktop_selected_result(account, user_id, run_key, item["title"], latest, bool(running))
            return
        if action in {"guide", "interrupt"}:
            if not running:
                raise ValueError("该会话当前没有本 Bot 发起的运行；无法控制桌面窗口内独立运行的回合。")
            if action == "guide":
                value = arg.split(maxsplit=2)[2].strip() if len(arg.split(maxsplit=2)) > 2 else ""
                if not value:
                    raise ValueError("用法：/d-session guide <会话编号> <引导内容>")
                if not self.desktop.runner.steer(running.conversation_key, value):
                    raise RuntimeError("引导未被当前回合接受。")
                self._send_text(account, user_id, "已发送引导。")
            else:
                self.desktop.runner.cancel(running.conversation_key, reset_session=False)
                self._send_text(account, user_id, "已请求中断；会话保留。")
            return
        if not self._is_admin(user_id):
            raise ValueError("归档、恢复和删除仅限 adminUsers。")
        item["lastTurnStatus"] = self.desktop.last_turn_status(target_account, thread_id)
        if running or item.get("lastTurnStatus") == "inProgress":
            raise ValueError("该会话可能正在运行；请先结束任务，再操作。")
        if action == "archive":
            if item["archived"]:
                raise ValueError("该会话已经归档。")
            self.desktop.archive(target_account, thread_id)
            self.state.clear_codex_thread(thread_id, target_account["name"])
            self.desktop_thread_cache.pop(conversation_key, None)
            self._send_text(account, user_id, f"已归档：{item['title']}")
            return
        if action == "unarchive":
            if not item["archived"]:
                raise ValueError("该会话未归档。")
            self.desktop.unarchive(target_account, thread_id)
            self.desktop_thread_cache.pop(conversation_key, None)
            self._send_text(account, user_id, f"已恢复：{item['title']}")
            return

    @staticmethod
    def _parse_interrupt_command(text):
        command = (text or "").strip()
        if command == "/cancel" or command == "/interrupt":
            return True, ""
        for prefix in ("/cancel ", "/interrupt "):
            if command.startswith(prefix):
                return True, command[len(prefix):].strip()
        return False, ""

    @classmethod
    def _guidance_text(cls, text):
        command = (text or "").strip()
        if command == "/guide":
            return ""
        if command.startswith("/guide "):
            return command[len("/guide "):].strip()
        parts = cls._workspace_run_parts(command)
        if len(parts) >= 4:
            return parts[3].strip()
        return command

    def _append_pending_guidance(self, conversation_key, text):
        value = str(text or "").strip()
        if not value:
            return False
        with self.pending_guidance_guard:
            self.pending_guidance.setdefault(conversation_key, []).append(value)
        log.info(f"queued guidance conversation={conversation_key}")
        return True

    def _pop_pending_guidance(self, conversation_key):
        with self.pending_guidance_guard:
            items = self.pending_guidance.pop(conversation_key, [])
        return [item for item in items if str(item or "").strip()]

    def _clear_pending_guidance(self, conversation_key):
        with self.pending_guidance_guard:
            self.pending_guidance.pop(conversation_key, None)

    @staticmethod
    def _format_guidance_prompt(items):
        lines = [
            "用户在你处理上一项任务期间追加了以下补充引导。",
            "请基于当前会话上下文继续处理；如果与之前要求冲突，以这些最新引导为准。",
            "",
        ]
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. {item}")
        return "\n".join(lines)

    def _run_pending_guidance(self, account, user_id, conversation_key):
        while True:
            items = self._pop_pending_guidance(conversation_key)
            if not items:
                return
            self._send_text(account, user_id, f"继续处理 {len(items)} 条补充引导。")
            self._run_codex_and_reply(account, user_id, conversation_key, self._format_guidance_prompt(items))

    def _schedule_after_lock(self, account, user_id, base_conversation_key, conversation_key, text):
        def run():
            lock = self._conversation_lock(conversation_key)
            with lock:
                if self.stop_event.is_set():
                    return
                self._handle_message(account, user_id, base_conversation_key, text, conversation_key)

        self.executor.submit(run)

    def _read_all_codex_usage(self):
        accounts = list_codex_accounts(self.config)
        if not accounts:
            return "没有配置 Codex 账号。"
        codex_bin = self.config["codex"].get("bin") or "codex"
        max_workers = min(4, len(accounts))
        results = [None] * len(accounts)

        def read_one(index, codex_account):
            try:
                usage = read_codex_usage(
                    codex_bin,
                    codex_home=codex_account.get("codexHome") or "",
                )
                return index, {"account": codex_account, "usage": usage}
            except Exception as err:
                return index, {"account": codex_account, "error": str(err)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(read_one, index, codex_account)
                for index, codex_account in enumerate(accounts)
            ]
            for future in concurrent.futures.as_completed(futures):
                index, result = future.result()
                results[index] = result
        return format_codex_usage_all([result for result in results if result is not None])

    def _read_claude_usage_for_account(self, claude_account, cwd=""):
        claude_config = self.config.get("claude", {}) or {}
        usage = read_claude_usage(
            claude_config.get("bin") or "claude",
            timeout_s=int(claude_config.get("usageTimeoutSeconds") or 30),
            claude_config_dir=claude_account.get("claudeConfigDir") or "",
            permission_mode=claude_config.get("permissionMode") or "",
            cwd=cwd,
            include_admin_usage=False,
        )
        return format_claude_usage(usage, claude_account)

    def _read_claude_admin_usage(self, days=None):
        claude_config = self.config.get("claude", {}) or {}
        usage = read_claude_admin_usage(
            days=int(days or claude_config.get("adminUsageDays") or 7),
            timeout_s=int(claude_config.get("adminUsageTimeoutSeconds") or 60),
            keychain_service=claude_config.get("adminKeychainService") or "",
        )
        return format_claude_admin_usage(usage)

    def _read_all_claude_usage(self):
        accounts = list_claude_accounts(self.config)
        if not accounts:
            return "没有配置 Claude 账号。"
        max_workers = min(4, len(accounts))
        results = [None] * len(accounts)

        def read_one(index, claude_account):
            try:
                usage = read_claude_usage(
                    self.config.get("claude", {}).get("bin") or "claude",
                    timeout_s=int(self.config.get("claude", {}).get("usageTimeoutSeconds") or 30),
                    claude_config_dir=claude_account.get("claudeConfigDir") or "",
                    permission_mode=self.config.get("claude", {}).get("permissionMode") or "",
                    cwd=self.config.get("claude", {}).get("workingDirectory") or self.config["codex"]["workingDirectory"],
                    include_admin_usage=False,
                )
                return index, {"account": claude_account, "usage": usage}
            except Exception as err:
                return index, {"account": claude_account, "error": str(err)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(read_one, index, claude_account)
                for index, claude_account in enumerate(accounts)
            ]
            for future in concurrent.futures.as_completed(futures):
                index, result = future.result()
                results[index] = result
        return format_claude_usage_all([result for result in results if result is not None])

    def _read_all_agent_usage(self):
        return "\n\n".join([self._read_all_codex_usage(), self._read_all_claude_usage()])

    def _usage_text(self, conversation_key, args):
        values = [str(arg or "").strip().lower() for arg in args if str(arg or "").strip()]
        session = self._get_session(conversation_key)
        current_agent = resolve_session_agent(self.config, session)
        if len(values) >= 2 and values[0] == "claude" and values[1] in {"api", "admin"}:
            if len(values) > 3 or (len(values) == 3 and not values[2].isdigit()):
                return "用法：/usage claude api [days]\n例如：/usage claude api 7"
            return self._read_claude_admin_usage(int(values[2]) if len(values) == 3 else None)
        if not values:
            target = current_agent
            scope = "current"
        elif values == ["all"]:
            target = "all"
            scope = "all"
        elif len(values) == 1 and values[0] in {"codex", "claude"}:
            target = values[0]
            scope = "current"
        elif len(values) == 2 and values[0] in {"codex", "claude"} and values[1] == "all":
            target = values[0]
            scope = "all"
        else:
            return "用法：/usage、/usage all、/usage codex、/usage claude、/usage claude api [days]、/usage codex all、/usage claude all"

        if target == "all":
            return self._read_all_agent_usage()
        if target == "codex":
            if scope == "all":
                return self._read_all_codex_usage()
            codex_account = resolve_session_codex_account(self.config, session)
            usage = read_codex_usage(
                self.config["codex"].get("bin") or "codex",
                codex_home=codex_account.get("codexHome") or "",
            )
            return format_codex_usage(usage)
        if scope == "all":
            return self._read_all_claude_usage()
        claude_account = resolve_session_claude_account(self.config, session)
        return self._read_claude_usage_for_account(claude_account, cwd=session.get("cwd") or "")

    def _workspace_name_from_key(self, base_conversation_key, conversation_key):
        conversation_key = self._desktop_parent_key(conversation_key)
        if conversation_key == base_conversation_key:
            return self.state.DEFAULT_WORKSPACE
        prefix = f"{base_conversation_key}:"
        if conversation_key.startswith(prefix):
            return conversation_key[len(prefix):] or self.state.DEFAULT_WORKSPACE
        return self.state.DEFAULT_WORKSPACE

    def _validate_workspace_name(self, name, allow_default=False):
        value = str(name or "").strip()
        if allow_default and value == self.state.DEFAULT_WORKSPACE:
            return ""
        if value == self.state.DEFAULT_WORKSPACE:
            return "default 是保留工作区名，不能用 /ws add 创建。"
        if not self.WORKSPACE_NAME_RE.match(value):
            return "工作区名称只能包含中英文字母、数字、点、下划线和中划线，长度 1-64，且必须以字母或数字开头。"
        return ""

    def _resolve_cwd(self, value, base_cwd=None):
        raw = str(value or "").strip()
        if not raw:
            return "", "目录不能为空。"
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = Path(base_cwd or self.config["codex"]["workingDirectory"]) / path
        resolved = path.resolve()
        if not resolved.exists():
            return "", f"目录不存在: {resolved}"
        if not resolved.is_dir():
            return "", f"不是目录: {resolved}"
        return str(resolved), ""

    def _new_project_path(self, value, base_cwd):
        raw = str(value or "").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        if not raw:
            raise ValueError("用法：/new-project <目录> 或 /d-new-project <目录>")
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = Path(base_cwd or self.config["codex"]["workingDirectory"]) / path
        path = path.resolve()
        if path.exists() and not path.is_dir():
            raise ValueError(f"不是目录：{path}")
        if not path.exists():
            path.mkdir(parents=True)
        return str(path)

    def _project_workspace_name(self, base_conversation_key, cwd):
        workspaces = self.state.list_workspaces(base_conversation_key)
        existing = next((item["name"] for item in workspaces if item.get("cwd") == cwd), "")
        if existing:
            return existing
        used = {item["name"] for item in workspaces}
        stem = re.sub(r"[^\w.-]+", "-", Path(cwd).name, flags=re.UNICODE).strip("._-")[:64]
        if not stem or self._validate_workspace_name(stem) or stem == self.state.DEFAULT_WORKSPACE:
            stem = "project"
        candidate = stem
        index = 2
        while candidate in used:
            suffix = f"-{index}"
            candidate = stem[:64 - len(suffix)] + suffix
            index += 1
        return candidate

    def _handle_new_project_command(self, account, user_id, base_conversation_key, conversation_key, arg, desktop=False):
        if not arg:
            prefix = "/d-new-project" if desktop else "/new-project"
            self._send_text(account, user_id, f"用法：{prefix} <目录>")
            return
        if desktop:
            self._handle_new_desktop_project_command(
                account, user_id, base_conversation_key, conversation_key, arg
            )
            return
        with self._desktop_selection_lock(base_conversation_key):
            current = self._get_session(conversation_key)
            cwd = self._new_project_path(arg, current.get("cwd"))
            name = self._project_workspace_name(base_conversation_key, cwd)
            workspace_key = self._workspace_key(base_conversation_key, name)
            if self.codex.is_running(workspace_key):
                raise ValueError("该项目工作区有任务运行中，请稍后再新建会话。")
            updates = {
                "cwd": cwd,
                "agent": resolve_session_agent(self.config, current),
                "codexClient": "",
                "desktopProjectId": "",
                "codexThreadId": "",
                "claudeSessionId": "",
                "desktopPendingId": "",
            }
            for field in ("codexAccount", "claudeAccount", "codexModel", "codexReasoningEffort", "claudeModel", "claudeEffort"):
                if field in current:
                    updates[field] = current[field]
            self.state.upsert_workspace(
                base_conversation_key, name, cwd, project_client="cli",
            )
            self.state.update_session(workspace_key, **updates)
            self.state.set_active_workspace(base_conversation_key, name)
            self.desktop_thread_cache.pop(workspace_key, None)
            self.desktop_project_cache.pop(workspace_key, None)
            self.desktop_project_cache.pop(conversation_key, None)
            self.session_selection_cache.pop(workspace_key, None)
        self._send_text(account, user_id,
            f"已切换到项目工作区：{name}\n目录：{cwd}\n已准备新的 {self._agent_label(updates['agent'])} 会话；直接发送任务即可开始。")

    def _handle_new_desktop_project_command(self, account, user_id, base_conversation_key,
                                            conversation_key, arg):
        current = self._get_session(conversation_key)
        cwd = self._new_project_path(arg, current.get("cwd"))
        codex_account = self._desktop_account(conversation_key)
        projects = self.desktop.projects(codex_account)
        project = next((p for p in projects if cwd in (p.get("roots") or [])), None)
        existed = project is not None
        if not project:
            project = self.desktop.create_project(
                codex_account, Path(cwd).name or "新项目", cwd, uuid.uuid4().hex
            )
        with self._desktop_selection_lock(base_conversation_key):
            self.desktop_account_selection[base_conversation_key] = codex_account["name"]
            self.state.set_active_workspace(base_conversation_key, self.state.DEFAULT_WORKSPACE)
            self.state.update_session(
                base_conversation_key, agent="codex", codexClient="desktop", codexThreadId="",
                codexAccount=codex_account["name"], cwd=cwd, desktopProjectId=project["id"],
                desktopPendingId=uuid.uuid4().hex, codexModel="", codexReasoningEffort="",
            )
            self.desktop_thread_cache.pop(base_conversation_key, None)
            self.desktop_project_cache.pop(base_conversation_key, None)
        prefix = "已切换到已有桌面项目" if existed else "已创建桌面 Codex 项目"
        self._send_text(account, user_id,
            f"{prefix}：{project['name']}\n目录：{cwd}\n已准备新的桌面 Codex 会话；发送第一条任务时创建。")

    def _workspace_key(self, base_conversation_key, workspace_name):
        return self.state.workspace_conversation_key(base_conversation_key, workspace_name)

    def _format_workspace_list(self, base_conversation_key):
        active = self.state.get_active_workspace(base_conversation_key)
        lines = ["工作区：", f"当前: {active}", ""]
        entries = [
            {
                "name": self.state.DEFAULT_WORKSPACE,
                "cwd": self._get_session(base_conversation_key).get("cwd"),
            }
        ]
        entries.extend(self.state.list_workspaces(base_conversation_key))
        for item in entries:
            name = item.get("name") or self.state.DEFAULT_WORKSPACE
            key = self._workspace_key(base_conversation_key, name)
            session = self._get_session(key)
            cwd = session.get("cwd") or item.get("cwd") or self.config["codex"]["workingDirectory"]
            marker = "*" if name == active else "-"
            status = "running" if self.codex.is_running(key) else "idle"
            agent = resolve_session_agent(self.config, session)
            session_id = (
                (session.get("claudeSessionId") or "")
                if agent == "claude"
                else (session.get("codexThreadId") or "")
            )[:12] or "-"
            lines.append(f"{marker} {name} [{status}]")
            lines.append(f"  cwd: {cwd}")
            lines.append(f"  agent: {agent}")
            lines.append(f"  session: {session_id}")
        if len(entries) == 1:
            lines.extend(["", "还没有添加项目工作区。"])
        lines.extend(
            [
                "",
                "用法：",
                "/ws add <名称> <路径>",
                "/new-project <目录>（缩写 /n-p）",
                "/d-p-n <目录> 在桌面 Codex 创建原生项目（/d-new-project、/d-n-p）",
                "/ws use <名称>",
                "/ws agent <名称> <codex|claude>",
                "/ws run <名称> <任务>",
                "/ws reset <名称>",
            ]
        )
        return "\n".join(lines)

    def _workspace_help_text(self):
        return "\n".join(
            [
                "工作区命令：",
                "/ws 或 /ws list 查看当前微信用户的项目工作区",
                "/ws add <名称> <路径> 添加项目工作区",
                "/new-project <目录> 新建并切换 CLI 项目工作区（缩写 /n-p）",
                "/d-p-n <目录> 新建并切换桌面 Codex 原生项目（/d-new-project、/d-n-p）",
                "/ws use <名称> 切换当前工作区",
                "/ws agent <名称> <codex|claude> 设置指定工作区使用的 Agent",
                "/ws run <名称> <任务> 在指定工作区派发任务",
                "/ws reset <名称> 取消运行中的任务并重置该工作区当前 Agent 会话",
                "名称示例：a、project-a、work_1",
            ]
        )

    def _handle_workspace_command(self, account, user_id, base_conversation_key, conversation_key, command):
        parts = command.split(maxsplit=3)
        action = parts[1].lower() if len(parts) >= 2 else "list"
        if action in {"list", "ls"}:
            self._send_text(account, user_id, self._format_workspace_list(base_conversation_key))
            return
        if action in {"help", "-h", "--help"}:
            self._send_text(account, user_id, self._workspace_help_text())
            return
        if action == "add":
            if len(parts) < 4:
                self._send_text(account, user_id, "用法：/ws add <名称> <路径>")
                return
            name = parts[2].strip()
            error = self._validate_workspace_name(name)
            if error:
                self._send_text(account, user_id, error)
                return
            cwd, error = self._resolve_cwd(parts[3], self.config["codex"]["workingDirectory"])
            if error:
                self._send_text(account, user_id, error)
                return
            self.state.upsert_workspace(base_conversation_key, name, cwd)
            workspace_key = self._workspace_key(base_conversation_key, name)
            self.state.update_session(workspace_key, cwd=cwd, codexThreadId="", claudeSessionId="", desktopPendingId="")
            self._send_text(
                account,
                user_id,
                f"已添加工作区: {name}\nCWD: {cwd}\n发送 /ws use {name} 切换，或 /ws run {name} <任务> 直接派活。",
            )
            return
        if action == "use":
            if len(parts) < 3:
                self._send_text(account, user_id, "用法：/ws use <名称>")
                return
            name = parts[2].strip()
            error = self._validate_workspace_name(name, allow_default=True)
            if error:
                self._send_text(account, user_id, error)
                return
            item = self.state.get_workspace(base_conversation_key, name)
            if name != self.state.DEFAULT_WORKSPACE and not item:
                self._send_text(account, user_id, f"未知工作区: {name}\n发送 /ws 查看已添加工作区。")
                return
            with self._desktop_selection_lock(base_conversation_key):
                self.state.set_active_workspace(base_conversation_key, name)
                if item and item.get("cwd"):
                    self.state.update_session(self._workspace_key(base_conversation_key, name), cwd=item["cwd"])
                workspace_key = self._workspace_key(base_conversation_key, name)
                session = self._get_session(workspace_key)
                cwd = session.get("cwd")
                self._send_text(account, user_id, f"已切换当前工作区: {name}\nCWD: {cwd}")
                if session.get("codexClient") == "desktop" and session.get("codexThreadId"):
                    run_key = self._desktop_execution_key(workspace_key)
                    codex_account = resolve_session_codex_account(self.config, session)
                    thread_id = session["codexThreadId"]
                    latest = self.desktop.latest_result(codex_account, thread_id)
                    self._send_desktop_selected_result(
                        account, user_id, run_key, thread_id[:12], latest,
                        bool(self._desktop_running_context(thread_id)),
                    )
            return
        if action == "agent":
            if len(parts) < 4:
                self._send_text(account, user_id, "用法：/ws agent <名称> <codex|claude>")
                return
            name = parts[2].strip()
            error = self._validate_workspace_name(name, allow_default=True)
            if error:
                self._send_text(account, user_id, error)
                return
            item = self.state.get_workspace(base_conversation_key, name)
            if name != self.state.DEFAULT_WORKSPACE and not item:
                self._send_text(account, user_id, f"未知工作区: {name}\n发送 /ws 查看已添加工作区。")
                return
            target = normalize_agent(parts[3])
            if not target:
                self._send_text(account, user_id, "未知 Agent。可用：codex、claude")
                return
            workspace_key = self._workspace_key(base_conversation_key, name)
            if self.codex.is_running(workspace_key):
                self._send_text(account, user_id, "该工作区有任务运行中，请等待结束或 /ws reset 后再切换 Agent。")
                return
            self.state.update_session(workspace_key, agent=target)
            self._send_text(account, user_id, f"已设置工作区 {name} 的 Agent: {target}")
            return
        if action == "run":
            if len(parts) < 4:
                self._send_text(account, user_id, "用法：/ws run <名称> <任务>")
                return
            name = parts[2].strip()
            prompt = parts[3].strip()
            error = self._validate_workspace_name(name, allow_default=True)
            if error:
                self._send_text(account, user_id, error)
                return
            if not prompt:
                self._send_text(account, user_id, "任务内容不能为空。")
                return
            item = self.state.get_workspace(base_conversation_key, name)
            if name != self.state.DEFAULT_WORKSPACE and not item:
                self._send_text(account, user_id, f"未知工作区: {name}\n请先发送 /ws add {name} <路径>。")
                return
            workspace_key = self._workspace_key(base_conversation_key, name)
            if item and item.get("cwd"):
                self.state.update_session(workspace_key, cwd=item["cwd"])
            self.state.touch_workspace(base_conversation_key, name)
            self._run_codex_and_reply(account, user_id, self._desktop_execution_key(workspace_key), prompt)
            return
        if action in {"reset", "cancel"}:
            if len(parts) < 3:
                self._send_text(account, user_id, "用法：/ws reset <名称>")
                return
            name = parts[2].strip()
            error = self._validate_workspace_name(name, allow_default=True)
            if error:
                self._send_text(account, user_id, error)
                return
            item = self.state.get_workspace(base_conversation_key, name)
            if name != self.state.DEFAULT_WORKSPACE and not item:
                self._send_text(account, user_id, f"未知工作区: {name}")
                return
            workspace_key = self._workspace_key(base_conversation_key, name)
            run_key = self._desktop_execution_key(workspace_key)
            killed = self._cancel_runner(run_key, reset_session=True)
            if run_key != workspace_key:
                self.state.reset_session(workspace_key, agent="codex")
            self.state.update_session(workspace_key, desktopPendingId="")
            self._clear_pending_guidance(run_key)
            agent = resolve_session_agent(self.config, self._get_session(workspace_key))
            message = f"已取消正在运行的 {self._agent_label(agent)} 并重置该工作区。" if killed else "已重置该工作区。"
            self._send_text(account, user_id, message)
            return
        self._send_text(account, user_id, self._workspace_help_text())

    def _typing_ticket(self, account, user_id, context_token):
        if not context_token:
            return ""
        try:
            response = self._api_for_account(account).get_config(user_id, context_token)
        except Exception as err:
            log.warn(f"get typing ticket failed account={account['accountId']} user={user_id}: {err}")
            return ""
        return response.get("typing_ticket") or ""

    def _format_agents(self, conversation_key):
        current = resolve_session_agent(self.config, self._get_session(conversation_key))
        lines = ["可用 Agent："]
        for name in ("codex", "claude"):
            marker = "*" if name == current else "-"
            label = "Codex CLI" if name == "codex" else "Claude Code CLI"
            lines.append(f"{marker} {name} - {label}")
        lines.extend(["", "切换：/agent codex 或 /agent claude"])
        return "\n".join(lines)

    def _handle_agent_switch(self, account, user_id, conversation_key, selector):
        session = self._get_session(conversation_key)
        current = resolve_session_agent(self.config, session)
        if not selector:
            self._send_text(account, user_id, self._format_agents(conversation_key))
            return
        target = selector.strip().lower()
        aliases = {
            "codex-cli": "codex",
            "codex_cli": "codex",
            "claude-code": "claude",
            "claude_code": "claude",
            "claude-cli": "claude",
            "claude_cli": "claude",
        }
        target = aliases.get(target, target)
        if target not in {"codex", "claude"}:
            self._send_text(account, user_id, "未知 Agent。可用：codex、claude")
            return
        if target == current:
            self._send_text(account, user_id, f"当前已在使用 Agent: {target}")
            return
        if self.codex.is_running(conversation_key):
            self._send_text(account, user_id, "当前工作区有任务运行中，请等待结束或 /interrupt 后再切换 Agent。")
            return
        self.state.update_session(conversation_key, agent=target)
        self._send_text(
            account,
            user_id,
            f"已切换 Agent: {target}\nCodex 和 Claude 会话彼此独立，不会共享上下文。",
        )

    def _handle_current_agent_account_switch(self, account, user_id, conversation_key, selector):
        session = self._get_session(conversation_key)
        agent = resolve_session_agent(self.config, session)
        if agent == "claude":
            self._handle_claude_switch(account, user_id, conversation_key, selector)
            return
        self._handle_codex_switch(account, user_id, conversation_key, selector)

    def _handle_codex_switch(self, account, user_id, conversation_key, selector):
        session = self._get_session(conversation_key)
        current_agent = resolve_session_agent(self.config, session)
        switching_agent = current_agent != "codex"
        if switching_agent and self.codex.is_running(conversation_key):
            self._send_text(account, user_id, "当前工作区有任务运行中，请等待结束或 /interrupt 后再切换 Agent。")
            return
        current = resolve_session_codex_account(self.config, session)
        if not selector:
            if switching_agent:
                self.state.update_session(conversation_key, agent="codex")
            self._send_text(
                account,
                user_id,
                "\n".join(
                    [
                        "已切换 Agent: codex" if switching_agent else "当前 Agent: codex",
                        f"当前 Codex 账号: {current.get('name')}",
                        f"CODEX_HOME: {current.get('codexHome')}",
                        "",
                        "查看全部：/codex-accounts",
                        "切换：/account <编号或名称> 或 /codex <编号或名称>",
                    ]
                ),
            )
            return
        lowered = selector.lower()
        if lowered in {"next", "n", "下一个"}:
            target = adjacent_codex_account(self.config, current.get("name"), 1)
        elif lowered in {"prev", "previous", "p", "上一个"}:
            target = adjacent_codex_account(self.config, current.get("name"), -1)
        else:
            target = find_codex_account(self.config, selector)
        if not target:
            self._send_text(
                account,
                user_id,
                "未知 Codex 账号。可用账号：\n"
                + "\n".join(f"{i}. {name}" for i, name in enumerate(codex_account_names(self.config), start=1)),
            )
            return
        name = target.get("name")
        if name == current.get("name") and not switching_agent:
            self._send_text(account, user_id, f"当前已在使用 Codex 账号: {name}")
            return
        updates = {"agent": "codex"}
        account_changed = name != current.get("name")
        if account_changed:
            updates.update(codexAccount=name, codexThreadId="")
        self.state.update_session(conversation_key, **updates)
        lines = []
        if switching_agent:
            lines.append("已切换 Agent: codex")
        if account_changed:
            lines.append(f"已切换 Codex 账号: {name}")
            lines.append(f"CODEX_HOME: {target.get('codexHome')}")
            lines.append("已重置当前 Codex thread。")
        else:
            lines.append(f"当前已在使用 Codex 账号: {name}")
        self._send_text(
            account,
            user_id,
            "\n".join(lines),
        )

    def _handle_codex_login(self, account, user_id, conversation_key, command):
        if not self._is_admin(user_id):
            self._send_text(account, user_id, "只有 adminUsers 可以远程登录 Codex CLI。")
            return
        parts = command.split()
        action = parts[1].lower() if len(parts) > 1 else ""
        if action in {"status", "cancel"}:
            if len(parts) > 3:
                self._send_text(account, user_id, "用法：/codex-login status|cancel [账号名]")
                return
            selector = parts[2] if len(parts) == 3 else ""
        else:
            if len(parts) > 3:
                self._send_text(account, user_id, "用法：/codex-login [账号名] [期望邮箱]")
                return
            selector = parts[1] if len(parts) > 1 else ""
        target = (find_codex_account(self.config, selector) if selector else
                  resolve_session_codex_account(self.config, self._get_session(conversation_key)))
        if not target:
            self._send_text(account, user_id, f"未知 Codex 账号：{selector}。发送 /codex-accounts 查看可用账号。")
            return
        name = target["name"]
        home = target["codexHome"]
        if action == "status":
            logged_in, status = login_status(self.config["codex"].get("bin") or "codex", home)
            email = cached_email(home) if logged_in else ""
            lines = [f"Codex 账号：{name}", f"CODEX_HOME：{home}",
                     f"设备码登录进行中：{'是' if self.codex_device_login.is_running(home) else '否'}",
                     f"CLI 登录状态：{status or ('已登录' if logged_in else '未登录')}"]
            if email:
                lines.append(f"邮箱：{email}")
            self._send_text(account, user_id, "\n".join(lines))
            return
        if action == "cancel":
            cancelled = self.codex_device_login.cancel(home)
            self._send_text(account, user_id, f"已取消 {name} 的设备码登录。" if cancelled else f"{name} 当前没有进行中的设备码登录。")
            return
        expected_email = parts[2] if len(parts) > 2 else ""
        if expected_email and ("@" not in expected_email or len(expected_email) > 254):
            self._send_text(account, user_id, "期望邮箱格式无效。用法：/codex-login [账号名] [期望邮箱]")
            return
        if self.codex_device_login.is_running(home):
            self._send_text(account, user_id, f"{name} 的设备码登录已在进行中。发送 /codex-login status {name} 查看状态。")
            return
        for run in self._active_runs():
            if run.get("agent") != "codex":
                continue
            running_session = self._get_session(run.get("conversationKey"))
            if resolve_session_codex_account(self.config, running_session).get("codexHome") == home:
                self._send_text(account, user_id, f"{name} 当前有 Codex 任务运行，请任务结束后再登录。")
                return

        def on_code(url, code):
            lines = [f"请为 Codex 账号 {name} 完成登录：", f"打开：{url}", f"输入一次性代码：{code}",
                     "代码 15 分钟后过期。仅在你本人发起此命令时继续。"]
            if expected_email:
                lines.append(f"请在官方页面选择账号：{expected_email}")
            self._send_text(account, user_id, "\n".join(lines))

        def on_done(message):
            self._send_text(account, user_id, f"Codex 账号 {name}：{message}")

        started = self.codex_device_login.start(home, on_code, on_done, expected_email=expected_email)
        if started:
            self._send_text(account, user_id, f"正在为 {name} 启动 Codex 设备码登录。登录地址和代码将单独发送。")
        else:
            self._send_text(account, user_id, f"{name} 的设备码登录已在进行中。")

    def _handle_claude_switch(self, account, user_id, conversation_key, selector):
        session = self._get_session(conversation_key)
        current_agent = resolve_session_agent(self.config, session)
        switching_agent = current_agent != "claude"
        if switching_agent and self.codex.is_running(conversation_key):
            self._send_text(account, user_id, "当前工作区有任务运行中，请等待结束或 /interrupt 后再切换 Agent。")
            return
        current = resolve_session_claude_account(self.config, session)
        if not selector:
            if switching_agent:
                self.state.update_session(conversation_key, agent="claude")
            self._send_text(
                account,
                user_id,
                "\n".join(
                    [
                        "已切换 Agent: claude" if switching_agent else "当前 Agent: claude",
                        f"当前 Claude 账号: {current.get('name')}",
                        f"CLAUDE_CONFIG_DIR: {current.get('claudeConfigDir')}",
                        "",
                        "查看全部：/claude-accounts",
                        "切换：/account <编号或名称> 或 /claude <编号或名称>",
                    ]
                ),
            )
            return
        lowered = selector.lower()
        if lowered in {"next", "n", "下一个"}:
            target = adjacent_claude_account(self.config, current.get("name"), 1)
        elif lowered in {"prev", "previous", "p", "上一个"}:
            target = adjacent_claude_account(self.config, current.get("name"), -1)
        else:
            target = find_claude_account(self.config, selector)
        if not target:
            self._send_text(
                account,
                user_id,
                "未知 Claude 账号。可用账号：\n"
                + "\n".join(f"{i}. {name}" for i, name in enumerate(claude_account_names(self.config), start=1)),
            )
            return
        name = target.get("name")
        if name == current.get("name") and not switching_agent:
            self._send_text(account, user_id, f"当前已在使用 Claude 账号: {name}")
            return
        updates = {"agent": "claude"}
        account_changed = name != current.get("name")
        if account_changed:
            updates.update(claudeAccount=name, claudeSessionId="")
        self.state.update_session(conversation_key, **updates)
        lines = []
        if switching_agent:
            lines.append("已切换 Agent: claude")
        if account_changed:
            lines.append(f"已切换 Claude 账号: {name}")
            lines.append(f"CLAUDE_CONFIG_DIR: {target.get('claudeConfigDir')}")
            lines.append("已重置当前 Claude session。")
        else:
            lines.append(f"当前已在使用 Claude 账号: {name}")
        self._send_text(
            account,
            user_id,
            "\n".join(lines),
        )

    def _current_codex_model(self, conversation_key, session):
        if session.get("codexClient") != "desktop":
            return resolve_session_model(self.config, session)
        current = {"model": session.get("codexModel") or "",
                   "reasoningEffort": session.get("codexReasoningEffort") or "",
                   "source": "本地缓存（会话模型尚未确认）"}
        thread_id = session.get("codexThreadId") or ""
        if thread_id:
            try:
                native = self.desktop.read(resolve_session_codex_account(self.config, session), thread_id)
                if native.get("model"):
                    current.update(model=native["model"], reasoningEffort=native.get("reasoningEffort") or "",
                                   source="会话设置（默认沿用）")
                    self.state.update_session(conversation_key, codexModel=current["model"],
                                              codexReasoningEffort=current["reasoningEffort"])
                else:
                    current["warning"] = "未读取到会话模型；续聊时由 Codex 解析，不使用微信全局模型覆盖。"
            except Exception as err:
                current["warning"] = f"读取会话模型失败：{err}；显示的是缓存设置。"
        else:
            current.update(model="", reasoningEffort="", source="新会话默认（启动时确定）")
        override = session.get("desktopModelOverride") or {}
        if override:
            current.update(model=override.get("model") or "", reasoningEffort=override.get("reasoningEffort") or "",
                           source="微信指定（下一轮生效，当前任务不变）")
        return current

    def _available_model_options(self, session=None):
        if self._model_options is not None:
            return self._model_options
        if (session or {}).get("codexClient") == "desktop":
            return self.desktop.model_options(resolve_session_codex_account(self.config, session))
        return model_options(self.config)

    def _available_claude_model_options(self, session=None):
        if self._claude_model_options is not None:
            return self._claude_model_options
        session = session or {}
        account = resolve_session_claude_account(self.config, session)
        cwd = session.get("cwd") or (self.config.get("claude") or {}).get("workingDirectory") or self.config["codex"]["workingDirectory"]
        return claude_model_options(
            self.config,
            claude_config_dir=account.get("claudeConfigDir") or "",
            cwd=cwd,
        )

    @staticmethod
    def _format_model_options_for_wechat(options):
        lines = ["可切换模型（发送 /model 编号 切换）："]
        last_model = None
        for index, option in enumerate(options, start=1):
            model = option.get("model") or ""
            if model != last_model:
                if last_model is not None:
                    lines.append("")
                lines.append(model)
                last_model = model
            lines.append(f"{index}. {format_model_option(option)}")
        return "\n".join(lines)

    @staticmethod
    def _format_claude_model_options_for_wechat(options):
        lines = ["可切换 Claude 模型（发送 /model 编号 切换）："]
        last_group = None
        for index, option in enumerate(options, start=1):
            group = option.get("groupLabel")
            if not group and option.get("effort"):
                group = option.get("model") or ""
            if group and group != last_group:
                if last_group is not None:
                    lines.append("")
                lines.append(group)
                last_group = group
            lines.append(f"{index}. {format_claude_model_option(option)}")
        lines.extend(MultiWechatCodexService._claude_effort_note_lines(options))
        return "\n".join(lines)

    @staticmethod
    def _claude_effort_note_lines(options):
        efforts = []
        sources = set()
        for option in options:
            for effort in option.get("efforts") or []:
                effort = str(effort or "").strip()
                if effort and effort not in efforts:
                    efforts.append(effort)
            source = str(option.get("effortSource") or "").strip()
            if source:
                sources.add(source)
        if not efforts:
            return []
        lines = ["", f"CLI 全局 --effort: {', '.join(efforts)}"]
        if "cli-help-global" in sources:
            lines.append("说明：这些 effort 来自 claude --help 的全局参数，不是逐模型验证矩阵。")
        elif "fallback-global" in sources:
            lines.append("说明：当前未查询到 Claude CLI，以上 effort 是兜底提示，不是逐模型验证矩阵。")
        else:
            lines.append("说明：这些 effort 是选项附带的全局提示，不是逐模型验证矩阵。")
        lines.append("编号只切模型；如需指定 CLI effort，可发送 /model sonnet:high。")
        lines.append("ultracode 属于 Claude 交互式 /effort 会话模式，本服务不会把它枚举到所有模型。")
        return lines

    def _handle_model_switch(self, account, user_id, conversation_key, selector, list_only=False):
        conversation_key = self._desktop_execution_key(conversation_key)
        session = self._get_session(conversation_key)
        agent = resolve_session_agent(self.config, session)
        if agent == "claude":
            self._handle_claude_model_switch(account, user_id, conversation_key, selector, list_only=list_only)
            return
        desktop = session.get("codexClient") == "desktop"
        if desktop and selector.lower() in {"auto", "default", "inherit"}:
            self.state.update_session(conversation_key, desktopModelOverride={})
            current = self._current_codex_model(conversation_key, self._get_session(conversation_key))
            lines = ["已恢复自动沿用会话模型，不重置会话。",
                     f"当前: {current.get('model') or 'default'}:{current.get('reasoningEffort') or 'default'}",
                     "来源：" + current["source"]]
            if current.get("warning"):
                lines.append(current["warning"])
            self._send_text(account, user_id, "\n".join(lines))
            return
        try:
            options = self.desktop_model_options_cache.get(conversation_key) if desktop and selector.isdigit() else None
            if options is None:
                options = self._available_model_options(session)
            if desktop and (not selector or list_only):
                self.desktop_model_options_cache[conversation_key] = list(options)
        except Exception as exc:
            self._send_text(account, user_id, f"无法获取模型列表：{exc}")
            return
        current = self._current_codex_model(conversation_key, session)
        if not options:
            self._send_text(account, user_id, "当前桌面账号未返回可用模型，请检查该账号登录状态。" if desktop else
                            "没有可用模型选项。可在 config.json 的 codex.modelOptions 中配置。")
            return
        if list_only:
            self._send_text(
                account,
                user_id,
                self._format_model_options_for_wechat(options),
            )
            return
        if not selector:
            lines = [
                "Codex 模型：",
                f"当前: {current.get('model') or 'default'}:{current.get('reasoningEffort') or 'default'}",
                "",
            ]
            for index, option in enumerate(options, start=1):
                marker = "*" if (
                    option.get("model") == current.get("model")
                    and option.get("reasoningEffort") == current.get("reasoningEffort")
                ) else "-"
                lines.append(f"{marker} {index}. {format_model_option(option)}")
            lines.extend(["", "切换：/model <编号或 model:reasoning>", "例如：/model 2 或 /model gpt-5.5:high"])
            if desktop:
                lines.extend(["来源：" + current["source"], "切换只影响下一轮，保留会话和历史；默认沿用会话上次设置。",
                              "取消未生效的手动设置：/model auto"])
                if current.get("warning"):
                    lines.append(current["warning"])
            self._send_text(account, user_id, "\n".join(lines))
            return
        target = find_model_option(options, selector)
        if not target:
            self._send_text(account, user_id, "未知模型选项。发送 /model 查看可用选项。")
            return
        if (
            target.get("model") == current.get("model")
            and target.get("reasoningEffort") == current.get("reasoningEffort")
        ):
            label = "下一轮已设置为" if desktop and session.get("desktopModelOverride") else "当前已在使用"
            self._send_text(account, user_id, f"{label}: {format_model_option(target)}")
            return
        if desktop:
            self.state.update_session(conversation_key, desktopModelOverride={
                "model": target.get("model") or "", "reasoningEffort": target.get("reasoningEffort") or "",
            })
            self._send_text(account, user_id, f"下一轮将使用 {format_model_option(target)}\n"
                            "保留当前会话和历史，不重置；正在执行的任务不受影响。")
            return
        self.state.update_session(
            conversation_key,
            codexModel=target.get("model") or "",
            codexReasoningEffort=target.get("reasoningEffort") or "",
            codexThreadId="",
        )
        self._send_text(account, user_id, f"已经切换到 {format_model_option(target)} 模型\n已重置当前 Codex thread。")

    def _handle_claude_model_switch(self, account, user_id, conversation_key, selector, list_only=False):
        session = self._get_session(conversation_key)
        try:
            options = self._available_claude_model_options(session)
        except Exception as exc:
            self._send_text(account, user_id, f"无法获取 Claude 模型列表：{exc}")
            return
        current = resolve_session_claude_model(self.config, session)
        if not options:
            self._send_text(account, user_id, "没有可用 Claude 模型选项。可在 config.json 的 claude.modelOptions 中配置。")
            return
        if list_only:
            self._send_text(
                account,
                user_id,
                self._format_claude_model_options_for_wechat(options),
            )
            return
        if not selector:
            lines = [
                "Claude 模型：",
                f"当前: {current.get('model') or 'default'}:{current.get('effort') or 'default'}",
                "",
            ]
            for index, option in enumerate(options, start=1):
                marker = "*" if (
                    option.get("model") == current.get("model")
                    and (
                        option.get("effort") == current.get("effort")
                        or not option.get("effort")
                    )
                ) else "-"
                lines.append(f"{marker} {index}. {format_claude_model_option(option)}")
            lines.extend(self._claude_effort_note_lines(options))
            lines.extend(["", "切换：/model <编号或 model:effort>", "例如：/model 2 或 /model sonnet:high"])
            self._send_text(account, user_id, "\n".join(lines))
            return
        target = find_claude_model_option(options, selector)
        if not target:
            self._send_text(account, user_id, "未知 Claude 模型选项。发送 /model 查看可用选项。")
            return
        if target.get("model") == current.get("model") and target.get("effort") == current.get("effort"):
            self._send_text(account, user_id, f"当前已在使用: {format_claude_model_option(target)}")
            return
        self.state.update_session(
            conversation_key,
            claudeModel=target.get("model") or "",
            claudeEffort=target.get("effort") or "",
            claudeSessionId="",
        )
        self._send_text(account, user_id, f"已经切换到 {format_claude_model_option(target)} 模型\n已重置当前 Claude session。")

    def _handle_runner_switch(self, account, user_id, selector):
        current = str(self.config.get("codex", {}).get("runner") or "exec").strip().lower() or "exec"
        aliases = {
            "exec": "exec",
            "cli": "exec",
            "app-server": "app-server",
            "appserver": "app-server",
            "server": "app-server",
        }
        if not selector:
            self._send_text(
                account,
                user_id,
                "\n".join(
                    [
                        f"当前 Codex runner: {current}",
                        "",
                        "切换：/runner exec",
                        "切换：/runner app-server",
                        "注意：运行时切换只修改当前服务进程内配置；重启后仍以 config.json 为准。",
                    ]
                ),
            )
            return
        target = aliases.get(selector.strip().lower())
        if not target:
            self._send_text(account, user_id, "未知 runner。可用：exec、app-server")
            return
        if target == current:
            self._send_text(account, user_id, f"当前已在使用 Codex runner: {target}")
            return
        try:
            if hasattr(self.codex, "terminate_agent"):
                self.codex.terminate_agent("codex")
            else:
                self.codex.terminate_all()
        except Exception:
            pass
        self.config.setdefault("codex", {})["runner"] = target
        new_runner = self._create_codex_runner(self.config)
        if hasattr(self.codex, "replace_runner"):
            self.codex.replace_runner("codex", new_runner)
        else:
            self.codex = new_runner
        with self.pending_guidance_guard:
            self.pending_guidance.clear()
        self._send_text(
            account,
            user_id,
            f"已切换 Codex runner: {target}\n已关闭旧 runner 的后台进程。重启后仍以 config.json 为准。",
        )

    def _get_session(self, conversation_key):
        return self.state.get_session(
            conversation_key,
            self.config["codex"]["workingDirectory"],
            default_codex_account(self.config),
            self.config.get("defaultAgent") or "codex",
        )

    def _ensure_account_session(self, account):
        account_id = (account or {}).get("accountId")
        user_id = (account or {}).get("userId")
        if not account_id or not user_id:
            return False
        self._get_session(self.state.conversation_key(account_id, user_id))
        return True

    def _send_login_qr(self, account, user_id, qr_content):
        context_token = self.state.get_context_token(account["accountId"], user_id)
        if not context_token:
            raise RuntimeError("缺少 context_token，无法发送登录二维码")
        path = render_qr_png(qr_content, PROJECT_DIR, self.state.state_dir / "login_qr")
        with self.media_semaphore:
            send_local_media(self._api_for_account(account), user_id, context_token, path, "image")
        return path

    def _start_typing_loop(self, account, user_id):
        context_token = self.state.get_context_token(account["accountId"], user_id)
        typing_ticket = self._typing_ticket(account, user_id, context_token)
        if not typing_ticket:
            return lambda: None
        stop_event = threading.Event()
        client = self._api_for_account(account)

        def loop():
            while not stop_event.is_set():
                try:
                    client.send_typing(user_id, typing_ticket, TYPING_STATUS_TYPING)
                except Exception:
                    pass
                stop_event.wait(10)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()

        def stop():
            stop_event.set()
            try:
                client.send_typing(user_id, typing_ticket, TYPING_STATUS_CANCEL)
            except Exception:
                pass

        return stop

    def _send_text(self, account, user_id, text):
        context_token = self.state.get_context_token(account["accountId"], user_id)
        if not context_token:
            log.error(f"cannot reply: missing context_token account={account['accountId']} user={user_id}")
            return
        self._send_text_with_context_token(account, user_id, context_token, text)

    def _send_text_with_context_token(self, account, user_id, context_token, text):
        if not context_token:
            log.error(f"cannot reply: missing context_token account={account['accountId']} user={user_id}")
            return
        client = self._api_for_account(account)
        for chunk in split_text(text, int(self.config.get("textChunkLimit") or 4000)):
            client.send_text(user_id, context_token, chunk)

    def _schedule_restart(self):
        def restart():
            time.sleep(1)
            log.info("restart requested; exiting for supervisor restart")
            try:
                self.stop()
            finally:
                os._exit(0)

        threading.Thread(target=restart, daemon=True).start()

    def _is_allowed(self, user_id):
        allowed = set(self.config.get("allowedUsers") or [])
        return not allowed or user_id in allowed

    def _is_admin(self, user_id):
        admins = set(self.config.get("adminUsers") or [])
        return bool(admins) and user_id in admins

    @staticmethod
    def _help_text(account_id, full=False):
        if not full:
            return "\n".join([
                "常用命令：",
                "/status 状态；/active 运行中的任务",
                "/sessions [codex|claude|all] CLI 会话；/session 管理 CLI 会话",
                "/session delete 3 4 5 批量删除预览；60 秒内再加 confirm 确认（管理员）",
                "/d-projects 桌面项目；/d-sessions all 2 查看会话第 2 页",
                "/d-session 管理桌面会话；/d-account 选择桌面账号",
                "/new-project <目录> 新建 CLI 工作区；/d-p-n <目录> 新建桌面原生项目",
                "/ws 工作区；/agent 切换 Agent；/account 切换账号",
                "/model 查看/切换模型；桌面会话用 /model auto 恢复自动沿用",
                "/guide <内容> 引导；/interrupt 中断；/reset 重置",
                "/update cli 更新 Codex CLI；/update desktop 触发桌面内置更新（管理员，不消耗 token）",
                "桌面内置更新仅 macOS，需先打开 App 并授予辅助功能权限；最终安装按 App 提示操作",
                "/update 查看更新用法；/update status 查看结果及日志",
                "/help all 查看全部命令及归档、删除用法",
                f"当前 bot accountId: {account_id}",
            ])
        return "\n".join(
            [
                "命令：",
                "/status 查看当前工作区状态",
                "/reset 重置当前工作区当前 Agent 会话",
                "/cancel 或 /interrupt 中断当前任务，保留当前会话",
                "/interrupt <新任务> 中断当前任务，保留当前会话并改做新任务",
                "/guide <补充要求> 在任务运行中追加引导；直接发普通消息也会追加",
                "/usage 查看当前 Agent 用量",
                "/usage all 查看配置里所有 Codex 和 Claude 账号的用量",
                "/sessions [codex|claude|all] 查看 CLI 会话",
                "/new-project <目录> 新建并切换 CLI 项目工作区（/n-p）",
                "/d-p-n <目录> 新建并切换桌面原生项目（/d-new-project、/d-n-p）",
                "/sessions archived [codex|claude|all] 查看 CLI 归档会话",
                "/session use|new|archive|unarchive|delete 管理 CLI 会话（/session 看用法）",
                "/session delete 3 4 5 批量删除预览；60 秒内发送 /session delete 3 4 5 confirm（管理员）",
                "/d-projects 查看桌面项目；/d-sessions [all|项目编号] [页码] 查看会话",
                "/d-sessions page <页码> 查看全部项目指定页；/d-session <编号> 快速切换",
                "/d-project use <编号> 切换已有桌面项目并准备新会话",
                "/d-sessions archived [all|项目编号] [页码] 查看桌面归档会话",
                "/d-session new 在当前桌面项目新建会话；/d-session 查看其他操作；/d-account [账号] 选择桌面账号",
                "/d-session delete 3 4 5 桌面批量删除预览；再加 confirm 确认（管理员）",
                "桌面项目和会话使用 Codex 原生数据；任务结果回传微信。",
                "/agents 查看可用 Agent",
                "/agent <codex|claude> 切换当前工作区使用的 CLI",
                "/account 查看或切换当前 Agent 账号",
                "/account <编号|名称|next> 切换当前 Agent 账号",
                "/codex [编号|名称|next] 切到 Codex，可同时切账号",
                "/codex-login [账号名] [期望邮箱] 远程登录 Codex CLI（adminUsers only）",
                "/codex-login status|cancel [账号名] 查看或取消设备码登录（adminUsers only）",
                "/claude [编号|名称|next] 切到 Claude，可同时切账号",
                "/model 查看或切换当前 Agent 的模型和 effort",
                "桌面会话切模型不重置历史、下一轮生效；/model auto 恢复沿用会话设置",
                "/runner 查看或切换 Codex exec/app-server runner",
                UPDATE_HELP,
                "/cwd <path> 切换当前工作区工作目录",
                "/ws 查看项目工作区",
                "/ws add <名称> <路径> 添加项目工作区",
                "/ws agent <名称> <codex|claude> 设置指定工作区 Agent",
                "/ws run <名称> <任务> 指定工作区派活",
                "/active 查看正在交互中的用户",
                "/accounts 或 /users 查看已连接用户",
                "/login [昵称] 新增用户（adminUsers only）",
                "/user rename <昵称|accountId|编号> <新昵称> 修改用户昵称（adminUsers only）",
                "/user delete <昵称|accountId|编号> 删除用户（adminUsers only）",
                "/restart 重启后台服务（adminUsers only）",
                "/help 查看帮助",
                "",
                f"当前 bot accountId: {account_id}",
                "支持媒体标记：[[send_image:/path]] [[send_file:/path]] [[send_video:/path]]",
            ]
        )
