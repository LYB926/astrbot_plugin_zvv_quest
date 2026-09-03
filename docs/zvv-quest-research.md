# VVQuest 查询机制调研

调研日期：2026-09-03

## 结论

不要抓取 `https://zvv.quest/?q=...&n=...` 的 HTML。网页只是客户端，普通查询实际调用：

```text
GET https://api.zvv.quest/search?q=<查询文本>&n=<数量>
```

插件第一版应直接适配这个 legacy GET API，将响应中的 HTTPS 图片 URL 作为 AstrBot 图片组件发送。API 地址和请求超时应可配置，因为线上接口与上游仓库当前 `main` 分支的新版自托管 API 已经发生协议分叉。

## 网站与项目背景

- `zvv.quest` 当前页面由 Next.js 输出；页面查询参数 `q`、`n` 只负责初始化前端表单，浏览器随后请求 `api.zvv.quest`。页面脚本中同时写明了普通搜索 `/search` 和联网增强 `/enhancedsearch`。[线上页面](https://zvv.quest/)、[当前页面脚本](https://zvv.quest/_next/static/chunks/app/page-7b1b9b80711f8041.js)
- 页面中的 GitHub 链接原为 `DanielZhangyc/vvquest`，目前会跳转到 `MemeMeow-Studio/MemeMeow`。该项目把自己描述为自然语言表情包检索工具，并采用 MIT 许可证。[MemeMeow 仓库](https://github.com/MemeMeow-Studio/MemeMeow)、[许可证](https://github.com/MemeMeow-Studio/MemeMeow/blob/main/LICENSE)
- 仓库始于 2025-02-11；2025-02-23 增加了公开 GET API 文档，2025-03-07 从 VVQuest 更名为 MemeMeow。[初始提交](https://github.com/MemeMeow-Studio/MemeMeow/commit/ab04e8e)、[API 文档提交](https://github.com/MemeMeow-Studio/MemeMeow/commit/c0006ec)、[更名提交](https://github.com/MemeMeow-Studio/MemeMeow/commit/4b38212)
- 上游 README 声称最初内置的张维为表情包数据来自一个知乎回答，并附有侵权联系删除说明。这个声明只能说明上游声称的数据来源；MIT 许可证覆盖项目代码，不自动授予第三方图片版权。[当前 README 的数据声明](https://github.com/MemeMeow-Studio/MemeMeow#%EF%B8%8F-%E6%95%B0%E6%8D%AE%E5%A3%B0%E6%98%8E)

## 线上普通搜索 API

### 请求

`q` 和 `n` 都是必填查询参数：

```http
GET /search?q=%E6%88%91%E4%BB%AC%E7%9A%84%E7%BD%91%E6%B0%91&n=25 HTTP/2
Host: api.zvv.quest
Accept: application/json
```

应让 HTTP 客户端通过 `params={"q": query, "n": count}` 编码参数，不要手工拼接或自行只编码一部分 URL。

历史第一方文档给出的 `n` 范围是 1 到 50。[历史 README API 说明](https://github.com/MemeMeow-Studio/MemeMeow/blob/51557491a0f4844f3094e179c9a4b057e95b88e7/README.md#-api)

### 成功响应

实测示例：[查询“我们的网民”，返回 25 项](https://api.zvv.quest/search?q=%E6%88%91%E4%BB%AC%E7%9A%84%E7%BD%91%E6%B0%91&n=25)

```json
{
  "code": 200,
  "data": [
    "https://cn-nb1.rains3.com/vvq/images/我们的网民有很多很多创意.png"
  ],
  "msg": ""
}
```

本次请求返回 HTTP 200、`Content-Type: application/json`，并带有 `Access-Control-Allow-Origin: *`。`data` 是图片 URL 数组，数量可达到请求的 `n`。

### 错误响应不是统一格式

插件必须同时判断 HTTP 状态和 JSON 信封内的 `code`：

- 缺少 `q` 或 `n`、`n` 不是整数：HTTP 400，响应是纯文本，例如 `Failed to deserialize query string: missing field n`，不能假定总能解析 JSON。
- `n=51`：HTTP 仍为 200，但 JSON 为 `{"code":400,"data":null,"msg":"单次请求的图片过多"}`。[边界请求](https://api.zvv.quest/search?q=test&n=51)
- `/openapi.json` 当前返回 404，线上 legacy API 没有可依赖的机器可读 OpenAPI 文档。[OpenAPI 路径](https://api.zvv.quest/openapi.json)

因此客户端应使用以下成功条件：HTTP 为 2xx、JSON 是对象、`code == 200`、`data` 是字符串数组。其余情况统一转成插件自己的稳定错误类型，并将上游 `msg` 作为适度清理后的用户提示或日志信息。

### 图片托管

本次结果指向 `cn-nb1.rains3.com/vvq/images/...`。抽查第一张图片：

- HTTP 200，无重定向；
- 文件名后缀为 `.png`，但响应 `Content-Type` 是 `binary/octet-stream`；
- `Content-Length` 为 995327 字节；
- 支持 Range，并有 ETag、Last-Modified；
- 响应头表明对象位于名为 `vvq` 的兼容 S3 存储桶后方。

这意味着不能依赖 CDN 的 MIME 类型判断是否为图片。AstrBot 4.26.6 的 `Image.fromURL()` 只把 HTTP(S) URL 放入消息组件，适合 MVP；如果以后改为插件自行下载，应同时做 URL 安全校验、重定向限制、字节上限和真实图片解码校验。

## 搜索原理

从公开源码能确认的设计是语义向量检索，而不是对文件名做简单包含匹配：

1. 为每张图片的描述生成并缓存归一化 embedding；
2. 为查询文本生成归一化 embedding；
3. 计算点积（归一化向量下等价于余弦相似度）；
4. 按相关度降序取前 `n` 项，并处理重复或相似图片。

历史实现没有最低相似度阈值，所以任意输入通常也会得到“最接近”的结果，而不代表结果真的高度相关。历史实现还会在共享同一 embedding 描述的图片变体中随机选择，返回值不应被当作永久稳定排序。[历史 `ImageSearch.search` 实现](https://github.com/MemeMeow-Studio/MemeMeow/blob/51557491a0f4844f3094e179c9a4b057e95b88e7/services/image_search.py)

需要谨慎区分：公开源码说明了项目的检索设计，但线上 `api.zvv.quest` 的当前部署版本、具体 embedding 模型和网关实现没有公开可验证的版本标识，不能声称线上恰好运行某个仓库提交。

## 联网增强搜索

当前网页还提供：

```text
GET https://api.zvv.quest/enhancedsearch?q=<查询文本>&n=<数量>
```

成功时网页兼容以下结构：

```json
{
  "code": 200,
  "data": {
    "explanation": "...",
    "memes": ["https://..."]
  },
  "msg": ""
}
```

作者维护的客户端也兼容把图片数组命名为 `images`。[官方油猴客户端的请求与解析](https://github.com/DanielZhangyc/vvquest-tampermonkey-extension/blob/master/vvquest_userscript.js)

网页说明该功能会使用 LLM 联网解析输入，通常约 10 秒、最长可能约半分钟，每分钟限制 5 次，单次查询限制 50 字。普通网页查询有约 2 秒前端冷却，官方油猴客户端设置普通 3 秒、增强 10 秒冷却。这些是客户端/页面公布的使用约束，不应误写成已验证的服务器配额。

第一版插件建议不接入增强搜索：它更慢、有更严格的滥用风险，且普通语义搜索已经覆盖核心需求。后续若加入，应作为默认关闭的独立命令或配置，使用更长超时和更严格冷却。

## 重要的版本漂移

截至调研日期，上游仓库当前 `main` 已演进为通用 MemeMeow 工作台：Vue 3 前端、FastAPI/PostgreSQL/pgvector 后端，并文档化了另一套自托管协议：

```http
POST /search
Content-Type: application/json

{"query":"无奈叹气","n_results":5,"llm_enhance":false}
```

响应是 `{"results":["/media/<meme_id>"]}`，当前文档还明确说旧 `GET /search?q=...` 不是兼容入口。[当前 README](https://github.com/MemeMeow-Studio/MemeMeow#-api)、[当前 API 文档](https://github.com/MemeMeow-Studio/MemeMeow/blob/main/api.md#post-search)

线上 `api.zvv.quest` 却仍提供 legacy GET 信封协议。因此插件不能拿当前 `main` 的 `POST /search` 直接调用线上域名。建议将协议封装在 `ZvvQuestClient` 中，并让 endpoint 可配置；若未来支持自托管 MemeMeow v2，再新增明确的第二种协议适配器，而不是在同一解析函数里猜响应格式。

## 本插件的实现落地

本仓库的 v1.0.0 已按讨论后的约束实现，而非沿用本调研初稿的 MVP 建议：

- 命令固定为 `/zvv <描述> [数量]`；配置只有 `default_count`（默认 3）和 `max_count`（默认 10），
  两者均限制在 1–50。
- 使用共享 `httpx.AsyncClient` 调用硬编码的 legacy `GET /search`，不提供 endpoint、超时或冷却的
  控制面板配置。
- 插件先将图片下载到内存（每张至多 8 MiB），检验 PNG/JPEG/GIF/WebP 签名，再使用
  `Image.fromBytes()`；多张结果封装为 QQ/NapCat 合并转发。
- 全局 API 查询并发上限为 2，单用户固定冷却 3 秒，图片下载并发上限为 3；API 与图片都只对
  连接/超时/502/503/504 重试一次。
- `main.py` 保留 AstrBot 生命周期和消息层，`client.py` 处理 HTTP、响应与图片校验，`logic.py`
  处理参数、配置及冷却；默认测试通过 `httpx.MockTransport` 离线运行。

这些选择适配当前线上 legacy API；将来支持自托管 MemeMeow v2 时，应新建明确的协议适配器，
不在同一个响应解析函数中猜测协议。

## 尚未得到保证的事项

- 未找到线上服务的 SLA、稳定性承诺、版本号或正式鉴权/配额文档。
- 未验证线上部署使用的确切 embedding 模型或是否与某个开源提交一致。
- 未调用增强搜索，以免消耗其明确标注的稀缺 LLM/联网额度；其契约来自当前网页和作者客户端源码。
- 未验证 QQ/NapCat 对一次消息发送多张约 1 MB 图片的实际限制；实现后应做一次小规模 live test。
