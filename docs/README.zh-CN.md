# Local Codex Bridge

[English](../README.md) | [简体中文](README.zh-CN.md)

Local Codex Bridge 是一个轻量的本地 MCP bridge，用于 ChatGPT ↔ Codex 工作流。它允许 ChatGPT 针对已配置的本地仓库 profile 启动和检查 **本地 Codex CLI** 任务，同时避免给 ChatGPT 宽泛的本机权限。

它适合这样的场景：你不想把云端 Codex 当作执行器，而是需要本地仓库访问、本地 git remotes，以及由操作者控制的 Codex 模型，例如本地 Codex CLI 支持的 `gpt-5.5`。

Local Codex Bridge 是独立、通用、project-agnostic 的工具。它不假设下游项目，也不要求固定 PR workflow、review methodology 或个人工程方法论。

## 产品层次

LCB 应首先被理解为 bridge：

- **Core bridge** 是默认心智模型：project profiles、本地 Codex task 执行、task logs、repo status、changed-file inspection 和 allowlisted verification。你可以只使用这一层。
- **Controlled actions** 是可选的、由 bridge 拥有的 mutation tools，适合希望让 LCB 处理 branch creation、commit/push、PR creation、PR merge 或 post-merge local sync 的用户。这些能力是可选的；但一旦调用，runtime safety gates 就是强制的，因为它们保护真实的权限边界。
- **Engineering-control workflow** 是更严格 ChatGPT ↔ Codex 循环的可选 guidance：review contracts、readiness checks、evidence-first review、human approval gates 和 operating checklists。轻量 bridge 用法不要求采用它。

更多信息：

- [Product shape](PRODUCT_SHAPE.md) — 产品边界的事实来源。
- [Engineering-control workflow](ENGINEERING_CONTROL.md) — 可选严格 workflow guidance。
- [Full controlled-loop smoke runbook](FULL_LOOP_SMOKE.md) — 完整 controlled loop 的可选 maintainer release-smoke 指南。
- [Tool profiles design](TOOL_PROFILES.md) — 未来 runtime profiles 的 design-only 说明；当前尚未实现。

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
  -> 在接受 authority-changing step 前审查证据

ChatGPT
  -> 通过 custom MCP connector 连接
  -> 检查项目 profile、git status、HEAD 和 remotes
  -> 启动有边界的本地 Codex 任务
  -> 读取任务日志、changed-file evidence、diff 和 verification output

Local Codex Bridge
  -> 运行在操作者自己的机器上
  -> 只暴露已配置的项目 profile 和 allowlist 操作
  -> 在指定 repo 内调用本地 Codex CLI
  -> 在显式使用时，可选执行受控 branch、commit/push、PR、merge 和 local sync actions

Local Codex CLI
  -> 使用操作者本地选择的模型和配置
  -> 在被授权时修改本地项目 repo
  -> 返回实现输出和验证证据

GitHub 或其他 VCS host
  -> 作为 commit、PR、diff 和最终落地状态的持久事实来源
```

## 工具接口

当前 runtime 暴露以下工具。Runtime profiles 尚未实现；design-only 的 profile 说明见 [TOOL_PROFILES.md](TOOL_PROFILES.md)。

### Core bridge tools

只使用这些工具，就可以把 LCB 当作轻量 bridge：

- `list_projects` — 列出已配置的项目 profiles。
- `get_project_status` — 返回项目的 git status、HEAD 和 remotes。
- `start_codex_task` — 在已配置项目中启动 `codex exec`。可选的 `review_contract` 会追加面向审查的简洁输出 guidance。
- `get_task` — 读取任务元数据和 stdout / stderr 尾部。
- `list_tasks` — 列出最近的 bridge 任务记录。
- `abort_task` — 终止正在运行的本地 Codex 进程。
- `git_get_branch_status` — 返回当前分支、dirty 状态、HEAD、remotes、upstream 和 ahead/behind 证据。
- `get_git_diff` — 检查 git status、unstaged/staged diffs，以及有边界的 untracked 文件预览。
- `get_review_package` — 返回紧凑、只读的变更文件索引和 status/stat 证据，不包含完整 diff 或完整文件内容。
- `get_changed_file_diff` — 返回单个 changed/staged/untracked 文件的有边界 targeted diff。
- `get_changed_file_text` — 返回单个 changed/staged/untracked 文件的有边界 UTF-8 文本。
- `run_verification` — 运行项目配置中 allowlist 的验证命令。
- `run_verification_bundle` — 按顺序运行多个已配置 verification keys，并返回有边界的逐命令证据。

### Optional controlled action tools

这些工具会改变带有权限意义的状态。能力本身是可选的，但调用时 safety gates 不是可选的：

- `git_create_work_branch` — 基于已有本地 base 分支创建并切换到新的本地工作分支。
- `git_commit_and_push` — 在人工批准后，stage 已批准文件、创建一个 commit，并 push 到 `origin` 上的当前分支。
- `github_create_pr` — 通过已安装的 `gh` CLI，为已经 push 的当前分支创建 GitHub pull request。
- `github_merge_pr` — 在人工批准后，通过固定 `gh pr merge` argv merge 单个 ready 的 GitHub pull request。
- `git_sync_local_branch_to_origin` — 审查后把干净的本地目标分支同步到本地 `origin/<target>` ref；不 fetch、pull、push、merge 或修改 PR。

### Optional engineering-control / readiness helpers

这些工具和选项支持更严格 review loop，但不会让该 workflow 变成必需：

- `start_codex_task` with `review_contract: true` — 要求 Codex 返回简洁实现摘要，而不是完整 diff 或完整文件内容。这是行为 guidance，不是安全边界。
- `get_acceptance_readiness` — 只读预检当前 repo 状态是否看起来可以执行人工批准的 `git_commit_and_push`。
- `github_get_pr_status` — 通过已安装的 `gh` CLI 读取 GitHub pull request 状态、证据和保守 advisory 的 PR-only readiness 证据。
- `get_pr_sync_readiness` — 只读报告把 PR readiness 与本地目标分支 sync readiness 结合后的 advisory 证据。

v0 不暴露任意 shell 执行。验证命令必须在每个项目 profile 中显式 allowlist。Bridge-owned Git/GitHub tools 使用固定 argv，并在不安全输入或状态下返回结构化 `blocked_*` diagnostics。

GitHub PR 工具把 `gh` 作为外部 substrate。Local Codex Bridge 不实现原生 GitHub API / token 处理，也不存储、打印或管理 GitHub token。

可选 workflow guidance 不是安全边界。Mutation tools 的 runtime safety gates 与安全相关；即使你没有采用 engineering-control workflow，它们仍会被执行。

## 环境要求

- macOS、Linux，或其他可以运行 Python 和 Codex CLI 的环境。
- Python 3.11+。
- 已安装并登录的本地 OpenAI Codex CLI。
- 一个你希望 Codex 操作的本地 git 仓库。
- GitHub PR 工具可选需要：已安装并对 `github.com` 完成认证的 GitHub CLI `gh`。
- 如果要让 ChatGPT Web 连接本地服务，需要 ngrok 或 Cloudflare Tunnel 之类的 tunnel provider。
- ChatGPT custom MCP connector 权限。

Tunnel 是外部部署层，不是 Local Codex Bridge 的 Python runtime 依赖。

## 认证状态

Local Codex Bridge 现在有一等公民的认证配置，并且会对公开式的无认证部署 fail closed。默认 `auth.mode = "auto"` 只允许在 loopback 本地开发且没有 `server.public_base_url` 时无认证运行。显式 `auth.mode = "disabled"` 也只允许 loopback。

推荐的公开 ChatGPT 兼容模式是 `auth.mode = "oidc_proxy"`，它使用 FastMCP 内置 OIDC proxy auth。将 `server.public_base_url` 设置为真实公开 HTTPS tunnel/domain，不要带 `/mcp`；ChatGPT connector URL 使用 `{public_base_url}/mcp`，IdP redirect URI 使用 `{public_base_url}/auth/callback`。OIDC client ID 和 client secret 必须来自环境变量。文档中的 `example.com` 和 `YOUR-...` 都只是占位符，并不存在。

`auth.mode = "static_bearer"` 也可用，token 必须来自 `LCB_AUTH_TOKEN` 这类环境变量。该模式只适合本地、内部或测试客户端发送标准 `Authorization: Bearer ...` header；它不是推荐的公开 ChatGPT custom MCP 认证路径。不要使用 query-string token。Cloudflare Tunnel 和 ngrok 只是传输层；LCB auth 才是安全边界。详见 [AUTH.md](AUTH.md)。


## 1. 安装 Local Codex Bridge

对于固定版本安装，请使用 GitHub tag。这是当前 tagged release 推荐的用户安装方式。

使用 `pipx`：

```bash
pipx install "git+https://github.com/coolsidsudo/local-codex-bridge.git@v0.2.0"
local-codex-bridge --help
```

使用 `uv`：

```bash
uv tool install "git+https://github.com/coolsidsudo/local-codex-bridge.git@v0.2.0"
local-codex-bridge --help
```

tag 安装不会自动创建 `~/.local-codex-bridge/config.toml`。`v0.2.0` 及之后的版本包含 `local-codex-bridge init`，这是推荐的配置路径。如果你安装的版本还没有 `init`，请使用下面的手动 `config.example.toml` 备用流程。

如果是 contributor / development 工作，请使用 clone 和 editable install：

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

对于包含 init wizard 的版本，推荐运行：

```bash
local-codex-bridge init --config ~/.local-codex-bridge/config.toml
local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
```

`init` 会一步步写入通用的项目配置，保持 server 默认只绑定 loopback，询问一个项目 profile，并且默认只 allowlist 安全的 `git_status` 验证命令。它不会启动 MCP、运行 Codex、联系 OIDC provider、收集 secrets，也不会把 token / client secret 值写入 TOML。对于 OIDC 或 static bearer，它只写入环境变量名称，并打印带占位符的 export 示例。

高级 / 手动备用流程（也适用于 `v0.1.0` 安装）：

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

## 4. 用 doctor 检查认证 / 设置

启动服务器前，先运行：

```bash
local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
```

`doctor` 会验证配置，但不会启动 MCP、运行 Codex，也不会联系你的 identity provider。对于 `auth.mode = "oidc_proxy"`，它会打印 ChatGPT connector URL、IdP redirect URI、provider config URL，以及已配置的 OIDC credential 环境变量是否已设置。它只打印环境变量名称，绝不会打印 bearer token、OIDC client ID 或 OIDC client secret 的值。

## 5. 本地启动 bridge

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

## 6. 测试本地 MCP endpoint

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

## 7. 安装并配置 ngrok

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

## 8. 启动 HTTPS tunnel

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

如果使用 Cloudflare Tunnel 或其他 provider，它只是传输层：把它指向同一个本地 bridge endpoint（通常是 `http://127.0.0.1:8765`）。公开 ChatGPT 工作应配置 LCB `auth.mode = "oidc_proxy"`；LCB auth 才是安全边界。稳定的 Cloudflare Tunnel 设置请参考 [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md)。

## 9. 测试 tunnel endpoint

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

## 10. 认证值分别填在哪里

对于 `auth.mode = "oidc_proxy"`：

- `server.public_base_url`：真实 HTTPS tunnel/domain，不带 `/mcp`。
- ChatGPT connector URL：`{public_base_url}/mcp`。
- IdP redirect URI：`{public_base_url}/auth/callback`。
- 环境变量：OIDC client ID 和 client secret。

`example.com` 域名和 `YOUR-...` 值只是占位符，并不存在；请替换为你的真实值。

## 11. 在 ChatGPT 中添加 custom MCP connector

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

9. 公开 ChatGPT 兼容部署应使用 `auth.mode = "oidc_proxy"`。`static_bearer` 仅适合本地/内部/测试。
10. 保存并连接 connector。

连接后，ChatGPT 设置中应该能看到这些 bridge actions：

```text
list_projects
get_project_status
start_codex_task
get_task
list_tasks
abort_task
get_review_package
get_changed_file_diff
get_changed_file_text
get_git_diff
git_get_branch_status
git_create_work_branch
get_acceptance_readiness
run_verification
run_verification_bundle
git_commit_and_push
github_create_pr
github_get_pr_status
github_merge_pr
get_pr_sync_readiness
git_sync_local_branch_to_origin
```

ChatGPT 侧 developer MCP 错误，例如 `FORBIDDEN: This conversation does not support developer MCPs`，应视为平台 / conversation gating。Local Codex Bridge 不能保证通过修改 bridge 代码来解除这个平台限制。

## 12. 在聊天中选择 connector

在新的或刷新后的 ChatGPT 聊天中：

1. 打开消息框旁边的 `+` 菜单。
2. 如有需要，打开 **More**。
3. 选择 **Local Codex Bridge**。

然后发送：

```text
Use Local Codex Bridge and list configured projects.
```

健康的响应应该显示你配置的项目 profiles。

## 13. 不修改文件的项目 smoke test

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

## 14. Optional engineering-control workflow

如果团队或 operator 希望采用更严格的 ChatGPT ↔ Codex loop，请参见 [ENGINEERING_CONTROL.md](ENGINEERING_CONTROL.md)。该文档包含 evidence-first review posture、readiness guidance 和 operating checklist；这些内容不再让 README 显得过重。

你不需要采用这套 workflow 才能把 LCB 当作轻量 bridge 使用。如果你使用 controlled mutation tools，它们的 runtime safety gates 仍会执行，与 workflow 风格无关。

## 15. 常见问题

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

## 16. 安全说明

这个 bridge 可以让本地 Codex 修改已配置仓库中的文件。明确使用可选 controlled action tools 时，它还可以创建本地工作分支、commit 并 push 已批准文件、创建 GitHub PR、merge 已批准 PR，以及把本地目标分支同步到 `origin/<target>`。请把它当作强大的本地自动化工具。

建议默认做法：

- 只绑定到 `127.0.0.1`。
- 不要在没有 LCB auth 的情况下公开暴露；tunnel 只是传输层，不是安全边界。
- 只配置你愿意让 ChatGPT / Codex 操作的 repo。
- 验证命令必须 allowlist。
- 在接受变更前审查 staged/unstaged diffs、有边界的 untracked 预览和 verification output。
- 只有在人工明确批准具体 operation、files、message、PR、merge method 或 sync target 后，才使用 mutation tools。
- 只对已审查和已批准文件使用 `git_commit_and_push`。
- 不要在 prompt 中传递 secrets。
- 不要在公开 issue 或 docs 中发布临时 tunnel URL、auth 环境变量或 bearer token。
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
