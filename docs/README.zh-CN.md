# Local Codex Bridge

[English](../README.md) | [简体中文](README.zh-CN.md)

Local Codex Bridge 是一个基于项目 profile 的本地 MCP 服务器。它允许 ChatGPT 启动并检查你自己电脑上的 **本地 Codex CLI** 任务。

它适合这样的工作流：你不想使用云端 Codex 作为执行器，而是希望使用本地仓库访问、本地 git remotes，以及由操作者控制的 Codex 模型，例如本地 Codex CLI 支持的 `gpt-5.5`。

Local Codex Bridge 是一个独立、通用的开发者 MCP bridge。它不假设任何特定下游项目；它应支持任何已配置的本地仓库 profile。

## 为什么需要它

有些云端任务路径会隐藏或自动选择模型，也可能没有稳定的 PR / push 路径，并且需要人反复复制粘贴提示词和结果。Local Codex Bridge 把执行器留在本地：

```text
ChatGPT -> MCP connector -> HTTPS tunnel -> Local Codex Bridge -> local Codex CLI -> local repo
```

这样 ChatGPT 只获得一个窄的、可检查的工具接口，而真正的 Codex 执行仍发生在操作者自己的机器上。

## 工作路径

```text
Human
  -> 每个工作会话启动一次 bridge server 和安全 HTTPS tunnel
  -> 在接受变更前审查 diff 和验证输出

ChatGPT
  -> 通过 custom MCP connector 连接
  -> 检查项目 profile、git status、HEAD 和 remotes
  -> 启动有边界的本地 Codex 任务
  -> 读取任务日志、diff 和验证输出
  -> 审查结果，并在 acceptance commit/push 前请求人工批准

Local Codex Bridge
  -> 运行在操作者自己的机器上
  -> 只暴露已配置的项目 profile 和 allowlist 操作
  -> 在指定 repo 内调用本地 Codex CLI
  -> 可以对已批准文件执行受控的、人工批准的 git add/commit/push

Local Codex CLI
  -> 使用操作者本地选择的模型和配置
  -> 在被授权时修改本地项目 repo
  -> 返回实现输出和验证证据

GitHub 或其他 VCS host
  -> 作为 commit、PR、diff 和最终落地状态的持久事实来源
```

## 工具接口

工具接口刻意保持保守：

- `list_projects` — 列出已配置的项目 profiles。
- `get_project_status` — 返回项目的 git status、HEAD 和 remotes。
- `start_codex_task` — 在已配置项目中启动 `codex exec`。
- `get_task` — 读取任务元数据和 stdout / stderr 尾部。
- `list_tasks` — 列出最近的 bridge 任务记录。
- `abort_task` — 终止正在运行的本地 Codex 进程。
- `get_git_diff` — 检查 git status、diff stat 和 diff。
- `run_verification` — 运行项目配置中 allowlist 的验证命令。
- `git_commit_and_push` — 在人工批准后，stage 已批准文件、创建一个 commit，并 push 到 `origin` 上的当前分支。

v0 不暴露任意 shell 执行。验证命令必须在每个项目 profile 中显式 allowlist。`git_commit_and_push` 是 bridge 自有的 Git acceptance 操作，不是通用 shell 或通用文件系统工具。

## 受控 acceptance 流程

预期的 acceptance 工作流是：

```text
ChatGPT 规划 / 审查
  -> 本地 Codex CLI 修改已配置 repo
  -> ChatGPT 审查 diff 和验证输出
  -> 人工接受
  -> Local Codex Bridge 执行受控 git add/commit/push
```

只有在人工已经审查精确 diff 和验证证据后，才应调用 `git_commit_and_push`。它的安全措施包括：

- 只能访问已配置项目根目录。
- 不暴露任意 shell 执行。
- 验证命令仍按项目 profile allowlist。
- remote 当前只支持 `origin`。
- 如果省略 `branch`，bridge 使用当前 checkout 的分支。
- 如果显式提供 `branch`，它必须匹配当前 checkout 的分支。
- 已批准文件路径会规范化为 repo-relative 路径，并且必须留在已配置项目根目录内。
- Git staging 使用 literal pathspec 处理，因此 `:(glob)*` 这类 pathspec magic 不会被展开。
- 支持 modified、新增和删除的文件。
- 拒绝未批准的预先 staged 文件。
- 创建 commit 前，staged 文件必须与已批准文件列表完全一致。
- 不安全输入或状态会返回结构化 `blocked_*` diagnostics，并在相关时包含有用的 git 证据。

## 环境要求

- macOS、Linux，或其他可以运行 Python 和 Codex CLI 的环境。
- Python 3.11+。
- 已安装并登录的本地 OpenAI Codex CLI。
- 一个你希望 Codex 操作的本地 git 仓库。
- 如果要让 ChatGPT Web 连接本地服务，需要 ngrok 或 Cloudflare Tunnel 之类的 tunnel provider。
- ChatGPT custom MCP connector 权限。

Tunnel 是外部部署层，不是 Local Codex Bridge 的 Python runtime 依赖。

## 1. 安装 Local Codex Bridge

```bash
git clone https://github.com/coolsidsudo/local-codex-bridge.git
cd local-codex-bridge

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

检查命令是否可用：

```bash
local-codex-bridge --help
```

## 2. 验证本地 Codex CLI 和模型

```bash
codex --version
codex exec -m gpt-5.5 "Print exactly: local codex bridge readiness ok"
```

预期结果中应该包含一次 Codex 运行，并最终输出：

```text
local codex bridge readiness ok
```

如果 Codex CLI 提示 `gpt-5.5` 需要更新版本，请先升级 Codex CLI，再重试：

```bash
codex --upgrade
hash -r
codex --version
codex exec -m gpt-5.5 "Print exactly: local codex bridge readiness ok"
```

如果 `codex --upgrade` 不可用或没有更新，请使用你平台对应的 Codex 安装 / 更新方式。对于 npm 安装，通常是：

```bash
npm install -g @openai/codex@latest
hash -r
codex --version
```

如果你的本地 Codex CLI 不支持 `gpt-5.5`，请使用你的安装版本实际支持的模型 id，并把它写入 bridge config。

## 3. 配置项目 profiles

创建配置文件：

```bash
mkdir -p ~/.local-codex-bridge
cp config.example.toml ~/.local-codex-bridge/config.toml
$EDITOR ~/.local-codex-bridge/config.toml
```

通用项目示例：

```toml
[server]
host = "127.0.0.1"
port = 8765
task_dir = "~/.local-codex-bridge/tasks"
codex_bin = "codex"
default_model = "gpt-5.5"
default_codex_args = ["--json"]

[projects.my_project]
name = "My Project"
path = "~/Projects/my-project"
default_model = "gpt-5.5"

[projects.my_project.verification]
git_status = ["git", "status", "--short", "--branch"]
test = ["python", "-m", "pytest"]
```

文档站点项目示例：

```toml
[projects.docs_site]
name = "Docs Site"
path = "~/Projects/docs-site"
default_model = "gpt-5.5"

[projects.docs_site.verification]
git_status = ["git", "status", "--short", "--branch"]
build = ["npm", "run", "build"]
```

每个本地 repo 使用一个项目 profile。Bridge 应保持通用，不应在 bridge 源码中硬编码某个具体项目的逻辑。

## 4. 本地启动 bridge

在终端 1 中运行：

```bash
cd ~/Projects/local-codex-bridge
source .venv/bin/activate
local-codex-bridge serve --config ~/.local-codex-bridge/config.toml
```

服务器应该显示类似：

```text
Starting MCP server 'Local Codex Bridge' with transport 'streamable-http'
on http://127.0.0.1:8765/mcp
```

保持这个终端开启。

## 5. 测试本地 MCP endpoint

在另一个终端运行：

```bash
curl -i http://127.0.0.1:8765/mcp
```

普通 curl 请求可能返回 `406 Not Acceptable` 或类似 MCP 协议响应。这是正常的，因为 curl 不是完整 MCP client。重要信号是服务器有响应并记录了请求。

常见的可达性响应类似：

```text
HTTP/1.1 406 Not Acceptable
...
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Not Acceptable: Client must accept text/event-stream"}}
```

## 6. 安装并配置 ngrok

ngrok 是一种临时 / 开发 tunnel 选项。Cloudflare Tunnel 也是很多场景下偏好的 / 未来可选部署方式。Tunnel 客户端属于外部部署工具；Local Codex Bridge 的 Python runtime 不依赖 `ngrok`、`cloudflared` 或任何 tunnel provider package。

在 macOS 上可用 Homebrew 安装 ngrok：

```bash
brew install ngrok/ngrok/ngrok
ngrok version
```

创建或登录 ngrok 账号，然后添加 authtoken：

```bash
ngrok config add-authtoken YOUR_NGROK_TOKEN
```

命令应返回类似：

```text
Authtoken saved to configuration file: /Users/<you>/Library/Application Support/ngrok/ngrok.yml
```

## 7. 启动 HTTPS tunnel

保持 bridge server 运行，在终端 2 中运行：

```bash
ngrok http 8765
```

ngrok 应该显示一个 forwarding URL：

```text
Forwarding  https://example-name.ngrok-free.dev -> http://localhost:8765
```

你的 MCP endpoint 是 forwarding URL 加上 `/mcp`：

```text
https://example-name.ngrok-free.dev/mcp
```

不要把这个 URL 提交到 repo。免费 ngrok URL 通常是临时的，应当作为当前会话的运行细节处理。

如果使用 Cloudflare Tunnel 或其他 provider，把它指向同一个本地 bridge endpoint（通常是 `http://127.0.0.1:8765`），并将远程 HTTPS URL 加 `/mcp` 暴露给 ChatGPT connector。 稳定的 Cloudflare Tunnel 设置请参考 [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md)。

## 8. 测试 tunnel endpoint

在第三个终端运行：

```bash
curl -i -H "Accept: text/event-stream" https://example-name.ngrok-free.dev/mcp
```

类似下面的响应是正常的：

```text
HTTP/2 400
...
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}
```

这表示 HTTPS tunnel 已经能到达 MCP server。真正的 MCP client 会处理 session setup；curl 不会。

## 9. 在 ChatGPT 中添加 custom MCP connector

在 ChatGPT Web 中：

1. 打开 **Settings**。
2. 进入 **Apps & Connectors**。
3. 打开 **Advanced settings**。
4. 如有需要，启用 **Developer mode**。
5. 回到 **Apps & Connectors**。
6. 创建一个新的 custom connector。
7. 名称可使用 `Local Codex Bridge`。
8. MCP server URL 填入当前 tunnel URL 加 `/mcp`：

```text
https://example-name.ngrok-free.dev/mcp
```

9. 第一次本地验证可以不设置认证；严肃使用时建议把 tunnel 放在访问控制之后。
10. 保存并连接 connector。

连接后，ChatGPT 设置中应该能看到这些 bridge actions：

```text
list_projects
get_project_status
start_codex_task
get_task
list_tasks
abort_task
get_git_diff
run_verification
git_commit_and_push
```

ChatGPT 侧 developer MCP 错误，例如 `FORBIDDEN: This conversation does not support developer MCPs`，应视为平台 / conversation gating。Local Codex Bridge 不能保证通过修改 bridge 代码来解除这个平台限制。

## 10. 在聊天中选择 connector

在新的或刷新后的 ChatGPT 聊天中：

1. 打开消息框旁边的 `+` 菜单。
2. 如有需要，打开 **More**。
3. 选择 **Local Codex Bridge**。

然后发送：

```text
Use Local Codex Bridge and list configured projects.
```

健康的响应应该显示你配置的项目 profiles。

## 11. 不修改文件的项目 smoke test

让 ChatGPT 通过 bridge 执行这个流程：

```text
Use Local Codex Bridge.

Smoke test only. Do not edit files.

1. Call list_projects.
2. Call get_project_status for project_id `my_project`.
3. Call run_verification for project_id `my_project`, command_key `git_status`.
4. If the worktree is dirty, stop.
5. If the worktree is clean, start one local Codex task with a no-edit prompt:

   Read the repo root and report the current directory, git branch, visible Codex run metadata, and final status `bridge_smoke_test_completed`. Do not edit files.

6. Poll get_task until it exits.
7. Call get_git_diff and confirm no files changed.
```

## 12. 日常使用检查清单

开始真正实现任务之前：

```text
1. 启动 local-codex-bridge serve。
2. 启动安全 HTTPS tunnel，例如 ngrok http 8765。
3. 如果 tunnel URL 改变，刷新 ChatGPT connector URL。
4. 在聊天中选择 Local Codex Bridge。
5. 运行 list_projects。
6. 对目标项目运行 get_project_status。
7. 运行 git_status verification。
8. 确认 branch、HEAD、remote 和 clean worktree。
9. 启动有边界的本地 Codex task。
10. 审查 stdout/stderr、git diff 和 verification output。
11. 如果变更不可接受，请让 Codex 修改或停止。
12. 如果变更可接受，明确批准精确文件列表和 commit message。
13. 只有在人工批准后才调用 git_commit_and_push。
14. 确认返回的 branch、remote、commit、push output 和 final status。
```

## 13. 常见问题

### `curl /mcp` 返回 406 或 400

通常没问题。这表示 MCP endpoint 是活的，但 curl 不是完整 MCP client。

### ChatGPT 设置里能看到 connector，但聊天里不能用

连接 app 后尝试新建聊天，然后从 `+` 菜单选择 connector。

如果 ChatGPT 返回 `FORBIDDEN: This conversation does not support developer MCPs`，请把它视为平台 / conversation gating。请尝试支持 developer MCP 的 ChatGPT surface / conversation；bridge 代码不能保证修复这个平台限制。

### `gpt-5.5` 需要更新的 Codex 版本

升级 Codex CLI 后重试。

### Codex stderr 出现无关 MCP token 错误

你的本地 Codex config 里可能配置了某个 token 过期的 MCP server。除非任务需要那个 MCP server，否则通常和这个 bridge 无关。

### 任务开始前 worktree 已经 dirty

停止并先检查。Bridge 故意暴露 dirty 状态，避免把无关改动混入 Codex 任务。

### `git_commit_and_push` 返回 `blocked_*`

阅读结构化 diagnostics。常见原因包括空文件列表、空 commit message、非 `origin` remote、branch mismatch、路径逃逸项目根目录、未批准的预先 staged 文件，或 staged 文件与已批准文件列表不完全一致。重试前请检查 git 状态，尤其是在失败操作可能留下已批准变更 staged 的情况下。

## 14. 安全说明

这个 bridge 可以让本地 Codex 修改已配置仓库中的文件，并且在明确人工批准后可以执行受控 acceptance commit/push。请把它当作强大的本地自动化工具。

建议默认做法：

- 只绑定到 `127.0.0.1`。
- 只通过认证 tunnel、私有网络或带访问控制的反向代理暴露。
- 只配置你愿意让 ChatGPT / Codex 操作的 repo。
- 验证命令必须 allowlist。
- 在接受变更前审查 diff 和 verification output。
- 只对已审查和已批准文件使用 `git_commit_and_push`。
- 不要在 prompt 中传递 secrets。
- 不要在公开 issue 或 docs 中发布临时 tunnel URL。
- 除非完全理解风险，否则不要添加任意 shell 执行能力。

更多信息见 [`SECURITY.md`](SECURITY.md)。

## 开发

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
python3 -m compileall src
python3 -m pytest
```

## 状态

Research-preview local workflow utility. Use carefully.
