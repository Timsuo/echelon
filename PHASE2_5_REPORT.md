# Phase 2.5 Implementation Report

范围：Reliability, History Recovery & Command UX。直接修改现有 working tree，未提交或推送。
`.env` 未读写，群白名单内容未改变；未实现 Phase 3 分类或 Phase 4 提醒。

## 1. Baseline

修改前完整测试：208 passed，0 failed，1 warning。
使用项目内 `.pytest_tmp` 避免 Windows 默认 TEMP 的已知清理权限问题。

## 2. Current bugs fixed

### /config 数字误判

只将首个完整数字 token 识别为显式 group_id，且必须已在白名单。
没有显式目标时，从当前账号的策略别名中唯一匹配；日期、天数、MB 等正文数字不参与群号判断。
相同别名或无匹配时返回候选并要求明确群号，LLM 不产生 group_id。
测试覆盖“14天”“10月15日”“100MB”、显式群号、未知群号、同名别名。
此修复不代表增加了自动到期策略或每群 MB 阈值，帮助和 README 已说明。

### FileResolver transient URL cleanup

消息/附件事务内按实际下载状态更新内存缓存，只有 pending 且无 file_id 的附件才保留直链。
skipped / failed / downloaded 不重新缓存，已有终态缓存清除；downloading 不被重复事件打断。
缓存仍限1000条。下载 worker 原有终态清理保持。
新记录移除已识别 file segment、CQ file、group_upload 的 URL 字段，不新增 SQLite 临时 URL 存储。
旧原始事件不批量重写，附件下载与转发不依赖其旧地址。

### 取消期间事务可靠性

测试发现 BEGIN 可能已在 SQLite 线程执行，而等待者此时被取消，原异常保护尚未生效。
将 BEGIN 纳入事务异常保护，并在 Starlette/AnyIO 取消期间保护回滚和断连状态落盘。
新增取消回归测试，确保后续事务可以继续执行。

## 3. History Recovery

- realtime：原实时接收链，ingest_source=realtime。
- reconnect：验证后的 WebSocket 重连结束 gap、创建持久任务；HistoryWorker 在稳定期后有限拉取，ingest_source=history_recovery。
- periodic：priority 默认120秒、inbox 300秒、summary_only 1800秒；ignore 不轮询。以有限最新记录重叠回查，ingest_source=history_poll，无成功通知刷屏。
- manual：/sync [群号] 创建持久任务，立即回复入队，后台完成后通知；ingest_source=history_recovery。

历史 Action 经过 ActionGateway → HistoryAdapter 验证 → EventProcessor → normalize → Repository.add_message。
所有路径复用 UNIQUE(self_id,group_id,message_id)，历史重复不覆盖旧消息、不重复产生 Inbox。
历史路径拒绝私聊，不执行旧 /config 或 /summary。历史附件遵守相同白名单、策略和大小限制；无可用引用时附件失败而消息保留。

接口只拉有限 N 条，不做递归分页。兼容字段参考 [NapCat 官方 Action 源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts)，差异集中在 app/onebot/history.py。
count 最大500；重连默认200，周期/手动默认100。窗口额外重叠默认300秒。
瞬时连接错误、超时、provider 拒绝最多额外2次；无效响应外层、身份不一致、非白名单、ignore 等直接失败。
任务 attempts 持久保存，重启不重置 API 重试预算。

## 4. Gap Detection

已验证连接断开 → collection_gaps 创建 open 窗口；重连 → ended_at 落盘，并按当前允许且非 ignore 群创建 reconnect 任务。
连接 session 标识防止旧连接的异步清理误关闭新连接状态。
如果新握手先于旧连接清理完成，则根据上次接收/连接检查点保守补建 gap，避免快速重连遗漏缺口；新增回归测试覆盖该时序。

正常服务停止记录 service_clean_shutdown=true、last_service_stop 和 service_stop gap。
启动设置 clean_shutdown=false。旧运行存在连接证据时，使用停机时间或最后实时接收/连接检查点推断离线窗口；无证据的首次启动不虚构历史缺口。
进程崩溃/断电标为 offline_window_unclean；正常停机为 service_stop / offline_window_clean。
缺口一直延伸到实际 NapCat 重连，因此包含 Python 已启动而 NapCat 尚未在线的阶段。
running 历史任务启动后恢复 queued；worker 随生命周期 cancel 并 gather。

恢复报告默认等60秒连接稳定期，合并连续未报告 gap，通过账号隔离的 private_outbox 投递。
报告和已报告标记同事务保存。断线期间不尝试通过离线 NapCat 即时通知。

## 5. Coverage

| 状态 | 判断 |
| --- | --- |
| likely_covered | 有效历史最早时间不晚于窗口开始、最晚时间不早于窗口结束，且无无效记录；仅时间范围证据 |
| partial | 有有效消息但未覆盖窗口两端，或部分消息无效 |
| unknown | 空记录、无法确定有效时间、或没有可执行恢复群 |
| failed | 验证失败或重试耗尽 |
| pending / running | 仍未核验完成 |

不会将 gap 标为 complete。每群 job 记录获取/新增/无效数量、最早/最晚时间及覆盖结论，gap 汇总采用保守结果。
`/summary` 按 self_id、group_id、时间窗口查询未确认 gap，由 Python renderer 增加“数据覆盖警告”；不阻止摘要，不让 LLM 决定是否警告。
没有重叠或另一账号的 gap 不会污染该摘要。其他群恢复良好也不能证明本群完整。
HistoryRepository.unresolved 可供未来 Inbox 按关联源消息时间查询，没有生成假的优先级。

## 6. Commands

- /help：分组首页。
- /help <command>：全部14个命令都有用法、说明、例子和副作用；注册和展示共用 CommandSpec。
- /coverage：连接、最后实时接收、最近缺口及恢复数量、周期核验、未确认缺口数。
- /sync：全部白名单非 ignore 群有限同步；/sync <群号>：指定群。
- 未知命令只引导 /help；普通私聊不进入命令系统；所有命令仍限 ADMIN_QQ。

原 /status、/summary、Inbox、GroupPolicy 命令继续保留，README 新增 Command Reference 和完整可靠性说明。

## 7. Migration

在现有启动迁移事务中幂等增加：

- messages.ingest_source，旧行默认 realtime；合法值 realtime / history_recovery / history_poll。
- collection_gaps。
- history_sync_jobs。
- history_sync_state（self_id, group_id 唯一）。

现有 messages、attachments、Inbox、group_policies、summaries、outbox 等数据保留。
测试逐表对比旧 Phase 2 数据、重复迁移，并注入迁移异常验证新增字段和新表同时回滚。
真实生产数据库未为了测试而启动或迁移；下次正常启动自动应用，无需删 DB。

事件原时间保留；last_received_at、last_message_event_time、last_realtime_received_at 分离，另存账号时间键。
旧 last_event_time 保留实时接收兼容语义，历史回补不会更新它。
未来提醒必须使用 event_time，而非历史回补的 received_time。

## 8. Security

人工搜索确认 app 内 OneBot action 的唯一 WebSocket send_json 仍位于 ActionGateway。
READ_ONLY_ACTIONS 只有 get_group_file_url / get_group_msg_history；私聊输出仍只有 send_private_msg / upload_private_file，目标仍必须 ADMIN_QQ。
没有允许 send_group_msg、upload_group_file、trans_group_file、delete_msg、set_group_* 或任何未知动作。
历史读取校验当前连接账号、数据库绑定、白名单、count 和响应身份；命令/任务/覆盖查询继续按 self_id 隔离。
LLM 没有获得 history、OneBot 或数据库修改权限。附件仍为 opaque binary，不解析、不发送给 DeepSeek。

## 9. Tests

最终：**298 passed，0 failed，1 warning**。新增90个参数化后用例。
唯一 warning 为原有 Starlette TestClient 对 httpx 的弃用提示。

已执行：

```powershell
.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp
.\venv\Scripts\python.exe -m compileall -q app
.\venv\Scripts\python.exe -m ruff check app tests
.\venv\Scripts\python.exe -m pip check
git diff --check
```

全部通过。包括真实本机 uvicorn 启动、模拟 OneBot 重连、延迟历史 API 同时实时采集、恢复报告、迁移、取消回滚、来源与去重、策略周期、有限重试、覆盖警告、URL缓存和帮助命令。
没有删除/skip/xfail 旧测试。两处旧测试的必要调整：

1. test_identity 的模拟 Services 增加 history 依赖；原有 detach / connected / action 安全断言不变。
2. test_policies 中“123 456 mode inbox”的目标期望改为123，符合“只解析首个目标 token，正文数字不是群号”的新要求；未知群和无明确目标仍拒绝。

## 10. Manual Verification

1. 备份 data，停止旧实例后运行 start.bat；保持原 NapCat Reverse WebSocket / Token / X-Self-ID。
2. /help、/help coverage、/coverage，记录初始状态。
3. 暂停 NapCat WebSocket 或关闭 NapCat；白名单非 ignore 群发送测试消息，等待约2分钟。
4. 恢复 NapCat，观察 gap 结束、持久恢复任务创建，等待默认60秒稳定期及 API 处理。
5. 检查补入消息、event_time、ingest_source 和唯一键去重，收到采集恢复报告。
6. /coverage 确认保守状态；/summary 30m 在未确认 gap 重叠时应显示警告。
7. /sync <群号> 验证后台核验；周期核验只静默补入，不发每轮通知。
8. 正常重启验证 clean shutdown / service_stop；只在测试环境模拟异常退出并检查任务恢复。
9. 测试别名加日期/天数/MB 数字，仍须检查配置 diff；没有新增自动到期或每群阈值功能。

## 11. Remaining limitation

> NapCat / QQ history API cannot be treated as a guaranteed complete offline event replay mechanism.

- 每次只回查有限最新记录；离线窗口太长、高流量、接口缓存或版本差异均可能导致 partial / unknown / failed。
- likely_covered 只证明时间范围覆盖，无法证明区间内每条消息均已返回；安静群也可能保守显示 partial。
- 崩溃时刻只能根据检查点推断；数据库不可写时无法保证运行状态记录成功。
- 单条已入库而 job 计数尚未保存时崩溃，新增计数可能低估；消息本身仍去重保留。
- 周期核验只是静默修补，不能证明从未漏报；没有离线期间的其他通知通道。
- 缺少稳定 file_id 的附件直链仍不能跨重启可靠重取；本次不会突破大小限制或扩大下载 URL 权限。
- 尚未替代真实 NapCat 版本联调；接口不支持时会有限失败并明确展示，不承诺完整离线恢复。

主要新增模块：app/onebot/history.py、app/history/、app/storage/history_repository.py、app/storage/history_schema.py、app/commands/help.py、app/commands/history.py。
没有实现 priority/category/labels/deadline、Semantic Watch、Heartbeat、Digest、Web UI 或群写操作。
