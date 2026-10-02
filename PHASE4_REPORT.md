# Phase 4 Implementation Report

日期：2026-10-02。范围：Phase 4A Dynamic Group Authorization、Phase 4B Delivery Automation；Phase 4C 另见 [PHASE4C_REPORT.md](PHASE4C_REPORT.md)。代码直接修改当前 working tree，未提交或推送。自动验证结果见第 18 节；真实 NapCat / QQ 验收仍待完成。

## 1. Baseline

基线为 `c9164c0`（Phase 3）。改动前用仓库虚拟环境执行全套测试：414 passed、1 warning。默认系统临时目录曾在 Windows 清理阶段出现 PermissionError，改用项目内专用 `--basetemp` 后完整通过。

保留原 FastAPI / OneBot / Repository / DeepSeek / private_outbox 边界。开发过程中发现 `config/config.yaml` 的群列表被外部修改，该修改已保留；本次没有编辑 `.env`、重启在线服务或直接迁移生产数据库。

## 2. Dynamic Group Authorization

新增按 `(self_id, group_id)` 隔离的 `group_authorizations`，记录 active、群名、加入／移除时间及更新时间。`/allow` 列出当前授权及保留历史的停用群；`/allow add <group>` 和 `/allow remove <group>` 都产生十分钟有效提案，经 `/confirm <id>` 才生效。

添加先用只读 `get_group_list(no_cache=true)` 验证机器人实际成员身份，再用 `get_group_info` 取群名。不能仅凭后者成功判断已入群：[NapCat GetGroupInfo](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/group/GetGroupInfo.ts) 含非成员群资料查询回退；[GetGroupList](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/group/GetGroupList.ts) 提供成员群列表。验证失败不授权。

群验证通过现有配置队列执行，避免在 WebSocket 接收循环中等待同一连接的 API 回包。新群默认 SUMMARY_ONLY；重新添加保留原 GroupPolicy，提案展示将恢复的 Summary、Inbox、Priority Watch 和附件下载能力。移除保留消息、Inbox、总结、策略及已下载附件。

## 3. Bootstrap migration

`config.groups.allowed` 只作为一次性 seed。合法 self_id 完成账号绑定后，在同一事务中导入并记录 `authorization_settings.bootstrapped_at`；空 seed 也记录完成。迁移本身不猜账号、不授权；重启或以后修改 YAML 不重新恢复已移除群。

## 4. Runtime authorization refactor

采集、群命令、历史调度／读取、附件下载、Triage、总结创建／提交、即时提醒和 Digest 选择均查询数据库动态授权，不使用启动时静态集合判断权限。`/groups` 只列 active 群，`/config`、`/sync` 拒绝停用群。

撤权使 queued/running 任务失败、待下载附件停止、待投递提醒取消；历史 gap 聚合状态同步收敛。消息写入、分类结果落库、总结提交均在事务内复核。历史批次与消息写入携带授权版本，阻止撤权后立即重加时旧工作继续入库。附件提交同样核对授权版本与任务状态。

ActionGateway 在实际发送群历史／文件读取请求前再次核验动态授权及连接账号。已经下载的历史附件可继续通过原有来源、路径、摘要及管理员校验后私聊转发。

## 5. Proposal architecture refactor

`ConfigurationProposalService` 用四个处理器统一分发 `group_policy`、`triage_preferences`、`group_authorization`、`delivery_preferences`。账号、管理员所有权、有效期、pending 状态、前置值和取消处理共用入口；配置修改与提案状态原子提交。确认时配置已改变则使旧提案失效。

`configuration_requests.target_group_id` 对群目标显式存值，对全局偏好为空；保留旧 `group_id` 列兼容历史数据库，业务不再用虚构群号作为全局配置目标。

## 6. Delivery architecture

新增 `app/delivery` 的模型、偏好、时间计算、渲染、Repository、Worker。Worker 使用现有数据库与私聊 outbox，不直接调用 QQ 发送接口。投递评估、延后、Digest 调度与发送确认均可从数据库恢复。

`delivery_revision_state` 保存每项修订的评估决定；`inbox_deliveries` 保存即时提醒快照、fingerprint、计划时间及状态；`digest_runs` 与 `digest_run_items` 保存窗口、事项修订和发送快照。

## 7. Inbox revision provenance

新增 `inbox_revision_events`，在 Triage 新建事项或产生实质修订的同一事务内记录 item、revision、triage job、self_id、group_id。复合外键约束来源关系。旧库不合成历史 revision 事件。

投递从 revision 对应任务及该事项实际引用的源消息取得 source_kind 和 event_time，不把回补入库时间当事件发生时间。只评估当前修订；被更新覆盖的未发送内容不会作为陈旧提醒投递。

## 8. Heartbeat

默认 60 秒，可配置 10–3600 秒。没有工作时不发送心跳消息。每轮协调已发送状态、标记 fast-track、评估新修订、处理延后提醒和 Digest；手动收信任务可提前唤醒处理。`/status` 提供授权数量、heartbeat、下一收信时间和延后数量。

## 9. Fast-track

重要关键词、重要发送者及确定性紧急词只加快既有 Triage 排队，不自行制造高优先级事项。默认且最低 30 秒 debounce；后续修正消息延后批次成熟时间，保留消息聚合、模型结构校验及并发上限。Priority 最终仍由现有分类链生成。

## 10. Urgent alert

要求 active 授权、非 ignore、Priority Watch 开启、投递开关允许、未归档且为配置内 HIGH / CRITICAL，并有 Phase 4 后可验证来源的修订。普通消息、Summary Only 默认策略、没有实质变化的重复评估不即时提醒。

Material fingerprint 包含 priority、category、deadline、action_required、规范化 title/action_text；忽略 summary、理由、confidence 和标点／空白变化。已投递同 fingerprint 不再创建提醒；升级或截止／行动变化可生成更新。入 outbox 前复核最新事项、授权、策略、期限及静默规则。

## 11. Quiet hours

默认 23:30–07:00；跨午夜按配置时区计算。HIGH 默认延后，CRITICAL 默认可突破，各自可配置。延后状态持久化，重启后继续；恢复发送前重新检查归档、撤权、降级、指纹变化和过期情况。多个自动 Digest 在静默结束后合并，避免一次发出多份旧窗口收信。

## 12. Scheduled Digest

默认每天 07:30、12:20、18:00、22:30。按配置时区及持久化游标排程，错过时段在默认 180 分钟 catchup 范围内合并补发；范围外旧时刻不逐条补发。

只使用启用 Phase 4 以后窗口内的新修订，默认排除已读、归档、停用群及 ignore 群；按 CRITICAL / HIGH / NORMAL / LOW 展示。已提前提醒的事项可以进入一次 Digest，并带标记；同一 revision 已入投递队列或已发送后不重复收信。

`/digest now` 复用同一窗口与去重规则，按命令消息去重，可显式绕过自动调度开关和静默时段。默认不发空 Digest；开启空收信时使用“基于已采集数据”的措辞，并保留未确认覆盖缺口警告。

## 13. Recovery alerts

依据源消息 event_time 判断年龄。默认最近 12 小时内且仍可行动的 HIGH / CRITICAL 可即时回补提醒；较旧但存在未来 deadline 的事项也可提醒。已过期限不即时发送。展示离线回补标记、事件时间及覆盖提示。

现有事项没有独立 `schedule_start` 字段，因此不能可靠判断仅含未来日程、无 deadline／行动要求的旧事项；采取保守策略进入 Digest，不凭模型文字推测未来时间。

## 14. Delivery Preferences

`/delivery` 显示设置；`/notify <描述>` 通过确定性短语／时间解析或现有 DeepSeek 严格 JSON 意图解析生成提案，再确认生效。支持 heartbeat、urgent priorities、fast-track、Digest 时间／开关／已读／空收信／群概览、静默、突破静默和 recovery 年龄配置。

Pydantic 拒绝未知字段、错误类型、越界数值、非 HH:MM 时间、空或超量收信时刻、同起止静默区间及未知 action。时间列表排序去重。模型只有意图解析能力，不能直接修改设置或授权。

## 15. Outbox crash safety

投递生产者状态、Digest 事项快照、文本与所有 outbox 分块在同一事务内保存；`(self_id, producer_kind, producer_key, producer_chunk)` 唯一索引防止重建相同分块。注入提交前错误时整体回滚。

启动后根据所有分块的 sent_at 协调 enqueued → delivered，继续复用原 outbox 重试。撤权取消 queued/deferred 提醒；已进入 outbox 的文本不做中途重写，保留既有可靠发送行为。

这里保证数据库侧幂等，不承诺 QQ 端端到端 exactly-once：QQ 已接收但回包丢失，或成功回包后、sent_at 提交前进程崩溃，仍可能重发。

## 16. Migration

迁移为加表／加列／加索引，并纳入既有数据库事务，可重复运行。保留 Phase 1–3 数据、已有配置提案、outbox、附件、Inbox 与总结。首次写入 Phase 4 cutoff，不补录旧 Inbox revision，也不在迁移阶段导入授权。

验证包括真实 Phase 3 schema 构造的升级测试、重复迁移、三处注入失败回滚。另以只读连接备份实际运行数据库至项目测试目录，重复迁移后 32 张原有表的 940 条记录逐列哈希一致，integrity_check 为 ok、无外键错误。该实际副本取得时已经是 Phase 4，因此它证明实际数据的重复迁移兼容性，不代表实际生产 Phase 3 快照升级测试。

## 17. Security

授权只能由管理员私聊确定性命令及确认完成。所有账号范围继续隔离；群聊内容、历史总结与模型输出均不作为工具指令。模型没有 OneBot、数据库、文件或发送工具。群聊仍只读，ActionGateway 拒绝群发送及管理动作。

唯一 QQ 出口仍是 ActionGateway，管理员私聊通知使用纯文本 segment；候选群资料查询不等于采集授权。没有读取或输出密钥，没有调用真实收费模型或发送真实 QQ 测试消息。

## 18. Tests

最终全量：**555 passed、1 warning，173.47 秒**；比基线增加 141 个通过用例。警告为既有 Starlette TestClient / httpx 弃用提示。执行命令：

```powershell
.\venv\Scripts\python.exe -m pytest -q --basetemp .pytest_tmp/phase4_final
.\venv\Scripts\python.exe -m ruff check app tests
.\venv\Scripts\python.exe -m compileall -q app
.\venv\Scripts\python.exe -m pip check
git diff --check
```

静态检查、编译、依赖完整性和 diff 检查均通过。完整测试日志保存于 `.pytest_tmp/phase4_final.log`。运行期审计确认 `groups.allowed` 只用于 application 初始化 seed，WebSocket `send_json` 仅位于 ActionGateway。新增覆盖主要位于：

- `tests/test_authorization.py`：bootstrap、命令／提案、恢复策略、撤权及各运行期权限边界。
- `tests/test_delivery.py`：提醒资格、指纹、更新、静默、重启恢复、Digest、回补来源、偏好及 fast-track。
- `tests/test_phase4_migration.py`：旧 schema 数据保留、幂等迁移、事务回滚。
- `tests/test_phase4_integration.py`：WebSocket API 往返、发送锁竞争、撤权／重加、确认及 outbox 原子性、Digest 快照。
- `tests/test_hierarchical_summary.py`：Phase 4C，详见独立报告。

原 Phase 1–3 测试保留；部分 fixture 改为显式建立账号／群授权，原静态白名单变更测试改为真实撤权操作，没有删除或跳过旧测试。

## 19. Manual verification

尚未执行真实 NapCat / QQ 人工验收，不把模拟 OneBot 通过视作现场完成。完整步骤见 [README Manual Verification](README.md#phase-4--4c-manual-verification)。重点核验：

1. 停服备份 data，启动后检查一次性导入；测试已入群但未授权群的添加、确认、默认 SUMMARY_ONLY 和消息采集。
2. 配置 Priority Watch，测试普通／高优先级、连续修正、截止更新、静默、重启后延后提醒、手动／定时收信。
3. 断开 NapCat 后制造测试消息，恢复后检查回补标签、event_time 和 coverage warning。
4. 撤权确认后验证不再采集或发起群读取；重启不恢复授权，旧 Inbox／附件／总结仍可查阅。

## 20. Remaining limitations

- NapCat / QQ history API **不能视为 100% 可靠的离线事件重放机制**；LIKELY_COVERED 不等于完整覆盖。
- 真实 NapCat 版本、QQ 展示、真实 DeepSeek 配额及现场流程仍待验收；模型分类与总结不能保证完全正确。
- 不提供 QQ 端 exactly-once；已入 outbox 的普通文本不会因后续撤权被重写。
- 无独立日程开始时间字段，回补日程采取保守策略；fast-track 仍受 heartbeat、debounce 和模型耗时影响。
- 添加群的验证请求完成与提案创建分别提交，极端崩溃可能产生重复待确认提案；确认的前置状态检查阻止重复生效。
- 保持单账号、单进程运行范围，不加入群回复、自动加群、Web UI 或多进程调度。
