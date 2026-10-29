# 本地配置

公开分享时只携带 `assets/config.example.json`，不要附带填写过的本地配置、Vault、运行状态、凭据或个人日志。无需修改安装目录里的文件。

推荐按 [README.md](../README.md) 执行 `python scripts/setup.py`，自动在 `~/.config/obsidian-knowledge-companion/config.json` 创建本地配置并初始化知识库。也可将示例复制到分享目录之外，按需修改后使用：

```powershell
python scripts/vault_tool.py --config "<CONFIG_PATH>" show-config
python scripts/vault_tool.py --config "<CONFIG_PATH>" init
python scripts/vault_tool.py --config "<CONFIG_PATH>" check-archive
```

`show-config` 校验并显示合并后的配置，不创建 Vault。其输出含实际路径，不要原样公开。

配置选择优先级：`--config` > 环境变量 `OBSIDIAN_COMPANION_CONFIG` > 标准本地配置 `~/.config/obsidian-knowledge-companion/config.json` > 随包默认配置 `assets/config.example.json`。显式指定的配置不存在或无效时报错，不回退。未填写的字段继承随包默认值；安装脚本会把完整默认值落盘，后续技能更新不会覆盖它们。

Vault 路径优先级：`--vault` > `OBSIDIAN_VAULT` > 所选配置中的 `vault`。默认 `~/ObsidianKnowledge` 自动展开当前用户主目录。配置内其他相对路径以配置所在目录为基准；命令行或环境变量的相对路径以工作目录为基准。不读取其他已安装技能的私有配置。

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `vault` | `~/ObsidianKnowledge` | 知识库路径 |
| `timezone` | Asia/Shanghai | IANA 时区名；Windows 缺少数据库时安装 `tzdata` |
| `auto_archive` | true | 自动检查达到时间后的归档；手动 `check-archive --manual` 不受此开关限制 |
| `archive_time` | 17:30 | 每日归档时间，24 小时 HH:MM |
| `show_sources` | false | 默认展示实际采用的笔记、状态和日期 |
| `auto_retrieve` | true | 对相关工程问题、决策、故障和偏好自动检索 |
| `auto_capture` | true | 自动记录工程工作和决策 |
| `capture_preferences` | true | 配合自动记录，允许保存个人偏好 |

时间安排由脚本执行，`show_sources`、`auto_retrieve`、`auto_capture` 和 `capture_preferences` 由 AI 读取后遵守。用户本轮明确指令优先。本技能只操作本地知识库，不需要 Git、远端地址或认证。

旧配置中的 `git_sync_enabled`、`sync_weekday`、`sync_time`、`allow_sensitive_sync` 会被忽略，不出现在生效配置中，不能重新开启上传。旧运行目录里的同步状态文件不读取、不重试，也不会自动删除。

时间仅用于会话触发检查，不启动后台任务。更换时区或迁移资料之前先处理未完成事务。
