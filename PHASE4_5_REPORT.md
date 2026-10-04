# Phase 4.5 Implementation Report

验收日期：2026-10-04（Asia/Shanghai）。本阶段基于 main `8d026d588de5730fee76ec464c4eae92ff61f676` 的实际实现修改当前 working tree。范围为 Operational Hardening, Outbox QoS & Production Readiness；没有实现 Web UI 或 public HTTP API。

## 1. Baseline

已检查 `PHASE3_REPORT.md`、`PHASE4_REPORT.md`、`PHASE4C_REPORT.md`、README，以及 application、storage、delivery、notifier、jobs、commands、policies、onebot 和相关 tests。此前报告中的 Phase 4 限制属于历史记录；本报告记录本次修复后的行为。

本地 Windows / Python 3.14 baseline：

| 检查 | 实际结果 |
|---|---|
| pytest | **555 passed, 1 warning，200.00s** |
| Ruff | PASS |
| compileall | PASS |
| pip check | PASS |
| git diff --check | PASS |

baseline 使用 `--basetemp=.pytest_tmp/phase45_baseline`。Warning 为已有 Starlette TestClient/httpx deprecation。本次未变更依赖来掩盖 warning。保留用户原有 `config/config.yaml` 修改；未删除、重置生产数据库或改写 Git 历史。

## 2. Correctness Fixes

- `Repository.fail_job(job, error)` 使用任务账号创建失败通知；显示群名称、任务 ID 和安全说明。任务元数据缺少账号时，仅从该任务持久化记录恢复身份，不借用当前连接账号；无法确定身份的 legacy 任务不创建无主通知。
- 所有新文本和文件 outbox producer 明确传入 `self_id`；文本写入拒绝 NULL、bool、非正数等无效身份，文件通知从匹配账号的附件记录读取持久身份。旧 NULL outbox 保留兼容读取，迁移不猜测账号。
- 授权／policy recheck 后没有群可创建总结时，返回“当前没有符合条件的群可以创建总结任务。请检查 /allow 和 /groups。”并保留原子 command receipt。
- 已审计 Summary call graph：生产服务只走 `complete_summary()` v2；`complete_job()` 保留但标记 deprecated，测试禁止生产 Summary 路径误用旧接口。
- 重放相同私聊 message_id 不重复执行 `/summary`、`/digest now`、`/config`、`/pref`、`/notify`、`/allow add`、`/allow remove`、`/confirm`、`/cancel` 的副作用。路由提前识别重放，实际写入仍由事务内 receipt／request 唯一性保护。

## 3. Configuration Proposal Idempotency

新增 nullable `configuration_proposals.source_request_id`，以及 `(self_id,kind,source_request_id)` 非 NULL partial unique index。旧提案保持 NULL，允许多个 NULL，不根据文本推断来源。

统一修复 group_policy、triage_preferences、delivery_preferences、group_authorization 四条异步路径。OneBot 验证／模型解析在事务外；事务内重新检查 request 的账号、管理员、kind 与 running 状态，创建关联 proposal、写提案回复并完成 request。已存在关联 proposal 时不会再生成第二个。

`configuration_requests` 实际 INSERT 成功才创建“正在验证／解析”的 acknowledgement；重复请求检查先于队列容量检查，避免队列满后重放产生新提示。确认／取消的 receipt、状态修改、回复也在同一事务提交。

测试在 proposal INSERT 后、request completion 前注入中断，验证整个事务回滚；重新打开数据库恢复 request 后再次执行，只保留一个 proposal。四种 kind 都覆盖，并包含实际授权验证路径。

## 4. Outbox QoS

新增 `OutboxPriority`，由 Python producer 决定优先级，LLM 无设置入口。顺序为 CRITICAL、HIGH、Recovery、交互回复、手动 digest、定时 digest、Summary、后台通知。CRITICAL recovery 保留 CRITICAL 优先级。内部整数不是用户 API。

ready selection 同时检查未发送、未取消、未 dead-letter、已到 next_attempt，以及没有更早未完成 producer chunk；按 aging 后的优先级降序、ID 升序选择。当前账号与 legacy NULL 分别做索引范围查询再合并，旧 Summary 重试不再阻塞 ready urgent／交互回复。

每等待一分钟提高一级，最多提高 60 级，总有效优先级上限为 CRITICAL。同有效优先级按 ID FIFO；普通 Summary 等待后能够越过持续到达的新高优先级 producer。测试注入时间，不进行分钟级真实 sleep。

## 5. Producer Ordering

采用**producer 之间可抢占、producer 内严格顺序**：A1 发完后允许 CRITICAL B 插入，再发 A2、A3。所有新文本／文件都有 producer identity；后续 chunk 必须等待前序成功，即使它本身已 ready。

一个 chunk 进入 dead-letter 时，同 producer 剩余未发送 chunk 一起终止，记录 `producer_chunk_failed`，不会出现 1/3 后直接发送 3/3。已发送部分不会删除或重发。

旧无 producer_key 行按独立 ready 行兼容发送；丢失的历史 chunk 归属无法安全重建，不通过通知文本猜测。严格 producer 顺序保证适用于具有 producer identity 的行。

## 6. Retry Semantics

新增 `outbox.retry_base_seconds=2`、`retry_max_seconds=300`、`terminal_retry_limit=3`，配置带取值校验。文本 `ConnectionError`／`TimeoutError` 及其子类继续重试，不因三次断线永久终止；NapCat 未连接时 notifier 保持等待。

provider rejection／validation／permission 等非网络错误使用独立 `terminal_attempts` 预算，默认第三次进入 dead-letter。普通 attempts 记录失败次数；混合网络与 provider 错误不会让网络失败消耗 terminal 预算。退避有上限，数据库 error 仅保存安全类别。

文件上传沿用 Phase 2 原有有限重试和 cancelled 语义，包括其既有超时策略；本次长期网络重试保证针对文本通知。没有扩展上传 action 权限。

## 7. Dead Letter

新增 `private_outbox.dead_letter_at`。最终失败行及其 producer_kind、producer_key、attempts、error 保留，重启后仍可诊断。不删除 Inbox、Summary、Attachment、附件文件或来源关系。

没有实现可选 `/outbox retry`：避免在未完成相关性和整组恢复规则前恢复过期通知。dead-letter 保持终态；管理员先排查发送原因，通过后续业务操作产生新的投递。当前没有自动删除 dead-letter 的 retention 任务。

## 8. Delivery Failure Reconciliation

`inbox_deliveries`、`digest_runs` 采用 additive `failed_at`／`failure_reason`，避免重建原 CHECK 表及 provenance 外键。`inbox_delivery_status`／`digest_run_status` views 提供明确 `effective_status='failed'`；未来读取状态应使用该视图或应用 failed_at overlay，不能只读取原 status。

notifier dead-letter 事务立即同步失败；delivery reconciliation 也能在重启后识别 dead-letter／cancelled。只有所有 chunk 成功才标记 delivered，部分成功不算完成。

失败记录保留原 revision/snapshot 去重信息，同一失败 revision 不会被每次 heartbeat 自动重新排队；新 revision 可按正常规则处理。此行为避免持久拒绝造成无限生成新 producer。

## 9. Worker Scheduling

DeliveryWorker 根据 next_due（heartbeat、scheduled digest、quiet-hour deferred work、manual digest）运行完整 tick，空闲时不再每秒执行 digest selection／history gap scan。

`asyncio.Event` 仅作唤醒提示：manual digest、偏好确认、Inbox revision、账号绑定可触发；事件在读取数据库之前清除，运行期间新事件保留。最长等待 30 秒用于健康更新，但未到 due 不做完整业务扫描。

SQLite 是可靠状态来源。启动即扫描持久工作；Event 丢失后也会在下一 heartbeat 恢复。测试覆盖空闲 loop、手动即时唤醒、丢失事件／重启恢复、定时 digest 与 quiet-hour/deferred due。

## 10. /doctor

新增管理员只读诊断及 `/help doctor`，展示 Service、OneBot、Database quick_check、授权群、最近实时收集、未解决缺口、triage/history/summary 数量与 queue age、24h 失败、delivery、outbox、DeepSeek 最近状态、附件、Storage、worker 与 operational table 增长数量。

各诊断块独立捕获异常，仅返回类别／安全提示。DeepSeek 不可用不阻止诊断；不调用 LLM、修复、VACUUM、删除、retry 或配置变更。诊断函数测试确认数据库 total_changes 不增加；命令回复仍经过正常 private outbox，这是发送诊断结果本身所需的持久化。

不扫描全部 messages 正文，不递归遍历附件目录。附件大小取下载记录的 file_size 聚合，为 recorded estimate；DB/WAL 取文件 stat。quick_check 每次执行，未默认执行 integrity_check。大型数据库下 quick_check 和 operational 聚合仍有成本，后续可按实测增加缓存。

## 11. /outbox

新增 `/outbox`、`/outbox failed` 和帮助说明。Ready／Retrying／Deferred 按互斥行数展示；Dead Letter 按 producer 数展示；附最老待发年龄、legacy NULL 行数、最近失败。

`failed` 最多显示 10 个 producer，选择根失败而非每个级联 chunk，只读 ID、类型、key、安全错误类别、尝试次数与时间。不选择正文、snapshot、provider response、token 或临时 URL；非法 legacy 元数据做安全替换。权限和账号隔离沿用管理员路由约束。

## 12. Worker Health

新增 `worker_health`，跟踪 summary、attachment、configuration、history、triage、delivery、notifier。真实成功 loop 调用心跳，写入按 monotonic 时间节流至每 30 秒最多一次；独立计时器不会掩盖阻塞 worker。

启动登记 expected，正常停止清除 expected；stale threshold 按模型／OneBot 超时预算设置。`/doctor` 显示 STALE；`/status` 仅增加 Health OK 或 `/doctor 查看` 提示。heartbeat 不参与业务任务存在性或去重判断。

## 13. Database / Index Audit

迁移在现有 Database.open 事务内执行，所有列／索引／视图可重复执行；六个 DDL 阶段注入失败验证 schema 和数据回滚。旧 outbox priority 一律普通默认值，不解析旧正文。没有删除业务数据或要求重新初始化。

另外对当前生产数据库通过 SQLite **只读连接 backup** 创建本地副本，在副本连续执行两次迁移：原 **32 张表、1384 行**的原有列内容逐项摘要一致，完整性检查 ok、无 foreign-key violation。现场原库未被迁移或重启。副本留在忽略的 `.pytest_tmp/`，没有上传。

1002 行合成 outbox 的实际 `EXPLAIN QUERY PLAN` 关键结果：

```text
SEARCH private_outbox USING INDEX outbox_ready (self_id=? AND next_attempt<?)
UNION ALL
SEARCH private_outbox USING INDEX outbox_ready (self_id=? AND next_attempt<?)
SCAN o
SEARCH p USING INDEX outbox_predecessor
  (self_id=? AND producer_kind=? AND producer_key=? AND producer_chunk<?)
USE TEMP B-TREE FOR ORDER BY
```

`SCAN o` 扫描已按账号和到期时间筛选后的 CTE；不是对所有 outbox 历史行的无条件全表扫描。aging 需要对 ready 候选排序，仍使用临时 B-tree。该次本地查询耗时 **0.0039s**，仅作当前规模观测，不承诺任意队列规模常数时间。负载回归允许 2 秒上限，覆盖 critical、retry、dead-letter、multi-chunk 混合数据。

其他实际计划：delivery due 使用 `deliveries_due` covering index `(self_id,status)`；summary health 使用 `summary_health` covering index `(self_id)`。新增 digest pending 与 outbox failure 索引，复用已有授权／history／triage 索引，没有为每种聚合盲目新增索引。

增长审计：private_outbox、command_receipts、configuration_requests、configuration_proposals、triage_jobs、history_sync_jobs、digest_runs、delivery_revision_state 均持续增长，`/doctor` 报告当前账号行数。因为 receipt／revision／producer 与 dedupe、reconciliation、provenance 和审计相连，本阶段**没有自动 cleanup**。业务 Summary history 保留。

## 14. CI

新增 `.github/workflows/ci.yml`：push、pull_request；windows-latest，Python 3.12／3.14；locked requirements + editable test extras；pytest、Ruff、compileall、pip check 和 tracked-file 检查。版本范围与 pyproject 相符。workflow YAML 解析和关键字段有自动测试。

测试 socket／DNS guard 默认阻止外部网络，真实 loopback 测试服务可用；已有启动子进程也安装 guard。真实 DeepSeek／QQ／NapCat 不参与测试，全部使用 fake/mock/test adapter，不要求账号 secrets。安装依赖仍需访问包源。没有上传 DB、日志、.env 的 artifact 步骤。

GitHub CI：**Not yet verified — workflow added but remote CI run not verified**。本报告代码验收时尚未观察远端 green run；本地 Python 3.14 成功不能替代推送后的远端 3.12／3.14 matrix 结果。

## 15. Security

静态 AST 审计和回归确认：只有 `ActionGateway` 调用 WebSocket `send_json`。read-only actions 仍仅为 `get_group_file_url`、`get_group_msg_history`、`get_group_info`、`get_group_list`；private output 仍仅为 `send_private_msg`、`upload_private_file`。`config.groups.allowed` 仅在 application bootstrap seed 使用。

tracked-file 检查阻止 `.env`、数据库、运行日志、附件、本地 venv 进入 Git；允许既有空目录 `.gitkeep` 和 `.env.example`，不误封 `app/attachments` 源码。新测试使用假凭据，新诊断不输出原始消息／模型响应／附件二进制／临时 QQ URL。

| 最终安全问题 | 答案 |
|---|---|
| 新增任何 group write OneBot action？ | **No** |
| LLM 获得任何 OneBot／DB／filesystem tool？ | **No** |
| /doctor 诊断修改数据、配置或执行修复？ | **No**；仅正常回复会进入 outbox |
| dead-letter 删除用户业务数据？ | **No** |
| config.groups.allowed 重新成为 runtime authority？ | **No** |
| QQ delivery 实现真正 exactly-once？ | **No** |

## 16. Tests

| 检查 | 实际结果 |
|---|---|
| Baseline | **555 passed, 1 warning** |
| Final | **631 passed, 1 warning，281.32s** |
| 新增测试用例 | **76**（555 → 631） |
| Ruff | **PASS** |
| compileall | **PASS** |
| pip check | **PASS**：No broken requirements found |
| git diff --check | **PASS**（Git 提示既有 LF/CRLF 转换，不是 whitespace error） |
| tracked-file safety | **PASS** |
| workflow YAML／CI 配置测试 | **PASS**，包含在完整 suite |
| GitHub CI | **Not yet verified** |
| Manual NapCat | **Not yet verified** |
| Soak Test | **Not yet performed** |

最终本地命令：

```powershell
.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp/phase45_final
.\venv\Scripts\python.exe -m ruff check app tests
.\venv\Scripts\python.exe -m compileall -q app
.\venv\Scripts\python.exe -m pip check
.\venv\Scripts\python.exe scripts/check_tracked_files.py
git diff --check
```

测试日志为忽略的 `.pytest_tmp/phase45_final.log`。唯一 warning 与 baseline 相同，为 Starlette TestClient/httpx deprecation；没有新增测试 warning。最后完整 suite 后仅更新本文档结果，没有再修改实现。

新增专项文件：`test_operational_correctness.py`、`test_outbox_qos.py`、`test_operational_health.py`、`test_operational_migration.py`、`test_ci_safety.py`。覆盖本报告前述 correctness、四类 proposal 原子回滚／重启、命令重放、priority/FIFO/aging/chunk、网络与 terminal budget、dead-letter 持久化／业务数据保留、部分发送 reconciliation、撤权与 retry/dead-letter、worker due/wakeup、隐私与模块独立失败、迁移无损与回滚、千行负载及安全边界。

Crash 测试模拟 send 成功后、sent_at 提交前退出：重启仍能看到待发行，可能再次发送，数据库状态可解释。没有把无法控制的 QQ ACK 窗口描述为 exactly-once。

原测试没有 skip 或降低断言；必要更新仅包括显式 self_id 的调用签名、迁移 fixture 的历史版本边界，以及不同私聊命令使用不同 message_id（相同 ID 重放另有专门测试）。完整回归曾发现两处兼容问题：旧 notify 测试调用未传 self_id，以及缺失任务账号元数据导致 fail_job 无法结束任务；现已修复并增加持久账号切换回归。

## 17. Manual Verification

| 项目 | 状态 |
|---|---|
| 生产 DB 只读副本迁移／重复迁移／原有数据保留 | PASS |
| 合成负载 EXPLAIN 与 query timing | PASS |
| Manual NapCat／QQ／DeepSeek 现场链路 | **Not yet verified** |
| 真实 HIGH／CRITICAL、断开重连、quiet hours、重启 | **Not yet verified** |
| 24–72h Soak Test | **Not yet performed** |

README 已补齐上线检查清单。现场需要备份 data 后验证正常／失败 Summary（群名、账号、任务、安全错误）、撤权空队列、配置请求中断／重放、Summary retry 期间 urgent 越队、分块插队、provider rejection dead-letter、NapCat 断开重连、/doctor 模块故障、/coverage、时区、磁盘、附件增长、自启动和 DeepSeek 可用性。

建议选择 summary_only、inbox、priority 各一个测试群，至少运行 24 小时，包含一次 NapCat restart、Echelon restart、DeepSeek 暂时不可用、HIGH、CRITICAL、deadline update、manual digest、scheduled digest，并记录 `/doctor`、`/outbox`、`/coverage`。所有群测试输入由用户正常发送；Bot 始终 passive read-only observer。

## 18. Remaining Limitations

- 不宣布 Production Ready：真实 QQ/NapCat/DeepSeek 验收、远端 CI 和 soak 尚未完成。
- QQ 已收到但 ACK 丢失，或 ACK 成功而 sent_at 尚未落盘时崩溃，仍可能重复私聊。准确保证是 **best-effort duplicate suppression + persistent outbox**。
- 当前仍为单 Python process、SQLite、asyncio workers；没有多进程发送锁或复杂消息中间件。
- 没有自动 retention、可选 `/outbox retry` 或 `/jobs failed`；失败业务状态通过 /doctor、/outbox 和持久状态视图可见。
- 很大的 ready 集合仍需 aging 排序；quick_check 与 operational 聚合也有增长成本。附件 recorded size 不代表扫描后的实际目录占用。
- 旧无 producer identity 的通知无法重建 chunk 关系；旧 NULL self_id 保留兼容，不自动归属账号。
- Phase 5 已具备继续开发的数据与运维基础：Inbox、Summary、授权、delivery effective status、outbox 状态与 worker health 可读取；这不等于生产上线验收通过。本阶段没有创建前端、public API、OCR、RAG 或任何群写能力。
