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

先编辑 `config.json`：将 `codex.workingDirectory` 设为实际项目目录，检查 `codex.accounts` 中的账号目录。示例文件含 `main`、`backup`、`work`，这些目录需要分别登录；不使用的账号可以从数组中删除。

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
| `codex.bin`、`claude.bin` | CLI 命令名或绝对路径 |
| `codex.desktopBin` | 桌面会话 App Server 的可执行文件；macOS 默认优先使用 ChatGPT.app 内置的 Codex，与桌面版本保持一致 |
| `codex.workingDirectory` | 默认项目目录；`claude.workingDirectory` 为空时沿用此目录 |
| `codex.runner` | `exec` 或 `app-server`；默认 `exec` |
| `codex.defaultAccount`、`codex.accounts` | 默认 Codex 账号和各账号的 `codexHome` |
| `claude.defaultAccount`、`claude.accounts` | 默认 Claude 账号和各账号的 `claudeConfigDir`；空路径使用系统默认登录态 |
| `codex.model`、`codex.reasoningEffort` | Codex 默认模型和 reasoning 档位 |
| `claude.model`、`claude.effort` | Claude 默认模型和 effort 档位 |
| `codex.modelOptions`、`claude.modelOptions` | 固定微信中的可选模型列表；空数组时由 CLI 发现 |
| `codex.modelDiscoveryTimeoutSeconds` | Codex 模型发现超时，默认 30 秒 |
| `claude.modelDiscoveryTimeoutSeconds`、`modelDiscoveryCacheSeconds` | Claude 模型发现超时和缓存，默认 20 秒、0 秒 |
| `codex.timeoutMs`、`claude.timeoutMs` | 单次任务超时，默认 2 小时 |
| `codex.bypassApprovalsAndSandbox` | 是否给 Codex 传入跳过审批与沙箱的参数，默认 `true` |
| `claude.permissionMode` | Claude Code 权限模式，默认 `bypassPermissions` |
| `codex.extraPrompt`、`claude.extraPrompt` | 追加到对应 Agent 的提示 |
| `concurrency.maxWorkers`、`commandWorkers` | 普通任务与命令任务的线程数，默认 4、2 |
| `concurrency.perConversationSerial` | 是否串行处理同一工作区，默认 `true` |
| `state.saveDebounceMs` | 状态文件写入防抖时间，默认 1000 毫秒 |
| `media.maxFileBytes`、`maxConcurrentTransfers` | 单文件发送上限与媒体传输并发数 |
| `media.generators` | 可选的外部图片、视频等媒体生成命令 |
| `allowedUsers`、`adminUsers` | 可访问用户与管理员的微信 `userId` 列表 |
| `textChunkLimit`、`logLevel` | 微信文本分片长度和日志级别 |

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

修改 `config.json` 后重启服务才会加载新设置。微信中的 `/runner` 切换仅作用于当前服务进程，重启后仍采用配置文件中的值。

## 微信命令

命令由服务直接处理，不会作为普通提示发给 LLM。以下命令都在与 Bot 的聊天中发送。

### 状态和用量

| 命令 | 作用 |
| --- | --- |
| `/help`、`/help all` | 查看常用命令或完整命令清单 |
| `/status` | 查看当前工作区、目录、Agent、账号、模型、会话与运行状态 |
| `/active` | 查看正在运行的任务 |
| `/accounts`、`/users` | 列出已连接的微信 Bot 账号和用户信息 |
| `/usage` | 查看当前 Agent、当前账号用量 |
| `/usage codex`、`/usage claude` | 查看当前工作区对应的 Codex 或 Claude 账号用量 |
| `/usage all`、`/usage codex all`、`/usage claude all` | 汇总配置中的全部账号用量 |
| `/usage claude api [days]` | 用 Anthropic Admin API 查看组织级 Claude API 用量；需要 Admin Key |

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
| `/model <编号|model:档位>` | 切换模型或 reasoning/effort，重置当前 Agent 会话 |
| `/runner [exec|app-server]` | 查看或临时切换 Codex runner |

`/codex-use <名称>` 是旧版兼容命令，等价于 `/codex <名称>`。账号选择支持编号、完整名称和唯一前缀；切换账号会重置该 Agent 的会话 ID。切换 Agent 时，Codex 和 Claude 各自的会话 ID 分开保存。

当 `modelOptions` 为空时，Codex 使用默认账号的 `CODEX_HOME` 调用 `codex debug models`；发现超时才退回内置列表。Claude 通过 CLI 的 stream-json 初始化协议发现模型；失败时会提示错误。模型清单取决于已安装的 CLI 和账号，README 不固定列出某个版本的清单。`/model <编号>` 中的编号以刚查询到的列表为准。

Claude 的 `ultracode` 是支持 `xhigh` 的模型可选的 effort 模式，不是独立模型。切换模型或档位会清空当前 Agent 的会话 ID，下次任务会开启新会话。

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
| `/session delete <编号|sessionId前缀>` | 预览永久删除；60 秒内再加 `confirm` 确认，仅管理员可用 |
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
| `/d-session delete <会话编号>` | 预览永久删除；60 秒内再加 `confirm` 确认，仅管理员可用 |
| `/d-account [账号]`、`/d-session off` | 选择会话账号、退出桌面 App Server 路由 |
| `/reset` | 取消当前任务并重置当前 Agent 会话 |

每个 `accountId:userId` 有自己的默认工作区；额外工作区的会话 key 为 `accountId:userId:workspaceName`。同一用户可以让不同工作区并行执行。工作区分别保存目录、Agent、Codex 与 Claude 会话、账号和模型选择。

`/new-project /Users/bot/Documents/demo` 会在目录不存在时创建目录，以目录名 `demo` 登记 CLI 工作区并切换进去。缩写为 `/n-p`。

`/d-p-n /Users/bot/Documents/demo` 通过 Codex App Server 的 `project/create` 创建桌面原生项目，以该目录作为项目根目录；目录不存在时先创建。同一路径已有桌面项目则直接选中。微信只保存当前选中的原生项目 ID、目录和会话 ID，不额外创建微信工作区。发送第一条任务时通过 `thread/start` 指定该项目 ID；任务结果发回微信，用户消息和 Codex 回复也写入桌面端同一会话。任务完成后，可在 `/d-sessions <项目编号>` 或桌面应用的该项目中查看、续聊。已有项目可用 `/d-project use <项目编号>` 切换，再发送任务创建新会话。`/d-new-project`、`/d-n-p` 与 `/d-p-n` 等价。

`/sessions` 读取本机 CLI 会话数据，不请求模型。列表中的编号只对当前工作区最近一次查询有效；列出归档会话后，编号用于 `/session unarchive` 或 `/session delete`。`/session use` 在当前任务运行时会拒绝切换。Codex CLI 与桌面 Codex 共用本地会话库，因此归档和删除 Codex CLI 会话也会影响桌面端。Claude Code 没有原生归档命令；`/session archive` 对 Claude 只在本 Bot 的列表中隐藏会话，`/session unarchive` 可恢复显示，本机 Claude CLI 仍可找到它。删除 Claude 会话会移除该账号 `projects` 中对应的会话 JSONL 和 `usage-data/session-meta` 元数据；这不代表清除 Claude Code 的所有缓存或其他副本。

`/d-` 前缀表示桌面 Codex；旧版 `/desktop` 命令继续兼容。桌面命令通过同一 `CODEX_HOME` 的 Codex App Server 协议读写原生项目和会话。桌面应用与微信使用同一份本地项目、会话数据；桌面窗口可能需要刷新才能看到外部进程新建的项目或会话。`status` 显示最后保存的回合状态，只有本 Bot 发起的回合能显示实时运行状态并接受 `guide` 或 `interrupt`。普通 ChatGPT Chat/Work 对话不在此命令范围内。会话编号以最近一次 `/d-sessions` 列表为准；切换账号后需重新列出。归档和删除可能影响派生子会话；删除不可恢复。

翻页示例：`/d-sessions all 2` 或 `/d-sessions page 2` 查看所有项目的第 2 页；`/d-sessions 3 2` 查看项目 3 的第 2 页。单独的 `/d-sessions 2` 表示“项目 2”，不是“第 2 页”。每页 20 条，第 2 页的编号从 21 开始；发送 `/d-session 21` 或 `/d-session use 21` 就能切到第 21 条。编号以最近一次列表为准，列表末尾会给出上一页和下一页命令。

同一微信用户可以在桌面会话 A 执行期间用 `/d-session use` 切到 B，并向 B 发起另一个任务。各会话独立运行；后台会话完成时暂不主动发送结果。每次成功切换桌面会话，微信都会重发该会话最近一次完整的文字回答，帮助接上进度。若新任务仍在运行，会同时标明“正在执行中”和“上一条完整回答”；任务结束后切回，会显示新结果。回顾消息只发送文字，不重复执行媒体发送动作。当前选中的会话完成时会直接回复微信；`/d-session view` 只读取指定会话的最新结果，不回放历史回合。

### 运行中的任务

| 命令 | 作用 |
| --- | --- |
| 普通消息 | 正在运行时视为补充引导 |
| `/guide <内容>` | 显式追加补充引导 |
| `/interrupt`、`/cancel` | 中断任务并保留当前 Agent 会话 |
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
| `/restart` | 重启后台服务；需有 launchd 等监护进程自动拉起 |

`/accounts`、`/users` 可由允许访问 Bot 的用户查询；修改、登录和重启操作需要 `adminUsers`。如果你不希望普通用户看到账号列表，应只允许可信用户访问 Bot。终端也可运行 `python3 -m wechat_codex_multi rename-user <选择器> <新昵称>` 或 `delete-user <选择器>`。

## 远程登录 Codex

先在 `config.json` 注册独立账号目录，例如 `backup` 对应 `~/.codex-accounts/backup`。如果要用 `~/.codex-accounts/backup2`，就在 `codex.accounts` 中添加 `{"name":"backup2","codexHome":"~/.codex-accounts/backup2"}`。后台服务需要能够调用 `codex`，但此目录可以尚未登录。新增配置后重启服务。

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

常用选项：`--skip-account`、`--skip-npm`、`--no-start`、`--config /path/to/config.json`、`--venv /path/to/.venv`。脚本不会安装 Codex 或 Claude CLI，也不会覆盖已有配置。默认 launchd label 是 `com.wechat-codex-multi`；如果机器上已经用其他 label 运行本项目，先确认旧服务，避免同时启动两个轮询进程。可以设置 `WECHAT_CODEX_MULTI_LABEL` 使用现有 label。

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
| `workspace routing discovery unauthorized (401)` | 用 `/account` 确认实际选中的账号，再查对应 `CODEX_HOME` 的 `codex login status`；必要时重新设备码登录 |
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
