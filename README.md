# wechat-codex-multi-channel

把多个微信 Bot 账号接到同一台机器上的 Codex CLI、桌面 Codex 或 Claude Code CLI。下列微信命令由本地服务按固定规则处理；其他消息交给所选 Agent。CLI 工作区由微信服务管理；桌面项目和会话使用 Codex 自己的本地数据，微信只保存当前选择。

## 快速开始

要求：macOS 或 Linux、Python 3.9+、Node.js/npm、可访问微信 Bot 接口。要运行 Codex 或 Claude 任务，机器上还需安装对应 CLI。Codex 可以在 Bot 启动后通过微信完成设备码登录。

```bash
cd /path/to/wechat-codex-multi-channel
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
npm install
cp config.example.json config.json
```

先安装 Codex CLI（`npm install -g @openai/codex`），或使用本机已安装的桌面应用内置 Codex。编辑 `config.json`：将 `codex.workingDirectory` 设为实际项目目录，检查 `codex.accounts` 中的账号目录。示例文件只配置 `main`；需要多个账号时再添加独立目录，并在每个目录分别登录。

添加第一个微信 Bot 账号，按终端提示扫码：

```bash
python3 -m wechat_codex_multi add-account
python3 -m wechat_codex_multi status
```

终端 `status` 中的 `userId` 是扫码登录的微信用户 ID。如果此用户就是管理员，将它写入 `config.json` 的 `adminUsers`；如需限制访问，也写入 `allowedUsers`。然后启动：

```bash
python3 -m wechat_codex_multi start
```

在微信里发送 `/status` 检查连接。如果管理员不是扫码登录的人，可以先在空 `allowedUsers` 下让他发送 `/status`，从返回的 `conversation: accountId:userId` 取最后一段 `userId`，写入 `adminUsers` 后重启。首次使用 Codex 时，可在本机执行 `codex login`，或以管理员身份发送 `/codex-login main user@example.com` 完成远程设备码登录。要使用 Claude，先在对应 `CLAUDE_CONFIG_DIR` 执行 `claude auth login`。这里的 `user@example.com` 仅是示例邮箱，替换为你自己的账号。

已有微信 Bot 账号时，直接运行 `start`，不必再次扫码。自定义配置文件可用 `python3 -m wechat_codex_multi --config /path/to/config.json start`，或设置 `WECHAT_CODEX_MULTI_CONFIG`。

连接成功后，在微信发送 `菜单` 打开中文编号菜单；发送 `/任务` 查看任务进度，发送 `/结果` 查看最近结果。

## 账号、用户与数据目录

| 名称 | 用途 | 存放位置 |
| --- | --- | --- |
| 微信 Bot 账号 | 接收和发送微信消息；可同时连接多个 | `stateDir/state.json` |
| 微信用户 | 向 Bot 发消息的人；用消息发送者的 `userId` 控制访问和管理员权限 | `config.json` 中的名单、`stateDir/state.json` 中的会话 |
| Codex 账号 | 运行 Codex CLI；每个账号对应独立 `CODEX_HOME` | `codex.accounts[].codexHome` |
| Claude 账号 | 运行 Claude Code CLI；可设置独立 `CLAUDE_CONFIG_DIR` | `claude.accounts[].claudeConfigDir` |

`/login` 添加微信 Bot 账号；`/codex-login` 授权 Codex CLI。这两个命令处理的是不同的登录。`/codex-login` 只接受配置中已有的 Codex 账号名，不接受任意磁盘路径。

`stateDir` 包含微信 Bot 凭据、会话状态和接收的媒体；Codex 的 `auth.json` 也包含登录凭据。不要把这些文件或 `config.json` 提交到仓库。默认 `allowedUsers: []` 会响应所有能联系该 Bot 的微信用户；按实际使用范围配置访问名单。默认 `codex.bypassApprovalsAndSandbox: true` 会让 Codex 以较高本机权限运行。

## 配置

`config.example.json` 是完整配置样例，`wechat_codex_multi/config.py` 给出运行时默认值。无需把所有字段复制到自己的配置里；只写需要覆盖的字段即可。常用项：

| 字段 | 作用 |
| --- | --- |
| `stateDir` | 微信账号、会话和媒体的本地状态目录 |
| `defaultAgent` | 新工作区默认使用 `codex` 或 `claude` |
| `wechat.baseUrl`、`botType`、`routeTag` | 微信 Bot 接口设置；`routeTag` 非空时作为请求头发送 |
| `codex.bin`、`claude.bin` | CLI 命令名或绝对路径；Codex 默认从 PATH、常见安装目录和桌面 App 中定位，不要复制其他机器的绝对路径 |
| `codex.desktopBin` | 桌面会话 App Server 的可执行文件；macOS 默认查找系统及用户 Applications 下的 ChatGPT.app、Codex.app，兼容平铺与嵌套 CLI 路径；找不到内置程序时使用 `codex.bin` |
| `codex.workingDirectory` | 默认项目目录；`claude.workingDirectory` 为空时沿用此目录 |
| `codex.runner` | `exec` 或 `app-server`；默认 `exec` |
| `codex.defaultAccount`、`codex.accounts` | 默认 Codex 账号和各账号的 `codexHome` |
| `codex.accountsDirectory` | 自动注册新 Codex 账号时的目录根路径，默认 `~/.codex-accounts` |
| `claude.defaultAccount`、`claude.accounts` | 默认 Claude 账号和各账号的 `claudeConfigDir`；空路径使用系统默认登录态 |
| `codex.model`、`codex.reasoningEffort` | Codex 默认模型和 reasoning 档位 |
| `claude.model`、`claude.effort` | Claude 默认模型和 effort 档位 |
| `codex.modelOptions`、`claude.modelOptions` | 固定 CLI 路由的可选模型列表；空数组时由 CLI 发现。桌面路由按所选账号从 App Server 查询 |
| `codex.modelDiscoveryTimeoutSeconds` | Codex 模型发现超时，默认 30 秒 |
| `claude.modelDiscoveryTimeoutSeconds`、`modelDiscoveryCacheSeconds` | Claude 模型发现超时和缓存，默认 20 秒、0 秒 |
| `codex.timeoutMs`、`claude.timeoutMs` | 单次任务超时，默认 2 小时 |
| `codex.bypassApprovalsAndSandbox` | 是否给 Codex 传入跳过审批与沙箱的参数，默认 `true` |
| `claude.permissionMode` | Claude Code 权限模式，默认 `bypassPermissions` |
| `codex.extraPrompt`、`claude.extraPrompt` | 追加到对应 Agent 的提示 |
| `concurrency.maxWorkers`、`commandWorkers` | 普通任务与命令任务的线程数，默认 4、2 |
| `concurrency.perConversationSerial` | 是否串行处理同一工作区，默认 `true` |
| `state.saveDebounceMs` | 状态文件写入防抖时间，默认 1000 毫秒 |
| `updates.timeoutSeconds` | 每条更新命令超时，默认 900 秒 |
| `updates.cliCommand`、`desktopCommand` | 可选的本机更新命令 argv 数组；空数组时自动识别安装方式 |
| `updates.desktopAppPath` | 可选的 macOS 桌面 App 路径；必须是 `com.openai.codex`，支持 Codex.app 和该应用改名后的 ChatGPT.app，普通独立 ChatGPT App 暂不支持 |
| `updates.desktopVersionCommand` | 可选的桌面版本查询 argv 数组；其他系统使用自定义更新命令时必须配置 |
| `updates.desktopMethod` | 桌面更新方式：`native`（默认，App 内置菜单）、`auto`（按 App 运行状态选择）、`installer`（安装包） |
| `updates.desktopUpdateMenuTitles` | 可选的内置更新菜单标题数组；默认匹配英文和简繁中文的“检查更新”，支持其他界面语言 |
| `media.maxFileBytes`、`maxConcurrentTransfers` | 单文件发送上限与媒体传输并发数 |
| `media.generators` | 可选的外部图片、视频等媒体生成命令 |
| `allowedUsers`、`adminUsers` | 可访问用户与管理员的微信 `userId` 列表 |
| `textChunkLimit`、`logLevel` | 微信文本分片长度和日志级别 |
| `notifications.taskReceipts` | 自动发送任务接收回执，默认 `true` |
| `notifications.backgroundCompletion` | 后台任务完成或失败时发送简短提醒，默认 `true`；微信用户可单独设置 |
| `notifications.retryFailedDeliveriesOnMessage` | 收到新消息时自动补发最近一条发送失败的结果，并重试未送达的后台提醒，默认 `true` |
| `notifications.menuTimeoutSeconds` | 中文菜单编号选择的有效时间，默认 120 秒，最短 10 秒 |
| `handoff.enabled`、`handoff.maxChars` | 切换账号后自动生成本地项目交接，默认开启；下一条任务携带的交接原文最多 6000 字符，可设置 1000–20000 |

例如添加独立 Codex 账号：

```json
{
  "codex": {
    "defaultAccount": "main",
    "accounts": [
      {"name": "main", "codexHome": "~/.codex"},
      {"name": "backup", "codexHome": "~/.codex-accounts/backup"}
    ]
  },
  "adminUsers": ["你的微信 userId"],
  "allowedUsers": ["你的微信 userId"]
}
```

新增 `codex.accounts` 项会在下一条微信消息或账号命令时加载，无需重启；登录命令自动注册的账号立即生效。已有账号的目录修改、删除、默认账号，以及其他配置字段需要重启服务加载。热加载只追加独立账号，不替换运行中任务的身份；无效 JSON 或重复账号目录不会覆盖当前账号列表。微信中的 `/runner` 切换仅作用于当前服务进程，重启后仍采用配置文件中的值。

## 微信命令

命令由服务直接处理，不会作为普通提示发给 LLM。以下命令都在与 Bot 的聊天中发送。

### 中文菜单与自动任务反馈

连接 Bot 后发送 `菜单` 或 `/menu`，即可在中文菜单中查看状态、项目、会话、任务、结果、模型和用量，或停止当前任务。默认在 120 秒内回复编号选择，也可发送 `/选择 <编号>`；回复 `0` 退出。非选择消息会退出菜单并按原用途处理；刚过期的菜单编号会提示重新打开菜单，不会被送给模型。`帮助`、`状态` 也支持不带 `/` 的完整词匹配，其他中文命令使用下表中的 `/` 前缀。英文命令和中文别名使用同一套本地处理逻辑，支持全角 `／`。

| 中文入口 | 英文命令 | 用途 |
| --- | --- | --- |
| `/菜单`、`菜单` | `/menu` | 中文编号菜单 |
| `/帮助` | `/help` | 常用帮助；`/帮助 all` 查看完整帮助 |
| `/状态` | `/status` | 当前状态 |
| `/项目`、`/切换项目 <名称>` | `/ws`、`/ws use <名称>` | 查看或切换项目工作区 |
| `/会话`、`/切换会话 <编号>` | `/sessions`、`/session use <编号>` | 查看或切换 CLI 会话 |
| `/桌面项目`、`/桌面会话` | `/d-projects`、`/d-sessions` | 查看桌面项目或会话 |
| `/切换桌面会话 <编号>` | `/d-session use <编号>` | 切换桌面会话 |
| `/任务 [未读]` | `/tasks [unread]` | 最近 10 条任务、排队状态、用时、未读结果和发送失败状态 |
| `/任务 执行中` | `/tasks active` | 当前 Bot 下自己的全部工作区中执行中、排队中的任务，显示完整任务 ID，不限 10 条 |
| `/结果 [任务ID]` | `/result [任务ID]` | 查看结果及未发送附件；默认最近有结果的任务，支持唯一 ID 前缀；暂无结果时显示任务状态 |
| `/重发 [任务ID]` | `/resend [任务ID]` | 补发未成功的文字分片和附件；默认最近有结果的任务 |
| `/提醒 [开启\|关闭]` | `/notify [on\|off]` | 查看或设置后台完成提醒；每个微信用户的设置会持久保存 |
| `/停止`、`/取消`、`/重置` | `/interrupt`、`/cancel`、`/reset` | 停止保留会话，或重置会话 |
| `/停止 <任务ID>`、`/取消 <任务ID>` | `/cancel <任务ID>` | 按完整 12 位任务 ID 取消自己的任务，保留其会话，无需切换工作区 |
| `/补充 <要求>` | `/guide <要求>` | 为运行中或排队中的任务追加要求 |
| `/模型`、`/账号`、`/用量`、`/目录` | `/model`、`/account`、`/usage`、`/cwd` | 查看或设置对应选项 |

普通任务与 `/ws run` 自动获得任务 ID 和接收回执，回执显示项目、目录、Agent、账号及目标会话。接收时固定目标工作区和桌面会话；任务排队期间切换项目，已接收任务仍在原目标执行。同一 CLI 会话有任务排队或执行时，修改会话、账号、模型、目录等会先提示停止或等待。`/停止` 也能取消尚未开始的排队任务，保留已有会话。运行中或排队中追加的普通消息仍作为补充引导；原生引导通过命令线程处理，不等待任务线程池空闲。

先发送 `/任务 执行中`（`/tasks active`）查看自己的全部进行中任务，再发送 `/停止 a1b2c3d4e5f6` 或 `/cancel a1b2c3d4e5f6` 取消指定任务。也可直接复制带方括号的 ID，如 `/cancel [a1b2c3d4e5f6]`。查询和取消都由本地服务处理，不调用模型；无效 ID、其他用户或其他 Bot 的任务 ID、已结束的任务均不会取消当前任务或启动替代任务。`/cancel` 的参数用于任务 ID；需要中断当前任务后改做新任务时，使用 `/interrupt <新任务>`。同一会话若有多个同时执行的任务，无法区分其执行进程时会拒绝按 ID 取消，避免误停其他任务。

任务在当前会话完成时发送完整结果；切到其他工作区或桌面会话后，后台完成或失败会自动发送一条简短提醒。提醒中的结果摘录直接截取已生成文字，不额外调用模型总结。完整结果和附件保留在任务记录中，发送 `/结果 <任务ID>` 即可查看；发送 `/提醒 关闭` 后仍会保存未读结果，可用 `/任务 未读` 查找。

任务结果与文字分片、附件的发送状态保存在 SQLite 数据库 `stateDir/tasks.sqlite3`，按当前 Bot 和微信用户隔离。任务 ID、所属用户、状态、接收时间、未读结果和发送恢复使用索引查询，不再在启动时将全部历史任务加载到内存；列表查询支持数据库分页。任务完整内容及扩展字段以 JSON 保存于数据库记录中。写入使用事务，启用 WAL 和 `synchronous=FULL`。附件在确认发送成功后才从待发送队列清理；部分发送失败会显示“任务已完成，但部分结果发送失败”，保留未成功内容。下一次微信消息会自动尝试补发最近一条失败记录，也可随时用 `/重发` 补发；按本地确认的发送状态跳过已成功的内容。查询、菜单、回执、提醒、发送恢复均由本地服务处理，不创建 Agent 回合、不消耗模型 token。执行实际任务时按原设置调用所选 Agent。

旧版 `stateDir/tasks/*.json` 会在新版首次启动时自动导入，保留完整任务 ID、结果、文字分片、附件、未读及发送状态，原 JSON 文件保留作为迁移备份。也可以在旧服务仍运行时先执行预迁移：

```bash
python3 -m wechat_codex_multi migrate-tasks
```

预迁移不会停止服务或把执行中的任务标记为中断；可重复执行。随后手动重启服务，新版启动时会再同步旧服务最后写入的 JSON，并在同一事务中记录迁移完成。完成切换后只读写 SQLite，后续启动或重复运行预迁移不会用旧 JSON 覆盖新状态。迁移命令显示导入数量、异常文件和数据库完整性检查结果；异常 JSON 保留原文件并报告文件名。读取文件失败或数据库写入失败时整个导入事务回滚，可修复后重试。数据库在线备份应使用 SQLite 的 backup 接口或 `sqlite3 ... '.backup ...'`，避免只复制主文件而遗漏 WAL 中的已提交记录。会话和微信账号仍保存在原有 `stateDir/state.json`。

服务重启后保留结果、未读状态和失败发送记录。尚未完成的任务标记为“服务重启，执行结果未确认”，不会自动重新执行，避免重复操作。未知 `/命令` 和顶层参数错误会返回本地帮助提示；拼错命令不会被当作任务执行。

### 更新 Codex CLI 和桌面 App

这些命令完全由本机执行，不创建 Agent 回合，不消耗模型 token。微信中仅 `adminUsers` 可用；它们更新的是服务配置选中的本机程序，与当前工作区、登录账号和所选 Agent 无关。`/help` 显示常用更新命令，`/help all` 和 `/update` 显示完整用法、支持范围及授权要求。

| 命令 | 作用 |
| --- | --- |
| `/update` | 显示更新命令帮助 |
| `/update cli`、`/codex-update` | 后台更新 Codex CLI，完成后发送结果 |
| `/update desktop`、`/d-update` | macOS 上调用桌面 Codex App 自带“检查更新”；需先打开 App 并授予辅助功能权限，无需先退出，回报触发结果 |
| `/update cli check`、`/update desktop check` | 只查看已安装版本、程序路径和更新方式，不下载或安装 |
| `/update status` | 查看两种程序的最近更新结果及日志路径 |
| `/update cli status`、`/update desktop status` | 查看指定程序的最近更新结果 |

快捷命令也接受 `check` 和 `status`，例如 `/codex-update check`、`/d-update status`。`check` 用于预览本机更新方案，不查询远端最新版本。开始更新的通知只代表已启动。安装包更新的最终结果以退出码、更新后的版本查询及 `/update status` 为准；内置菜单方式仅报告“已触发内置更新”，不把检查请求冒充为安装成功。之后查询 `/update status`，检测到更高的已安装 App 构建号时才显示“更新完成”；没有版本变化时仍显示尚未确认安装完成。

终端命令使用相同实现，无需微信账号或模型登录；实际更新会等待完成，失败返回非零退出码：

```bash
python3 -m wechat_codex_multi update cli --check
python3 -m wechat_codex_multi update cli
python3 -m wechat_codex_multi update desktop --check
python3 -m wechat_codex_multi update desktop
python3 -m wechat_codex_multi update status
python3 -m wechat_codex_multi update --help
```

也可用 `npm run update:codex`、`npm run update:desktop`，追加 `-- --check` 可预览。自定义配置继续使用全局 `--config` 参数或 `WECHAT_CODEX_MULTI_CONFIG`。

CLI 自动更新支持 npm 全局包和 Homebrew 的 `codex` cask/formula，根据 `codex.bin` 指向的实际文件识别安装来源。若同时安装多份，只更新配置选中的程序；npm 的全局目录不匹配、独立安装器安装或手动安装时，会要求配置 `updates.cliCommand`，不会偷偷另装一份。npm 使用 `npm install -g @openai/codex@latest`；Homebrew 先更新索引，再升级对应的 cask/formula。官方方式见 [Codex CLI 文档](https://learn.chatgpt.com/docs/codex/cli)。

macOS 桌面更新默认调用 App 自带的“检查更新”，微信只需发送 `/update desktop`，终端只需运行 `python3 -m wechat_codex_multi update desktop`，通常无需指定 `native` 或 `--method`。它通过 macOS 原生辅助功能接口读取菜单并触发对应条目，使用应用自身更新器；App 未运行时会提示先打开，不自动改用安装包。

支持范围由应用标识决定：目前仅识别 `com.openai.codex`，包括 Codex.app 以及该应用改名后的 ChatGPT.app。普通独立 ChatGPT App（例如标识为 `com.openai.chat`）暂不支持，不能只凭文件名判断兼容性。默认在当前用户的 `~/Applications` 和系统 `/Applications` 中查找，也可从 `codex.desktopBin` 确定应用位置；特殊安装位置或检测到多份 App 时，需配置 `updates.desktopAppPath`。

内置更新无需先退出应用，最终是否需要确认下载、安装或重启，按 App 自己的提示处理。微信服务不点击“安装并重启”，不关闭应用，也不把未变化的版本报告成已升级。它可与 Codex 任务同时运行，不关闭服务中的 App Server。App 实际更新后，可重启微信服务，让后续桌面请求使用更新后的程序。

需要原有安装包方式时，可配置 `updates.desktopMethod: "installer"`；`auto` 则在 App 运行时走内置更新、未运行时走安装包。安装包方式优先使用拥有当前 App 的 Homebrew cask，否则使用 [OpenAI 官方安装包](https://learn.chatgpt.com/docs/enterprise/manage-app-updates)。这些高级方式仍支持终端 `--method` 参数和旧微信命令，日常无需指定方式。

自动点击菜单需要 macOS 辅助功能权限，部署到每台 Mac 后都要单独授权：

1. 打开“系统设置 → 隐私与安全性 → 辅助功能”。
2. 为运行服务的 Python（launchd 部署时）或终端（终端启动时）开启权限。缺少权限的报错会显示当前 Python 路径；若列表里没有它，可用 Finder 的“前往文件夹”定位该路径，将实际可执行文件加入列表。若路径是符号链接，应选择其指向的实际文件。
3. macOS 要求验证身份时，在系统窗口中完成。管理员身份不等于已经获得辅助功能授权；本项目不提供自动授予权限的命令。
4. 授权后重启微信服务，再发送 `/update desktop`；也可直接在 App 菜单中选择“检查更新”。

此方式使用系统原生 API，无需给 System Events 添加自动化权限，不依赖额外 GUI 工具、应用私有 IPC、固定用户名、Python 安装位置或 App 版本。默认匹配英文和简繁中文菜单；其他界面语言可配置 `updates.desktopUpdateMenuTitles`，值为菜单标题字符串数组，按应用实际显示的文字填写。应用未来若更改菜单结构或更新入口，可能需要调整实现；企业策略禁用内置更新时，以 App 提示为准。

安装包方式需先完全退出桌面 App，服务不会主动关闭它。直接安装包方式会验证 Apple 信任链、OpenAI 开发者签名和应用标识，比较构建号后复制到临时目录，再替换原位置；替换失败恢复旧 App，不降级，不修改账号和会话数据。Homebrew 管理的 App 采用 Homebrew 自身的安装流程。

同一 `stateDir` 下的终端更新和微信更新共用文件锁。CLI 和桌面安装包更新与 Codex 任务互斥；这类更新期间拒绝新的 Codex 任务，Claude 任务可继续。更新成功后微信服务关闭空闲的 Codex App Server 并清除模型缓存，后续请求启动新程序。内置菜单请求使用独立的请求锁，可在 Codex 任务运行时发起。外部独立运行的 CLI 进程不在微信服务的任务锁范围内。日志和结果存入 `stateDir/updates/`，服务重启后仍可查询。安装包更新期间应等待最终结果再重启服务；服务退出会中断更新流程，已启动的安装命令可能继续运行。在 macOS/Linux 上该命令继承更新锁，结束前不会允许另一轮更新；未记录成功结果时不会报告更新完成。

本实现不依赖本机 skill、个人脚本、固定用户名、安装前缀或 launchd。其他安装方式可在本机配置明确的 argv 数组，例如使用独立 npm 前缀：

```json
{
  "codex": {"bin": "/your/npm-prefix/bin/codex"},
  "updates": {
    "cliCommand": ["npm", "install", "-g", "--prefix", "/your/npm-prefix", "@openai/codex@latest"]
  }
}
```

桌面更新内置流程面向 macOS。Windows、Linux 或企业软件分发可配置 `updates.desktopCommand` 和 `updates.desktopVersionCommand`，由管理员填写适用于本机的更新、版本查询程序；macOS 也可覆盖默认方式。命令以 argv 直接启动，默认不经过 shell，不接受微信用户传入任意命令；确需 shell 时须在本机配置中显式指定解释器。服务不自动提权，目录权限或更新命令错误会写入日志并返回失败。

### 状态和用量

| 命令 | 作用 |
| --- | --- |
| `/help`、`/help all` | 查看常用命令或完整命令清单 |
| `/status` | 查看当前对话标题、Desktop/CLI 来源、会话所属账号，以及工作区、目录、模型和运行状态 |
| `/active` | 查看正在运行的任务 |
| `/accounts`、`/users` | 列出已连接的微信 Bot 账号和用户信息 |
| `/usage` | 查看当前 Agent、当前账号用量 |
| `/usage codex`、`/usage claude` | 查看当前工作区对应的 Codex 或 Claude 账号用量 |
| `/usage all`、`/usage codex all`、`/usage claude all` | 汇总配置中的全部账号用量 |
| `/usage claude api [days]` | 用 Anthropic Admin API 查看组织级 Claude API 用量；需要 Admin Key |

`/status` 顶部显示当前执行会话的标题、来源（Codex Desktop、Codex CLI 或 Claude CLI）及所属账号。仅用 `/d-account` 切换列表浏览账号不会改变状态里的会话归属。标题按当前账号和完整会话 ID 从本地元数据读取，桌面会话会复用已有的只读 `thread/read`；不创建、恢复会话，也不调用模型。标题变更在下次查询时读取；本地元数据不可用时会注明列表缓存或“标题暂不可用”。切换账号、新建或重置后的空会话显示“新会话（尚未创建）”。

Codex 用量按服务端返回的窗口时长显示，不固定把主窗口当作 5 小时窗口。Pro 显示“5 小时限制：不适用”，并显示返回的周额度或其他窗口；缺少的窗口不会显示成 0%。切到 desktop 会话后，`/usage` 和 `/usage codex` 使用该会话所属账号的 `CODEX_HOME` 和桌面 Codex 程序，通过独立 App Server 查询当前登录与额度，支持系统钥匙串中的登录信息。查询不调用模型、不恢复 thread，也不会占用会话写锁。`/usage codex all` 仍按配置逐个查询账号。

Claude 普通用量查询通过本机 Claude Code TUI 的 `/usage` 完成；组织级 `api` 查询是另一条路径。`ANTHROPIC_ADMIN_KEY` 可通过环境变量或 macOS Keychain 提供，Keychain service 默认是 `wechat-codex-multi.anthropic-admin-key`。相关超时、查询天数和 Keychain service 可在 `claude.*` 中配置。

### Agent、账号和模型

| 命令 | 作用 |
| --- | --- |
| `/agents`、`/agent` | 查看可用 Agent 或当前 Agent |
| `/agent codex`、`/agent claude` | 切换当前工作区使用的 CLI |
| `/account [编号|名称|next|prev]` | 查看或切换当前 Agent 的登录账号 |
| `/codex-accounts`、`/claude-accounts` | 列出对应 CLI 的配置账号 |
| `/codex [编号|名称|next|prev]` | 切到 Codex，可同时选择 Codex 账号 |
| `/claude [编号|名称|next|prev]` | 切到 Claude，可同时选择 Claude 账号 |
| `/models`、`/model` | 查看当前 Agent 的模型选项与选择说明 |
| `/model <编号|model:档位>` | 切换模型或 reasoning/effort；桌面会话保留历史，从下一轮生效；CLI 路由重置当前 Agent 会话 |
| `/model auto` | 桌面会话取消尚未生效的手动模型设置，恢复沿用会话自身设置；`default`、`inherit` 同义 |
| `/runner [exec|app-server]` | 查看或临时切换 Codex runner |

`/codex-use <名称>` 是旧版兼容命令，等价于 `/codex <名称>`。账号选择支持编号、完整名称和唯一前缀；切换账号会重置该 Agent 的会话 ID，保留当前项目目录，并为下一条任务准备本地交接。切回旧账号也会新建会话，不自动恢复那个账号曾经处理的其他会话；原会话可通过会话列表手动选择。切换 Agent 时，Codex 和 Claude 各自的会话 ID 分开保存。

#### 切换账号继续同一项目

CLI 工作区先用 `/ws use <项目名>` 选中项目，等待任务结束或发送 `/停止`，然后 `/codex secondary`（Claude 使用 `/claude <账号>`）。下一条普通任务会在新账号下新建会话，沿用原目录，并自动带入交接内容。切换账号这一步只处理本地记录，不启动模型；不自动恢复目标账号以前的会话，也不自动重跑失败任务。

桌面 Codex 可直接用 `/codex secondary` 或 `/account secondary` 切换当前项目的执行账号。`/d-account secondary` 仍只切换项目和会话列表的浏览账号；随后 `/d-session new` 才在当前目录准备新账号会话，并生成交接。首次执行时，系统会在新账号的本地项目列表中找到对应目录，或创建指向同一目录的项目记录。原账号的会话、项目和凭据保留在原目录。

交接只读取同一 Bot、微信用户、工作区及所选会话的记录：保留最初要求和最近两轮的要求、结果或失败信息，以及本地 Git 文件状态。没有 Bot 任务记录时，会读取所选桌面会话或本地 CLI 会话的文字历史。交接使用有长度上限的原文摘录，不调用 LLM 总结，不复制登录凭据，不回放历史媒体发送动作。新账号实际处理任务时，这些上下文仍计入正常输入 token。

交接在成功完成一轮任务后清除；登录或执行失败会保留，供下一次任务使用。连续切换账号且未执行任务时，交接可继续传递，不嵌套重复上下文。`/reset`、显式新建同账号会话、选择其他历史会话或更换目录会清除待交接内容。设置 `handoff.enabled: false` 可恢复仅清空会话 ID 的旧行为。

当 `modelOptions` 为空时，Codex CLI 路由使用默认账号的 `CODEX_HOME` 调用 `codex debug models`；发现超时才退回内置列表。桌面路由使用当前所选会话账号的 App Server `model/list`，读取该账号实际返回的模型和逐模型推理档位；不会用 CLI 默认账号或内置旧列表替代。Claude 通过 CLI 的 stream-json 初始化协议发现模型；失败时会提示错误。模型清单取决于已安装的 CLI 和账号，README 不固定列出某个版本的清单。`/model <编号>` 中的编号以刚查询到的列表为准；桌面路由按当前会话最近一次显示的模型列表解释编号。桌面路由只写模型名、不写档位时，使用该模型返回的默认档位。

选中 `/d-session` 后，`/status`、`/d-session status <编号>` 会只读原生会话的 `model` / `reasoningEffort`，不会为查询占用写锁。`/status` 还会显示完整会话 ID 和模型来源；若有手动设置，会标明“下一轮生效”。默认不覆盖原生会话模型，续聊沿用上次设置；即使后来在桌面改过模型，微信下一轮也跟随原生设置，而不是旧缓存。读取失败会明确提示缓存/未知，不把微信全局默认模型冒充为会话模型。

桌面示例：先发 `/model` 查看当前模型及选项，再发 `/model 2` 或 `/model gpt-5.6-sol:xhigh`。手动设置只属于所选会话，在下一轮被接受后由 Codex 持久化；模型切换不清空会话 ID，也不打断当前运行。新建桌面项目/会话时也可先指定模型再发第一条任务。`/model auto` 仅取消未执行的手动设置，不会撤销已保存到会话的模型；完全没有历史的新会话使用 Codex 默认设置。

Claude 的 `ultracode` 是支持 `xhigh` 的模型可选的 effort 模式，不是独立模型。CLI 路由切换模型或档位仍会清空当前 Agent 的会话 ID，下次任务开启新会话；上述不重置行为仅适用于桌面路由。

### 工作区和会话

| 命令 | 作用 |
| --- | --- |
| `/cwd [路径]` | 查看或修改当前工作区目录；修改后重置两种 Agent 的会话 |
| `/ws`、`/ws list` | 列出当前微信用户的项目工作区 |
| `/ws add <名称> <路径>` | 添加工作区；`default` 为保留名称 |
| `/new-project <目录>`、`/n-p <目录>` | 建立并切换 CLI 项目工作区，准备新会话；名称取目录名 |
| `/d-new-project <目录>`、`/d-p-n <目录>`、`/d-n-p <目录>` | 在桌面 Codex 创建原生项目，指定目录为项目根目录，并准备新会话 |
| `/ws use <名称>` | 切换当前工作区 |
| `/ws agent <名称> <codex|claude>` | 设置指定工作区使用的 Agent |
| `/ws run <名称> <任务>` | 不切换当前工作区，直接向指定工作区派发任务 |
| `/ws reset <名称>` | 取消任务并重置指定工作区的当前 Agent 会话 |
| `/sessions [codex|claude|all]` | 按更新时间列出本机可恢复的 CLI 会话 |
| `/sessions archived [codex|claude|all]` | 列出 CLI 归档会话 |
| `/session use <编号|sessionId前缀>` | 将当前工作区切换到指定会话 |
| `/session new [codex|claude]` | 新建当前工作区的 CLI 会话 |
| `/session archive|unarchive <编号|sessionId前缀>` | 归档或恢复 CLI 会话，仅管理员可用 |
| `/session delete <编号或sessionId前缀> [更多编号或前缀]` | 预览单个或批量永久删除；60 秒内在同一命令末尾加 `confirm` 确认，仅管理员可用 |
| `/d-projects` | 列出当前 Codex 账号的桌面原生项目 |
| `/d-project use <项目编号>` | 切换已有桌面项目，并准备新会话 |
| `/d-sessions [all|项目编号] [页码]` | 列出全部项目或指定项目的 Codex 会话，20 条一页 |
| `/d-sessions page <页码>` | 快速查看全部项目的指定页 |
| `/d-sessions archived [all|项目编号] [页码]` | 列出桌面归档会话 |
| `/d-session <会话编号>` | 按最近一次会话列表的编号快速切换并查看最新结果 |
| `/d-session view|status|use <会话编号>` | 查看最新结果、状态或选中会话；选中后直接发送消息续聊 |
| `/d-session new` | 在当前桌面项目准备一个全新的会话 |
| `/d-session guide <会话编号> <内容>`、`/d-session interrupt <会话编号>` | 引导或打断本 Bot 发起的回合 |
| `/d-session archive|unarchive <会话编号>` | 归档或恢复会话，仅管理员可用 |
| `/d-session delete <会话编号或ID前缀> [更多编号或前缀]` | 预览单个或批量永久删除；60 秒内在同一命令末尾加 `confirm` 确认，仅管理员可用 |
| `/d-account [账号]`、`/d-session off` | 选择会话账号、退出桌面 App Server 路由 |
| `/reset` | 取消当前任务并重置当前 Agent 会话 |

每个 `accountId:userId` 有自己的默认工作区；额外工作区的会话 key 为 `accountId:userId:workspaceName`。同一用户可以让不同工作区并行执行。工作区分别保存目录、Agent、Codex 与 Claude 会话、账号和模型选择。

`/new-project /Users/you/Documents/demo` 会在目录不存在时创建目录，以目录名 `demo` 登记 CLI 工作区并切换进去。缩写为 `/n-p`。

`/d-p-n /Users/you/Documents/demo` 通过 Codex App Server 的 `project/create` 创建桌面原生项目，以该目录作为项目根目录；目录不存在时先创建。同一路径已有桌面项目则直接选中。微信只保存当前选中的原生项目 ID、目录和会话 ID，不额外创建微信工作区。发送第一条任务时通过 `thread/start` 指定该项目 ID；任务结果发回微信，用户消息和 Codex 回复也写入桌面端同一会话。任务完成后，可在 `/d-sessions <项目编号>` 或桌面应用的该项目中查看、续聊。已有项目可用 `/d-project use <项目编号>` 切换，再发送任务创建新会话。`/d-new-project`、`/d-n-p` 与 `/d-p-n` 等价。

`/sessions` 只读取本机 CLI 会话数据（Codex 的 `cli`/`exec` 来源与 Claude Code 会话），不混入桌面 Codex 会话，也不请求模型；桌面会话统一使用 `/d-sessions`。列表中的编号只对当前工作区最近一次查询有效；列出归档会话后，编号用于 `/session unarchive` 或 `/session delete`。`/session use` 在当前任务运行时会拒绝切换。Codex CLI 与桌面 Codex 共用底层本地会话库，但这里按来源分开展示；归档和删除同一个 Codex thread 仍会影响所有客户端。Claude Code 没有原生归档命令；`/session archive` 对 Claude 只在本 Bot 的列表中隐藏会话，`/session unarchive` 可恢复显示，本机 Claude CLI 仍可找到它。删除 Claude 会话会移除该账号 `projects` 中对应的会话 JSONL 和 `usage-data/session-meta` 元数据；这不代表清除 Claude Code 的所有缓存或其他副本。

批量删除示例：先发送 `/sessions all` 查看编号，再发送 `/session delete 3 4 5` 查看目标清单；确认无误后，在 60 秒内发送 `/session delete 3 4 5 confirm` 执行。桌面会话使用 `/d-sessions all`、`/d-session delete 3 4 5` 和 `/d-session delete 3 4 5 confirm`，旧版 `/desktop delete` 同样支持多个目标。编号和唯一 ID 前缀可以混用，重复目标只删除一次；CLI 列表中的 Codex 与 Claude 会话可在同一批次删除。

预览和确认都会检查全部目标及其运行状态；任何目标无效、匹配不唯一或正在运行，整批都不会开始删除。确认绑定完整会话 ID 和所属账号；若列表刷新使同一组编号对应了其他会话，确认会被拒绝，需要重新预览。实际删除按目标逐个执行；如果中途失败，会停止执行后续目标，并列出已完成删除的会话、失败目标及未执行数量，已完成的删除无法回滚。删除尝试后需重新查询会话列表获取最新编号；归档和恢复命令仍只接受一个目标。

`/d-` 前缀表示桌面 Codex；旧版 `/desktop` 命令继续兼容。桌面命令通过同一 `CODEX_HOME` 的 Codex App Server 协议读写原生项目和会话。`/d-project use` 后第一条任务会携带原生 `projectId` 创建 thread，并立即写入会话名称，供桌面会话索引收录。桌面应用与微信使用同一份本地项目、会话数据；由于桌面窗口运行的是独立 App Server 进程，窗口已打开时仍可能需要切换项目或刷新一次，才会重新扫描外部进程刚创建的会话。`status` 显示最后保存的回合状态，只有本 Bot 发起的回合能显示实时运行状态并接受 `guide` 或 `interrupt`。普通 ChatGPT Chat/Work 对话不在此命令范围内。会话编号以最近一次 `/d-sessions` 列表为准；切换账号后需重新列出。归档和删除可能影响派生子会话；删除不可恢复。

翻页示例：`/d-sessions all 2` 或 `/d-sessions page 2` 查看所有项目的第 2 页；`/d-sessions 3 2` 查看项目 3 的第 2 页。单独的 `/d-sessions 2` 表示“项目 2”，不是“第 2 页”。每页 20 条，第 2 页的编号从 21 开始；发送 `/d-session 21` 或 `/d-session use 21` 就能切到第 21 条。编号以最近一次列表为准，列表末尾会给出上一页和下一页命令。

同一微信用户可以在桌面会话 A 执行期间用 `/d-session use` 切到 B，并向 B 发起另一个任务。各会话独立运行；后台会话完成或失败时默认发送简短提醒，完整结果和附件保存在任务记录中，可用 `/结果 <任务ID>` 查看。每次成功切换桌面会话，微信都会重发该会话最近一次完整的文字回答，帮助接上进度。若新任务仍在运行，会同时标明“正在执行中”和“上一条完整回答”；任务结束后切回，会显示新结果。回顾消息只发送文字，不重复执行媒体发送动作；尚未发送的附件可用 `/结果` 或 `/重发` 获取。当前选中的会话完成时会直接回复微信；`/d-session view` 只读取指定会话的最新结果，不回放历史回合。

#### 微信与桌面的会话占用

Codex 同一会话只能由一个 App Server 进程持有写锁。微信现在为每轮任务使用独立执行进程，在回复、失败、取消或超时后关闭该进程，释放原生会话占用；下一条消息重新恢复同一个会话，不另建会话。列表和历史查询使用独立的共享连接，不会因任务结束而中断；其他会话的运行也不受影响。引导和中断只发送给该任务的执行进程。超时与 `/interrupt` 保留原会话，只有显式重置才清空选择。

不能只依赖 `thread/unsubscribe`：它取消事件订阅，但当前协议允许线程在无订阅后继续驻留 30 分钟，写锁不会立即释放（[官方协议说明](https://learn.chatgpt.com/docs/app-server#unsubscribe-from-a-loaded-thread)）。任务完成后关闭独立执行进程可避免这个等待。此模式以单轮任务为生命周期，不适合依赖 App Server 长期驻留的自主目标或后台执行任务。

若微信提示 `active writer`，表示另一客户端仍持有该会话。请先在占用端结束任务并关闭该会话；若仍有占用，结束其他任务后退出占用端应用再重试，无需删除或归档会话。微信不会强抢桌面的写锁，也不会用新会话替代所选会话。桌面显示“已在另一个应用中打开”时，应等微信任务结束后重新打开该会话；旧版服务遗留的占用需在任务结束后发送 `/restart` 释放并加载新代码。

### 运行中的任务

| 命令 | 作用 |
| --- | --- |
| 普通消息 | 正在运行时视为补充引导 |
| `/guide <内容>` | 显式追加补充引导 |
| `/interrupt`、`/cancel` | 中断任务并保留当前 Agent 会话 |
| `/停止 <任务ID>`、`/cancel <任务ID>` | 取消指定的执行中或排队中任务，保留其会话 |
| `/interrupt <新任务>` | 中断任务后在同一会话处理新任务 |

默认同一工作区串行执行。Codex `app-server` runner 支持原生 `turn/steer`；Codex `exec` 和 Claude headless runner 会把补充引导排队，在当前任务完成后继续。`/runner` 可切换 Codex runner；Claude 只使用 headless 方式。需要丢弃上下文时使用 `/reset`。

### 管理员命令

`adminUsers` 中的微信 `userId` 才能使用下列命令。空 `adminUsers` 表示无人拥有管理员权限。

| 命令 | 作用 |
| --- | --- |
| `/login [昵称]` | 扫码添加微信 Bot 账号 |
| `/user rename <昵称|accountId|编号> <新昵称>` | 修改微信用户昵称 |
| `/user delete <昵称|accountId|编号>` | 删除微信用户并清理会话 |
| `/codex-login [账号名] [期望邮箱]` | 为配置中的 Codex 账号发起设备码登录 |
| `/codex-login status [账号名]` | 查看 Codex CLI 登录状态和正在进行的设备码流程 |
| `/codex-login cancel [账号名]` | 取消正在进行的设备码流程 |
| `/codex-logout <账号名>` | 退出指定 Codex CLI 账号的登录；必须填写配置中的完整账号名 |
| `/restart` | 重启后台服务；需有 launchd 等监护进程自动拉起 |

`/accounts`、`/users` 可由允许访问 Bot 的用户查询；修改、登录和重启操作需要 `adminUsers`。如果你不希望普通用户看到账号列表，应只允许可信用户访问 Bot。终端也可运行 `python3 -m wechat_codex_multi rename-user <选择器> <新昵称>` 或 `delete-user <选择器>`。

## 远程登录 Codex

后台服务需要能够调用 `codex`。管理员可以直接发送 `/codex-login secondary user@example.com`；新账号会自动创建独立目录、追加到实际加载的 `config.json` 并立即生效。目录默认是 `~/.codex-accounts/secondary`，可用 `codex.accountsDirectory` 设置根路径。账号名支持 1–64 位字母、数字、下划线、点和短横线，不接受路径或纯数字。`status` 和 `cancel` 查询不会自动创建账号；没有写入权限或配置无效时，会返回本地错误提示。

`/login secondary user@example.com` 是等价的 Codex 登录简写。原有 `/login` 和 `/login <微信Bot昵称>` 继续用于微信扫码添加 Bot，不改变含义。也可手工在 `codex.accounts` 中添加独立目录定义；新增项会在下一条消息加载。

管理员在微信中发送：

```text
/codex-login backup user@example.com
```

服务直接启动 `CODEX_HOME=~/.codex-accounts/backup codex login --device-auth`，然后发送官方登录地址和一次性代码。账号持有人在浏览器中登录、输入代码；成功后服务检查 CLI 登录状态，并在本地凭据可读取时比对邮箱。`期望邮箱` 用来核对结果，不能强制官方登录页面选择该账号。代码约 15 分钟有效，过期后重新发送命令。设备码登录需在 ChatGPT 账号安全设置或工作区权限中启用。

```text
/codex-login status backup
/codex-login cancel backup
/codex backup
```

前两条检查或取消授权，最后一条才把当前工作区切换到该账号。也可在机器上直接运行：

```bash
CODEX_HOME="$HOME/.codex-accounts/backup" codex login --device-auth
CODEX_HOME="$HOME/.codex-accounts/backup" codex login status
```

登录状态只说明本地 CLI 有凭据；如果实际请求仍返回 401，应在相同的 `CODEX_HOME` 重新登录。每个机器和账号目录应维护自己的新鲜授权，不要反复复制旧的 `auth.json` 覆盖已经刷新的文件。

没有显式配置 `codex.accounts` 时，默认账号沿用 `CODEX_HOME` 环境变量（未设置时为 `~/.codex`）；显式配置的账号目录优先。只在桌面端登录或在别的目录登录，不代表所选账号目录已获得授权。凭据可以保存在文件或系统钥匙串中，不要求手动复制 `auth.json`。相关行为见 [OpenAI 官方认证文档](https://learn.chatgpt.com/docs/auth)。

遇到 `workspace routing discovery unauthorized (401)`，服务仅在请求被工作区发现阶段拒绝时，通过官方 App Server `account/read`、`refreshToken: true` 刷新所选账号令牌并重试一次。CLI 已产生工具活动或回复、模型回合已经执行后失败时，不会自动重放。刷新失败会给出所选账号和重新登录命令，保留原会话。`codex login status` 不用于刷新令牌。接口说明见 [OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server)。

要替换 `main` 的实际登录账号，管理员先发送退出命令，收到成功回复后再发起登录：

```text
/codex-logout main
/codex-login main new-user@example.com
/codex-login status main
```

退出命令直接调用指定 `CODEX_HOME` 下的 `codex logout`，不会调用模型。该账号在服务中有任务运行或设备码登录进行时会拒绝退出；退出期间也拒绝启动该账号的新任务或登录。成功后清理该账号的 App Server 登录缓存，后续登录完成也会刷新缓存。账号配置和本地会话历史保留，其他独立账号目录不受影响。共用该目录的本机 Codex 客户端也会受到登录态变化影响。

## 媒体

入站支持文字、微信提供的语音转写、图片、文件和视频。图片、文件、视频会下载到 `stateDir/inbound_media`，本地路径交给当前 Agent。

推荐由 Agent 使用内置 `send-media` skill 登记本地文件；服务在当前任务结束后发送：

```bash
python3 -m local_agent_tools media-send /absolute/path/to/report.pdf
python3 -m local_agent_tools media-send --kind image /absolute/path/to/image.png
```

Agent 运行时会收到 `LOCAL_AGENT_MEDIA_OUTBOX`，因此通常不需要手动设置 outbox。旧版回复标记仍受支持：`[[send_image:/absolute/path]]`、`[[send_file:/absolute/path]]`、`[[send_video:/absolute/path]]`。Codex 原生图片生成事件也可自动转为微信图片。

可在 `media.generators` 中配置自定义生成器。生成命令从 stdin 接收提示，并在 stdout 最后一行输出文件路径；Agent 调用 `python3 -m local_agent_tools media-generate <名称> <提示>`，再用 `media-send` 登记文件。配置示例：

```json
{
  "media": {
    "generators": [
      {
        "name": "image",
        "kind": "image",
        "command": "python3 /path/to/generate_image.py",
        "description": "本地图片生成器"
      }
    ]
  }
}
```

## macOS 后台部署

项目提供 `./scripts/deploy_macos.sh`。它创建或复用虚拟环境、安装 Python 和 npm 依赖、创建缺失的配置、在没有微信账号时扫码添加账号，并写入、启动 launchd 服务：

```bash
./scripts/deploy_macos.sh
```

常用选项：`--skip-account`、`--skip-npm`、`--no-start`、`--config /path/to/config.json`、`--venv /path/to/.venv`。脚本不会安装 Codex 或 Claude CLI，也不会覆盖已有配置。部署时按配置检查默认 Codex 账号的登录状态，并将当前终端的 PATH（包括 nvm/npm 自定义安装目录）和已设置的 CODEX_HOME 写入 launchd 环境。默认 launchd label 是 `com.wechat-codex-multi`；如果机器上已经用其他 label 运行本项目，先确认旧服务，避免同时启动两个轮询进程。可以设置 `WECHAT_CODEX_MULTI_LABEL` 使用现有 label。

```bash
launchctl print gui/$(id -u)/com.wechat-codex-multi
tail -f ~/Library/Logs/wechat-codex-multi/stderr.log
```

微信管理员可用 `/restart` 重启由 launchd 监护的服务。前台 `start` 模式应在终端停止并重新运行。更新配置或代码后都需要重启进程。

## 排障

| 现象 | 检查和处理 |
| --- | --- |
| `/codex-login` 提示无权限 | 用微信 `/status` 返回的 `conversation` 找到自己消息对应的 `userId`，写入 `adminUsers` 并重启 |
| `/codex-login` 找不到账号 | 先把账号名和 `codexHome` 加入 `codex.accounts`，重启后再发送命令 |
| 设备码过期或未收到 | 重新发送 `/codex-login <账号名> <邮箱>`；确认 CLI 在服务的 `PATH` 内，查看服务日志 |
| `No such file or directory: .../ChatGPT.app/.../codex` | 更新服务并重启；旧桌面程序路径会自动重新定位。自定义路径须改成本机真实的 `codex.bin` / `codex.desktopBin`；完全未安装时先安装 Codex CLI |
| 终端能运行 codex，后台提示找不到程序 | 更新后重新执行部署脚本，使 launchd 继承当前终端 PATH；也可在 `codex.bin` 配置 `command -v codex` 返回的绝对路径 |
| `workspace routing discovery unauthorized (401)` | 自动刷新仍失败时，用 `/account` 确认账号，再由管理员发送 `/codex-login <账号名>`；终端登录必须设置该账号的 `CODEX_HOME`。`codex login status` 成功只代表本地存在凭据 |
| refresh token already used | 在发生错误的那台机器、对应 `CODEX_HOME` 重新登录；停止用其他机器或旧备份的 `auth.json` 覆盖它 |
| `/login` 没有微信二维码 | 确认已安装 npm 依赖；可在本机终端执行 `python3 -m wechat_codex_multi add-account` |
| `/models` 查询失败 | 检查所选 CLI 是否已安装、可执行，以及账号登录状态；也可在配置中设置固定 `modelOptions` |
| 修改代码后命令仍旧 | 重启运行中的服务；launchd 模式可由管理员发送 `/restart` |

## 本地 CLI 与开发

```bash
python3 -m wechat_codex_multi status
python3 -m wechat_codex_multi --help
python3 -m wechat_codex_multi claude-usage --days 7
python3 -m unittest discover -s tests
```

本地账号管理命令还有 `add-account [昵称]`、`rename-user <选择器> <新昵称>`、`delete-user <选择器>`，均以 `python3 -m wechat_codex_multi` 开头。`npm run setup`、`npm run start`、`npm run status` 分别用于初始化、启动和查看状态。主要实现位于 `wechat_codex_multi/`；媒体命令位于 `local_agent_tools/`，使用说明见 `skills/send-media/SKILL.md`。
