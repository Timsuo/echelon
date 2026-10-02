# Phase 4C Implementation Report

日期：2026-10-02。代码、README 和自动测试已在当前 working tree 实现；真实 QQ 阅读体验与 NapCat 流程仍待现场验收。Phase 4A / 4B 见 [PHASE4_REPORT.md](PHASE4_REPORT.md)。

## 1. Baseline

从 Phase 3 `c9164c0` 的 414 个通过测试出发，与本次动态授权／自动投递改造集成。沿用原 SummaryService、DeepSeekClient、SQLite 和 private_outbox，不增加模型提供商、外部存储或递归代理。

## 2. Summary Schema v2

保留 LegacySummaryData / LegacyTopic 读取 v1。新模型响应为严格 v2：每个 Topic 必须有 start_message_id、end_message_id，可含 notable_message_ids、continuation、importance 和 category。未知字段和枚举拒绝。

模型响应结构不接受自行生成的 topic_start / topic_end。Python 验证后写入 StoredSummaryData / TimedTopic，包括实际时间与 duration_seconds。CompactSummaryData 单独存储重点话题、决定、待办、事件、不确定事项、折叠数量及覆盖标记。

## 3. Topic Time Provenance

所有边界与参考 ID 必须存在于当前 self_id／group_id／window 的原始消息集合；时间通过 ID → 原消息 event_time 映射取得，拒绝不存在的 ID 或逆序边界。持续时长为 end − start，与消息入库时间无关。

渲染按配置时区显示起止与分钟／秒；跨日增加日期，跨年增加年份。时间溯源是确定性的，但模型选择的话题边界是否最贴切仍依赖模型理解。

## 4. Summary Continuity

每次总结以当前窗口 Raw Messages 为主要事实源，再加入有限近期旧总结以理解术语、事项延续和未决问题。没有当前消息时直接生成空窗口总结，不调用模型、不用旧总结制造新讨论。

不创建 Summary-of-Summary 循环，不通过旧总结替代当前原文，也不新增任意层级压缩调用。

## 5. Previous Summary Context

仅查询同 self_id、同 group_id、已完成且 window_end ≤ 当前 window_start 的总结；默认最多 3 份、回看 48 小时。只传结构化主题／决定／待办等字段，不传已渲染文本或其指令式装饰。

Prompt 明确把 `previous_summaries` 标为不可信模型上下文。**Previous Summary 不是权威事实源**，不能提升为系统指令或补造当前窗口事实。先检查当前原文数量和长度，再按预算剔除较旧上下文；原文超限仍要求缩短窗口，不能利用旧总结绕过限制。

## 6. Detailed Summary

Detailed 保存全部经校验的结构化内容和完整 rendered_text，包含话题时间、讨论内容、决定、待办、重要事件、不确定事项与参考 ID。先获得实际 summary ID，再渲染链接，避免失败任务导致 job ID 与 summary ID 不一致。

Detailed 是长期保留的完整记录，不被 Compact 覆盖或自动删除；与任务完成和默认通知在同一事务中保存。没有修改既有用户数据删除／备份责任。

## 7. Compact Summary

默认 Detailed 超过 1600 字符时发送 Compact；短总结默认发送 Detailed。Compact 目标约 1000 字符、最多 5 个重点话题，可在 SummaryConfig 调整。

采用本地确定性结构压缩：按 importance 排序、抽取完整句子、折叠完整话题；极长话题使用完整总结指针，不截取渲染文本前缀。保留全部决定、待办、重要事件和不确定事项，附折叠数量和 `/summary detail <id>`。Compact 不显示昵称长列表和参考消息 ID。

**Compact 只是默认视图**。目标长度为软限制：必要行动内容本身很长时保留完整事实，并使用原 outbox 分块，不能为了长度静默删掉截止或待办。

## 8. Compression Fallback

本次选择本地压缩方案，未启用可选的第二次 LLM 压缩，不增加 DeepSeek 请求次数。压缩异常时构造保留行动字段、覆盖警告及 detail 入口的备用视图，任务仍完成，Detailed 仍保存。

**Compact 失败不会丢掉 Detailed**。旧结构无法解析时也提供完整旧总结入口；存在缺口时继续展示覆盖警告。读取已保存总结不重新调用模型。

## 9. Emoji Visual System

`app/rendering/icons.py` 集中 priority、category、label、status 和 coverage 图标映射。Summary、Inbox、Alert、Digest 使用相同语义，例如 CRITICAL 🚨、HIGH 🟠、NORMAL 🔵、LOW ⚪、schedule 📅、material 📎。

图标由 Python renderer 选择，模型只给枚举；模型展示正文中的 Emoji 会过滤，原结构化事实保留。文本标签独立可读，Emoji 只辅助扫读。Detailed 参与者最多 5 人全列，超过时显示前三人及总数；Compact 不列昵称。

## 10. Summary Commands

新增：

- `/summary list`：最近 10 份总结、群名、时间、消息／话题数量和 detail 入口。
- `/summary detail <id>`：已保存完整渲染结果。
- `/summary compact <id>`：已保存 Compact，旧数据则本地生成并缓存。
- `/summary topic <id> <序号>`：单个话题的完整信息。

命令严格校验参数、管理员入口和 self_id；不会调用模型。群名优先 alias，其次缓存 group_name，最后 group_id。原 `/summary <window>` 创建方式继续使用。

## 11. Legacy Compatibility

旧行默认 schema_version=1，不重写原 summary_json 或 rendered_text。旧 detail 原样读取；旧 compact 从 v1 结构本地生成，无法解析则保留 detail 入口。旧话题没有真实边界来源时不伪造时间。

撤权后的总结仍可读取，列表注明已停止接收。其他账号的 summary ID 不可读取；空列表、未知 ID、无效序号均有明确反馈。

## 12. Dynamic Authorization Integration

新任务创建、执行及最终提交均复核 active 授权与 summary_enabled；ignore 或撤权禁止新总结。撤权把 queued/running 总结标记失败；即使推理已在外部进行，返回结果也不能提交为新总结。

旧资料查询不要求当前群仍 active，因此停止采集不影响历史保存与阅读。active summary_only 群可正常创建总结。

## 13. Digest Integration

`digest_include_group_summaries` 默认 false。开启时，仅为 active summary_only 群展示窗口内最新一份简短概览：最多两个话题标题、话题数量和 detail 入口。

不复制整份 Detailed 或 Compact，不把群总结变成 urgent alert。Digest 原有 coverage 规则及按账号隔离保持有效。

## 14. Migration

在 summaries 上增量增加 schema_version（默认 1）、compact_json、compact_rendered_text。新结果写 v2；不批量重算旧总结，不触发模型调用。迁移与 Phase 4 共享事务和重复执行保护。

旧 schema 测试证明旧列及数据保留；实际运行库只读副本的重复迁移校验通过。该副本取得时已是 Phase 4，不能据此声称实际生产 Phase 3 升级已现场验收。

## 15. Security

当前聊天记录和 Previous Summary 都是非可信数据。Summary LLM 无工具，只能返回严格 JSON；不能调用 OneBot、修改数据库／授权／策略／投递配置、读文件或发送 QQ。本地 Compact 无模型调用及工具执行入口。

ID 和时间由当前消息集合校验；coverage warning 由 Python 根据数据库决定，不能被模型声明“完整”覆盖。渲染结果仅经既有管理员私聊 outbox 发送。

## 16. Tests

最终全量：**555 passed、1 warning，173.47 秒**；原有警告为 Starlette TestClient / httpx 弃用提示。Ruff、compileall、pip check 和 git diff --check 均通过。命令与日志位置见 [Phase 4 Tests](PHASE4_REPORT.md#18-tests)。

`tests/test_hierarchical_summary.py` 覆盖 schema v2、边界 ID、时间顺序／跨日、历史上下文账号和窗口隔离、数量／预算限制、无原文不调用模型、Detailed 保存、默认视图选择、压缩失败、命令／旧格式／撤权兼容、参与者限制、统一图标、Digest 概览及实际 summary ID。

`tests/test_phase4_integration.py` 验证推理期间撤权不提交总结；旧 Phase 1–3 测试继续保留。所有模型与 OneBot 行为使用受控替身，未执行真实收费调用。

## 17. Manual Verification

尚未执行真实 QQ 验收。按 README 的人工清单检查：活跃群 `/summary 2h` 的真实话题起止／时长；长总结默认 Compact；detail／compact／list／topic 可读且不重新调用模型；稍后窗口理解话题延续但不复述旧事实为新讨论；多人话题不刷满昵称；缺口窗口仍有警告；撤权后不能生成新总结但仍能读取旧总结。

QQ 客户端 Emoji 字体、分段显示和整体扫读效果需要真实客户端确认，自动字符串测试不能替代这一步。

## 18. Remaining Limitations

- **Summary 不能保证模型理解完全正确**；ID／时间真实性校验不证明模型语义或话题边界选择一定正确。
- Previous Summary 仅为不可信辅助上下文，当前 Raw Messages 始终是主要事实源。
- 本地 Compact 没有独立模型再概括能力；保留必要行动字段时可能超过软长度目标。
- 旧 v1 总结没有足够来源时不补造讨论时间；异常旧结构只能查看原 Detailed。
- Detailed 长期保留、Compact 只是视图；压缩失败不丢失完整结果。
- **NapCat history 仍不能保证完整离线重放**；LIKELY_COVERED 不是完整性承诺。
- 真实 NapCat / QQ / DeepSeek 现场验收尚未完成，不能将自动测试通过等同全部上线验收完成。
