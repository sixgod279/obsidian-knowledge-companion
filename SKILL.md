---
name: obsidian-knowledge-companion
description: 检索、沉淀和维护用户配置的三层 Obsidian 知识库；用于知识检索、记录、归档和维护重复或过时知识。路径、时间安排和使用偏好由本地配置决定。
---

# Obsidian Knowledge Companion

维护三层个人工程知识库。用检索层低成本路由，以资料层提供证据，把所有日常新增和修订先写入待入层。

## 配置与权限

首次安装按 [README.md](README.md) 执行 `scripts/setup.py`，自动生成独立本地配置并初始化知识库。配置细节见 [references/configuration.md](references/configuration.md)。默认配置可直接用于新知识库；用户指定路径或偏好时覆盖默认值，不搜索或改动其他已安装技能。

- 先运行 `show-config` 读取生效配置；无本地配置时使用随包默认值。无效配置必须报告，不绕过错误配置。命令可省略 `--config` 使用标准本地配置；示例中的 `<CONFIG_PATH>` 替换为实际配置路径。

- 使用配置的 `vault`、`timezone`、`archive_time`。配置只控制交互时检查，不创建后台定时任务。
- `auto_retrieve` 为 true 时，对工程问题、决策、故障和偏好自动检索；否则仅在用户请求使用知识库时检索。
- `auto_capture` 为 true 时，自动捕获工程工作与决策；否则仅按用户明确要求记录。个人偏好还需 `capture_preferences` 为 true，或用户明确要求记录该偏好。
- `show_sources` 控制是否默认展示实际采用的来源；用户本轮要求优先。
- 自动归档仅在 `auto_archive` 为 true 时执行。本技能仅在本地工作，不上传、推送或同步知识库到远端。
- `00-检索层`：正常问答只读；仅归档事务持有独占锁时可写。
- `10-资料层`：正常问答只读；仅归档事务持有独占锁时可写。
- `90-待入层`：正常问答可写，但每个 AI 只能修改自己持锁的当前问题笔记及附件。
- 自动归档或用户主动要求归档时，本轮对三层具有完整权限，但只能通过归档事务修改，并锁定上两层及归档清单内的待入文件。
- 用户明确要求不查知识库时，本轮跳过检索、捕获、维护和自动归档；若用户同时明确要求写入或归档，以该明确要求为准。

首次使用或结构缺失时运行：

```powershell
python scripts/vault_tool.py --config "<CONFIG_PATH>" init
```

## 每轮入口

仅在使用本技能处理知识库任务时执行以下流程；非知识库问题只在相关自动偏好已启用时进入。用户要求跳过时不执行：

1. 运行 `health`。若存在未完成归档事务，先按报告恢复或继续。
2. 运行 `check-archive`（手动归档使用 `--manual`）。返回 `due: true` 时先归档报告中的全部 `pending_dates`。
3. 按 `auto_retrieve` 和用户本轮请求执行分层检索。
4. 依据当前代码、日志、实测和适用范围回答，不能让旧知识覆盖更新的直接证据。
5. 按 `auto_capture`、`capture_preferences` 和用户本轮请求建立/更新一问题一文档；纯一般知识不自动写入。

```powershell
python scripts/vault_tool.py --config "<CONFIG_PATH>" health
python scripts/vault_tool.py --config "<CONFIG_PATH>" check-archive
```

## 分层检索

1. 只搜索检索层：

```powershell
python scripts/vault_tool.py --config "<CONFIG_PATH>" search-index --query "项目 器件 现象 版本" --limit 8
```

2. 检查命中路由的项目、器件、版本、范围、状态摘要、来源哈希、更新时间和资料链接。可选择多个确有关系的路由。
3. 只读取路由直接指向的文件或狭窄目录；禁止从 Vault 根目录无约束搜索或批量读取资料层。
4. 归档查重时运行 `candidate-search`，使用 BM25、标题、项目/范围、状态、链接和实体重叠召回候选。候选只缩小范围，不决定合并；Codex 必须阅读候选全文，判断为 `完全重复`、`语义重复`、`版本并存`、`真实冲突` 或 `无关`。
5. 无路由时明确知识库未发现直接记录，不绕过检索层扩大普通检索；在待入笔记记录索引缺口。

### 目录准确性评估

每次根据路由主动进入资料层并读完对应资料后，本轮可评估该路由是否准确、充分和仍然适用。发现摘要、关键词、范围、状态计数、链接或来源哈希需要更新时，正常模式不得直接修改检索层，而要创建待入层索引修订建议：

```powershell
python scripts/vault_tool.py --config "<CONFIG_PATH>" propose-index-update --index "00-检索层/项目目录.md" --source "10-资料层/对应资料.md" --reason "摘要未覆盖实际适用边界" --owner "本会话UUID"
```

在建议中写出准确且充分的新目录项。归档时重新核对来源和索引哈希；哈希变化则重新评估，不能盲目套用旧建议。

## 日常捕获与锁

为当前问题创建或复用笔记，并取得文件锁：

```powershell
python scripts/vault_tool.py --config "<CONFIG_PATH>" new-note --title "问题标题" --kind experience --project "项目" --owner "本会话UUID"
```

- `owner` 在一次会话中保持不变，使用 UUID；不要复用其他 AI 的 owner。
- 普通锁精确到当前问题 Markdown 和附件目录，不锁整个项目或整天目录；不同工程和不同问题可并行。
- 编辑内容先写到临时输入文件，再用 `safe-write --owner ... --path ... --expected-sha256 ... --input ...` 原子保存；删除用 `safe-delete` 进入7天隔离。保存后运行 `heartbeat` 续租，完成本次回答后运行 `release-lock`。
- 同一天同一问题更新原笔记；不要为每轮对话重复建立文档。
- 新推测标为 `未验证`。区分代码审查、编译、软件测试和实机验证。
- 原始 PDF、图片、电路图、日志等保持原件不改，用旁注记录来源、日期、哈希、适用对象和状态。依赖 Codex 当前可用能力解析；必要时在 Skill 内补充本地工具。无法可靠解析时标记未解析/未验证，不根据文件名猜测。

锁、租约、冲突检测和恢复命令见 [references/concurrency-transactions.md](references/concurrency-transactions.md)，任何写入前必须遵守。

## 知识状态

经验文档和待入文档的每个知识性段落必须使用 `事实`、`未验证`、`错误`、`过时` 状态块，并包含规定日期和证据。写入或修改前读取 [references/status-schema.md](references/status-schema.md)。

- `未验证 -> 事实/错误` 必须有验证或证伪证据。
- `事实 -> 过时` 必须证明适用版本、硬件、环境或时间边界改变。
- `事实 -> 错误` 必须有足以推翻原结论的新证据并保留更正原因。
- 无学习价值的明确错误可在归档时移入隔离区；有纠错价值的错误保留。

## 自动与主动归档

启用 `auto_archive` 后，在配置时区达到 `archive_time` 的当日首次符合条件交互中执行一次自动归档。事务开始时冻结归档清单：

- 包含所有早于今天且仍有内容的日期目录；
- 包含今天在事务开始前已有的待入内容；
- 归档期间或归档后新写入的今天内容留待以后处理；
- 同一天自动归档成功后不再次自动归档；
- 下一次归档补处理全部积压日期；
- 用户主动要求归档时忽略时间，处理事务开始时的全部积压。

归档前读取 [references/archive-workflow.md](references/archive-workflow.md)。必须：取得上两层独占锁和清单内待入锁；建立 Vault 外事务快照；按清单整理；更新资料与索引；验证哈希、状态、链接、孤立资料和目录准确性；提交事务；释放锁。失败时保留事务状态，下次交互先恢复或继续。恢复快照和删除隔离只保留 7 天。

## 维护原则

- 检索层只保存准确、充分的路由，不复制详细正文。来源哈希变化的目录项视为待复核。
- 原始资料保持原貌；相同原件只保留一个规范副本，其他主题用链接引用。
- 合并必须基于全文语义、适用范围和证据，不能只依据相似度分数或日期新旧。
- 删除、覆盖和移动经事务处理；恢复副本保留 7 天后清理。
- 证据不足的冲突不擅自选边，保留双方并记录待验证项。
- 正常问答不得写上两层；归档中资料与索引必须在同一事务内同步。

## 资源

- [references/vault-layout.md](references/vault-layout.md)：三层结构与目录项格式。
- [references/status-schema.md](references/status-schema.md)：段落状态和迁移证据。
- [references/concurrency-transactions.md](references/concurrency-transactions.md)：锁、事务、冲突和7天恢复。
- [references/archive-workflow.md](references/archive-workflow.md)：积压归档和语义维护。
- `scripts/vault_tool.py`：确定性的结构、检索、锁、事务、扫描辅助命令。
- `assets/*.md`：Obsidian 待入模板。
