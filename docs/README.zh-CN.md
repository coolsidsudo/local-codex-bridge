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
  -> 可以在修改前创建受控的本地工作分支
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
- `start_codex_task` — 在已配置项目中启动 `codex exec`。可选的 `review_contract` 会追加 bridge 自有 guidance，要求 Codex 返回简洁实现摘要，而不是完整 diff 或完整文件内容。
- `get_task` — 读取任务元数据和 stdout / stderr 尾部。
- `list_tasks` — 列出最近的 bridge 任务记录。
- `abort_task` — 终止正在运行的本地 Codex 进程。
- `get_review_package` — 返回紧凑、只读的变更文件索引和 status/stat 证据，不包含完整 diff 或完整文件内容。
- `get_changed_file_diff` — 在审查 package index 之后，返回某个 changed/staged/untracked 文件的有边界 targeted diff。
- `get_changed_file_text` — 在 targeted diff 审查之后，返回单个 changed/staged/untracked 文件的有边界 UTF-8 文本内容。
- `get_git_diff` — 检查 git status、unstaged/staged diffs，以及有边界的 untracked 文件预览。
- `git_get_branch_status` — 返回当前分支、dirty 状态、HEAD、remotes、upstream 和 ahead/behind 证据。
- `git_create_work_branch` — 基于已有本地 base 分支创建并切换到新的本地工作分支。
- `get_acceptance_readiness` — 只读预检当前 repo 状态是否看起来可以执行人工批准的 `git_commit_and_push`。
- `run_verification` — 运行项目配置中 allowlist 的验证命令。
- `run_verification_bundle` — 按顺序运行多个已配置的验证 key，并返回有边界的逐命令证据。
- `git_commit_and_push` — 在人工批准后，stage 已批准文件、创建一个 commit，并 push 到 `origin` 上的当前分支。
- `github_create_pr` — 通过已安装的 `gh` CLI，为已经 push 的当前分支创建 GitHub pull request。
- `github_get_pr_status` — 通过已安装的 `gh` CLI 读取 GitHub pull request 状态、证据和保守 advisory 的 PR-only readiness 证据。
- `github_merge_pr` — 在人工批准后，通过固定 `gh pr merge` argv merge 单个 ready 的 GitHub pull request。
- `get_pr_sync_readiness` — 只读报告把 PR readiness 与本地目标分支 sync readiness 结合后的 advisory 证据。
- `git_sync_local_branch_to_origin` — 审查后把干净的本地目标分支同步到本地 `origin/<target>` ref；不 fetch、pull、push、merge 或修改 PR。

v0 不暴露任意 shell 执行。验证命令必须在每个项目 profile 中显式 allowlist。`git_create_work_branch` 和 `git_commit_and_push` 是 bridge 自有的 Git 操作，不是通用 shell 或通用文件系统工具。

GitHub PR 工具把 `gh` 作为外部 substrate。Local Codex Bridge 不实现原生 GitHub API / token 处理，也不存储、打印或管理 GitHub token。

`start_codex_task` 的 review contract 只是行为 guidance，不是安全边界。ChatGPT 和人工 reviewer 应通过 Local Codex Bridge 工具检查真实仓库状态、验证证据和 readiness 证据，而不是信任 Codex summary、粘贴的 diff 或粘贴的文件内容。

## 受控分支工作流

`git_create_work_branch` 用于在 Codex 开始修改前，把干净的已配置 repo 移到安全的 feature / work 分支。它的安全措施包括：

- 只接受本地分支名，不把 `main` 硬编码为通用 base。
- 如果省略 `base_branch`，使用当前 checkout 的分支。
- `base_branch` 必须是已存在的本地分支；拒绝 `origin/main` 这类 remote-style base、`refs/heads/main` 这类完整 ref，以及 `HEAD`。
- 目标分支不能已经存在；切换已有分支暂不支持。
- worktree 必须按 `git status --porcelain=v1 --untracked-files=normal` 判断为干净。
- 拒绝 detached HEAD。
- 分支名必须通过 Local Codex Bridge 的保守校验和 `git check-ref-format --branch`。
- 它会在本地创建并 checkout 新分支，但不会 push、merge、删除分支、创建 PR，或触碰 tags。

## 受控 acceptance 流程

预期的 acceptance 工作流是：

```text
ChatGPT 规划 / 审查
  -> 本地 Codex CLI 修改已配置 repo
  -> ChatGPT 审查 package index、targeted diffs 和验证输出
  -> ChatGPT 对已批准文件集合做只读 acceptance readiness 预检
  -> 人工接受
  -> Local Codex Bridge 执行受控 git add/commit/push
```

只有在人工已经审查来自 `get_git_diff` 和 `run_verification` 或 `run_verification_bundle` 的精确 diff 和验证证据后，才应调用 `git_commit_and_push`。`get_acceptance_readiness` 是针对已批准文件集合的只读预检：它报告当前 branch/HEAD/remotes、staged/unstaged/untracked 文件、approved-file 覆盖情况、可用的 origin/upstream 证据，以及 `git_commit_and_push` 是否可能被阻止。它不会 stage、commit、push、fetch、修改分支、创建 PR，或触碰 tags/releases。`run_verification_bundle` 只接受已配置的验证 key，按顺序运行其固定 allowlist argv 数组并使用 `shell=False`，返回有边界的逐命令 stdout/stderr 证据；bundle 编排本身不会增加 Git/GitHub/PR/tag/release 修改权限，但实际副作用取决于被配置的 allowlist 命令，因此如果 operator 想要只读验证语义，应把验证 key 配置为只读命令。作为第一步审查索引，`get_review_package` 会返回 branch/HEAD/remotes、status/stat 证据、变更文件分类和有边界的 untracked 预览元数据，但不返回完整 diff 或完整文件内容。`get_changed_file_diff` 是只读 follow-up，用于单个 changed/staged/untracked 路径；它使用 targeted fixed git commands，拒绝 unsafe path 和 binary content，并限制输出大小。`get_changed_file_text` 是进一步的只读 follow-up，用于单个当前 changed/staged/untracked 文件的有边界 UTF-8 内容；它会拒绝 unchanged path、deleted/no-content path、binary/invalid UTF-8、unsafe path、目录、symlink 和非普通文件。`get_git_diff` 会区分 unstaged 和 staged 变更，并在安全时包含有边界的 untracked 文本预览。它的安全措施包括：

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

## 受控 GitHub PR 工作流

`github_create_pr` 用于 push 之后的审查步骤。只有当已配置 repo 的 `origin` remote 位于 `github.com`、当前分支是普通本地分支、worktree 干净，并且当前分支已经在 `origin` 上且 SHA 与本地 `HEAD` 完全一致时，它才会创建 pull request。

安全措施包括：

- PR 操作只使用固定的 `git` 和 `gh` argv，并使用 `shell=False`；不提供任意 `gh` 透传。
- `gh --version` 和 `gh auth status -h github.com` 必须成功。
- 支持常见公开 GitHub HTTPS 和 SSH `github.com/OWNER/REPO` remote 形式。
- 如果省略 `base_branch`，bridge 通过 `gh repo view` 读取仓库默认分支；不会硬编码 `main`。
- 显式 `base_branch` 会经过保守校验，并且必须存在于 `origin`。
- 如果当前分支是 GitHub 默认分支，或当前分支等于选定 base 分支，则拒绝创建 PR。
- 未发布分支和 remote SHA 不匹配会被拒绝；C2 不增加 push-upstream 权限。
- 如果当前分支已有 open PR，则返回已有 PR 证据，不创建重复 PR。
- 新 PR 默认是 draft；允许创建非 draft PR。

`github_get_pr_status` 包含紧凑的规范化 `pr_readiness` 区块，提供保守 advisory 的 PR-only 证据，例如 draft/open 状态、mergeability、review decision、checks、本地 branch/HEAD 是否匹配，以及本地 dirty 状态。`ready_to_consider_merge` 不是 GitHub 的权威 mergeability，也不是保证；checks missing/unknown、缺少 review decision、本地分支不匹配或本地 HEAD 不匹配，都可能让 readiness 为 false，即使 GitHub 允许人工 merge。它不包含目标分支 sync readiness，也不返回建议 operator commands。

`github_merge_pr` 是一个窄执行工具，用于通过 `gh pr merge` merge 单个已经人工批准的 PR。它会在 merge 前立即收集新的 PR 证据，默认使用 squash merge，也支持 GitHub 的 merge 和 rebase 方式，始终使用 `--match-head-commit <fresh_head_sha>`；只有在显式设置 `delete_branch: true` 时才会传入 `--delete-branch`。这个删除标志只表示 gh 的 PR head branch 删除行为，不是任意本地或远端分支清理。E3 故意严格：它要求 PR open、非 draft、目标为 `main`、有完整 PR head SHA、review decision 为 `APPROVED`、checks 通过、mergeability 为 `MERGEABLE` / `CLEAN`、本地状态 clean 且非 detached，并且本地 branch/HEAD 匹配 PR head。通常应在已经审查过的 PR head branch 上运行。它可能会阻止 GitHub 手动界面允许 merge 的 PR，尤其是在 `reviewDecision` 缺失或未知时。它不会 fetch、pull、reset、switch、本地 sync、auto-merge、admin-bypass、push refs，或触碰 tags/releases；GitHub branch protection 仍然是权威。

`get_pr_sync_readiness` 是人工 PR / acceptance 尾部流程的只读 follow-up。它把 `github_get_pr_status` 的 PR readiness 证据和本地 git 证据合并，报告 PR 是否看起来可供人工/operator 考虑 merge，以及本地目标分支（默认 `main`）是否基于本地 refs 看起来可以同步到 `origin/<target>`。其合并后的 `ready_to_consider_merge` 仍是保守 advisory 证据，不是 GitHub 的权威 mergeability，也不是保证。它不会 merge、auto-merge、修改 PR、fetch、reset、switch、pull、push、删除分支，或触碰 tags/releases。返回的 operator commands（如果有）只是建议文本，bridge 不会执行它们。

`git_sync_local_branch_to_origin` 是 post-merge 本地 sync 尾部流程的窄执行工具。它只使用本地 refs，拒绝 dirty、detached、ahead 或 diverged 状态；当目标分支已经等于 `origin/<target>` 时返回 `ok_noop` 且不切换分支；只有在所有 gate 通过后的 behind 状态下，才可以运行固定 argv：`git switch <target>` 和 `git reset --hard origin/<target>`。它不会 fetch、pull、push、merge、修改 PR、删除分支，或触碰 tags/releases。

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

## 14. 日常使用检查清单

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
9. 启动有边界的本地 Codex task；建议使用 `review_contract: true` 获取简洁、面向审查的输出。
10. 审查 stdout/stderr、`get_review_package`、按需查看 targeted `get_changed_file_diff` / `get_changed_file_text` 证据，以及 verification output。
11. 如果变更不可接受，请让 Codex 修改或停止。
12. 如果变更可接受，明确批准精确文件列表和 commit message。
13. 只有在人工批准后才调用 git_commit_and_push。
14. 确认返回的 branch、remote、commit、push output 和 final status。
15. PR 创建并审查后，使用 get_pr_sync_readiness 或 github_get_pr_status 获取保守 advisory 的 PR merge-consideration 证据。
16. 明确人工批准后，只有在严格 fresh gate 通过且位于已审查 PR head branch 时，才调用 github_merge_pr。
17. PR 已 merge 且本地 refs 已经是最新之后，只有在希望 bridge 执行基于本地 refs 的窄本地同步时，才调用 git_sync_local_branch_to_origin。
```

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

这个 bridge 可以让本地 Codex 修改已配置仓库中的文件，并且在明确人工批准后可以执行受控 acceptance commit/push。请把它当作强大的本地自动化工具。

建议默认做法：

- 只绑定到 `127.0.0.1`。
- 不要在没有 LCB auth 的情况下公开暴露；tunnel 只是传输层，不是安全边界。
- 只配置你愿意让 ChatGPT / Codex 操作的 repo。
- 验证命令必须 allowlist。
- 在接受变更前审查 staged/unstaged diffs、有边界的 untracked 预览和 verification output。
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
