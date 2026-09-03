# ZVVQuest AstrBot 插件实现计划

## Context

本插件为 AstrBot 4.26.x 提供显式 `/zvv` 指令，通过线上 legacy 接口
`GET https://api.zvv.quest/search` 查询张维为表情包。第一版只实现普通搜索，不抓取网页、
不调用 `/enhancedsearch`，也不提供 LLM Tool、分页、缓存、管理开关或可配置 API 地址。
搜索结果由插件先下载并校验，再以 AstrBot 图片组件发送；多图使用 QQ/NapCat 合并转发。

已完成的协议与背景调研记录在 `docs/zvv-quest-research.md`。其中早期 MVP 建议里的
`aiohttp`、`Image.fromURL()`、可配置 endpoint 等内容已被后续讨论替代；实现与最终 README
以本计划中的固定契约为准。

## 固定行为契约

- 指令格式：`/zvv <描述> [n]`。无描述时返回用法提示。
- 只有参数至少有两个 token 且末 token 是十进制整数时，才把末 token 解释为 `n`；因此
  `/zvv 2025` 搜索“2025”，而 `/zvv 评价 2025` 将 `2025` 视为数量并报超限。
- 省略 `n` 时使用有效默认值。显式 `n` 必须位于 `1..有效最大值`，否则直接返回用法与范围，
  不请求上游；不静默截断用户输入。
- 配置项只有 `default_count`（默认 3）与 `max_count`（默认 10），两者配置范围均为 1–50。
  非整数（含 bool）回退默认值并记录 warning，越界值截断到 1–50 并 warning；若默认值大于
  最大值，则运行时将有效默认值降为有效最大值并 warning。配置重载后生效。
- API 查询作业全局最多 2 个并发；每用户固定冷却 3 秒，网络或上游失败也消耗冷却。
- API 连接超时 5 秒、总超时 15 秒；连接失败、超时及 502/503/504 等待约 0.5 秒后重试
  一次。429 不重试，单独映射为“查询过于频繁，请稍后再试。”。
- API 响应体设 64 KiB 上限；成功必须同时满足 HTTP 2xx、JSON object、`code == 200`、
  `data` 为字符串列表。图片 URL 仅保留无用户名/密码的 HTTPS URL，并按原顺序去重。
- 图片连接超时 5 秒、总超时 20 秒，最多 3 个并发下载；每张最多 8 MiB。瞬时网络错误与
  502/503/504 重试一次，其余失败直接跳过。根据文件签名字节接受 PNG、JPEG、GIF、WebP，
  不信任响应 MIME。
- 实际成功下载 1 张时发送普通 `Image.fromBytes()`；多于 1 张时发送一个 `Nodes`，每张图
  独占一个昵称为 `ZVVQuest`、仅含图片的 `Node`。上游少返回或部分下载失败时发送实际成功项。
- 空结果提示“没有找到相关表情包。”；全部图片失败提示“搜索成功，但图片加载失败，请稍后再试。”；
  超时、协议异常及服务错误对用户统一提示“ZVVQuest 服务暂时不可用，请稍后再试。”，详细原因只写日志。

## 实现步骤

### 1. 项目元数据与依赖

- 新建 `metadata.yaml`：插件 id `astrbot_plugin_zvv_quest`，版本 `v1.0.0`，作者
  `LYB926`，仓库地址使用当前 origin，AstrBot 约束 `>=4.26,<5`。
- 新建 `pyproject.toml`：Python `>=3.12`、依赖 `httpx>=0.27,<1`，配置 pytest 的
  `tests` 路径和 `live` marker，并采用 Ruff Python 3.12、100 字符行宽及基础规则集。
- 新建 `requirements.txt`，只记录运行时依赖 `httpx>=0.27,<1`。
- 新建 `_conf_schema.json`，暴露 `default_count` 与 `max_count`；最大值提示明确写明不宜过大，
  建议不超过 10，否则会增加下载内存占用及合并转发失败概率。

参考：`astrbot-sb-6657/metadata.yaml:1-8`、`astrbot-sb-6657/pyproject.toml:1-27`、
`astrbot-sb-6657/_conf_schema.json:1-40` 的 AstrBot 元数据、测试和配置 schema 写法。

### 2. 纯逻辑层 `logic.py`

- 定义 `CommandUsageError`、不可变的 `SearchRequest(query, count)`，以及默认值、配置边界、
  固定冷却等常量。
- 实现 `extract_command_tail(message, command="zvv")`，兼容 AstrBot 传入完整消息文本并规范化空白。
- 实现 `parse_search_request(tail, default_count, max_count)`：执行上述“单 token 数字仍是查询”的
  末尾数量规则，保留描述内部空格，并生成稳定中文用法/范围错误。
- 实现 `normalize_count_config(config, logger)` 或等价的两个小函数，集中处理类型回退、1–50
  截断与 default/max 关系，避免 AstrBot handler 内散落配置分支。
- 实现单命令族的 `CooldownTracker`，使用 `time.monotonic()`，支持注入 clock 以离线测试；
  `acquire()` 在允许调用时立即记录时间，使后续失败也计入冷却。

复用：命令尾提取参考 `astrbot-sb-6657/logic.py:314-321`；冷却器结构参考
`astrbot-sb-6657/logic.py:463-483`，但本插件只需按 user id 记录 `/zvv` 一类调用。

### 3. 网络边界 `client.py`

- 定义硬编码 API URL、超时、API body 上限与图片大小上限，并建立稳定异常层级：通用服务错误、
  限流错误，以及仅供日志区分的响应/图片错误。
- 实现 `ZvvQuestClient.search(query, count)`：通过 `params={"q": ..., "n": ...}` 发起 GET，
  流式读取受限响应体，执行重试策略，解析并验证 JSON 信封，再校验、去重 URL。
- 实现私有 URL 校验函数，要求 scheme 为 HTTPS、hostname 非空且不含 userinfo；不跟随未经再次
  校验的重定向。
- 实现 `download_images(urls)` 与单图下载函数：用 `asyncio.Semaphore(3)` 和保持输入顺序的
  `gather` 并发下载，流式累计并在超过 8 MiB 时中止，校验 HTTP 状态和图片 magic bytes。
  单张失败记录 warning 后返回空槽，最终过滤失败项但保持成功项原顺序。
- 构造函数接收共享 `httpx.AsyncClient`，并允许注入 sleep，以便测试重试而不真实等待。

参考：HTTP 错误归一化及响应信封校验参考 `astrbot-sb-6657/client.py:408-434`；
`httpx.MockTransport` 客户端构造参考 `astrbot-sb-6657/tests/test_client.py:51-55`。

### 4. AstrBot 接入 `main.py`

- 用 `@register` 注册 `ZvvQuestPlugin`；构造函数只保存配置和创建冷却器、全局查询
  `Semaphore(2)` 占位，不在同步构造阶段打开网络资源。
- `initialize()` 规范化配置，创建一个共享 `httpx.AsyncClient` 与 `ZvvQuestClient`；
  `terminate()` 关闭 client 并清空引用，保证插件重载不泄漏连接。
- 用 `@filter.command("zvv")` 实现 handler：依次完成解析、冷却检查、查询并发门控、API 查询、
  图片下载、结果链构造和稳定错误映射。剩余冷却秒数向上取整后提示
  “操作太快，请在 N 秒后再试。”。
- 单图构造 `Image.fromBytes(data)`；多图构造
  `Nodes([Node(name="ZVVQuest", content=[Image.fromBytes(data)]) ...])`，通过
  `event.chain_result(...)` 一次返回。Node 不附加序号、文本或描述。
- 只捕获预期客户端异常并映射用户提示；意外异常用 `logger.exception` 记录后返回统一服务错误，
  不把上游响应、URL 或 traceback 暴露给聊天用户。

复用：生命周期与共享 HTTP client 结构参考 `astrbot-sb-6657/main.py:49-93`、
`astrbot-sb-6657/main.py:390-400`；配置容错风格参考 `astrbot-sb-6657/main.py:430-446`。
AstrBot 4.26.6 中 `Image.fromBytes()` 位于
`astrbot/core/message/components.py:499-527`，`Node`/`Nodes` 序列化位于同文件
`:653-730`；aiocqhttp 对群聊和私聊的合并转发分派位于
`astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py:144-172`。

### 5. 离线与可选线上测试

- `tests/test_logic.py`：覆盖空描述、普通描述、显式数量、单 token 数字查询、0/负数/超限、
  多空格、配置非法类型/越界/default 大于 max，以及冷却首次通过、失败后仍冷却和到期恢复。
- `tests/test_client.py`：用 `httpx.MockTransport` 覆盖请求参数、成功信封、空列表、HTTP 400
  纯文本、HTTP 200/code 400、429、502 重试、timeout 重试、畸形/超大 JSON、data 类型错误、
  HTTPS URL 校验与稳定去重；另覆盖四类图片签名、错误 MIME 仍接受、超大文件、非图片、
  部分失败、重试次数、最大下载并发为 3 及输出顺序。
- `tests/test_main.py`：安装最小 AstrBot stub，注入 fake client，覆盖初始化/终止、默认与显式数量、
  无效参数不请求 API、全局查询并发最多 2、所有用户提示、单图链、多图 `Nodes` 结构、部分/全部
  下载失败。断言多图每个 Node 只含一张 `Image` 且昵称一致。
- `tests/conftest.py` 只放共享 fixture/stub；由于目录名本身是合法 Python package 名，不照搬
  样例仓库为连字符目录建立别名 package 的代码。
- `tests/test_live_api.py` 用 `ZVV_QUEST_LIVE_TEST=1` 显式启用，只以无副作用关键词请求 1 张，
  验证搜索并下载到合法图片；默认完整测试不访问网络。

参考：AstrBot stub 与 lifecycle 测试方式参考 `astrbot-sb-6657/tests/test_main.py:18-155`、
`:172-188`；live marker 与环境变量门控参考 `astrbot-sb-6657/tests/test_live_api.py:1-35`。

### 6. 用户文档与发布记录

- 扩写 `README.md`：说明插件与网站非官方关系、命令示例、末尾数量歧义规则、两个配置项、
  3 秒冷却、并发/大小限制、合并转发的平台限制、外部只读 API、隐私与图片版权边界、安装方式、
  离线测试和 opt-in live test。
- 更新 `docs/zvv-quest-research.md` 的“实现建议”，使其不再与最终确定的 httpx、预下载、
  硬编码 endpoint 和默认/最大数量相冲突，同时保留调研事实与来源。
- 新建 `CHANGELOG.md`，记录 `v1.0.0` 首发功能、配置和边界。

## 实施顺序

1. 元数据、schema 与依赖。
2. `logic.py` 及其离线测试。
3. `client.py` 及协议/下载测试。
4. `main.py` 及 AstrBot handler 测试。
5. README、调研文档校准与 changelog。
6. 全量静态检查、离线测试、真实 AstrBot 导入和编译检查。

## 验证

在 `/home/ke/code-chat/astrbot_plugin_zvv_quest` 执行：

```bash
uv run --isolated --with pytest --with httpx pytest
uv run --isolated --with ruff ruff check .
/home/ke/.local/share/uv/tools/astrbot/bin/python -m compileall -q .
cd /tmp && PYTHONPATH=/home/ke/code-chat \
  /home/ke/.local/share/uv/tools/astrbot/bin/python -c \
  'import astrbot_plugin_zvv_quest.main'
```

按需执行只读线上冒烟测试（不纳入默认验收）：

```bash
ZVV_QUEST_LIVE_TEST=1 uv run --isolated --with pytest --with httpx pytest -m live
```

不在本轮自动同步到 `/home/ke/agent-explore/data/plugins/`，也不重启 AstrBot。若用户之后明确要求
部署，只复制工作区插件文件，并由用户在 WebUI 重载后小规模验证 NapCat 单图及多图合并转发。
