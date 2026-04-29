import concurrent.futures
import os
import re
import threading
import time
from pathlib import Path

from . import logging as log
from .actions import execute_actions, extract_actions
from .codex_accounts import (
    adjacent_codex_account,
    codex_account_names,
    default_codex_account,
    find_codex_account,
    list_codex_accounts,
    resolve_session_codex_account,
)
from .codex_cli import CodexCancelled, CodexCliRunner
from .codex_models import find_model_option, format_model_option, model_options, resolve_session_model
from .codex_usage import format_codex_usage, read_codex_usage
from .config import PROJECT_DIR
from .login import login_with_qr
from .state import StateStore
from .util import markdown_to_plain_text, split_text
from .wechat import MESSAGE_TYPE_USER, TYPING_STATUS_CANCEL, TYPING_STATUS_TYPING, WechatClient, extract_text


class MultiWechatCodexService:
    WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

    def __init__(self, config):
        self.config = config
        self.state = StateStore(
            config["stateDir"],
            save_debounce_ms=int(config.get("state", {}).get("saveDebounceMs") or 0),
        )
        self.codex = CodexCliRunner(config, self.state)
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
        self.monitor_accounts = set()
        self._model_options = None

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
        self.codex.terminate_all()
        self.command_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.state.flush()

    def _start_account_monitor(self, account):
        account_id = account["accountId"]
        if account_id in self.monitor_accounts:
            return
        self.monitor_accounts.add(account_id)
        thread = threading.Thread(target=self._monitor_account, args=(account,), daemon=True)
        thread.start()

    def _monitor_account(self, account):
        account_id = account["accountId"]
        client = self._api_for_account(account)
        get_updates_buf = account.get("getUpdatesBuf") or ""
        log.info(f"monitor started accountId={account_id}")
        failures = 0
        while not self.stop_event.is_set():
            try:
                response = client.get_updates(get_updates_buf)
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
        try:
            conversation_key = self._conversation_key_for_text(base_conversation_key, text)
            if self._can_run_without_conversation_lock(text):
                self._handle_message(account, user_id, base_conversation_key, text, conversation_key)
                return
            if self.config.get("concurrency", {}).get("perConversationSerial", True):
                lock = self._conversation_lock(conversation_key)
                if not lock.acquire(blocking=False):
                    if text.strip() == "/reset":
                        killed = self.codex.cancel(conversation_key)
                        message = "已取消正在运行的 Codex 并重置当前工作区。" if killed else "已重置当前工作区。"
                        self._send_text(account, user_id, message)
                        return
                    self._send_text(account, user_id, "上一条消息还在处理中，请稍后再试。需要放弃当前 Codex 会话时可发送 /reset。")
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
            return self.state.workspace_conversation_key(base_conversation_key, parts[2])
        active = self.state.get_active_workspace(base_conversation_key)
        return self.state.workspace_conversation_key(base_conversation_key, active)

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
            "/status",
            "/usage",
            "/codex-accounts",
            "/codex",
            "/model",
            "/models",
            "/cwd",
            "/restart",
        }

    def _handle_message(self, account, user_id, base_conversation_key, text, conversation_key=None):
        conversation_key = conversation_key or base_conversation_key
        command = text.strip()
        if command == "/help":
            self._send_text(account, user_id, self._help_text(account["accountId"]))
            return
        if command == "/accounts":
            self._send_text(
                account,
                user_id,
                "已连接账号：\n" + "\n".join(a["accountId"] for a in self.state.list_accounts()),
            )
            return
        if command == "/ws" or command.startswith("/ws "):
            self._handle_workspace_command(account, user_id, base_conversation_key, conversation_key, command)
            return
        if command == "/status":
            session = self._get_session(conversation_key)
            codex_account = resolve_session_codex_account(self.config, session)
            model_selection = resolve_session_model(self.config, session)
            workspace_name = self._workspace_name_from_key(base_conversation_key, conversation_key)
            self._send_text(
                account,
                user_id,
                "\n".join(
                    [
                        f"accountId: {account['accountId']}",
                        f"conversation: {conversation_key}",
                        f"workspace: {workspace_name}",
                        f"cwd: {session.get('cwd')}",
                        f"codexAccount: {codex_account.get('name')}",
                        f"codexHome: {codex_account.get('codexHome')}",
                        f"codexModel: {model_selection.get('model') or 'default'}",
                        f"reasoning: {model_selection.get('reasoningEffort') or 'default'}",
                        f"codexThreadId: {(session.get('codexThreadId') or '')[:12]}",
                        "accounts: " + ", ".join(a["accountId"] for a in self.state.list_accounts()),
                    ]
                ),
            )
            return
        if command == "/usage":
            session = self._get_session(conversation_key)
            codex_account = resolve_session_codex_account(self.config, session)
            usage = read_codex_usage(
                self.config["codex"].get("bin") or "codex",
                codex_home=codex_account.get("codexHome") or "",
            )
            self._send_text(account, user_id, format_codex_usage(usage))
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
        if command == "/model" or command == "/models" or command.startswith("/model "):
            selector = command[len("/model"):].strip() if command.startswith("/model ") else ""
            self._handle_model_switch(account, user_id, conversation_key, selector, list_only=command == "/models")
            return
        if command == "/login":
            if not self._is_admin(user_id):
                self._send_text(account, user_id, "只有 adminUsers 可以通过微信触发 /login。")
                return
            self._send_text(account, user_id, "开始新增 Bot 账号登录，请到运行服务的终端扫描二维码。")
            new_account = login_with_qr(
                base_url=self.config["wechat"]["baseUrl"],
                bot_type=self.config["wechat"]["botType"],
                route_tag=self.config["wechat"].get("routeTag"),
                project_dir=PROJECT_DIR,
            )
            self.state.upsert_account(new_account)
            self._start_account_monitor(new_account)
            self._send_text(account, user_id, f"新增账号已连接: {new_account['accountId']}")
            return
        if command == "/restart":
            if not self._is_admin(user_id):
                self._send_text(account, user_id, "只有 adminUsers 可以通过微信触发 /restart。")
                return
            self._send_text(account, user_id, "正在重启服务，稍后可发送 /status 确认。")
            self._schedule_restart()
            return
        if command == "/reset":
            self.state.reset_session(conversation_key)
            self._send_text(account, user_id, "已重置当前工作区会话。")
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
                self.state.update_session(conversation_key, cwd=cwd, codexThreadId="")
                self._send_text(account, user_id, f"已切换 CWD: {cwd}\n已重置当前 Codex thread。")
            return

        workspace_name = self._workspace_name_from_key(base_conversation_key, conversation_key)
        self.state.touch_workspace(base_conversation_key, workspace_name)
        self._run_codex_and_reply(account, user_id, conversation_key, text)

    def _run_codex_and_reply(self, account, user_id, conversation_key, text):
        stop_typing = self._start_typing_loop(account, user_id)
        try:
            result = self.codex.run(conversation_key, text)
        finally:
            stop_typing()
        cleaned, actions = extract_actions(result)
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

    def _workspace_name_from_key(self, base_conversation_key, conversation_key):
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
            return "工作区名称只能包含字母、数字、点、下划线和中划线，长度 1-64，且必须以字母或数字开头。"
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
            thread_id = (session.get("codexThreadId") or "")[:12] or "-"
            lines.append(f"{marker} {name} [{status}]")
            lines.append(f"  cwd: {cwd}")
            lines.append(f"  thread: {thread_id}")
        if len(entries) == 1:
            lines.extend(["", "还没有添加项目工作区。"])
        lines.extend(
            [
                "",
                "用法：",
                "/ws add <名称> <路径>",
                "/ws use <名称>",
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
                "/ws use <名称> 切换当前工作区",
                "/ws run <名称> <任务> 在指定工作区派发任务",
                "/ws reset <名称> 取消运行中的任务并重置该工作区 thread",
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
            self.state.update_session(workspace_key, cwd=cwd, codexThreadId="")
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
            self.state.set_active_workspace(base_conversation_key, name)
            if item and item.get("cwd"):
                self.state.update_session(self._workspace_key(base_conversation_key, name), cwd=item["cwd"])
            cwd = self._get_session(self._workspace_key(base_conversation_key, name)).get("cwd")
            self._send_text(account, user_id, f"已切换当前工作区: {name}\nCWD: {cwd}")
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
            self._run_codex_and_reply(account, user_id, workspace_key, prompt)
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
            killed = self.codex.cancel(workspace_key)
            message = "已取消正在运行的 Codex 并重置该工作区。" if killed else "已重置该工作区。"
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

    def _handle_codex_switch(self, account, user_id, conversation_key, selector):
        session = self._get_session(conversation_key)
        current = resolve_session_codex_account(self.config, session)
        if not selector:
            self._send_text(
                account,
                user_id,
                "\n".join(
                    [
                        f"当前 Codex 账号: {current.get('name')}",
                        f"CODEX_HOME: {current.get('codexHome')}",
                        "",
                        "查看全部：/codex-accounts",
                        "切换：/codex <编号或名称>",
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
        if name == current.get("name"):
            self._send_text(account, user_id, f"当前已在使用 Codex 账号: {name}")
            return
        self.state.update_session(conversation_key, codexAccount=name, codexThreadId="")
        self._send_text(
            account,
            user_id,
            f"已切换 Codex 账号: {name}\nCODEX_HOME: {target.get('codexHome')}\n已重置当前 Codex thread。",
        )

    def _available_model_options(self):
        if self._model_options is not None:
            return self._model_options
        return model_options(self.config)

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

    def _handle_model_switch(self, account, user_id, conversation_key, selector, list_only=False):
        try:
            options = self._available_model_options()
        except Exception as exc:
            self._send_text(account, user_id, f"无法获取模型列表：{exc}")
            return
        session = self._get_session(conversation_key)
        current = resolve_session_model(self.config, session)
        if not options:
            self._send_text(account, user_id, "没有可用模型选项。可在 config.json 的 codex.modelOptions 中配置。")
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
            self._send_text(account, user_id, f"当前已在使用: {format_model_option(target)}")
            return
        self.state.update_session(
            conversation_key,
            codexModel=target.get("model") or "",
            codexReasoningEffort=target.get("reasoningEffort") or "",
            codexThreadId="",
        )
        self._send_text(account, user_id, f"已经切换到 {format_model_option(target)} 模型\n已重置当前 Codex thread。")

    def _get_session(self, conversation_key):
        return self.state.get_session(
            conversation_key,
            self.config["codex"]["workingDirectory"],
            default_codex_account(self.config),
        )

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
        client = self._api_for_account(account)
        context_token = self.state.get_context_token(account["accountId"], user_id)
        if not context_token:
            log.error(f"cannot reply: missing context_token account={account['accountId']} user={user_id}")
            return
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
    def _help_text(account_id):
        return "\n".join(
            [
                "命令：",
                "/status 查看当前工作区状态",
                "/reset 重置当前工作区 Codex 会话",
                "/usage 查看 Codex 5 小时和周限额",
                "/codex-accounts 查看可用 Codex 账号",
                "/codex <编号|名称|next> 切换当前工作区使用的 Codex 账号",
                "/model 查看或切换模型和 reasoning",
                "/cwd <path> 切换当前工作区工作目录",
                "/ws 查看项目工作区",
                "/ws add <名称> <路径> 添加项目工作区",
                "/ws run <名称> <任务> 指定工作区派活",
                "/accounts 查看已连接 Bot 账号",
                "/login 新增 Bot 账号（adminUsers only）",
                "/restart 重启后台服务（adminUsers only）",
                "/help 查看帮助",
                "",
                f"当前 bot accountId: {account_id}",
                "支持媒体标记：[[send_image:/path]] [[send_file:/path]] [[send_video:/path]]",
            ]
        )
