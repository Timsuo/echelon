# Phase 2 Implementation Report

交付范围：Attachments & Inbox Foundation，以及 Group Policy / Conversational Configuration。
直接修改现有 working tree；未修改 `.env`、git remote、仓库名称，未 commit / push。

## 1. Baseline

修改前：90 passed，0 failed，1 warning。
本机默认 TEMP 的 pytest 清理权限异常，改用项目内 `.pytest_tmp` 后正常完成；仅新增该测试目录的忽略规则，没有改变 `.env` 的忽略策略。

## 2. Files changed

修改现有文件（17）：

- `.gitignore`
- `README.md`
- `pyproject.toml`
- `config/config.yaml`
- `app/application.py`
- `app/config.py`
- `app/main.py`
- `app/commands/router.py`
- `app/commands/status.py`
- `app/jobs/service.py`
- `app/llm/deepseek.py`
- `app/notifier/qq.py`
- `app/onebot/actions.py`
- `app/onebot/events.py`
- `app/storage/db.py`
- `app/storage/migrations.py`
- `app/storage/repository.py`

新增文件（24，包括本报告）：

- `app/attachments/__init__.py`
- `app/attachments/models.py`
- `app/attachments/storage.py`
- `app/attachments/worker.py`
- `app/commands/inbox.py`
- `app/commands/policies.py`
- `app/inbox/__init__.py`
- `app/inbox/renderer.py`
- `app/onebot/files.py`
- `app/policies/__init__.py`
- `app/policies/models.py`
- `app/policies/parser.py`
- `app/policies/worker.py`
- `app/storage/inbox_repository.py`
- `app/storage/phase2_schema.py`
- `app/storage/policy_repository.py`
- `app/storage/policy_schema.py`
- `tests/policy_helpers.py`
- `tests/test_attachments.py`
- `tests/test_inbox_files.py`
- `tests/test_phase2_migration.py`
- `tests/test_phase2_server.py`
- `tests/test_policies.py`
- `PHASE2_REPORT.md`

## 3. Database

幂等事务迁移新增：

- attachments：独立附件、下载状态/预算、哈希、本地路径、来源。
- inbox_items：可扩展信息单元，分类/优先级/截止时间等预留字段为空。
- inbox_item_messages：关联内部 messages.id，支持多对多。
- inbox_item_attachments：支持多个附件、多个条目间关联。
- group_policies：按 self_id / group_id 保存别名、模式和独立开关。
- configuration_proposals：管理员、账号、diff、状态、10 分钟有效期。
- configuration_requests：自然语言配置的持久后台解析队列。

private_outbox 兼容新增文件类型、账号、附件引用和取消状态；旧文本通知仍可发送。
Phase 1 的 messages、summary_jobs、summaries、runtime_state、command_receipts 及 outbox 原有字段数据保留。
迁移重复执行不重复创建数据；失败事务回滚。测试覆盖旧数据库升级、逐表内容保留以及策略/提案跨重启保留。

## 4. File ingestion

事件 → whitelist → GroupPolicy → adapter 标准引用 → 消息/附件/可选 Inbox 同事务保存 → 独立 AttachmentWorker → 本地磁盘。

支持 file segment / CQ file 和 group_upload notice；协议字段差异集中在 onebot/files.py。
有 file_id 时按账号/群/file_id 去重；缺失时按消息及段序号构造来源键，避免仅凭同名文件误合并。
通知缺少 message_id 时生成 notice 命名空间 ID，并保留原事件以便追溯。

下载有全局及群策略开关、上报大小/HTTP 大小/实际流量三层上限，默认 100 MiB。
临时 .part → 完整性检查 → SHA-256 → 原子改名 → downloaded；失败不会把半文件标记为完成。
临时网络错误最多额外重试 2 次，重试预算持久保存；重启恢复 interrupted downloading。
下载失败不会停止普通消息采集。附件二进制内容不解析、不执行、不发送给 DeepSeek。

## 5. Security

- 群仍严格只读。ActionGateway 唯一发送 OneBot action，允许的只读动作仅 get_group_file_url。
- 私聊输出只允许 send_private_msg / upload_private_file，recipient 必须等于 ADMIN_QQ。
- 群消息、群文件上传/删除/移动/转存、群管理和未知 action 均被拒绝。
- /file 只接受 Inbox ID 和附件序号。Gateway 重新从附件表获取记录，不接受 file path 参数。
- 路径必须精确匹配 storage root / self_id / group_id / attachment_id-safe_filename；验证文件存在、状态、SHA-256，并拒绝链接、junction、reparse point 和硬链接。
- 文件名清理 Windows 保留名、分隔符、路径穿越及非法字符；原名称仅作元数据。
- 下载 URL 限可信 QQ CDN 域、HTTP(S) 标准端口和公网 DNS 地址，每次重定向重新检查。
- 握手与逐事件账号验证保留；LLM 没有 ActionGateway、数据库修改或采集授权能力。

## 6. Inbox and Group Policy

/inbox：最近 10 个非归档条目；/inbox unread 只显示未读。
/detail：显示来源及附件，unread → read，已归档状态保持。
/archive：只修改状态，不删除数据或磁盘文件。
/file：已完成下载的附件通过 outbox 私聊 ADMIN_QQ；未准备好/超限明确提示，不能突破下载限制。
/status 保留已有运行信息并增加附件和未读统计。

新增群策略默认 summary_only：保存消息和附件元数据，默认不下载、不创建 Inbox。
inbox 启用文件 Inbox 和下载；priority 在此基础上保存 priority_watch_enabled；ignore 跳过业务处理。
独立开关允许“只总结但下载文件”。Priority Watch 仅保存设置，Phase 2 没有自动高优先级提醒。
采集入口、summary 入队/执行、附件下载执行和 Inbox 创建均实际读取策略。

/groups、/group 展示当前账号策略；/config 先由程序确定白名单内唯一目标。
明确格式本地解析，其他自然语言后台调用 DeepSeek 严格 JSON；模型输出不允许 group_id 或未知字段。
提案显示 Before → After；/confirm 才保存，/cancel 不修改。确认检查管理员、账号、白名单、过期状态和原字段是否已改变。
单独修改附件开关不会顺便改 mode/summary/inbox；重复确认幂等。
同名或无明确指代时返回候选群号，不猜测；不会通过自然语言扩张 groups.allowed。

## 7. self_id isolation

附件、Inbox、策略、配置请求、提案与文件 outbox 均携带 self_id。
所有新命令按当前绑定账号查询；附件发送再次检查连接账号和数据库绑定。
消息/附件/Inbox 关联以复合外键强制同账号，知道另一个账号的数据库 ID 也不能查看或转发。
原有 summary job → query → summary 的 self_id 隔离保持。

## 8. Tests

最终：208 passed，0 failed，1 warning。
其中原有 90 个测试保持不变；新增 118 个测试用例（含参数化用例）。
唯一测试警告为 Starlette TestClient 对 httpx 的弃用提示。

已执行并通过：

```powershell
.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp
.\venv\Scripts\python.exe -m compileall -q app
.\venv\Scripts\python.exe -m ruff check app tests
.\venv\Scripts\python.exe -m pip check
git diff --check
```

覆盖附件解析、白名单、幂等、失败/半文件/有限重试、路径及任意文件读取防护、Action Firewall、Inbox 命令、多对多关联、账号隔离、迁移、策略、提案及四个自然语言场景。
额外模拟慢下载和慢配置解析，验证 WebSocket 仍可采集普通消息并响应 /status；包含真实本机 uvicorn 启动测试。
新增附件测试显式设置 inbox 策略以符合追加的安全默认要求，未降低断言。未删除/skip/xfail 旧测试。

## 9. Manual verification and remaining risks

1. 停止旧 Monitor 并备份 data，运行 start.bat，避免启动第二实例。
2. NapCat 继续使用 Universal Reverse WebSocket：ws://127.0.0.1:8789/onebot/v11/ws，令牌一致并携带正确 X-Self-ID。
3. /status、/groups 验证连接和默认 summary_only。
4. /config <白名单群号> mode inbox → 检查 diff → /confirm <提案ID>。
5. 群上传小型 txt/docx；核验 attachments 状态 downloaded、磁盘文件和 SHA-256。
6. /inbox → /detail <id> → /file <id> 1，确认管理员收到文件，群里无输出。
7. /archive <id>，确认默认列表隐藏而数据/文件仍保留。
8. 配置 alias，再试“不要自动下载文件”，验证只改下载字段；试 cancel、过期、同名及非白名单拒绝。
9. 验证 summary_only、ignore、超限文件与非白名单文件行为，再验证 /summary 30m、2h、today。

本次使用模拟 OneBot/DeepSeek 与临时数据库，没有替代真实 NapCat 文件接口联调。直接相关的剩余限制：

- NapCat 必须能读取 Monitor 的本地附件路径；不同安装方式和文件 CDN 地址可能需针对版本适配，当前不放开任意 URL。
- 没有稳定 file_id 的直链仅在内存短期保存，重启后可能无法重新下载；失败保留元数据。
- QQ 接受文件但回执丢失时，重试可能重复私聊发送，无法保证跨 QQ 的 exactly-once。
- 自然语言语义仍需管理员检查提案 diff；明确格式命令可离线使用。
- 策略变更不删除旧内容、不自动追溯生成 Inbox 或重新排队 skipped 下载；已经开始的外部请求可能完成。
- 附件长期保留并持续占用磁盘，本阶段没有 retention policy。

## 10. Deferred

Phase 3：LLM triage、category、priority、labels、deadline extraction、personal preferences。
Phase 4：heartbeat、urgent alerts、scheduled digest、quiet hours。
后续：Web Inbox、retention policy；本阶段没有 OCR、文档解析、RAG、多机器人或任何群写入能力。

建议 commit message：

```text
feat: add attachment handling and inbox foundation
```
