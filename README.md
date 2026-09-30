# QQ Monitor · Phase 1

Windows 本地长期运行的 QQ 群聊只读采集与管理员私聊总结服务。

数据链：NapCatQQ → OneBot 11 Reverse WebSocket → 群白名单与标准化 → SQLite；
管理员 `/summary` → SQLite 任务队列 → DeepSeek → Pydantic 校验 → Python 排版 → 管理员私聊。

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
```

默认白名单为空，避免误采集；填入真实群号并重启。配置路径始终相对于项目根目录，不依赖终端当前工作目录。
默认模型可修改，当前名称参考 [DeepSeek JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)。
API 地址固定为 `https://api.deepseek.com`，不使用 OpenAI API Key。
普通总结默认显式发送 `thinking.type=disabled`；设置 `deepseek.thinking: true` 后发送 `enabled`。
参数格式参考 [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。

## NapCat 配置

1. 在 NapCat WebUI 的 OneBot 网络配置中添加 **WebSocket 客户端 / Reverse WebSocket Client**。
2. URL：`ws://127.0.0.1:8789/onebot/v11/ws`。
3. Token / Access Token 填写与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 完全一致的值。
4. 开启连接，建议重连间隔 3000–5000 ms，启用正常的心跳。
5. 推荐消息格式为 array / 消息段；string / CQ 字符串也能解析。
6. 必须使用事件和 API 响应共用的 **Universal 双向连接**；不要分开建立 Event 与 API 连接。
7. 保持 NapCat 在线。仅运行 Python 不会自动启动 NapCat，也不会登录 QQ。

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
所有白名单群各创建一个任务，超过 100 个待处理任务时拒绝入队。
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

- `ActionGateway` 是唯一 WebSocket 写入 OneBot action 的位置；白名单仅含 `send_private_msg`。调用其他 action 首先抛出 `PermissionError`。
- 私聊目的地还必须等于 ADMIN_QQ。参数不接受 group_id 或任意消息段；实际发送固定为 text segment，不解释 CQ 内容。
- 群记录只读，不删除、撤回、禁言、踢人，也不自动处理好友或入群请求。
- 模型没有工具、QQ 接口或凭据访问权限。聊天记录始终作为不可信数据，system prompt 明确禁止执行其中指令。
- 输出必须通过严格 JSON schema 和引用消息 ID 检查，才进入 renderer；防注入不意味着总结不会出现事实错误。
- 非法模型回复按要求写入轮转日志，配置中的密钥会被脱敏。日志可能含群聊内容，应和数据库一起限制本机访问权限。
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
- 断线期间未收到的消息、程序未运行时的历史消息不会主动补拉。OneBot 推送没有本项目可依赖的持久重放保证；磁盘满或数据库不可写会记错误并断开连接，**无法保证尚未落盘的事件可恢复**。
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

## Phase 2 / 尚未实现

不包含 OCR、图片理解、语音识别、RAG、向量数据库、Telegram、Web 管理页面、Docker、多 QQ、多进程 worker、Semantic watch、`/ask`、自动群回复、群管理、自动加群或自动好友请求。
后续扩展应继续通过 CommandRouter、Repository、DeepSeekClient 和 ActionGateway 的既有边界，保留群发送禁令。

真实 NapCat 连接、真实 DeepSeek 账号权限/余额，以及任务计划程序开机运行需要用户填入本机配置后完成验收。
