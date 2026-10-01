# 梯队系统 / Echelon

**Short tagline**

把群聊变成收件箱。

Turn noisy group chats into an intelligent inbox.

**Project subtitle**

梯队系统 —— 面向群聊信息过载的智能信息聚合与优先级管理系统

Echelon — An intelligent information aggregation and prioritization system for group chats.

**Repository name**

`echelon-inbox`

以上为项目 metadata / naming suggestion；实际仓库仍为 [Timsuo/echelon](https://github.com/Timsuo/echelon)。

**GitHub Description — 中文**

一个面向 QQ 群聊的信息聚合与智能收件箱系统：自动收集群消息、通知与文件，通过 LLM 进行摘要、分类、优先级判断和标签管理，并以定时收信与高优先级提醒的方式降低群聊信息过载。

**GitHub Description — English**

An intelligent inbox for QQ group chats that collects messages, notices and files, then uses LLMs to summarize, classify, prioritize and organize them into actionable information.

**Topics**

`qq` `napcat` `onebot` `llm` `ai` `message-aggregation` `smart-inbox` `notification` `summarization` `automation`

以上描述包含项目愿景；分类、优先级、标签已在 Phase 3 实现，自动投递仍未实现，具体以 Current Features 和 Roadmap 为准。

## Why Echelon

把群聊里的文件和可追溯信息整理成可查看、可归档的收件箱，减少反复翻找群记录的成本。
被监听群始终是只读来源，交互仅面向管理员私聊。

## Current Features

### Phase 1 — Implemented

- 白名单群消息采集、标准化、SQLite 去重持久化。
- 管理员 `/status`、`/summary 30m`、`/summary 2h`、`/summary today`。
- DeepSeek 严格 JSON 总结、有限重试、默认关闭 thinking。
- self_id 隔离、握手身份校验、Action Firewall、提示词注入防护。
- 持久私聊 outbox、任务恢复、Windows 进程锁与长期运行。

### Phase 2 — Implemented

- file 消息段与 group_upload 通知的附件元数据、幂等记录和异步下载。
- 大小限制、安全文件名、临时文件、SHA-256、原子改名、有限重试和重启恢复。
- 文件驱动的 InboxItem，多对多关联原始消息与附件；普通聊天不自动进入收件箱。
- `/inbox`、`/inbox unread`、`/detail`、`/archive`、`/file`。
- 文件仅转发给 ADMIN_QQ，不解析附件内容，不调用 LLM 分类。
- Group Policy：`summary_only`、`inbox`、`priority`、`ignore`，默认只总结。
- `/groups`、`/group`、`/config` → 配置提案 → `/confirm` 或 `/cancel`；自然语言后台解析。

### Phase 2.5 — Reliability / History Recovery / Command UX — Implemented

- `/config` 只将首个独立数字 token 作为显式群号，正文的日期、天数和 MB 数字不影响唯一别名匹配。
- 断连/停机缺口持久化、重连有限回补、在线低频重叠核验、手动 `/sync`。
- `ingest_source` 区分实时与历史；`/coverage` 展示保守覆盖状态，总结对未确认缺口添加 Python 生成的警告。
- `/help` 分类首页及全部命令详情来自统一 CommandSpec，与路由共用注册信息。
- 所有恢复均为 best effort，没有完整离线重放保证，未实现 Phase 4 紧急提醒。

### Phase 3 — Intelligent Triage & Personal Preferences — Current

- inbox / priority 群的新消息持久排队，经 debounce 和有界批处理后交给 LLM 聚合成 0..10 个事项。
- 固定分类、优先级与标签，行动要求、截止时间、简短判断依据；合并近期同群条目与已有文件条目。
- `/prefs`、`/pref` → typed proposal → 共用 `/confirm`、`/cancel`，偏好按账号隔离。
- 来源 ID、合并 ID、严格 Schema 与相对日期校验；结果和来源归属同事务应用。
- Coverage 警告由 Python 决定，保留历史 event_time；revision 为 Phase 4 投递去重准备。
- **分类结果仅进入 Inbox，不发送即时提醒，不实现 Heartbeat 或 Scheduled Digest。**

## Architecture

Windows 本地长期运行的 QQ 群聊只读采集与管理员私聊总结服务。

数据链：NapCatQQ → OneBot 11 Reverse WebSocket → 群白名单与标准化 → SQLite；
管理员 `/summary` → SQLite 任务队列 → DeepSeek → Pydantic 校验 → Python 排版 → 管理员私聊。

附件链：群文件事件 → 白名单 → GroupPolicy → adapter 标准引用 → messages + attachments 同事务保存；
启用 Inbox 时关联 InboxItem，启用下载时由 AttachmentWorker 保存安全本地文件。
私聊 `/file` → Inbox/附件账号与状态验证 → 持久 outbox → ActionGateway 再校验路径、SHA-256 和账号 → ADMIN_QQ。

配置链：管理员 `/config` → Python 解析唯一目标群 → 本地明确命令 / DeepSeek 严格 JSON → 校验 → 持久提案 → `/confirm` → group_policies。
自然语言解析使用独立 SQLite 队列和异步 worker，不阻塞 Collector；LLM 没有数据库或 OneBot 权限。

## 环境和安装

- Windows 10/11，Python 3.12+（当前验证环境为 Python 3.14.6）。
- 已登录 QQ 的 NapCatQQ，启用 OneBot 11 Reverse WebSocket Client。
- DeepSeek API Key；可以暂时留空，此时采集、`/status` 和无消息窗口总结仍可使用。

在 PowerShell 中进入项目目录（路径可以有空格）：

```powershell
Set-Location 'E:\QQ group chat monitor'
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
notepad config\config.yaml
```

如果激活脚本被系统策略阻止，可直接使用 `.\venv\Scripts\python.exe -m pip install -r requirements.txt`，不必改变系统策略。
当前目录已建立 venv 并安装依赖，可直接填写配置。以后重建环境时执行上面的步骤。
`requirements.lock.txt` 记录本次测试的依赖版本；复现可用
`.\venv\Scripts\python.exe -m pip install -r requirements.lock.txt -e '.[test]'`。

`.env` 内容：

```dotenv
DEEPSEEK_API_KEY=填写你的DeepSeek密钥
ONEBOT_ACCESS_TOKEN=填写一段足够长的随机令牌
ADMIN_QQ=填写管理员数字QQ号
```

这些是占位说明，不能直接使用。系统环境变量优先于 `.env`。`.env`、数据库、日志、虚拟环境均已加入 `.gitignore`。
Access Token 和管理员 QQ 必填；空 Token 会拒绝启动。程序不会输出密钥值。
不要让管理员 QQ 与机器人 QQ 相同，管理员需能向机器人发送私聊。

`config/config.yaml` 示例：

```yaml
groups:
  allowed: [123456789, 987654321]
timezone: Asia/Shanghai
websocket:
  host: 127.0.0.1
  port: 8789
  action_timeout: 15
deepseek:
  model: deepseek-flash
  thinking: false
  timeout: 60
  retries: 2
  max_messages: 1000
  max_input_chars: 60000
logging:
  level: INFO
attachments:
  enabled: true
  storage_dir: data/attachments
  auto_download: true
  max_auto_download_mb: 100
  retry_count: 2
inbox:
  default_page_size: 10
```

确认 groups.allowed 中的群号符合自己的监听范围；配置修改后重启。配置路径始终相对于项目根目录，不依赖终端当前工作目录。
默认模型可修改，当前名称参考 [DeepSeek JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)。
API 地址固定为 `https://api.deepseek.com`，不使用 OpenAI API Key。
普通总结默认显式发送 `thinking.type=disabled`；设置 `deepseek.thinking: true` 后发送 `enabled`。
参数格式参考 [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。

`max_auto_download_mb` 范围 1–1024，默认 100 MiB；`retry_count` 为额外重试次数，范围 0–5，默认 2。
`default_page_size` 范围 1–50，默认 10；非法配置会拒绝启动并报告字段名/错误类型。
`storage_dir` 应为专用目录，支持项目相对路径或绝对路径，不能包含 `..`，不能使用符号链接或 Windows junction。
`attachments.enabled: false` 关闭新附件记录及自动 Inbox 创建；群消息照常保存。
`auto_download: false` 仍保存附件；群策略允许时创建 Inbox，但下载标记 skipped。没有手动绕过大小限制或重新下载命令。

## NapCat 配置

1. 在 NapCat WebUI 的 OneBot 网络配置中添加 **WebSocket 客户端 / Reverse WebSocket Client**。
2. URL：`ws://127.0.0.1:8789/onebot/v11/ws`。
3. Token / Access Token 填写与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 完全一致的值。
4. 开启连接，建议重连间隔 3000–5000 ms，启用正常的心跳。
5. 推荐消息格式为 array / 消息段；string / CQ 字符串也能解析。
6. 必须使用事件和 API 响应共用的 **Universal 双向连接**；不要分开建立 Event 与 API 连接。
7. 保持 NapCat 在线。仅运行 Python 不会自动启动 NapCat，也不会登录 QQ。

Phase 2 需 NapCat 支持 `get_group_file_url` 和 `upload_private_file`，Phase 2.5 另需 `get_group_msg_history`；并与 Python 运行在同一电脑、
能访问同一个附件绝对路径。`group_upload` 通知中的 id/name/size/busid，以及 file 消息段字段，
均在 `app/onebot/files.py` 适配。参见 [NapCat 文件 API](https://napneko.github.io/onebot/api)
和 [群文件事件](https://napneko.github.io/onebot/event)。

不同 NapCat 版本标签可能不同，参见 [NapCat 配置指南](https://napneko.github.io/config/basic) 和
[OneBot Reverse WebSocket 协议](https://github.com/botuniverse/onebot-11/blob/master/communication/ws-reverse.md)。
本项目支持 `Authorization: Bearer TOKEN`、`Authorization: Token TOKEN`，以及兼容的 `access_token` 查询参数；优先使用请求头。
必须提供合法的 `X-Self-ID` 请求头。鉴权后，在数据库事务中绑定首个账号或校验已有绑定，
全部通过后才 accept 并 attach；缺失、非法或不匹配的账号会在握手阶段被拒绝，不会收到待发私聊。
事件仍检查 `self_id`；不匹配时立即 detach，再关闭连接。
如果提供 `X-Client-Role`，值必须为 `Universal`。字段兼容逻辑集中在 `app/onebot/adapter.py` 和 `normalizer.py`。

## 启动

```powershell
.\start.bat
```

或者直接 `.\venv\Scripts\python.exe -m app.main`。按 Ctrl+C 正常停止。
程序监听 `127.0.0.1:8789`；无 Web 管理页面。`start.bat` 对含空格路径作了引用。
不要使用 uvicorn `--reload` 或多个 workers。

启动日志在终端和 `logs/app.log`，单文件约 5 MB，保留 5 个轮转文件。
SQLite 位于 `data/messages.db`，自动初始化 WAL、NORMAL、foreign_keys 和 5000 ms busy_timeout。

## 首次验证

1. 启动 Python 和 NapCat，日志应出现 `Service ready`、`WebSocket connected`。
2. 在白名单群里发送一条普通文字和一条图片消息；应记录文字和 `[图片]`，不会调用图片识别。
3. 使用 SQLite 工具检查，或运行：

   ```powershell
   .\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/messages.db'); print(c.execute('SELECT group_id,message_id,normalized_text FROM messages ORDER BY id DESC LIMIT 5').fetchall())"
   ```

4. 管理员私聊机器人 `/status`：应看到 Connected、消息数、监听群数、任务和运行时间。未调用 API 时 DeepSeek 为 Unknown。
5. 管理员私聊 `/summary 2h`：先收到任务编号；后台完成后，每个白名单群分别返回一份总结。
6. 在其他群发消息，应有过滤日志但数据库不增加；非管理员私聊不会触发任务或回复。
7. 停止、重启 Python：历史消息保留。重发相同 OneBot 消息仍只有一条记录。
8. 模拟 DeepSeek 不可用：有消息的总结任务失败，但新的群消息继续入库。修复凭据后重新发送 `/summary 2h`。

`/summary 30m` 和 `/summary today` 也支持；today 按配置时区的本地零点计算。
窗口为 `[开始, 命令接收时间)`，使用消息的 OneBot event time 查询。最多 7 天。
为白名单中 summary_enabled 且非 ignore 的群各创建一个任务，超过 100 个待处理任务时拒绝入队。
超出消息数量或输入字符上限时明确失败，不静默截断。无消息窗口不调用 API。
`/watch`、`/ask` 仅预留扩展位置，当前不实现。

## Windows 任务计划程序

建议分别管理 NapCat 和 Monitor 两个任务，并确保 NapCat 的运行用户具有有效 QQ 登录状态。

1. 创建任务，例如 `QQ Monitor`，使用自己的 Windows 用户运行；一般无需最高权限。
2. 触发器选择“用户登录时”；需要开机运行可选“启动时”并延迟约 30 秒。无人登录时 NapCat 能否工作取决于其部署方式，请单独验证。
3. 推荐直接运行 Python，避免 cmd 子进程影响任务停止行为：
   - 程序：`E:\QQ group chat monitor\venv\Scripts\python.exe`（通过浏览选择）。
   - 参数：`-m app.main`。
   - 起始于：`E:\QQ group chat monitor`（此字段通常不加引号）。
4. 如需运行批处理：程序 `C:\Windows\System32\cmd.exe`，参数 `/d /s /c ""E:\QQ group chat monitor\start.bat""`，起始于同上。
5. 设置“任务失败时每 1 分钟重新启动”，例如尝试 999 次；按需要启用错过计划后尽快启动。
6. 取消“运行超过 3 天后停止”等运行时长限制；按需要取消仅交流电运行条件，并配置电脑不自动睡眠。
7. “如果任务已在运行”：选择 **不启动新实例**。不要同时手动启动另一个服务。
8. 先手动运行任务，验证最后结果、日志和 `/status`，再重启电脑验收。

程序使用 Windows 兼容的 OS 文件锁 `data/monitor.lock` 拦截同一项目的第二实例，进程退出后自动释放；残留锁文件本身不代表被占用，无需删除。
端口也只能由一个进程监听。锁保护以项目数据目录为边界，不要复制一份项目另开端口运行同一机器人。

## 安全边界与故障行为

- `ActionGateway` 是唯一 WebSocket 写入 OneBot action 的位置。READ_ONLY_ACTIONS 仅含 `get_group_file_url` / `get_group_msg_history`，
  PRIVATE_OUTPUT_ACTIONS 仅含 `send_private_msg` / `upload_private_file`；任何其他 action 首先抛出 `PermissionError`。
  不开放当前不需要的目录查询、`download_file` 或任何群文件修改 API。
- 私聊目的地必须等于 ADMIN_QQ。文字固定为 text segment，不解释 CQ 内容；文件调用只接受附件数据库 ID 与账号，
  不接受任意路径。Gateway 从数据库重取文件，验证连接账号、白名单群、下载状态、存储根目录、确定性路径和 SHA-256 后才发送。
- `/file` 只接受两个正整数，不能传入路径。文件名经清理并添加附件 ID 前缀，目录按 self_id/group_id 分隔。
  拒绝路径逃逸、符号链接、junction、reparse point、非普通文件和硬链接；附件目录须仅允许可信本机用户修改。
- 群记录只读，不删除、撤回、禁言、踢人，也不自动处理好友或入群请求。
- 模型没有工具、QQ 接口或凭据访问权限。聊天记录始终作为不可信数据，system prompt 明确禁止执行其中指令。
- 输出必须通过严格 JSON schema 和引用消息 ID 检查，才进入 renderer；防注入不意味着总结不会出现事实错误。
- 所有 LLM JSON / Schema 校验失败只记录 schema 名、响应长度、结束原因与错误类型；不记录群聊原文、模型完整回复、偏好全文或密钥。旧版产生的日志不会自动重写，应限制本机访问权限。
- SQLite 中 `UNIQUE(self_id,group_id,message_id)` 防重复；原始事件 JSON 和标准化内容同时保存。
- 任务通过 SQLite 原子 UPDATE…RETURNING 认领，两个数据库连接不会领取同一任务。当前只支持单进程、单总结 worker。
- 启动时在独占进程锁内把遗留 running 任务恢复成 queued。中断前已经发出的 DeepSeek 请求可能被重新调用并产生额外费用。
- DeepSeek 网络、超时、限流和服务端错误采用指数退避重试；401/其他不可恢复错误直接失败。retry_count 记录实际执行的重试次数。
- 空内容（含 None/空白）、空 choices、JSON 解析或严格 Schema 校验失败、`insufficient_system_resource` / `aborted` 会有限重试。
  API 和模型输出共用 `deepseek.retries`（默认 2，即总共最多 3 次请求），不会嵌套放大重试次数。
  `length` 直接失败并提示缩短时间窗口；`content_filter`、`tool_calls`、未知结束原因和普通程序 ValueError 不重试。
- 创建总结任务时校验当前绑定账号，并将 self_id 保存到 summary_jobs。worker 始终按 job.self_id 查询，
  summaries 同时保存该 self_id；即使排队后切换账号，也不会混合不同账号的消息。
- 总结、completed 状态和待发通知在同一事务保存；QQ 断开后通知保留，重连自动继续发送。completed 表示结果已保存，不等于已经送达。
- 私聊必须收到 echo 对应的成功响应才标记送达。若 QQ 已送达但确认丢失，重试可能导致重复私聊，无法实现端到端 exactly-once；消息带任务编号便于识别。
- 通知按顺序发送；管理员私聊不可达可能阻塞后续通知，`/status` 的回复也会排队。请检查 `send_private_msg failed` 日志和 ADMIN_QQ / 好友关系。
- 断线和停机窗口会触发有限历史回补；OneBot 推送没有本项目可依赖的持久重放保证。磁盘满或数据库不可写会记错误并断开连接，**无法保证尚未落盘的事件可恢复**。
- WAL + synchronous=NORMAL 适合长期运行，但突然断电仍可能丢失最近提交；重要数据应定期备份。停服后复制整个 data 目录最简单，运行中用 SQLite backup API，不要只复制 messages.db 而忽略 WAL。
- SQLite 和通知历史不自动清理；持续运行应监测磁盘容量。日志已轮转。
- `/status` 的 DeepSeek Healthy/Failed 表示最近一次实际调用结果，不会额外发起探测；最后收到消息指最后一次接收白名单群消息的本机时间。
- 默认仅绑定回环地址，适合同机 NapCat。Phase 1 不提供远程公网部署或 TLS。

更换机器人 QQ：停服、备份后删除 runtime_state 中 `onebot_self_id`，再启动新账号连接。此项是人工迁移，不是多 QQ 支持；不同账号数据由 self_id 区分。

## 项目结构

```text
app/
  main.py                 启动入口
  application.py          生命周期、进程锁、组件装配
  config.py               YAML 与环境配置校验
  logging_setup.py        轮转日志和密钥脱敏
  onebot/
    server.py             Reverse WS 与逐事件异常隔离
    adapter.py            协议字段、鉴权、响应适配
    events.py             白名单与入库
    normalizer.py         消息段和 CQ 标准化
    actions.py            唯一 Action Firewall 与 echo 响应关联
  storage/
    db.py, schema.py       SQLite 生命周期、事务、表结构
    repository.py         消息、任务、运行状态与私聊发件箱
  commands/
    router.py             管理员命令路由
    status.py, summary.py  状态展示与时间窗口解析
  jobs/
    service.py, worker.py  总结业务与异步任务消费
  llm/
    deepseek.py            SDK、超时、重试、JSON 解析
    prompts.py, schemas.py 提示词和 Pydantic 输出结构
  notifier/
    renderer.py, qq.py     纯文本排版与持久通知发送
config/config.yaml
tests/
data/, logs/
.env.example, .gitignore
requirements.txt, requirements.lock.txt, pyproject.toml
start.bat
```

数据库包含 messages、summary_jobs、summaries、runtime_state，以及 command_receipts（总结命令去重）、private_outbox（持久私聊待发队列）。
启动时执行 `app/storage/migrations.py` 中的小型事务迁移，无需删除旧数据库。
旧 summary_jobs / summaries 自动增加 self_id：旧任务使用迁移时有效的 onebot_self_id 回填，旧总结从关联任务回填。
没有有效绑定时，旧 queued/running 任务变成 failed，error 为 `missing self_id after schema migration`；
无法归属的历史记录保留 NULL，之后绑定账号也不会重新猜测归属。新任务/总结必须有 self_id。
迁移可重复执行，失败时事务回滚，不改动群消息。既有总结不会重新生成；若迁移前已经切换过账号或产生过混合总结，
旧 schema 缺失的真实来源无法可靠重建，回填值仅依据迁移时绑定，建议从隔离后的消息重新生成所需总结。

## 开发验证

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m ruff check app tests
.\venv\Scripts\python.exe -m compileall -q app
.\venv\Scripts\python.exe -m pip check
```

测试使用临时数据库、模拟 DeepSeek 和模拟 OneBot；另有真实 uvicorn 本机端口启动测试，不连接真实 QQ 或付费 API。
包含 action 拦截、鉴权、白名单、去重、非管理员、CQ 标准化、窗口解析、JSON 校验、重试、原子认领、故障保留消息、重启恢复和私聊响应关联。
当前 Starlette 测试客户端可能提示 httpx 迁移的弃用警告，不影响服务或测试结果。

## Commands — Phase 2

| 命令 | 行为 |
| --- | --- |
| `/inbox` | 最近 10 个非 archived 条目，显示当前账号未读数；数量可配置 |
| `/inbox unread` | 仅显示未读条目 |
| `/detail <id>` | 展示来源、消息数量、附件序号/大小/状态；unread 变为 read，archived 保持归档 |
| `/archive <id>` | 仅改为 archived，不删除消息、附件或文件 |
| `/file <id> <index>` | 发送已下载附件给 ADMIN_QQ；序号从 1 开始，按附件 ID 固定排序 |

所有命令只允许 ADMIN_QQ；所有新数据读取/归档均按当前绑定 self_id 隔离。
尚未下载时回复“附件尚未准备完成”；超限时回复“附件超过自动下载大小限制”，不会强行下载。
文件发送独立排队，不阻塞 WebSocket；发送失败最多尝试 3 次，最终取消该发送并私聊提示。
账号切换后，旧账号的文件发送请求留在 outbox，只有原账号重新绑定并连接后才能发送。

## Attachment Storage & Failure Behavior

- 默认 `data/attachments/<self_id>/<group_id>/<attachment_id>-<安全文件名>`。数据库保留 original filename。
- 下载前检查上报大小与 HTTP Content-Length，下载中再次累计字节并执行同一上限；未知大小也受限。
- 下载到 `.part`，完整接收并核对已知大小后保存 SHA-256、原子改名，再标记 downloaded。失败不会暴露半文件。
- 进程中断后 downloading 恢复 pending，重新下载并清理该附件的旧 `.part`；重试次数持久化，重启也不突破预算。
- 网络异常、超时及 HTTP 408/429/5xx 可有限重试；404、403、无效引用、权限/磁盘错误直接 failed。
- 有 file_id 时每次下载重新调用 get_group_file_url，不将返回 URL 存为永久附件地址。
  无 file_id 时以 self_id/group_id/原消息ID/消息段序号去重，同名文件不会误合并；如果带直链，仅在内存中临时使用。
  重启或缓存淘汰后，无稳定 file_id 的待下载附件可能 failed，元数据与 Inbox 保留；`/file` 永远使用已落盘文件。
- 下载只访问 HTTP(S) 的 qq.com / qpic.cn / gtimg.cn 域及子域、标准端口，要求 DNS 解析为公网 IP，每次重定向也验证；拒绝本机/内网目标、任意第三方 URL 或 file://。
  NapCat 若返回其他 CDN/数字 IP 地址会安全失败，应针对实际版本审查适配，不要放开任意 URL。
- 接收 group_upload 通知时，如果没有 OneBot message_id，生成带 notice 命名空间的稳定 ID 保存原始事件。
  相同 file_id 的消息/通知复用附件和自动 InboxItem，并关联各自内部 messages.id；无稳定 file_id 时不能保证跨事件形式识别同一文件。
- 仅当群策略 inbox_enabled 时为每个新文件创建基础 InboxItem；Phase 3 后台聚合可复用该条目并关联普通文字。`InboxRepository.create_item` 可明确聚合多个消息/附件；关联表复合外键禁止跨账号关联。
- 升级时在现有迁移事务中幂等创建 attachments、inbox_items、inbox_item_messages、inbox_item_attachments，
  并给 private_outbox 增加文件类型/账号/附件引用/取消状态；原有行及 Phase 1 数据保留。
- **附件长期保存，会持续占用磁盘**；本阶段无自动删除。未来将增加 retention policy。归档 Inbox 不释放磁盘空间。
- 文件上传依赖 NapCat 对该本地路径的读取权限。大文件上传确认耗时较长时，可按需将 websocket.action_timeout 调高（最大 120 秒）。
  确认丢失后重试仍可能重复私聊发送。

## Group Policies

`groups.allowed` 决定是否允许采集；GroupPolicy 只决定白名单内群的处理方式，不能扩大白名单。
策略以 `(self_id, group_id)` 隔离，未配置的白名单群使用 `summary_only`，不会突然生成 Inbox 或下载文件。

| 模式 | 保存消息 / 总结 | Inbox 文件条目 | 附件自动下载 | Priority Watch |
| --- | --- | --- | --- | --- |
| summary_only | 开启 | 关闭 | 关闭 | 关闭 |
| inbox | 开启 | 开启 | 开启 | 关闭 |
| priority | 开启 | 开启 | 开启 | 仅保存开关 |
| ignore | 暂停业务处理 | 关闭 | 关闭 | 关闭 |

这些是 profile 默认值，summary_enabled、inbox_enabled、attachment_download_enabled、priority_watch_enabled 可独立设置。
切换 mode 会在提案里展开该 profile 的默认开关；单独改下载开关不会改变 mode 或其他开关。
实际下载还必须同时满足全局 attachments.enabled / auto_download 和大小上限。
summary_only 可只保存附件元数据并标记 skipped；ignore 在业务入口跳过 messages / attachments / Inbox，保留最小运行日志。
策略对后续事件和待执行任务生效，不删除旧数据、不追溯生成旧文件的 Inbox、不自动重排 skipped 下载。
已开始的 API 请求或文件传输可能完成；下载 worker 在执行前重新检查策略。

**Priority Watch policy is stored, but automatic urgent alerts are planned for Phase 4.**
priority 模式提高历史核验频率（默认2分钟），Phase 3 还缩短批量分类等待时间（默认60秒），但不发送即时提醒。
自动 triage 要求 mode 为 inbox / priority 且 inbox_enabled=true；summary_only 和 ignore 不参与。

`/groups` 列出当前账号白名单群与策略；`/group <群号>` 查看单群。
群名称使用本地 alias，不自动查询 QQ 群列表；同名、多个候选或“这个群”没有唯一指代时，返回候选并要求指定群号，不猜测。

## Conversational Configuration

所有操作只接受 ADMIN_QQ 的显式 `/config` 入口；普通私聊不会被解释为配置。

```text
/config 756155087 mode inbox
/config 756155087 alias 高数群
/config 756155087 以后叫高数群，并作为课程收件箱处理
/config 音游群平时主要闲聊，只需要总结，不要放进收件箱
/config 高数群作为课程收件箱
/config 班级群需要重点关注
/config 高数群不要自动下载文件
/config 这个群主要闲聊，只需要总结
```

示例群号须先在 YAML 白名单内；别名须先配置。最后一个例子会要求明确群号。
明确格式 `mode <profile>`、`alias <别名>`、`<开关字段> true/false` 直接本地解析；简单中文文件开关也可本地识别。
其他自然语言后台调用 DeepSeek，沿用默认关闭 thinking、严格 JSON 校验和有限重试。缺少 API Key 不影响明确命令。
Python 先从本账号白名单/别名唯一解析目标，LLM 输出中不允许 group_id、SQL、路径或 action 权限，不能凭空决定群号。
只有 `/config` 后第一个完整数字 token 被解释为显式群号，其优先于别名；正文 `14天`、`10月15日`、`100MB` 均按普通文本处理。
同名别名会列出候选并要求群号。当前不支持自动到期策略或每群下载大小阈值；数字解析正确不表示新增了这些配置能力。

```text
/config ...
→ 显示 Before → After 的提案
→ /confirm <proposal_id> 应用，或 /cancel <proposal_id> 放弃
```

**LLM does not directly modify runtime configuration. It only produces a validated configuration proposal.**

提案 10 分钟过期；确认必须同一 self_id、同一管理员、仍在白名单、pending 且未过期。
重复确认幂等；若待修改字段已被另一提案改变，旧提案失效，防止覆盖新配置。
取消/过期不修改策略；所有变更仅应用显示的 diff。管理员应检查自然语言理解结果再确认。
配置存储在 SQLite，不修改 YAML、`.env` 或采集白名单。
启动迁移幂等新增 group_policies、configuration_proposals、configuration_requests，不改变既有 Phase 1 数据。
队列中断后可恢复，过期提案由后台定期处理；账号切换后其他账号的解析请求保留但不执行。

## Privacy

Echelon 在本地保存 QQ group messages、metadata、attachments、summaries 和 inbox items。
Phase 1 summary 会将对应窗口的群聊文本发送给配置的 DeepSeek API provider。
自然语言 `/config` 会将该配置文本发送给同一 provider，本地还保存配置解析请求、提案与群策略；明确格式命令不调用 API。
Phase 3 自动 triage 会发送相关消息文本、发送者 ID、原事件时间、附件元数据、近期同群条目与个人偏好；`/pref` 会发送该偏好请求文本。
本地保存 triage job、结构化结果、归属状态、个人偏好及提案；LLM 校验失败日志不保存完整模型输出。
**Phase 2 附件 binary 内容不会发送给 DeepSeek**：不读取文档内容、不 OCR、不解压、不进行图片或语音理解。
新事件持久化时移除已识别 file segment / CQ file / group_upload 的临时 URL；仅 pending 附件在内存中短期缓存直链。
升级前原始事件 JSON 可能已有 URL，本次不批量改写历史数据；恢复与转发不依赖这些旧地址。日志不记录完整 URL 或文件内容。

## Phase 2 Manual Verification

1. 停止旧服务，备份 data，启动 Echelon；无需删除数据库。
2. 确认 NapCat Connected，且管理员与机器人可以正常私聊。
3. 发送 `/groups`，确认默认 summary_only；发送 `/config <群号> mode inbox`，检查 diff，再 `/confirm <提案ID>`。之后在该白名单群上传小型 txt/docx。
4. 检查 attachments 表出现记录，观察 pending → downloading → downloaded。
5. 确认文件位于 data/attachments 对应账号/群目录，sha256 与 downloaded_at 已保存。
6. 管理员发送 `/inbox`，看到文件条目；发送 `/detail <id>`，看到附件序号和 downloaded。
7. 发送 `/file <id> 1`，确认 ADMIN_QQ 收到文件；群里不应出现回复或文件上传。
8. 发送 `/archive <id>`，再 `/inbox`，确认默认列表不显示归档条目，而文件仍在。
9. 在非白名单群上传文件，确认没有附件或 Inbox 记录。
10. 验证超限文件为 skipped，`/file` 不绕过限制。
11. 验证 `/status` 附件/收件箱计数，以及 `/summary 30m` 仍正常。
12. 用 `/config <群号> alias 高数群` 并确认；再 `/config 高数群不要自动下载文件`，确认 diff 只有下载开关变化。
13. 测试 `/cancel`、过期提案、非白名单群配置被拒绝；切换 summary_only 后新文件不进入 Inbox，ignore 后新消息不入库。

开发检查继续使用上文命令。若 Windows TEMP 清理权限异常，可使用
`.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp`，该目录仅作测试临时数据。
新增模块：`app/attachments/`（标准引用、安全存储、下载 worker）、`app/inbox/renderer.py`、
`app/commands/inbox.py`、`app/onebot/files.py`、`app/storage/inbox_repository.py`、`app/storage/phase2_schema.py`。

## Command Reference

命令仅限 ADMIN_QQ 私聊；普通文字不触发命令。未知命令只提示 `/help`。
`/help` 显示分类首页，`/help <命令>` 展示用法、参数、副作用、限制及例子。

| Command | Purpose |
| --- | --- |
| `/help [命令]` | 分类帮助或单命令详情 |
| `/status` | 连接、消息、附件、Inbox、DeepSeek、任务与运行时间 |
| `/coverage` | 采集缺口、恢复覆盖及周期核验状态 |
| `/sync [群号]` | 对全部非 ignore 白名单群或指定群建立有限历史核验任务 |
| `/summary <时间窗口>` | 总结开启 Summary 的群；支持 30m、2h、today |
| `/inbox [unread\|high\|critical\|deadline\|action]` | 非归档条目及简单筛选 |
| `/detail <id>` | 查看 Inbox 内容及附件序号，unread → read |
| `/archive <id>` | 归档，保留源消息与附件 |
| `/file <inbox_id> <attachment_index>` | 将已下载且安全的附件发给 ADMIN_QQ |
| `/groups` | 当前账号白名单群的策略列表 |
| `/group <群号>` | 查看单群策略 |
| `/config <群号或别名> <配置意图>` | 生成 Before → After 配置提案 |
| `/prefs` | 查看当前账号的个人分类偏好 |
| `/pref <自然语言>` | 生成个人偏好变更提案 |
| `/confirm <提案ID>` | 确认未过期且未发生冲突的提案 |
| `/cancel <提案ID>` | 取消待确认提案，不修改策略 |

复杂命令详见上文 Commands、Group Policies 和 Conversational Configuration。
`/help config` 包含完整配置确认流程；`/help groups` 解释模式。只想避免提醒但仍保留以后总结能力时使用 summary_only，不要使用 ignore。

### /coverage 与 /sync

`/coverage` 只读展示当前连接、最后实时消息接收、最近五个断连窗口、回补数量、未确认缺口总数及每群最近周期核验。
没有已记录 gap 也不代表没有漏报；`/status` 的在线状态不能替代数据覆盖检查。

`/sync` 或 `/sync 756155087` 创建持久 manual 任务，立即回复任务编号；完成后私聊简要结果。
必须在白名单且当前非 ignore。相同群已有活动 manual 任务时不重复排队。
手动核验使用 history.periodic_count（最多500），窗口为该群核验间隔加 overlap；不是全量群历史下载。
回补可能增加消息、附件和 Inbox，但不会重放历史私聊命令。

## Reliability and History Recovery

主要数据源仍是 **OneBot realtime event**；`get_group_msg_history` 仅用于 **best-effort repair**。
**NapCat / QQ history API cannot be treated as a guaranteed complete offline event replay mechanism.**
不能保证 QQ 离线期间所有消息都能返回；接口成功也不是完整覆盖证明。

实现参考 [NapCat 群历史 Action 源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts)。
兼容逻辑集中在 `app/onebot/history.py`：从最新位置拉取有限 N 条，不依赖 message_seq 翻页语义；
采用兼容参数 reverseOrder，不向 worker 暴露版本差异。若所用版本不支持，会有限失败并显示 failed，不无限探测接口。

```yaml
history:
  enabled: true
  reconnect_count: 200
  periodic_enabled: true
  inbox_interval_seconds: 300
  priority_interval_seconds: 120
  summary_only_interval_seconds: 1800
  periodic_count: 100
  retry_count: 2
  overlap_seconds: 300
  debounce_seconds: 60
  request_interval_seconds: 2
```

- count 范围1–500；间隔60–86400秒；额外重试0–5次；overlap 0–3600秒；debounce 30–300秒；请求间隔1–60秒。
- 周期调度每30秒检查一次到期群，priority 默认2分钟、inbox 5分钟、summary_only 30分钟，ignore 永不调度。
  首次连接先等待一个周期；故意回查有重叠的最近历史，再依靠消息唯一键去重，不依赖精确的单条游标。
- 重连关闭 gap 并创建每群 reconnect 任务，稳定连接 debounce 时间后开始回查。WebSocket handler 不等待历史 API。
- reconnect 只入库 gap_start−overlap 到 reconnect_time+overlap 的消息；periodic/manual 使用各自持久窗口加 overlap。
  即使窗口很长，每次仍只请求有限最新 N 条，不分页爬取全部历史。
- 历史消息先经过身份、群、消息ID、时间、发送者、消息段校验，再进入同一 EventProcessor / Repository.add_message。
  非法单条跳过并警告；外层结构或身份错误失败。私聊历史不会执行 CommandRouter。
- 消息唯一键保持 `(self_id,group_id,message_id)`；已有记录不会被历史覆盖或重复创建 Inbox。
  新历史文件遵守当前群策略、白名单和大小限制；缺少可用引用时附件失败，消息仍保存。
- transient OneBot 断连、超时及 provider 拒绝最多额外重试 retry_count 次；重试预算持久保存，重启不重置。
  非白名单、ignore、身份不一致、无效 response schema 直接失败。周期任务不发“同步完成”通知。

### Collection gaps 与停机

连接断开记录 open gap，重连时结束窗口并异步恢复。正常停机保存 service_clean_shutdown / last_service_stop，
下次启动保留 service_stop / offline_window_clean；崩溃或断电从最后已知实时接收/连接检查点保守推断 offline_window_unclean。
无法精确重建断电时刻，推断窗口可能偏大；首次全新启动没有过去在线证据时不虚构旧缺口。
gap 在实际重连前保持 open，包含 Python 已启动但 NapCat 尚未连接的时间。

重连报告通过当前账号 private_outbox 投递。短时连续 gap 等连接稳定后合并一份报告；报告与“已报告”标记同事务保存。
NapCat 断线期间只记录本地状态，不尝试通过离线 QQ 即时提醒。关闭 history 时 gap 仍保留并报告 unknown。

### Coverage 含义与总结警告

| 状态 | 含义 |
| --- | --- |
| likely_covered | 有效返回的最早时间不晚于窗口开始，最晚时间不早于窗口结束，且无无效记录；仍不保证每条完整 |
| partial | 有有效记录但未跨过窗口两端，或部分记录无效 |
| unknown | 空结果、无有效时间，或没有可核验的群任务 |
| failed | 有限重试耗尽或安全/结构验证失败 |
| pending / running | 等待重连或任务尚未完成，不视为已覆盖 |

窗口时间范围只是覆盖证据，不证明其中无漏报；安静群没有窗口末端消息时也可能保守显示 partial。
恢复报告列出窗口、原因、涉及群、获取/新增数量和覆盖状态。返回消息数包含接口返回的重复/无效项；新增数仅算本次成功插入。
进程恰在单条入库后、任务统计落盘前崩溃时，恢复后的新增计数可能低估，消息仍通过唯一键保留。

`/summary` 与当前账号/目标群的未确认 gap 相交时，Python renderer 增加“数据覆盖警告”，不阻止摘要，也不依赖 LLM 写警告。
其他群的良好恢复结果不能替该群证明完整；Phase 3 使用 `HistoryRepository.unresolved` 按候选消息原时间查询，并在原子应用结果时再次检查。

### 来源时间与迁移

messages.ingest_source：realtime、history_recovery（重连/手动）、history_poll（周期）。
旧 Phase 1/2 行默认 realtime；真正 event_time 保留，received_time 为本地入库时刻。
未来 urgent alert 必须按 **event_time** 判断新旧，不能把历史消息的 received_time 当新事件。
runtime_state 区分 last_received_at、last_message_event_time（最大消息时间）、last_realtime_received_at；
保留旧 last_event_time 作为实时接收兼容字段，历史回补不会更新它；同时保留按账号的时间键供 coverage 使用。

启动在同一迁移事务中幂等增加 ingest_source、collection_gaps、history_sync_jobs、history_sync_state。
现有消息、附件、Inbox、策略、总结保留；失败回滚。running 历史任务恢复 queued，所有新查询和任务绑定 self_id。
历史来源通过已验证连接上下文补齐缺失 self_id；响应明确返回其他 self_id 或 group_id 时拒绝，不混入数据。

## Phase 2.5 Manual Verification

1. 备份 data 后启动 Echelon 和 NapCat，保持现有 Reverse WebSocket 与令牌配置。
2. `/help`、`/help config`、`/coverage`，记录当前状态。
3. 暂停 NapCat WebSocket 或关闭 NapCat，在白名单非 ignore 群发送测试消息，等待约2分钟。
4. 恢复 NapCat；观察 collection_gaps 结束、history_sync_jobs 创建，等待默认60秒稳定期后回查。
5. 检查 messages 是否补入可获取消息、event_time 和 ingest_source 是否正确，重复消息是否仍只有一条。
6. 收到采集恢复报告后 `/coverage`，确认只显示 likely_covered / partial / unknown / failed 等保守状态。
7. `/summary 30m`；窗口仍有 partial/unknown/failed/pending/running gap 时，应显示数据覆盖警告。
8. `/sync <群号>` 验证立即入队与后台完成通知；等待一个策略周期，验证周期回查静默执行。
9. 正常停止并重启 Python，验证 service_stop 窗口与恢复；在测试环境模拟异常退出，验证 running → queued 和推断离线窗口。
10. 验证别名加14天/日期/100MB可确定正确群，但不会新增自动到期或每群大小配置能力；核对提案后再确认。

## Intelligent Triage

实时与历史消息继续使用相同的 EventProcessor / Repository。新插入、且群策略允许 triage 的消息，
在同一个消息事务里写入 `triage_message_state`；消息重复不会重复取得归属。
后台 TriageWorker 按账号、群和有界窗口建立 `triage_jobs` / `triage_job_messages`，模型调用在事务外。
没有关键词门槛：每条符合策略的新消息都进入持久 pending 状态，普通聊天由模型明确 ignored，而不是本地关键词永久丢弃。
关闭 triage 时不登记新消息；策略临时关闭时已存在 pending 保留，重新开启后可继续。已领取但策略变更的任务安全失败。

```yaml
triage:
  enabled: true
  inbox_debounce_seconds: 180
  priority_debounce_seconds: 60
  inbox_max_wait_seconds: 600
  priority_max_wait_seconds: 180
  max_messages_per_candidate: 100
  max_input_chars: 40000
  max_span_seconds: 3600
  retry_count: 2
```

持续聊天也会在 hard max wait 到达时封批；消息数、源文本预算或原消息时间跨度达到边界时提前分批。
max_messages 范围1–200，max_input_chars 12000–100000（包含 system prompt），max_span 60–86400秒，额外重试0–5次。
debounce 范围1–3600秒，max_wait 1–86400秒且不能小于对应 debounce。配置非法时启动失败。
批次先将约一半字符预算留给消息；最终序列化请求仍检查总上限，先移除末尾可选合并候选，不截断源消息。
单条超大消息或剩余上下文仍超限时任务 failed，源消息保留；本阶段没有手动重新分类命令。

每个批次可生成0–10个条目，所有源 ID 必须被引用或明确 ignored。非法分类、标签、伪造来源和合并目标都拒绝。
最多提供10个同群近期非 archived 条目，优先包含已关联本批消息的文件条目；普通合并候选限最近7天。
Python 会复用本批已关联的未分类文件条目；合并多个文件占位条目时转移关联并归档冗余占位条目，不删除来源或附件。
模型选择的目标在应用时再次验证账号、群、归档状态及 revision，避免覆盖管理员在推理期间的操作。

结果、Inbox 创建/更新、来源与附件关联、标签、message state、job completed 在单事务应用，失败全部回滚。
模型输出错误和瞬时 API 故障使用持久任务预算，默认最多3次；Triage SDK 内部不再次叠加重试。
进程重启 running → queued，保留 ownership 和 attempts；中断的请求可能产生额外计费，但已完成结果不重复应用。
长度超限、策略/账号变更、应用约束冲突直接失败，failed 消息不会伪装成 ignored。采集、附件、历史 worker 继续运行。
**智能分类仅更新 Inbox，不创建主动提醒 outbox。** 现有命令回复、总结和采集恢复报告仍保持原行为。

### Phase 3 Migration and Revision

现有迁移事务幂等创建 `triage_jobs`、`triage_job_messages`、`triage_message_state`、`triage_preferences`、
`inbox_item_labels` 和单行 `triage_settings`。后者保存首次升级的 `triage_start_at`；迁移不扫描旧消息创建分类任务。
仅升级后新插入且策略允许的消息登记 ownership，包含新回补的旧事件；重复回补已有旧记录不会触发批量分类。
messages.id 作为稳定 source ID，关联约束同时包含 self_id/group_id，防止跨账号或跨群归属。

inbox_items 增加 revision（旧行默认1）、model_priority、action_required、action_text、triaged_at、triage_model、coverage_status。
title、summary、category、priority、labels、action 或 deadline 实质变化才增加 revision；只读 `/detail` 标记 read 不增加。
`model_priority` 保存模型结果；当前最终 priority 与其相同，偏好通过模型上下文起作用，不另加隐藏的规则提权。
配置请求与提案增加 kind，旧行默认 group_policy；triage_preferences 复用原 `/confirm`、`/cancel` 分发与过期机制。
迁移失败回滚，无需删除 messages.db。升级前建议正常停服并备份整个 data。

## Personal Preferences

```text
/prefs
/pref 考试和调课对我很重要
/pref 123456789 是老师，他的消息需要重点关注
/pref 讲座通常是低优先级
/pref 课程资料正常优先级即可
```

支持 important_keywords、low_priority_keywords、important_senders、category_priority_preferences。
每类关键词最多30个、每个最多40字；重要发送者最多30个；分类偏好只接受固定10类与4种优先级。
默认全部为空；偏好按 self_id 保存，作为对该用户重要性的上下文，不作为消息丢弃过滤器。

`/pref` → 后台严格 PreferenceIntent → Python 校验明确提及的关键词/发送者/分类 → Before → After 提案 → `/confirm <id>`。
10分钟内可 `/cancel <id>`，未确认绝不修改；重复确认幂等、过期拒绝、已变更字段使旧提案失效。
只应用显示的差异，不修改 GroupPolicy、allowlist、文件路径或通知时间。普通私聊不触发偏好配置。
偏好只影响之后执行的分类，不自动重新分类已有 Inbox。模型仍可能误解意图，确认前请核对 diff。

## Priority Semantics

| 值 | 含义 |
| --- | --- |
| critical | 极少使用：明显时间紧迫，遗漏会有明显后果 |
| high | 需要重点留意或近期行动 |
| normal | 值得保留，无需立即处理 |
| low | 信息价值较低但仍值得保留 |

纯聊天通常不创建条目，不用大量 low 条目复制群聊。**Phase 3 会分类，但不会即时通知。**
`/inbox high` 和 `/inbox critical` 精确筛选对应优先级；`/inbox action` 筛选需要行动的条目。

## Categories and Labels

固定分类：announcement（一般通知）、schedule（时间地点/调课）、assignment（作业报告）、exam（考试）、
material（资料）、administrative（行政）、action_request（回复/确认/报名）、project（协作）、discussion（有价值讨论）、other。
固定标签：deadline、schedule_change、location_change、attendance、submission、file、requires_reply、
requires_preparation、exam、assignment、administrative。模型不能增加自由标签。
`/detail` 显示分类、优先级、标签、行动、置信度、简短事实依据、附件和覆盖提示；reason 不要求或展示内部推理过程。

## Deadline Semantics

每条输入明确包含原始 event_time、带时区 local_time 和 ingest_source，使用配置的 Echelon timezone。
“今天/明天/下周四”基于原消息时间，绝不以恢复接收时间或 API 调用时间为基准。
日期有歧义时 deadline_at=null，保留 deadline_text；有日期时必须返回带时区 ISO8601，再转 Unix timestamp 保存并按配置时区显示。
Python 额外核验今天、明天、后天、大后天、下周某日与引用消息原日期是否一致；跨多个不同基准日时要求保持 null。
其他自然语言日期仍依赖模型理解，不能承诺无误；请在 `/detail` 核对。
`/inbox deadline` 同时包含具体时间和仅有截止文字的条目。action_required=true 必须有 action_text，false 时必须为 null。

## Coverage and Triage

分类前后使用候选原时间范围查询未确认 gap，partial / unknown / failed / pending / running 不阻止分类，
但由 Python 将 coverage_status 设为 warning，并在 `/inbox`、`/detail` 提示可能缺少上下文。
合并既有 warning 条目时不会悄悄清除警告；本阶段不自动重新评估已存条目。
无已知缺口使用 no_known_gap，**不表示绝对完整**；likely_covered 仍只是有限时间范围证据。
历史消息保持 event_time 和 ingest_source，未来 Phase 4 必须按事件原时间及 revision 决定投递，不能把 received_time 当新事件时间。
附件只提供 filename、file_size、download_status 和 source ID，不读 binary、路径、临时 URL 或文件内容。

## Phase 3 Manual Verification

1. 正常停止旧实例，备份 data；启动 Echelon，检查迁移和 worker 启动，NapCat 保持原连接配置。
2. `/prefs`；`/pref 考试和调课对我很重要`，核对提案，`/confirm <id>` 后再 `/prefs`。
3. `/config <课程群号> mode inbox`，确认提案；白名单不得由模型扩张。
4. 群内连续发送“下周实验课改到302”“时间还是周四下午”“记得带实验报告”。
5. 等待默认180秒静默期（持续发言最多等待600秒封批），再加模型处理时间；priority 对应60/180秒。
6. `/inbox`、`/detail <id>`，检查合理聚合、category、priority、labels、action、deadline；应无自动紧急私聊。
7. 上传课程文件并补一句用途说明，等分类后检查复用文件条目、来源与附件关联，`/file <id> 1` 仍可转发。
8. 测试 `/inbox high`、`/inbox critical`、`/inbox deadline`、`/inbox action`。
9. summary_only 群发相同普通消息，不应创建智能 Inbox；ignore 群不进入采集。
10. 按 Phase 2.5 流程断连回补，确认旧消息的相对日期使用原 event_time，缺口条目有 coverage warning。
11. 回归 `/summary 30m`、`/sync`、`/coverage`、GroupPolicy 提案和附件转发；测试服务重启后的 running 任务恢复。

## Roadmap

| 阶段 | 状态 | 范围 |
| --- | --- | --- |
| Phase 1 — Message Collection & Summary | ✅ Implemented | 消息采集、持久化、私聊总结 |
| Phase 2 — Attachments & Inbox Foundation | ✅ Implemented | 附件、Inbox、管理员文件转发、Group Policy、Conversational Configuration |
| Phase 2.5 — Reliability / History Recovery / Command UX | ✅ Implemented | 缺口检测、有限历史回补、周期核验、覆盖警告、统一帮助 |
| Phase 3 — Intelligent Triage & Personal Preferences | Current | 消息聚合、categories、priorities、labels、deadline、personal preferences |
| Phase 4 — Delivery Automation | Planned | heartbeat、urgent alert、scheduled digest、quiet hours |
| Phase 5 — Web Inbox | Planned | browser UI、search、filtering、archive、attachment management |

以下仍未实现：

不包含 OCR、图片理解、语音识别、RAG、向量数据库、Telegram、Web 管理页面、Docker、多 QQ、多进程 worker、Semantic watch、`/ask`、自动群回复、群管理、自动加群或自动好友请求。
后续扩展应继续通过 CommandRouter、Repository、DeepSeekClient 和 ActionGateway 的既有边界，保留群发送禁令。

真实 NapCat 连接、真实 DeepSeek 账号权限/余额，以及任务计划程序开机运行需要用户填入本机配置后完成验收。
