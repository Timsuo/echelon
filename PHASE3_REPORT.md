# Phase 3 Implementation Report

在 Phase 2.5 现有架构上增量实现；未修改 `.env`、采集白名单、Git remote 或 ActionGateway 权限。
本阶段提交说明：`feat: add intelligent triage and personal preferences`。

## 1. Baseline

修改前：**298 passed，0 failed，1 warning**。警告为既有 Starlette TestClient/httpx 弃用提示。
使用项目 venv 和项目内 `.pytest_tmp`，未使用真实生产数据库测试迁移。

## 2. Privacy fix

首先移除 SummaryData 校验失败时的 raw_response 日志。
SummaryData、ConfigIntent、TriageResult、PreferenceIntent 共用安全的生成入口，
校验失败只记录 schema、response_length、finish_reason、error_class，不记录原文或完整模型输出。
四种 schema 的 malformed JSON 和合法 JSON/非法字段均有隐私回归测试。
SDK DEBUG 日志原有关闭策略保留。旧日志不会被本次升级自动删除或重写。

## 3. Triage architecture

新实时/回补消息 → 原 EventProcessor → 原 Repository.add_message → 消息与 pending ownership 同事务保存。
只有 inbox / priority 且 inbox_enabled=true、全局 triage.enabled=true 才登记；summary_only 不登记，ignore 仍直接跳过采集。
后台 TriageWorker → 持久 Candidate → DeepSeek 严格 JSON → Python 引用/日期验证 → 单事务应用结果。
LLM 调用在事务外，不阻塞 Collector、AttachmentWorker、HistoryWorker 或 ConfigurationWorker。
归属以内部 messages.id 为唯一键，关系包含 self_id/group_id；重复实时与历史消息不重复排队。
running 重启恢复 queued，attempts 保留；旧执行尝试不能覆盖已完成或重新认领的任务。

## 4. Candidate batching

- inbox 默认180秒 debounce、600秒 hard maximum wait。
- priority 默认60秒 debounce、180秒 hard maximum wait；不产生即时提醒。
- 默认最多100条、40000输入字符（包括 system prompt）、3600秒原消息跨度。
- 消息数、源文本预算、时间跨度到边界时提前封批；持续聊天不会无限延后。
- 最近合并上下文超预算时先缩减可选候选；源消息不静默截断。仍超限则明确 failed、保留原文。
- 新建 triage_settings.triage_start_at 作为升级时间标记；迁移不扫描旧 messages。
- 只有新 INSERT 获取 ownership，Phase 3 后插入的旧历史事件可分类；已有旧消息的重复回补不会触发旧库全量分类。

## 5. LLM schema

TriageResult.items 最多10项；固定10种 category、4种 priority、11种 label，extra=forbid、strict=True。
action_required 与 action_text 强制一致；置信度0–1，字符串和列表有长度限制。
每个 Candidate ID 必须被事项引用或 ignored，不能交叉、伪造或遗漏。
merge_into_item_id 只能来自 Python 提供的候选集；应用前再次查询账号、群、归档状态和 revision。
群文字、文件名、已有条目及偏好字符串都是不可信数据。system prompt 禁止执行其中指令，模型没有工具。
模型输出/瞬时网络错误共用持久 triage retry_count 预算，默认初次+2次；SDK 不叠加自己的重试。
length、输入过大、账号/策略变化、应用约束冲突直接失败。失败消息标 failed，不伪装成 ignored。

## 6. Inbox aggregation

一批可产生0..10个条目，也可多条消息聚合为一项。普通闲聊可明确 ignored。
同群最近7天最多10个非归档条目作为候选；已关联当前消息的文件条目优先，即使文件占位创建较早。
模型未选择合并时，Python 仍会复用当前消息关联的未分类文件占位条目。
多个文件占位合并时转移其关联并归档冗余占位；源消息、附件、磁盘文件不删除。
result_json、Inbox、message/attachment links、labels、message state 和 completed 同事务提交；注入失败已验证回滚。

revision 默认1；title/summary/category/priority/labels/action/deadline 实质变化才增加。
只读详情和 read 状态不增加 revision，reason/confidence 的变化也不触发实质内容修订。
model_priority 保留模型判断，当前 priority 与其一致，偏好通过模型上下文参与，不另做隐藏规则提权。
`/inbox high|critical|deadline|action` 已接入；`/detail` 展示分类、标签、行动、截止、置信度、简短依据和覆盖提示。

## 7. Deadline

每条模型输入提供 event_time、配置时区下 ISO local_time、ingest_source，不提供容易误用的 received_time。
deadline_at 必须带时区，安全转换为 Unix timestamp 存储；展示使用配置时区。
今天/明天/后天/大后天/下周某日额外由 Python 对引用消息原日期进行核验，不能使用恢复接收时刻。
歧义允许 deadline_at=null，保留 deadline_text。复杂自然语言日期仍需要管理员核对，不承诺模型解释永远正确。

## 8. Personal Preferences

`/prefs` 查看；`/pref` 后台生成严格 PreferenceIntent，不直接写偏好。
支持重要/低优先级关键词、重要发送者、固定分类优先级偏好，集合和元素都有上限。
Python 校验关键词、发送者、分类与操作方向有明确输入依据；仍需管理员核对自然语言提案。
configuration_requests/proposals 最小扩展 kind：group_policy / triage_preferences，旧记录默认 group_policy。
统一 `/confirm`、`/cancel` 按类型分发，10分钟过期、账号/管理员校验、重复确认幂等、旧 diff 冲突失效。
偏好增删只影响显示字段，不能修改 allowlist、GroupPolicy、路径、OneBot 或通知计划。
偏好仅影响后续执行的分类，不自动重算旧 Inbox。

## 9. Coverage integration

模型调用前使用 HistoryRepository.unresolved 查询原消息窗口；原子应用时再次检查新出现的 gap。
存在 partial/unknown/failed/pending/running 时仍分类，但 Python 持久保存 warning。
合并既有 warning 条目不会静默清除；无已知缺口标 no_known_gap，也明确不保证绝对完整。
history_recovery/history_poll 保持真实 event_time。未来 Phase 4 需结合源消息时间、ingest_source、revision 判断投递。

## 10. Migration

同一现有迁移事务幂等新增6张表：triage_jobs、triage_job_messages、triage_message_state、triage_preferences、inbox_item_labels、triage_settings。
inbox_items 增加 revision、model_priority、action_required、action_text、triaged_at、triage_model、coverage_status。
配置请求与提案增加 kind。messages 的 ingest_source 和历史缺口语义不变。
旧 Phase 2.5 数据逐表对比保留；重复迁移 no-op；注入失败时新增列和表一并 rollback。
真实 messages.db 将在下一次正常启动升级，无需删除数据库。

## 11. Security

人工搜索确认唯一 socket.send_json 仍在 ActionGateway，权限列表没有变化：
get_group_file_url、get_group_msg_history、send_private_msg、upload_private_file。
群发消息、群文件上传/修改、delete_msg、群管理、未知 Action 仍被拒绝；私聊仍限 ADMIN_QQ。
Triage LLM 仅得到文本与附件 filename/file_size/download_status，不得到本地路径、临时 URL 或附件 binary。
模型没有 OneBot、History、DB、文件或偏好修改工具。Python 严格校验后才应用分类，偏好仍须二阶段确认。
自动分类不创建 outbox 通知；原有命令、总结和断连恢复报告继续按既有路径工作。

## 12. Tests

最终：**414 passed，0 failed，1 warning**（127.02秒）；较基线新增116个参数化后用例。
唯一警告仍为 Starlette TestClient/httpx 弃用提示。以下检查全部通过：

```powershell
.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp
.\venv\Scripts\python.exe -m compileall -q app
.\venv\Scripts\python.exe -m ruff check app tests
.\venv\Scripts\python.exe -m pip check
git diff --check
```

覆盖策略调度、所有权去重、debounce/hard max、数量/字符/时间边界、重启预算、跨账号与群、
Schema 枚举与伪造 ID、多项聚合、文件合并、revision、归档/并发修改、原子回滚、历史日期与时区、
偏好四个示例与确认/取消/过期/冲突、Coverage、四类 LLM 日志隐私、完整应用生命周期和慢模型期间继续采集。
保留全部旧测试；唯一旧测试内容改动为 test_deepseek 的日志断言，从要求 raw_response 改为禁止原文、要求安全元数据，符合本阶段明确隐私要求。
测试均使用临时数据库和 Mock 模型，无真实 DeepSeek 调用。

## 13. Manual verification

1. 正常停服、备份 data，运行 start.bat；NapCat 继续原 Reverse WebSocket 配置。
2. `/prefs` → `/pref 考试和调课对我很重要` → 检查 diff → `/confirm <id>`。
3. 将课程白名单群配置为 inbox 并确认。
4. 连续发送“下周实验课改到302”“时间还是周四下午”“记得带实验报告”。
5. 等待默认180秒静默期及模型处理，查看 `/inbox`、`/detail <id>`，确认聚合与字段；没有即时提醒。
6. 上传文件并补充用途，检查复用文件条目，来源/附件关联与 `/file` 转发。
7. 检查四个新筛选；summary_only 普通消息不创建智能 Inbox。
8. 断连回补，核对原消息时间、deadline、coverage；回归 `/summary`、`/sync`、`/coverage`、群策略提案。

详细配置与验收步骤见 README 的 Phase 3 章节。真实 NapCat 与真实 DeepSeek 输出质量尚需以上联调。

## 14. Deferred to Phase 4

Heartbeat、即时高优先级私聊、Scheduled Digest、Quiet Hours、每日投递均未实现。
Web UI、RAG、OCR、附件解析、图片/语音理解、Semantic Watch、多用户/多机器人及任何群写操作也未实现。
失败分类保留状态与消息，本阶段未提供手动重分类命令；高流量、接口不可用、输入/输出超限可能形成积压或失败任务。
最近有限合并候选和模型判断不能保证所有相关事项都完美合并；自然语言意图与日期应核对。

NapCat / QQ history API cannot be treated as a guaranteed complete offline event replay mechanism.

### Files changed

新增：

- app/triage/__init__.py、models.py、prompts.py、deadlines.py、worker.py
- app/storage/triage_schema.py、triage_repository.py、preference_repository.py
- app/commands/preferences.py
- tests/test_triage.py、test_triage_schema.py、test_triage_migration.py、test_triage_lifecycle.py、test_preferences.py
- PHASE3_REPORT.md

修改：

- app/application.py、app/config.py、config/config.yaml
- app/llm/deepseek.py、app/policies/worker.py
- app/storage/db.py、repository.py、inbox_repository.py、policy_repository.py
- app/commands/router.py、help.py、inbox.py、app/inbox/renderer.py
- tests/test_deepseek.py、README.md
