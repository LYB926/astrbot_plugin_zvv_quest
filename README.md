# astrbot_plugin_zvv_quest

通过 [ZVVQuest](https://zvv.quest/) 的公开只读接口查询张维为表情包的 AstrBot 插件。
这是社区插件，与网站及图片权利人无隶属关系；插件不上传图片、不修改上游数据。

## 指令

```text
/zvv <描述> [数量]
```

示例：

- `/zvv 我们的网民`：使用控制面板中的默认数量（默认 3）。
- `/zvv 我们的网民 5`：本次请求 5 张。
- `/zvv 2025`：搜索“2025”，不会把唯一的数字视为数量。

显式数量必须在 1 至当前最大值之间；无描述、0、负数或超限都会返回用法提示而不访问上游。
每位用户的 `/zvv` 调用有固定 3 秒冷却，失败请求同样计入冷却。

单张结果作为普通图片发送。两张或以上时，插件会将每张图置于一个独立节点中，以 QQ/NapCat
合并转发发送。实际可发送数量会受上游结果和下载成功情况影响；不同平台对合并转发的支持可能不同。

## 配置

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `default_count` | `3` | 未显式提供数量时的返回数，范围 1–50。 |
| `max_count` | `10` | 单次指令的最大数量，范围 1–50。建议不超过 10。 |

`max_count` 过大将增加图片下载的内存占用，也更容易触发消息平台的合并转发限制。插件在重载时读取
配置：非法值回退或截断到合法范围；若默认值高于最大值，会临时按最大值发送并记录警告。

## 网络与安全边界

插件只调用 legacy 搜索接口：

```text
GET https://api.zvv.quest/search?q=<描述>&n=<数量>
```

它不会调用网页的联网增强搜索，也不会抓取网页 HTML。API 查询全局最多两个并发，请求连接/总超时为
5/15 秒；图片下载最多三个并发，连接/总超时为 5/20 秒。连接、超时和 502/503/504 会重试一次。

插件仅接受上游返回的无用户名和密码的 HTTPS 图片 URL，下载时限制每张 8 MiB，并用 PNG、JPEG、
GIF 或 WebP 文件签名验证内容，不依赖 CDN 的 MIME 类型。上游或网络故障的详细信息只写入机器人日志。

请注意：公开接口和第三方图片没有可用性或版权保证。原项目的 MIT 许可证覆盖其代码，不自动授予
图片再分发许可；如有权利问题，请遵循上游的处理渠道。

## 开发与测试

在本插件目录执行：

```bash
uv run --isolated --with pytest --with httpx pytest
uv run --isolated --with ruff ruff check .
/home/ke/.local/share/uv/tools/astrbot/bin/python -m compileall -q .
cd /tmp && PYTHONPATH=/home/ke/code-chat \
  /home/ke/.local/share/uv/tools/astrbot/bin/python -c \
  'import astrbot_plugin_zvv_quest.main'
```

默认测试全部离线，使用 `httpx.MockTransport`，不会请求 ZVVQuest。可选的只读线上冒烟测试：

```bash
ZVV_QUEST_LIVE_TEST=1 uv run --isolated --with pytest --with httpx pytest -m live
```

## License

[MIT](LICENSE)
