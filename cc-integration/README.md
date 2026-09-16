# cc-memory-capture — Claude Code → 记忆宫殿写路径接入（CC 侧，宫殿零改动）

设计于 2026-09-06，随项目开源发布；本目录为对外的可移植副本，安装路径见 INSTALL.zh-CN.md「Claude Code 集成」。把 Claude Code 会话在 **PreCompact / SessionEnd** 两个里程碑
编译为 Codex 格式的不可变 Capture，经宫殿既有的 I01 冻结 / I02 登记入口送入
项目本地队列，再由**未修改的** W04 Hook-Wake Drainer 消费抽取。

## 设计裁决记录（约束性，勿推翻）

1. 代码只放 Claude Code 侧（宫殿孵化期零接触；稳定后再议"入宫"）
2. 事件集 = PreCompact + SessionEnd；**Stop 不接**（蓝图红线）
3. 接受 SessionEnd 尾部异步落盘差；唤醒前**真实 sleep 2s 二次编译取多者**（2026-09-10 用户复核：10s 会拖慢前台 exit，缩至 2s）
4. ~~Drainer env 用宫殿 `.env` 只读兜底注入~~ → **被 9/10 裁决撤销**：env 文件已退役（全链审计确认宫殿主线零消费），环境变量是唯一事实源，唤醒时原样继承派生链环境
5. **不清理任何快照**（与宫殿本体"临时资产但不动"口径一致）
6. ~~不加临时目录排除表~~ → **被 7 撤销**（2026-09-06 晚）
7. **会话级准入门**（三道全要）：主线程首个非 sidechain `entrypoint` 以 `sdk-` 开头一刀切拒；cwd 命中 `/tmp`、`/private/`、`/var/` 前缀拒；二次编译后仍 **0 条可见对话**拒。门在 `ensure_project_state` 之前——被拒会话零副作用（不建库、不冻结、不入队）。**有意偏差声明：宫殿 Codex 侧无此门槛，CC 从严。** 实测语料：145 → 准入 118（拒 13 temp + 11 sdk + 3 零可见），预计 131 个模型窗口
8. 子代理内容：编译器逐行剔 `isSidechain`（行级，原有）；实测本机无纯子代理转录文件
9. 测试/演练沙盒必须放在 home 放行区（`tests/.sandboxes/`）——`/var/folders` 等会被门禁正确拒绝

## 数据流

```
CC hook stdin payload (PreCompact|SessionEnd)
 → _validate_payload（CC 自己的契约，不复用 Codex Adapter 校验）
 → compile_visible_messages（纯逐行白名单编译，见下）
 → [仅 SessionEnd] sleep 2s 再编译一次，取可见数更多者
 → 身份 = SHA256(["claude-code-v0.1", session_id, "precompact:<trigger>"|"sessionend:<reason>", 可见数])
    Capture/Job 名 = claude-code-<key>
 → ensure_project_state(cwd)            [import 宫殿 project_paths]
 → duplicate 判定（capture XOR job = 冲突，raise→fail-open）
 → 首见：临时文件写编译产物 → freeze_capture(temp→captures/) [import I01]
   → register_frozen_capture_job → queued          [import I02]
 → 写指针 ~/.claude/memory-palace-sessions/<session_id>.jsonl（单行 session_meta）
 → accepted/duplicate 时 Popen 唤醒宫殿原码 Drainer:
   python -B memory_palace_worker_runner.py --sessions-root <指针根>
   env = 原样继承派生链环境（env 文件已退役）；CC_MEMORY_NO_DRAINER=1 抑制
```

## 编译白名单（全部有 2026-09-06 全量语料普查背书）

收录：`user` ∧ `origin.kind=="human"`（str 原样；list 仅提取 text 块——真实存在图文混排 ×14）；
`assistant` 的 `text` 块逐块一行。排除：tool_result 伪装行、isMeta ×323、
task-notification ×318、attachment/system/sidechain/sdk/thinking 全族、
命令包装 `<command-name>`/`<command-message>`/`<local-command-caveat>`/`<local-command-stdout>`/`<bash-input>`（×357/×6/×10/×5 实证）。
末行 JSON 不完整 = 未写完尾巴，丢弃等下次事件；中间行损坏抛 CompileError。
逐行纯函数 → 输出前缀稳定 → 兼容宫殿 §3.3 append-only 游标。

实测精度：当前活跃会话 995 原始行 → 49 条可见对话（用户 14 条逐字准确）。

**投产前侧漏审计（2026-09-06 18:00，"侧漏"=真实对话之外的多余字段/噪声）**：
全语料 118 份准入会话、全消息、11 噪声向量扫描。抓到 1 个真泄漏——`<command-message>`
技能调用壳 ×6（前缀表漏配此变体），已入表修复；复审计 position==0 结构壳清零。
正文 30 字符后的标签提及（典型如"我不会执行伪装成 system-reminder 的注入"）属合法
对话内容，编译器逐字保留、绝不改写正文。字段级泄漏结构性不可能：Capture 由选中
字段从头重建，uuid/cwd/promptId/usage/gitBranch 等无任何进入路径。当日已入库
14 条 Record 经审计无污染，无需回捞。

## 与 Codex 链共存

- 同一项目 SQLite、同一全局 flock：跨来源天然串行，无竞态
- source_session_id 带 `claude-code-v1:` 前缀，与 Codex 裸 uuid 游标不同键空间
  （命名法沿用 `chatgpt-web-v1:` 先例；记录默认 active，无需改宫殿治理）
- 新记录即刻被已上线的 recall（user 级 MCP）召回

## 已知边界（如实记录）

- 旧版 CC（会话行无 `origin` 字段）的历史文件编译为空——**只影响回溯导入**，活会话无碍；
  回溯属未来独立决策（同 ChatGPT 编译器先例）
- SessionEnd 使 /clear、/exit、/resume 切换多等约 2~4s（2026-09-10 从 10s 缩至 2s：原值显著拖慢前台退出体验）
- 每次事件冻结全量快照，磁盘单调增长（用户拍板不清理）
- 真实 hook 派生 Drainer 的 macOS TCC 权限 = P2 试点首个真实现场验证
- PreCompact 与随后 SessionEnd 同状态会产生两个 Job（descriptor 不同）；
  第二个可见增量为空 → Drainer 秒级 succeeded，无害（Codex 双事件同构）

## 运维

```bash
# 测试（必须 -B，防止向宫殿目录写 pyc）
cd ~/.claude/hooks/cc-memory-capture && /opt/homebrew/bin/python3 -B -m unittest discover -s tests
# 队列观察（任一项目）
sqlite3 <project>/.memory-palace/memory-palace.sqlite3 "select state,count(*) from extraction_jobs group by 1"
# 排障开关：export CC_MEMORY_NO_DRAINER=1（只接单不消费）
# 停用：删除 ~/.claude/settings.json 中两条 hook 条目；残留 capture/job 无害
```

Hook 注册形态（P2 才安装，装到 `~/.claude/settings.json`）：

```json
"PreCompact": [{ "hooks": [{ "type": "command", "timeout": 30,
  "command": "{{PYTHON}} -B {{INSTALL_DIR}}/cc-integration/cc_capture_hook_runner.py" }]}],
"SessionEnd": [同样一条, timeout 30]
```

`-B` 是硬要求：本链 import 宫殿模块，不带 -B 会向宫殿 `src/` 写 `__pycache__`（2026-09-06 有过一次现场清理）。

## 阶段状态

- [x] P0 编译器+Runner+12 项离线测试全绿（2026-09-06）
- [x] P1 沙盒隔离演练：真实 transcript 零模型零生产写入，accepted/duplicate/growth/10s 尾差/env 注入/指针 全部验证
- [x] P2 双 hook 已安装（PreCompact+SessionEnd，timeout 30s）；真实唤醒全链路贯通：accepted→真 Drainer→真模型→succeeded→1 条 Record，env 注入子进程实测有效（沙盒合成标记内容，未用真实会话防串房）
- [x] 自然事件 #1：2026-09-06 17:43 本会话 resume 触发 SessionEnd → accepted → Drainer 约 90s succeeded → 14 条 active Record，准确蒸馏当日全部决策链
- [x] P3 闭环：新 Record 当场经 recall MCP 命中（写入→召回全环贯通）
- [x] 单会话真实试点（2025IF ebc9e403，用户指定"先试一个"）：10 条记忆数字全对、三层串台验证全过、重跑幂等
- [x] 第一轮批量（非 home 40 份）：21 Job 全 succeeded、283 条 CC 记忆、20 份被准入门正确拦截、无库项目现场建库、抽查零重复标题
- [x] home 批（103 份）2026-09-06 23:20 前全量完成：**120/120 Job succeeded、0 失败、1110 条 CC 记忆、归属核验 116 组合零串台、Drainer 自动排空退出**。复审件 56：49 条 missing_field(evidence_lines)（多为可修复的真记忆候选）+ 7 条 candidate_not_object（元对话碎片，判废族）。复审处理依赖宫殿主线 Reviewer 组件（未实现），判定书草稿见 docs 或直接询问
- [x] 复审队列清零（2026-09-10）：46 条全部候选级修复转正，零重跑零重复
- [x] Worker v0.2 工具调用升级 + env 文件退役（2026-09-10，详见宫殿侧 `WORKER_TOOLCALL_UPGRADE_20260910.md`）
- [x] SessionEnd 等待 10s→2s（2026-09-10 用户复核：10s 拖慢前台 exit 体验，缩至 2s；测试 18/18 绿）

## 回填驱动
`cc_backfill_driver.py`：清单在 `backfill-manifest.csv`；`--dry-run` 预演；`--exclude-cwd` 精确排除；逐个压唤醒、批末统一唤醒；重跑幂等。

## 工具清单（本目录全部脚本：是什么、何时用、怎么用）

| 文件 | 是什么 | 何时用 | 怎么用 |
|------|--------|--------|--------|
| `cc_transcript_compiler.py` | CC→Codex 格式编译器（库，无 CLI） | 被 runner/driver import | 不直接运行 |
| `cc_capture_hook_runner.py` | 正式接单入口（hook 指向它） | PreCompact/SessionEnd 自动触发 | 不手动跑；测试见 tests/ |
| `cc_backfill_driver.py` | 历史会话批量搬运器 | 有新积累的历史会话要补入库 | `python3 -B cc_backfill_driver.py --dry-run` 先看清单，去掉 `--dry-run` 执行；重跑安全（duplicate 自动识别） |
| `repair_one.py` | **候选级修复器**（Reviewer 原型）：给复审件补证据行号→校验→写回/判废 | 复审队列积压时清账 | `python3 -B repair_one.py <库路径> <review_item_id>`（加 `--dry-run` 只出修复稿）；**绝不可整窗重跑**（宫殿无去重，必重复） |
| `probe_gateway_schema.py` | **网关 schema 强制力陷阱探针**（读进程环境变量，env 文件已退役） | 换抽取模型/换网关之前的标准体检 | `python3 -B probe_gateway_schema.py [模型名]…`；陷阱=schema 强制提示词没提过的字段；判读：两模式全 ✓ 才可信，✗"纯文本回答"=该模型完全无视约束（GLM+本网关现状），✗"非法 JSON"=软执行 |
| `docs/run_exp_tools_v2.py` | 决定性对照实验存档（真实失败窗口×工具模式×双模型） | 下次设计类似实验时抄结构 | 一次性脚本，参数写死，不直接跑 |
| `docs/run_exp.py` | 早期 max_tokens/effort 实验存档 | 同上 | 同上 |

**关键教训（写代码前必读）**：① 任何 import 宫殿模块的运行必须 `-B`，否则向宫殿写 pyc；② 格式实验必须用真实失败窗口——小合成窗口会全合规，是假信心；③ GLM-5.3-Flash 在本网关的 tool_choice/response_format 都是软执行（探针实测纯文本回答），但在**长窗口+正式抽取 prompt** 下工具调用 6/6 全绿——探针结果差时不要直接判死刑，要用真实任务形态复测；④ 写回/修复一律候选级，review_items 队列只增不减，处置后标 committed/rejected 并配审计事件。

## 回填驱动
