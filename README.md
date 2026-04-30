# wechat-codex-multi-channel

把多个微信 Bot 账号接到同一个本地 Codex CLI 服务上。它适合把 Codex 当作微信里的长期在线助手使用，同时保留本机 `codex` 登录态、`CODEX_HOME`、工作目录、模型和 reasoning level 的控制权。

## 功能概览

- 多个微信 Bot 账号同时在线，统一由本地服务轮询和回复。
- 每个 `accountId:userId` 可创建多个项目工作区，每个工作区独立保存工作目录、Codex thread、Codex 账号和模型选择。
- 不同会话或不同工作区可并发处理；同一工作区默认串行，避免同一个 Codex thread 被并发写入。
- 同一工作区运行中继续发消息默认作为补充引导；`app-server` runner 会调用 Codex 原生 `turn/steer`，`exec` runner 会退回到队列续跑。
- 支持 `/codex` 在多个 `CODEX_HOME` 登录账号之间切换。
- 支持 `/models` 查看模型列表，`/model <编号|model:reasoning>` 切换模型。
- 支持 `/cwd` 为当前工作区切换 Codex 工作目录，支持 `/ws` 管理同一微信用户下的多个项目工作区。
- 支持 `/usage` 查看 Codex 5 小时窗口和周窗口用量。
- 支持文本、图片、文件、视频收发；入站媒体会下载到本地后把路径交给 Codex。
- 支持 Codex 回复媒体发送标记，把本地图片、文件、视频发回微信。
- 支持可配置的图片/视频生成器，以及 Codex 原生 `image_generation_end` 事件转微信图片。
- 支持 macOS `launchd` 开机自启和异常退出自动重启。

## 目录

- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [微信命令](#微信命令)
- [多 Codex 账号](#多-codex-账号)
- [模型和 Reasoning 切换](#模型和-reasoning-切换)
- [媒体发送协议](#媒体发送协议)
- [媒体生成器](#媒体生成器)
- [macOS 后台运行](#macos-后台运行)
- [开发和验证](#开发和验证)
- [常见问题](#常见问题)

## 快速开始

### 环境要求

- macOS 或 Linux。
- Python 3.10+。
- 已安装并登录本机 Codex CLI。
- 可访问微信 Bot 接口。

确认 Codex CLI 可用：

```bash
codex login status
```

安装并启动：

```bash
cd /path/to/project
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
npm install
cp config.example.json config.json
python3 -m wechat_codex_multi add-account
python3 -m wechat_codex_multi start
```

也可以用 npm 脚本：

```bash
npm run setup
npm run start
npm run status
```

继续添加微信 Bot 账号：

```bash
python3 -m wechat_codex_multi add-account
```

查看本地状态：

```bash
python3 -m wechat_codex_multi status
```

使用自定义配置文件：

```bash
WECHAT_CODEX_MULTI_CONFIG=/path/to/config.json python3 -m wechat_codex_multi start
python3 -m wechat_codex_multi --config /path/to/config.json start
```

## 配置说明

推荐从 `config.example.json` 复制一份本地配置：

```bash
cp config.example.json config.json
```

常用配置：

```json
{
  "stateDir": "~/.wechat-codex-multi",
  "defaultAgent": "codex",
  "wechat": {
    "baseUrl": "https://ilinkai.weixin.qq.com",
    "botType": "3",
    "routeTag": null
  },
  "codex": {
    "runner": "exec",
    "bin": "codex",
    "workingDirectory": ".",
    "model": "",
    "reasoningEffort": "",
    "modelOptions": [],
    "timeoutMs": 7200000,
    "bypassApprovalsAndSandbox": true,
    "defaultAccount": "main",
    "accounts": [
      {
        "name": "main",
        "codexHome": "~/.codex"
      }
    ],
    "extraPrompt": ""
  },
  "concurrency": {
    "maxWorkers": 4,
    "commandWorkers": 2,
    "perConversationSerial": true
  },
  "state": {
    "saveDebounceMs": 1000
  },
  "media": {
    "enabled": true,
    "maxFileBytes": 52428800,
    "maxConcurrentTransfers": 1,
    "generators": []
  },
  "allowedUsers": [],
  "adminUsers": [],
  "textChunkLimit": 4000,
  "logLevel": "INFO"
}
```

字段说明：

- `stateDir`：保存 Bot 账号、会话、`context_token` 和入站媒体文件。
- `wechat.baseUrl`：微信 Bot API 地址。
- `wechat.botType`：微信 Bot 类型，默认 `"3"`。
- `wechat.routeTag`：非空时作为 `SKRouteTag` 请求头发送。
- `codex.runner`：Codex 运行方式，`exec` 为兼容模式，`app-server` 使用 Codex 原生 app-server 支持运行中引导和 turn 中断。
- `codex.bin`：Codex CLI 路径，可以是 `codex` 或绝对路径。
- `codex.workingDirectory`：默认工作目录，微信中可用 `/cwd <path>` 覆盖当前工作区。
- `codex.model`：默认模型，非空时传给 `codex -m`。
- `codex.reasoningEffort`：默认 reasoning level，非空时传 `-c model_reasoning_effort="<level>"`。
- `codex.modelOptions`：固定 `/models` 展示的模型选项；为空时每次通过 `codex debug models` 实时查询 Codex CLI 可用模型。
- `codex.timeoutMs`：单次 Codex 执行超时，默认 2 小时。
- `codex.bypassApprovalsAndSandbox`：为 true 时传 `--dangerously-bypass-approvals-and-sandbox`。
- `codex.defaultAccount`：默认 Codex 账号名。
- `codex.accounts`：多个 Codex 登录账号，每个账号对应一个独立 `CODEX_HOME`。
- `codex.extraPrompt`：追加到发给 Codex 的系统提示。
- `concurrency.maxWorkers`：普通 Codex 任务线程数。
- `concurrency.commandWorkers`：微信命令处理线程数，避免 `/status`、`/models` 被长任务阻塞。
- `concurrency.perConversationSerial`：同一会话是否串行处理。
- `state.saveDebounceMs`：状态文件防抖写入间隔。
- `media.maxFileBytes`：允许发送的单个媒体文件最大字节数。
- `media.maxConcurrentTransfers`：媒体上传/下载并发数。
- `media.generators`：外部媒体生成器配置。
- `allowedUsers`：为空允许所有用户；非空时只响应列表内用户。
- `adminUsers`：允许通过微信触发 `/login` 的用户。
- `textChunkLimit`：长文本回复分片大小。
- `logLevel`：日志级别。

## 微信命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看帮助 |
| `/status` | 查看当前工作区、Codex 账号、模型、工作目录和 Bot 连接状态 |
| `/usage` | 查看 Codex 5 小时窗口和周窗口限额 |
| `/accounts` | 查看已连接的微信 Bot 账号 |
| `/codex-accounts` | 查看可用 Codex 账号 |
| `/codex` | 查看当前工作区使用的 Codex 账号 |
| `/codex <编号|名称|next|prev>` | 切换 Codex 账号，并重置当前 Codex thread |
| `/codex-use <name>` | 兼容旧命令，等价于 `/codex <name>` |
| `/models` | 查看可切换模型列表 |
| `/model` | 查看当前模型和模型切换说明 |
| `/model <编号|model:reasoning>` | 切换当前工作区模型，并重置当前 Codex thread |
| `/runner` | 查看当前 Codex runner |
| `/runner exec` | 切换到兼容的 `codex exec` runner |
| `/runner app-server` | 切换到 Codex 原生 app-server runner，支持运行中 `turn/steer` |
| `/cwd` | 查看当前工作区工作目录 |
| `/cwd <path>` | 修改当前工作区工作目录，并重置当前工作区 thread |
| `/ws` 或 `/ws list` | 查看当前微信用户的项目工作区、运行状态、工作目录和 thread |
| `/ws add <名称> <路径>` | 添加项目工作区 |
| `/ws use <名称>` | 切换当前工作区；之后普通消息会进入该工作区 |
| `/ws run <名称> <任务>` | 不切换当前工作区，直接在指定工作区派发 Codex 任务 |
| `/ws reset <名称>` | 取消运行中的任务并重置指定工作区 thread |
| `/reset` | 重置当前工作区；如果 Codex 正在运行，会先取消进程 |
| `/guide <补充要求>` | 当前任务运行中追加补充引导；直接发送普通消息也会追加 |
| `/interrupt` 或 `/cancel` | 立刻中断当前任务并重置当前工作区 |
| `/interrupt <新任务>` | 中断当前任务，重置当前 thread，并在当前进程退出后自动执行新任务 |
| `/login` | 新增微信 Bot 账号，仅 `adminUsers` 可用 |
| `/restart` | 重启后台服务，仅 `adminUsers` 可用 |

常见用法：

```text
/status
/usage

/codex-accounts
/codex 2
/codex backup
/codex next

/models
/model 3
/model gpt-5.5:high
/runner app-server

/cwd /path/to/project-a
/reset
/interrupt 改成先修登录页，不要继续做 README

/ws add a /path/to/project-a
/ws add b /path/to/project-b
/ws
/ws run a 帮我改 README
/ws run b 跑测试并修 bug
```

`/codex` 切换的是 Codex 登录账号，也就是不同的 `CODEX_HOME`。`/model` 切换的是当前工作区使用的模型和 reasoning level。两者都会清空当前工作区的 `codexThreadId`，确保下一次请求用新的账号或模型启动。

### 运行中引导和中断

默认开启同一工作区串行处理时，Codex 正在运行期间，同一微信用户继续发送普通消息不会被拒绝，而是作为补充引导处理。

如果 `codex.runner` 为 `app-server`，服务会调用 Codex 原生 `turn/steer`，让当前 active turn 在运行中调整方向。

如果 `codex.runner` 为 `exec`，由于 `codex exec` 是一次性进程，服务会退回到补充引导队列：当前任务返回后合并补充要求并沿用当前工作区 thread 续跑。

斜杠开头的系统命令不会被识别为引导，例如 `/status`、`/usage`、`/reset`、`/interrupt` 会按命令单独处理。

可显式发送：

```text
/guide 标题改短一点，结尾加行动项
```

也可以直接发送普通文本，效果相同。运行中不需要特意输入 `/guide`。

需要放弃当前运行中的任务时：

```text
/interrupt
/cancel
```

需要立刻改做另一件事时：

```text
/interrupt 不要继续重构，先修复登录报错并跑测试
```

注意：`exec` runner 的中断会终止当前 `codex exec` 进程并重置当前工作区 thread；`app-server` runner 会优先调用 Codex 原生 `turn/interrupt`，失败时再由服务层做降级处理。

运行时可以用 `/runner exec` 或 `/runner app-server` 临时切换当前服务进程内的 runner。切换时会关闭旧 runner 的后台进程；服务重启后仍以 `config.json` 里的 `codex.runner` 为准。

## 多 Codex 账号

Codex 账号通过 `CODEX_HOME` 隔离。每个账号使用一套独立目录，目录内包含 `config.toml`、`auth.json`、sessions、sqlite 状态等。

先准备独立目录并登录：

```bash
CODEX_HOME=$HOME/.codex-accounts/backup codex login
CODEX_HOME=$HOME/.codex-accounts/backup codex login status
```

加入 `config.json`：

```json
{
  "codex": {
    "defaultAccount": "main",
    "accounts": [
      {
        "name": "main",
        "codexHome": "~/.codex"
      },
      {
        "name": "backup",
        "codexHome": "~/.codex-accounts/backup"
      },
      {
        "name": "work",
        "codexHome": "~/.codex-accounts/work"
      }
    ]
  }
}
```

微信里查看和切换：

```text
/codex-accounts
/codex
/codex 2
/codex backup
/codex next
/codex prev
```

`/codex <编号或名称>` 支持编号、完整名称、唯一前缀。切换账号时会清空当前 `codexThreadId`，避免不同 Codex 登录态之间错误 resume。

## 模型和 Reasoning 切换

微信里发送：

```text
/models
```

服务会按模型分组显示可切换项，适合在微信中阅读：

```text
可切换模型（发送 /model 编号 切换）：
gpt-5.5
1. gpt-5.5:low
2. gpt-5.5:medium
3. gpt-5.5:high
4. gpt-5.5:xhigh

gpt-5.4
5. gpt-5.4:low
6. gpt-5.4:medium
7. gpt-5.4:high
8. gpt-5.4:xhigh

gpt-5.4-mini
9. gpt-5.4-mini:low
10. gpt-5.4-mini:medium
11. gpt-5.4-mini:high
12. gpt-5.4-mini:xhigh
```

切换方式：

```text
/model 3
/model gpt-5.5:high
```

切换成功后会回复：

```text
已经切换到 gpt-5.5:high 模型
已重置当前 Codex thread。
```

默认内置模型列表：

```text
gpt-5.5:low
gpt-5.5:medium
gpt-5.5:high
gpt-5.5:xhigh
gpt-5.4:low
gpt-5.4:medium
gpt-5.4:high
gpt-5.4:xhigh
gpt-5.4-mini:low
gpt-5.4-mini:medium
gpt-5.4-mini:high
gpt-5.4-mini:xhigh
gpt-5.3-codex:low
gpt-5.3-codex:medium
gpt-5.3-codex:high
gpt-5.3-codex:xhigh
gpt-5.2:low
gpt-5.2:medium
gpt-5.2:high
gpt-5.2:xhigh
codex-auto-review:low
codex-auto-review:medium
codex-auto-review:high
codex-auto-review:xhigh
```

也可以在 `config.json` 固定可选项：

```json
{
  "codex": {
    "modelOptions": [
      {
        "model": "gpt-5.5",
        "reasoningEffort": "medium",
        "label": "GPT-5.5 medium"
      },
      {
        "model": "gpt-5.5",
        "reasoningEffort": "high",
        "label": "GPT-5.5 high"
      }
    ]
  }
}
```

## 媒体发送协议

Codex 最终回复里可以包含这些标记，服务会发送对应本地文件，并从文本回复里移除标记：

```text
[[send_image:/absolute/path/to/image.png]]
[[send_file:/absolute/path/to/report.pdf]]
[[send_video:/absolute/path/to/video.mp4]]
```

也支持单独一行的裸标记和 `file://` 路径：

```text
send_image:/absolute/path/to/image.png
file:///absolute/path/to/report.pdf
```

Codex CLI 返回 `image_generation_end` 事件时，服务会按当前 `CODEX_HOME/generated_images/<thread_id>/<call_id>.png` 自动生成图片发送标记。

## 媒体生成器

在 `config.json` 的 `media.generators` 里配置外部命令。命令从 stdin 接收 prompt，stdout 最后一行输出生成文件路径。

示例：

```json
{
  "media": {
    "generators": [
      {
        "name": "image",
        "kind": "image",
        "command": "python3 /path/to/generate_image.py",
        "description": "Receives prompt on stdin and prints an output file path."
      }
    ]
  }
}
```

Codex 可以调用：

```bash
python3 -m wechat_codex_multi media-generate image "生成一张产品海报"
```

然后在最终回复里写：

```text
[[send_image:/path/from/generator.png]]
```

这可以接入 OpenAI API、本地 Stable Diffusion、ComfyUI、Sora 或任意自定义服务。

## macOS 后台运行

推荐用 `launchd` 以当前用户身份运行。它可以在登录后自动启动，并在程序异常退出后自动拉起。

先确认项目路径、虚拟环境、配置文件和微信账号都已经准备好：

```bash
cd /path/to/project
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
npm install
cp config.example.json config.json
python3 -m wechat_codex_multi add-account
mkdir -p "$HOME/Library/Logs/wechat-codex-multi"
```

创建 `~/Library/LaunchAgents/com.wechat-codex-multi.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.wechat-codex-multi</string>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/project/.venv/bin/python</string>
    <string>-m</string>
    <string>wechat_codex_multi</string>
    <string>start</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/path/to/project</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>WECHAT_CODEX_MULTI_CONFIG</key>
    <string>/path/to/project/config.json</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/wechat-codex-multi/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/wechat-codex-multi/stderr.log</string>
</dict>
</plist>
```

加载和启动：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wechat-codex-multi.plist
launchctl enable gui/$(id -u)/com.wechat-codex-multi
launchctl kickstart -k gui/$(id -u)/com.wechat-codex-multi
```

查看状态和日志：

```bash
launchctl print gui/$(id -u)/com.wechat-codex-multi
tail -f ~/Library/Logs/wechat-codex-multi/stdout.log
tail -f ~/Library/Logs/wechat-codex-multi/stderr.log
```

停止、卸载或重载：

```bash
launchctl bootout gui/$(id -u)/com.wechat-codex-multi

# 修改 plist 后重载
launchctl bootout gui/$(id -u)/com.wechat-codex-multi 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wechat-codex-multi.plist
launchctl kickstart -k gui/$(id -u)/com.wechat-codex-multi
```

注意：

- `KeepAlive=true` 表示进程退出后会自动重启；临时停服务要先 `bootout`。
- `PATH` 要包含 `codex` 所在目录。Apple Silicon Homebrew 通常是 `/opt/homebrew/bin`，Intel Mac Homebrew 通常是 `/usr/local/bin`。
- 如果 Codex 使用多个 `CODEX_HOME`，确保这些目录都已经单独 `codex login`。
- `/login` 会在运行服务的终端输出二维码。使用 `launchd` 后看不到交互终端，建议先手动运行 `python3 -m wechat_codex_multi add-account`。

## 开发和验证

运行测试：

```bash
python3 -m unittest discover -s tests
```

查看当前 Git 状态：

```bash
git status --short
```

本地调试常用命令：

```bash
python3 -m wechat_codex_multi status
python3 -m wechat_codex_multi media-generate image "测试图片"
```

## 重要设计

基础会话 key 是：

```text
accountId:userId
```

同一个微信用户在不同 Bot 账号里聊天，会进入不同 Codex thread。

项目工作区会把会话 key 扩展为：

```text
accountId:userId:workspaceName
```

`default` 工作区继续使用基础会话 key，兼容原有会话。`/ws run <名称> <任务>` 会直接使用指定工作区的 key，因此同一个微信用户可以同时让不同项目工作区并发执行。

当同一个工作区有任务正在运行时，新的普通消息会立即收到“上一条消息还在处理中”的提示，不会继续排队卡住。`/status`、`/usage`、`/codex-accounts`、`/codex`、`/model`、`/models`、`/accounts`、`/help`、`/cwd`、`/ws`、`/restart` 可在任务运行中直接响应；`/reset` 可取消当前工作区正在运行的 Codex 并重置会话，`/ws reset <名称>` 可取消指定工作区。

超时、服务停止或 `/reset` 取消时，服务会清理 Codex 进程组，避免残留进程继续占用会话。

每次任务都会启动一个 `codex exec` 子进程。任务完成、失败、超时或被取消后，服务会从运行表中移除该进程；正常完成的进程会自动释放，不需要手动清理。

## 常见问题

### `/models` 显示哪些模型？

如果 `config.json` 没有配置 `codex.modelOptions`，服务会在每次执行 `/models` 或 `/model` 时调用 `codex debug models` 实时查询当前 Codex CLI 返回的模型和 reasoning levels。需要固定可选范围时，配置 `codex.modelOptions`；配置后不会再实时查询。

### `/model 1` 切换后为什么要重置 thread？

Codex thread 和模型、reasoning、账号登录态有关。切换模型后清空当前 `codexThreadId`，下一次请求会用新模型启动新 thread，避免旧上下文混用。

### `/login` 看不到二维码怎么办？

后台服务没有交互终端时，二维码会出现在日志或后台 stdout 中。更稳妥的方式是在可见终端里执行：

```bash
python3 -m wechat_codex_multi add-account
```

### 修改代码后如何让后台服务生效？

如果使用 `launchd` 且配置了 `KeepAlive=true`，管理员可以在微信里发送：

```text
/restart
```

服务会先回复确认消息，然后退出当前进程，由 `launchd` 自动拉起新版本。也可以在机器上执行：

```bash
launchctl kickstart -k gui/$(id -u)/com.wechat-codex-multi
```

### 为什么普通消息会提示“上一条消息还在处理中”？

这是为了保护同一会话的 Codex thread 不被并发写入。命令类消息走独立 worker，通常仍可立即响应。
