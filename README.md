# Errorbook MCP

[![CI](https://github.com/stardustlil/errorbook-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/stardustlil/errorbook-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Local-first error notebook infrastructure for MCP agents, with FSRS scheduling and printable math PDFs.

Errorbook MCP 是一个可直接接入支持 Model Context Protocol (MCP) 的 agent 的错题管理服务。图片识别由 agent 自己的视觉能力完成；MCP 负责接收结构化题干、稳定编号、去重、复习调度、优先级维护，以及生成带数学公式的打印版 PDF。

## 核心设计

- 题目使用不可变编号（例如 `EB-2026-000001`），编辑不会换号。
- 复习间隔由版本锁定的 FSRS 算法维护；重复答错会缩短间隔并增加错题权重。
- 用户手动提高优先级会记录为独立事件，并随时间衰减，不会让旧题永久占据队首。
- 所有关键写操作支持幂等键；agent 重试不会重复录题或重复记一次作答。
- 每份 PDF 使用题目快照。之后修改题目不会改变已经生成的试卷。
- SQLite 默认启用 WAL、外键和写事务，适合本地单用户 agent 并发调用。

## MCP 能力

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| Prompt | `record_from_image` | 约束宿主视觉模型忠实转写图片，模糊内容先确认，公式输出为 Markdown + LaTeX |
| Tool | `create_problem` | 校验、去重、记录单题并分配固定编号 |
| Tool | `get_problem` | 按编号读取题目，可选完整复习和优先级历史 |
| Tool | `search_problems` | 按文本、编号、学科、标签、题型、状态和到期时间组合查询 |
| Tool | `update_problem` | 使用 `expected_version` 乐观锁修正 OCR 或题目内容 |
| Tool | `record_review` | 追加真实作答结果并更新 FSRS |
| Tool | `adjust_priority` | 记录用户明确提出的临时优先级调整 |
| Tool | `set_problem_status` | 在 active、mastered、archived 之间切换，不硬删除历史 |
| Tool | `create_review_sheet` | 冻结题目快照并生成仅含题目的 A4 错题复习卷 |
| Tool | `get_export_status` | 查询导出状态、路径、资源 URI、大小和 SHA-256 |
| Tool | `list_exports` | 列出历史 PDF 文件及其存在状态 |
| Tool | `delete_export` | 删除指定 PDF 导出，不影响题目调度 |
| Tool | `get_library_stats` | 查看题库、到期积压和学科分布 |
| Resource | `errorbook://exports/{export_id}/questions` | 读取 questions PDF 二进制内容 |

所有有副作用的操作都接收 `idempotency_key`。建议 agent 使用“用户会话/动作类型/随机 UUID”组成的 8-128 字符键，并在网络重试时保持不变。

## 记忆与选卷算法

系统明确分开两个问题：FSRS 决定“下一次何时复习”，队列评分决定“本周容量有限时先打印哪些题”。人工优先级不会篡改 FSRS 状态。

复习结果映射如下：

| 实际结果 | FSRS rating | 行为 |
| --- | --- | --- |
| 做错、空白、放弃 | Again | 缩短间隔，错误压力增加 1 |
| 部分正确、提示后正确 | Hard | 较短间隔，错误压力增加 0.5 |
| 独立正确 | Good | 正常增长稳定度，错误压力减半 |
| 明确轻松且无提示 | Easy | 更长间隔，错误压力降为四分之一 |
| 跳过 | 不评分 | 只留审计事件，不改变卡片状态 |

默认 `desired_retention=0.90`，使用依赖锁定的 FSRS 6.x 官方实现。普通“答对”必须记为 Good，不能由 agent 擅自升级为 Easy。mastered 题再次做错或部分正确会自动恢复为 active。

近期错误压力按 42 天半衰期衰减并在 6 封顶；人工 boost 按 14 天半衰期衰减并在正负 5 封顶。两者进入评分前都经过 `1 - exp(-mass)` 饱和，历史错误次数不会线性累加并永久霸榜。

周卷基础分为：

```text
score = 42% 到期紧迫度
      + 22% 近期错误压力
      + 12% FSRS 难度
      + 10% 人工 boost
      +  8% 用户长期 importance
```

到期紧迫度在到期点为 0.5，随逾期连续上升并趋近 1；未来到期则连续下降。默认候选仅包含 active 且在未来七天内到期的题，或仍有有效人工 boost 的题。已到期且创建超过 28 天的题进入 `fairness_must_include` 必选桶，防止大题库中长期饥饿。PDF 导出是只读查阅操作，不会改变 FSRS、队列优先级或题目数据。返回结果会报告 backlog、必选溢出和按当前卷容量估算的清空周数。

题库过载时算法不能凭空消除工作量：如果每周新增/到期数量长期高于 `max_questions`，agent 应把返回的 backlog 明确告诉用户，而不是悄悄丢题。

## 安装

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和 Chromium 系浏览器。Windows 会自动寻找 Microsoft Edge。

```powershell
git clone https://github.com/stardustlil/errorbook-mcp.git
Set-Location errorbook-mcp
uv sync --locked
uv run errorbook-mcp
```

环境变量不会自动读取 `.env`；请在 MCP 客户端配置中直接设置。最小配置示例：

```json
{
  "mcpServers": {
    "errorbook": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\errorbook", "run", "errorbook-mcp"],
      "env": {
        "ERRORBOOK_DATA_DIR": "C:\\path\\to\\errorbook-data",
        "ERRORBOOK_TIMEZONE": "Asia/Tokyo"
      }
    }
  }
}
```

### Hermes Agent

Hermes Agent v0.18+ 可以通过 stdio 直接加载全部工具。先完成上面的 `uv sync --locked`，再执行：

```powershell
hermes mcp add errorbook `
  --command ".venv\Scripts\python.exe" `
  --connect-timeout 30 `
  --env "ERRORBOOK_DATA_DIR=$env:LOCALAPPDATA\errorbook-data" "ERRORBOOK_TIMEZONE=Asia/Tokyo" `
  --args -m errorbook_mcp
```

当 Hermes 询问是否启用全部工具时直接回车，然后验证：

```powershell
hermes mcp test errorbook
hermes mcp list
```

请启动一个新的 Hermes 会话使配置生效。图片 OCR 由 Hermes 当前选择的视觉模型完成，因此该模型必须支持图片输入。

也可以直接运行模块：

```powershell
uv run python -m errorbook_mcp
```

## Agent 录题流程

1. 用户把错题图片上传给具备视觉能力的 agent。
2. agent 使用 MCP prompt `record_from_image` 的规则，将图片忠实转写成 Markdown；数学公式使用 `$...$` 或 `$$...$$` LaTeX。
3. agent 调用 `create_problem`。OCR 不清楚的内容不能猜，应先向用户确认。
4. MCP 校验题型和选项，检查重复内容，并分配固定编号。

一个题目只保存规范化后的文本；原始图片仍由宿主应用管理。这样不需要在 MCP 中配置第二套视觉模型或上传凭据。

## 复习与出卷

- 做错后调用 `record_review(outcome="incorrect")`；这既保留历史，也会通过 FSRS 自动提前下次复习。
- 用户明确说“提高 EB-2026-000123 的优先级”时调用 `adjust_priority`。不要用优先级工具伪造一次作答。
- 每周调用 `create_review_sheet`，默认选择已经到期和未来七天内到期的题目，并生成仅含题目的错题复习卷。
- 工具返回本地绝对路径、SHA-256 和 `errorbook://exports/...` MCP resource URI。

PDF 将原始 Markdown 转为静态 MathML，再由本机 Edge/Chrome/Chromium 打印，不执行 JavaScript 或 TeX 程序。原始 HTML、外部图片、链接和危险 LaTeX 命令会被拒绝。资源读取时会重新核对 SHA-256；文件被替换或损坏时不会静默返回。

导出使用数据库租约。进程在渲染中退出时，原幂等键会先保持 `generating`；租约最多五分钟后，同一请求和同一幂等键可从已冻结的题目快照恢复，不会重新选题。导出成功只会保存 PDF 文件和导出记录，不会写回题目或调度状态。

## 数据与备份

默认数据目录是当前目录下的 `data/`：

```text
data/
  errorbook.sqlite3
  exports/
  tmp/
```

停止 MCP 服务后，备份整个数据目录即可。不要只复制 SQLite 主文件而遗漏仍在使用中的 WAL 文件。

## 开发验证

```powershell
uv sync --extra dev
uv run pytest
```

测试覆盖数据库事务、并发稳定编号、幂等与崩溃恢复、FSRS 状态更新、优先级衰减、公平选卷、MCP stdio 往返、公式安全、PDF 文件完整性和真实浏览器渲染。

构建可分发 wheel：

```powershell
uv build
```

## 安全边界

- MCP 不读取任意图片路径，也不向外部 OCR 服务发送题目。
- 题干按纯 Markdown 处理，原始 HTML 被转义；LaTeX 只转换为 MathML，不执行 TeX 命令。
- stdio 模式的日志只写入 stderr，避免破坏 MCP 协议帧。
- 远程 streamable HTTP 部署需要由反向代理提供 TLS、认证和单用户隔离；默认配置面向本地 stdio。
- streamable HTTP 模式本身不提供多租户隔离。不要把默认服务直接暴露到公网。

## 参与项目

开发流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，架构与一致性边界见 [docs/architecture.md](docs/architecture.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 issue 中附带真实题目、数据库或访问凭据。
