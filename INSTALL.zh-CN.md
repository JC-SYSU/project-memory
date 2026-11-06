[English](INSTALL.md) | 中文版

# 安装指南（面向执行安装的 agent）

本文是操作规范：按顺序执行，每一步都有「期望输出」与「失败处理」。全部通过后运行文末「验收清单」，并把结果一并报告。**不要跳过任何一步**——本系统的组件相互独立，漏装任何一个都会表现为"装了但静默不工作"（例如 hooks 装好但没配环境变量：入队成功、抽取永远失败）。

仓库根目录记为 `$REPO`。以下命令均在 `$REPO` 下执行。平台：**macOS / Linux / Windows 均可**；Windows 下把下文所有 `python3` 换成 `python`（或 `py -3`），Codex 配置目录同样是 `%USERPROFILE%\.codex`（即 `~/.codex`），唯一例外是第 5 节 launchd 常驻（仅 macOS）。

## 0. 前置检查

| # | 检查项 | 命令 | 期望输出 | 失败处理 |
| ---- | ---- | ---- | ---- | ---- |
| 0.1 | Python ≥ 3.11 | `python3 --version` | 3.11.x 或更高 | 安装对应版本后重来 |
| 0.2 | Codex 已安装 | `codex mcp list` | 命令存在（可能报无 server） | 先装 Codex |
| 0.3 | 用户配置目录 | `ls ~/.codex/` | config.toml 或 hooks.json 存在即可 | 新建亦可，安装脚本会创建 |

### 0.5 先探测客户端，确定安装形态（三选一，决定后续所有步骤）

```bash
command -v codex   # Codex CLI 是否在 PATH
command -v claude  # Claude Code CLI 是否在 PATH
```

| 形态 | 机器上检测到 | 装什么 | 后续步骤 |
| ---- | ---- | ---- | ---- |
| **A** | 检测到 Codex，未检测到 Claude Code | 记忆宫殿本体：依赖 + Codex hooks/MCP 注册 + 环境变量 | 第 1、2、3、4 节；第 5 节可选；跳过 3.5；验收只做 Codex 项 |
| 不可装 | 两者都未检测到 | 本系统由 Codex 或 Claude Code 的 hook 触发，至少先装其中一个客户端 | — |
| **B** | Codex 和 Claude Code 都有 | 本体 + CC 兼容层**全套**（`cc-integration/` 脚本部署、`~/.claude/settings.json` hooks 注入（PreCompact、SessionEnd、SessionStart）、`~/.claude.json` mcpServers 注册） | 第 1–4 节 + 3.5 + 第 5 节可选；验收 Codex 项与 CC 项全做 |
| **C** | 只有 Claude Code | 本体 + CC 兼容层全套，仓库部署到 **CC 家目录** `~/.claude/memory-palace/`，所有路径指向该位置 | 第 1、2、4 节 + 3.5（占位符换成该目录）；不做任何 Codex 注册；第 5 节可选 |

形态 C 的部署位置决定路径（这是与 A/B 唯一的实质差异——脚本内部用相对路径推导宫殿 `src/`，只要保持仓库原布局，放哪个根目录都能工作）：

| 部件 | 形态 C 的路径 |
| ---- | ---- |
| 仓库根 | `~/.claude/memory-palace/` |
| hooks command | `{{PYTHON}} -B ~/.claude/memory-palace/cc-integration/cc_capture_hook_runner.py` |
| MCP command | `~/.claude/memory-palace/cc-integration/cc_memory_palace_recall_wrapper.zsh` |
| 指针目录 | 默认 `~/.claude/memory-palace-sessions`（`CC_MEMORY_MIRROR_ROOT` 可覆盖） |
| 环境变量注入目标 | CC 派生的 drainer 子进程（launchctl setenv / shell rc，同第 4 节） |
| launchd 常驻（可选） | `WorkingDirectory` 指向 `~/.claude/memory-palace`，其余同模板 |

所有形态共用的不变项：环境变量四件套的注入规则（第 4 节）、数据落盘布局（第 6 节）、卸载（第 7 节，形态 C 删除仓库目录即完成）。

## 1. 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

期望输出：`mcp`、`pydantic` 安装成功。
失败处理：检查网络与 pip 源；若目标机器的 Python 环境是虚拟环境，先激活再执行，后续所有 `python3` 命令都用同一个解释器。

## 2. 验证核心包可导入

```bash
PYTHONPATH=$REPO/src python3 -c "import memory_palace.worker_orchestrator, memory_palace.extraction_core, memory_palace.segmenter"
```

期望输出：无错误、无输出。
失败处理：缺依赖则回到第 1 步；报语法错误则仓库损坏，换源重新克隆。

## 3. 注册 Codex 集成（hooks + MCP）

一键安装器 `scripts/install.py`，默认只预览，`--apply` 才写入：

```bash
python3 scripts/install.py            # dry-run：打印将执行的全部动作
python3 scripts/install.py --apply    # 实际写入
```

安装器行为（与手动安装等效，见下文两张表）：

- 向 `~/.codex/hooks.json` **合并**两个事件：`PreCompact`（会话压缩前入队）与 `UserPromptSubmit`（每次提问注入召回路由提示词）。已有同名命令不重复添加，其余 hooks 原样保留。
- 向 `~/.codex/config.toml` **追加** `[mcp_servers.memory_palace_recall]` 段（已存在则跳过）。
- 每次写文件前备份为 `*.bak-<UTC时间戳>`；写后重新解析（JSON/TOML），解析失败自动回滚到备份并终止。

期望输出（`--apply`）：逐条打印 `hooks[...]: registered ...` 与 `config.toml: ... appended ...`。
失败处理：安装器任何一步失败即终止且不写配置；按报错信息处理（多为权限或路径问题）。

### 3.5 Claude Code 集成（形态 B/C 必装；形态 A 跳过）

Claude Code 通过同一记忆宫殿（同一队列、同一 drainer）获得双向接入，宫殿源码零改动。集成 = 三件事：

1. **写路径**：`PreCompact` / `SessionEnd` 两个 hook 事件编译 CC 会话为 Codex 格式 Capture 并入队（`cc-integration/cc_capture_hook_runner.py`，fail-open 静默退出 0）；
2. **读路径**：MCP server 经 wrapper 暴露给 CC（`cc-integration/cc_memory_palace_recall_wrapper.zsh` → `src/memory_palace_recall_mcp_server.py`）。wrapper 必须存在：MCP server 以启动时 cwd 绑定项目库，而 CC 只承诺注入 `CLAUDE_PROJECT_DIR` 不承诺 spawn 的工作目录，wrapper 负责显式 cd。
3. **召回提醒**：`SessionStart` hook（`cc-integration/cc_session_start_reminder.zsh`）在当前项目存在 `.memory-palace/` 库时，注入一句调用 `memory_recall` 的提醒。每个新会话一次；resume 会话和没有库的项目不输出，也不联网。

接入配置见 `templates/claude.example.json`（含注释）：其中 `PreCompact`、`SessionEnd`、`SessionStart` 三段 hook 并入 `~/.claude/settings.json` 的 `"hooks"` 键（同一事件已有条目时追加在旁边，不要覆盖），mcpServers 段并入 `~/.claude.json` 的 `"mcpServers"` 键。两个占位符与第 3 节相同（`{{PYTHON}}`/`{{INSTALL_DIR}}`）。指针文件目录默认 `~/.claude/memory-palace-sessions`，可用环境变量 `CC_MEMORY_MIRROR_ROOT` 覆盖。

验收：CC 新会话中问一句当前项目的历史问题，模型应能调用 `memory_recall`；观察 `~/.claude/memory-palace-sessions/<session-id>.jsonl`（单行 session_meta 指针）随会话出现，即写路径生效。历史会话批量回填工具：`cc-integration/cc_backfill_driver.py`（扫描 `~/.claude/projects/*/*.jsonl`，重跑安全）。CC 侧测试：`PYTHONPATH=../src python3 -m pytest cc-integration/tests/`。

环境变量：CC 集成不新增变量——`PROJECT_MEMORY_*` 四件套同样随环境继承喂给 CC 触发的 drainer 子进程，与第 4 节同一套。

### 手动等效操作（不信任安装器时）

**hooks** — `~/.codex/hooks.json` 合并以下结构（将 `{{PYTHON}}` 换成 0.1 里确定的解释器、`{{INSTALL_DIR}}` 换成 `$REPO`）：

```json
{
  "hooks": {
    "PreCompact": [
      { "matcher": "^(manual|auto)$", "hooks": [
        { "type": "command", "command": "{{PYTHON}} -B {{INSTALL_DIR}}/src/codex_precompact_hook_runner.py", "timeout": 30 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [
        { "type": "command", "command": "{{PYTHON}} -B {{INSTALL_DIR}}/src/codex_recall_prompt_hook_runner.py", "timeout": 5 } ] }
    ]
  }
}
```

**MCP** — `~/.codex/config.toml` 追加：

```toml
[mcp_servers.memory_palace_recall]
command = "{{PYTHON}}"
args = ["-B", "{{INSTALL_DIR}}/src/memory_palace_recall_mcp_server.py"]
startup_timeout_sec = 10.0
tool_timeout_sec = 30.0
```

## 4. 配置环境变量（必做，这一步最常被漏掉）

**推荐：傻瓜式向导** `python3 scripts/setup_env.py`——只需填 URL 和 Key 两样，其余自动完成：

```bash
python3 scripts/setup_env.py
```

- 交互式逐项提问：`BASE_URL` 与 `API_KEY` 必填（Key 输入遮蔽）；`MODEL` 优先探测端点的 `/models` 列表再让你选，探测不到才手输；`REASONING_EFFORT` 默认 `high`，直接回车接受；
- 填写后写入 `~/.zshenv`（`--rc-file` 可改，已有同名行不重复追加）；macOS 上另打印 `launchctl setenv` 命令（供 App 启动的进程继承）；
- 写完后**自动运行连通性与可用性测试**并逐项报告：
  1. 连通与认证（含 401/404/网络不可达的分级建议）；
  2. Chat Completions 协议是否正常；
  3. **强制工具调用**是否被端点接受（不接受则系统无法工作，提示换端点）；
  4. `reasoning_effort` 字段是否被容忍。
  
  测试失败的每一项都自带修复建议，按建议调整后重跑（可单独运行 `python3 scripts/test_connection.py` 复测）。

非交互（agent/自动化）：

```bash
python3 scripts/setup_env.py --set BASE_URL=... --set API_KEY=... \
    [--set MODEL=...] [--set EFFORT=high] [--rc-file <path>]
```

MODEL 未给时会尝试探测并取列表第一个。`--skip-write` 只收集与测试不落盘。

### 手动方式（备选）

四个变量从进程环境读取，**全部必填、无默认值**（`extraction_core.py` 的 `RuntimeConfig.from_env`）；缺失任一，抽取任务直接失败。**变量必须出现在实际运行 worker 的进程里**——worker 由 hook 作为子进程触发，继承 Codex 的启动环境；launchd 常驻时是 launchd 的环境。

| 变量 | 必填 | 填入什么 | 示例 |
| ---- | ---- | ---- | ---- |
| `PROJECT_MEMORY_API_KEY` | 是 | 模型服务密钥 | `sk-...` |
| `PROJECT_MEMORY_BASE_URL` | 是 | chat/completions 协议的 API 根，不含端点路径 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `PROJECT_MEMORY_MODEL` | 是 | 模型标识 | `glm-5.3-flash` |
| `PROJECT_MEMORY_REASONING_EFFORT` | 是 | 推理档位 `low`/`medium`/`high`/`max`，推荐 `high` | `high` |

`BASE_URL` 填法核对：本系统实现的是 chat/completions 协议。填 API 根（请求落到 `{BASE_URL}/chat/completions`）。**错误填法**：填了完整路径（如 `/v1/chat/completions`）会 404；填 `/responses`、`/messages` 等其他协议端点不可用；禁止非 HTTPS。

注入方式（按 Codex/worker 的启动路径选，通常要设多处；**形态 C 只注入 CC 派生链即可**）：

1. Codex 从终端启动：`export` 进终端环境；持久化写 `~/.zshenv`（bash 另写 `~/.bashrc`）。
2. Codex 从 macOS App 启动：`launchctl setenv PROJECT_MEMORY_API_KEY sk-...`（重启 App 生效；`launchctl getenv` 可查）。
3. launchd 常驻 worker：同一 `launchctl setenv`，或在 plist 的 `EnvironmentVariables` 键内写明。

设置完成后验证当前进程环境：

```bash
python3 -c "import os; [print(f'{k} =', 'OK' if os.environ.get(k) else 'MISSING') for k in ('PROJECT_MEMORY_API_KEY','PROJECT_MEMORY_BASE_URL','PROJECT_MEMORY_MODEL','PROJECT_MEMORY_REASONING_EFFORT')]"
```

期望输出：四行全 `OK`。任一 `MISSING` 都不得进入下一步。
安装器的 dry-run 与 `--apply` 都会对当前进程环境做同一检查并打印警告（不阻断——只装 hooks 不跑抽取时可暂时跳过，但验收清单第 3 项会失败）。

### 端点与模型要求

- 协议：HTTPS + `Authorization: Bearer`，请求体为 OpenAI Chat Completions 格式（`model`、`messages`、`reasoning_effort`、`max_tokens` 上限 32768、`tools`、`tool_choice`）。传输层为标准库 `urllib`，无额外 HTTP 依赖。
- **端点必须支持强制工具调用**（`tool_choice` 指定 function，响应从 `tool_calls[0].function.arguments` 取 JSON）。不支持强制的端点、或网关用语法约束/grammar 出结构的模式无法工作。
- 建议模型：`glm-5.3-flash`（生产实测，工具调用 6/6 合法）、`deepseek-v4-flash-0731`（备选）；其他同档模型先实测一次强制工具调用再上线。
- effort 用 `high`；`max` 在部分网关会把配额烧在推理上导致正文空/截断，遇响应损坏先降档。

## 5. （可选）launchd 常驻 worker（仅 macOS）

不配也能用（hook 每任务触发一次瞬态 drainer）。常驻即后台轮询；Windows/Linux 用户若要常驻，用各自的服务管理器（任务计划程序 / systemd）执行同一行命令，进程级文件锁与钩子触发机制是跨平台的。

```bash
sed -e "s|{{PYTHON}}|$(command -v python3)|" \
    -e "s|{{INSTALL_DIR}}|$REPO|" \
    -e "s|{{SESSIONS_ROOT}}|$HOME/.codex/sessions|" \
    templates/launchd.example.plist > ~/Library/LaunchAgents/com.yourname.project-memory-worker.plist
launchctl load -w ~/Library/LaunchAgents/com.yourname.project-memory-worker.plist
```

（Label 建议改成自己的标识。）卸载：`launchctl unload -w <同路径>` 后删文件。模板语义：`RunAtLoad` 启动即跑、`KeepAlive` 退出即拉起、`ThrottleInterval=10` 分钟级重启间隔；worker 单任务制，一次 `run_once()` 只领一个到期任务。

## 6. 数据落盘布局（验收时核对对象）

- 转录（权威证据）：Codex 会话 JSONL，位于 `~/.codex/sessions`，系统只读。
- 项目状态（派生层）：每个启用项目的 `.memory-palace/`，首次入队时由 `ensure_project_state` 创建：
  - `memory-palace.sqlite3` — 单库八张表：`records`、`evidence`、`review_items`、`session_cursor`、`model_usage`、`audit_events`、`window_commit_markers`、`extraction_jobs`；
  - `captures/` — 捕获原文的冻结副本。
- 记忆可重建：删 `.memory-palace/` 只丢检索层，转录不受影响。

## 6.5 首次启动：历史会话扫描与批量入队（建议做）

安装完成后，机器上已有的 Codex / Claude Code 历史会话**不会自动**产生记忆——入队只发生在将来的 hook 事件上。首次使用时先扫描现有会话、统计规模、按建议分批入队。流程固定为三段：**扫描（只读）→ 你确认 → 执行**。

**形态 A / B（Codex 历史）**——`scripts/backfill_codex.py`：

```bash
# 1) 扫描与统计（只读，不写任何东西）：
python3 scripts/backfill_codex.py
#    输出：可入队会话数/体积、按项目分组计数、跳过数与原因、批次安排建议
# 2) 确认方案后执行：
python3 scripts/backfill_codex.py --apply [--batch-size 50]
```

- 入队口径：有 `session_meta` 记录且 cwd 指向仍存在的项目目录 → 可入队；无 meta / 目录已删 → 报告并跳过，永不入队。
- 幂等：每个会话走与 PreCompact hook 完全相同的接单门（`process_payload`），已入队的重跑报 `duplicate` 跳过，可随时中断重跑，不重复。
- 批次策略：按项目分组、组内大文件先入；批内逐个入队并**压制 drainer 唤醒**，批末统一唤醒一次，由 drainer 串行抽取（避免一次 N 个 worker 风暴）。
- 结果写入 `backfill-codex-manifest.csv`（仓库根，已被 .gitignore 拦）；执行完的统计行 `accepted=新入队 / duplicate=已存在 / ignored=被拒 / error=异常`。

**形态 B / C（Claude Code 历史）**——`cc-integration/cc_backfill_driver.py`（同一三段流程）：

```bash
python3 cc-integration/cc_backfill_driver.py --dry-run   # 1) 预演统计，只读
python3 cc-integration/cc_backfill_driver.py             # 2) 确认后正式入队
```

- 口径：扫描 `~/.claude/projects/*/*.jsonl`（可 `--exclude-cwd` 排除特定项目），复用 CC hook 的准入门与幂等，重跑安全。
- 批次策略：整批入队、逐个压唤醒、批末统一唤醒一次；清单断点续跑。

两个客户端的历史可以都跑一遍（形态 B）。抽取本身由 drainer 串行执行，会真实调用模型（消耗配额）——规模大的批次建议在空闲时段跑。

## 6.6 健康检查（MCP 工具）

recall MCP server 还提供两个诊断工具。它们对数据库都是只读的。

**`memory_health`** 扫描 `$HOME` 5 层内所有 `.memory-palace/memory-palace.sqlite3`，返回固定格式的 Markdown 报告：

- 抽取队列状态与合计；
- 审查队列状态与合计；
- 需要处理的库；
- 失败明细；
- 有异常时的简短备注。

它不联网、不写库。直接问「记忆健康检查」「队列状态」「有没有失败」，agent 会调用工具并原样转述报告。

**`memory_upstream_check`** 检查配置的模型端点，必须分两步：

1. 不带参数调用：返回端点、模型、reasoning_effort 和 API Key 是否已配置的预览。**不发送任何请求。**
2. 带 `confirm: true` 调用：运行与 `scripts/test_connection.py` 相同的四项探针，返回通过/失败报告。这会调用上游模型 4 次，可能产生费用；只在用户明确要求时使用。

安装验证仍可使用 CLI：

```bash
python3 scripts/test_connection.py
```

MCP 工具和 CLI 共用同一套探针实现（`src/memory_palace/upstream_probe.py`）。

### 会话工具

**`memory_list_sessions`** 与 **`memory_read_session`** 只读、不调用模型。读取范围是 `~/.codex/sessions`、`~/.codex/archived_sessions`（`.jsonl` 与 `.jsonl.zst`）和 `~/.claude/projects/<编码后的 cwd>`，按文件内记录的 cwd 精确匹配。`memory_read_session` 把干净对话写入 `/tmp/memory-palace-sessions/` 下权限 600 的文件，只返回元数据、路径、估算 token 数；超过 10 万 token 时另给每块约 6 万 token 的分块行号区间。Claude Code 会把超过 2.5 万 token 的 MCP 结果转存到磁盘，Codex 会把工具输出截断在约 1 万 token，所以正文从不经过工具返回值。读取其他项目的会话时传 `project` 参数。同一读取器也有 CLI：

```bash
python3 src/memory_palace_sessions.py list
python3 src/memory_palace_sessions.py read <session_id> --out session.md
```

#### 可选：`continue-session` skill 与会话读取路由 hook

- **Skill** `skills/continue-session/`：说"读一下会话 `<id>`"或"接着会话 `<id>` 继续"（或输入 `/continue-session <id>`）即可触发。它调用 `memory_read_session`；估算超过 10 万 token 的会话先停下问你：完整读入，还是派子代理分块精炼（合计不超过 5 万 token）；随后用 300 字以内复述，并提醒状态可能已过时、旧会话里的授权不延续。把整个目录复制到 skill 目录即可（Claude Code 为 `~/.claude/skills/`，Codex 为 `~/.codex/skills/`，两者可共用一个目录）。
- **Hook** `cc-integration/route_session_read.sh`（Claude Code，需要 `jq`）：拦截读取 `~/.claude/projects` 或 `~/.codex/sessions` 下原始会话的 `Bash`/`Read`/`Grep`/`Glob` 调用，提示 agent 改用 `memory_read_session`。不装它，agent 被要求"读这几个会话"时可能自己去解析原始 JSONL。注册方式见 `templates/claude.settings.hooks.example.json`。在维护宫殿本身的目录工作时，用 `MEMORY_PALACE_DEV_DIRS`（冒号分隔的绝对路径）豁免。

## 验收清单（全部通过才算安装完成）

形态判定见 0.5：**形态 A 做 Codex 项 1–5；形态 B 做 Codex 项 1–5 与 Claude Code 项 C1–C5；形态 C 做 Claude Code 项 C1–C5 与第 2 项（环境变量）**。

| # | 验证内容 | 命令 | 通过标准 |
| ---- | ---- | ---- | ---- |
| 1 | MCP 注册 | `codex mcp list` | 存在 `memory_palace_recall`，状态 connected |
| 2 | 环境变量 | 第 4 节的检查命令 | 四行全 OK |
| 3 | UserPromptSubmit 注入 | 新 Codex 会话，`/hooks` 确认信任后发一条普通消息 | 会话内无报错（runner 静默成功） |
| 4 | PreCompact 入队 | 构造最小转录 + 合法 payload 触发（命令见下） | 退出码 0，`.memory-palace/` 出现 sqlite 与 `captures/` |
| 5 | 抽取链路（选做，消耗配额） | 第 4 步后等 drainer 完成 | `extraction_jobs` 表出现非 `failed` 终态记录 |

第 4 项具体命令：

```bash
mkdir -p /tmp/pm-test-project
printf '{"type":"session_meta","payload":{"id":"test-session-1","cwd":"/tmp/pm-test-project"}}\n' > /tmp/pm-test-session.jsonl
echo '{"session_id":"test","cwd":"/tmp/pm-test-project","hook_event_name":"PreCompact","model":"test","turn_id":"1","trigger":"auto","transcript_path":"/tmp/pm-test-session.jsonl"}' \
  | python3 $REPO/src/codex_precompact_hook_runner.py; echo "exit=$?"
ls /tmp/pm-test-project/.memory-palace/
```

说明：runner 是 fail-open 设计，**成功路径无任何输出，退出码 0 即正常**；`transcript_path` 传 `null` 走 ignored 分支（同样静默退出 0、不建库），可用来确认 runner 进程活着。第 4 项 accepted 会触发一次真实抽取尝试（模型调用）。验证后清理：`rm -rf /tmp/pm-test-project /tmp/pm-test-session.jsonl`。

Claude Code 项（形态 B/C）：

| # | 验证内容 | 命令 | 通过标准 |
| ---- | ---- | ---- | ---- |
| C1 | MCP 注册 | `claude mcp get memory-palace-recall` | `Status: ✔ Connected` |
| C2 | 召回提醒 | `echo '{"source":"startup","cwd":"<有 .memory-palace/ 的项目>"}' \| <INSTALL_DIR>/cc-integration/cc_session_start_reminder.zsh` | 输出一行以「本项目配有记忆宫殿库」开头；把 `source` 换成 `"resume"` 时无输出 |
| C3 | 会话内召回 | 在有历史的项目新开 CC 会话，问一个过往决定 | 模型调用 `memory_recall` |
| C4 | 写路径 | 结束这个 CC 会话（或执行 `/compact`） | 出现 `~/.claude/memory-palace-sessions/<session-id>.jsonl`（单行 `session_meta`），该项目的 `extraction_jobs` 多一条记录 |
| C5 | CC 侧测试 | `PYTHONPATH=../src python3 -m pytest cc-integration/tests/` | 全绿 |

写路径准入门会按设计拒绝三类会话：cwd 在临时目录下的（前缀 `/tmp`、`/var` 以及 macOS 的 `/private`），Agent SDK 启动的，以及没有可见对话的。C4 请在普通项目目录下做。

## 7. 卸载

1. 从 `~/.codex/hooks.json` 删除 `PreCompact`（matcher `^(manual|auto)$`）与 `UserPromptSubmit` 下 command 指向本仓库的 entry；
2. 从 `~/.codex/config.toml` 删除 `[mcp_servers.memory_palace_recall]` 段；
3. 若有 launchd 常驻：`launchctl unload -w <plist>` 并删除 plist；
4. Claude Code（形态 B/C）：从 `~/.claude/settings.json` 删除运行 `cc_capture_hook_runner.py` 的 `PreCompact`、`SessionEnd` 条目，以及运行 `cc_session_start_reminder.zsh` 的 `SessionStart` 条目；从 `~/.claude.json` 删除 `mcpServers.memory-palace-recall`（或执行 `claude mcp remove memory-palace-recall -s user`）；指针目录 `~/.claude/memory-palace-sessions/` 可一并删除；
5. 可选组件：从 `~/.claude/settings.json` 删除 `route_session_read.sh` 的 PreToolUse 条目，并从 skill 目录删除 `continue-session` 文件夹；
6. 删除仓库即可——项目 `.memory-palace/` 与转录都不在仓库内。

## 8. 故障排查

| 现象 | 原因与处理 |
| ---- | ---- |
| hooks 不生效 | 新 hooks 未被信任：Codex 会话内 `/hooks` 确认 |
| `codex mcp list` 里 memory_palace_recall 报错 | `command` 解释器与 Codex 运行环境不一致（venv 未激活等），手动编辑 config.toml 修正；`startup_timeout_sec` 默认 10s 不够则调大 |
| MCP 工具能列出但调用超时 | `tool_timeout_sec` 默认 30s，大库召回按需调大 |
| 入队成功、抽取一直失败 | 最常见原因：环境变量只设在终端、未进入 Codex/launchd 进程环境。对照第 4 节注入方式逐路检查 |
| `pip install` 失败 | pip 源不可达；确认 `mcp`/`pydantic`（Python < 3.14 时还有 `zstandard`）可从目标源安装 |
| 安装脚本解析失败 | 已自动回滚备份；手工对比 `.bak-*` 恢复 |
| 请求 404 | `BASE_URL` 填了完整端点路径（多拼了一次 `/chat/completions`），回到第 4 节核对 |
| 响应空正文/JSON 损坏 | effort 档位过高烧穿配额：降档到 `high`/`medium` 重试 |
| CC：始终看不到 `memory_recall` | `claude mcp get memory-palace-recall` 显示未连接：wrapper 没有执行权限（`chmod +x`），或它用的 python 缺 `mcp`；resume 的会话沿用启动时的工具列表，修好后要新开会话 |
| CC：会话结束后没有指针文件、没有任务 | 会话被准入门拒绝（cwd 在临时目录、Agent SDK 启动、没有可见对话），或 hook 里的 `{{PYTHON}}` 路径不对；用测试 payload 手动运行 hook 命令看退出码 |
| CC：新会话开头没有召回提醒 | resume 会话和没有 `.memory-palace/` 的项目本来就不提醒；其他情况检查 `SessionStart` 条目和脚本执行权限 |
| agent 不用 `memory_read_session`，自己去读原始 `.jsonl` | 安装可选的 `route_session_read.sh` hook（Claude Code）。Codex 侧暂无同类拦截 |