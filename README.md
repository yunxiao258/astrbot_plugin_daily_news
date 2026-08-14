# AstrBot 每日新闻聚合播报插件

AstrBot 插件：手动或定时抓取并推送今日聚合新闻（标题+摘要+来源）。

## 功能

### 手动抓取 (`/新闻`)
发送 `/新闻` 立即抓取并推送今日聚合新闻（默认最多 5 条，含标题、摘要、来源）。

### 定时播报
- 后台每 30 秒检查一次，到达配置时间 `news_push_time` 时向目标群自动推送今日聚合新闻
- 同一天同一群只推送一次（去重记录持久化到 plugin_data），避免重复骚扰
- 抓取失败返回空时本次跳过推送，不影响插件运行

## 依赖

- 仅依赖 AstrBot 核心库 + Python 标准库（`urllib.request` / `xml.etree`），无需第三方包

## 配置

见 `_conf_schema.json`。关键项：

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `news_push_enable` | bool | `false` | 是否启用定时播报 |
| `news_push_time` | string | `"08:00"` | 每日播报时间（HH:MM，非法值回退 08:00） |
| `news_push_groups` | string | `""` | 目标群号，英文逗号分隔，留空则不推送 |
| `news_push_platform` | string | `"onebot"` | 消息平台标识（UMO 前缀） |

定时播报推送目标 UMO 由 `平台:GroupMessage:群号` 组成，例如 `onebot:GroupMessage:123456789`。

## 新闻来源

`main.py` 中的 `NEWS_SOURCES` 定义了可替换的抓取源，默认优先使用国内可访问的免费 RSS：

- 36氪 RSS：`https://36kr.com/feed`
- cnBeta RSS：`https://www.cnbeta.com.tw/backend.php`
- 新浪新闻 RSS：`https://rss.sina.com.cn/news/china/focus15.xml`

`_fetch_news()` 依次尝试各源（单源失败自动降级到下一源），兼容 RSS（`<item>`）与 Atom（`<entry>`），用标准库 `urllib.request` 抓取、`xml.etree.ElementTree` 解析。全部失败返回空列表，不影响插件运行。

## 数据

去重推送记录持久化于 `plugin_data/astrbot_plugin_daily_news/`：

- `pushed.json`：`{日期: [已推送群号, ...]}`，用于同天同群去重（跨重启生效）

## 使用示例

```
/新闻                    # 手动抓取并推送今日聚合新闻
```

配置示例（定时播报）：
```json
{
  "news_push_enable": true,
  "news_push_time": "08:00",
  "news_push_groups": "123456789,987654321",
  "news_push_platform": "onebot"
}
```

## 测试

运行（Windows）：
```bash
D:\uv-tools\astrbot\Scripts\python.exe test_daily_news.py
```

测试覆盖：手动推送、新闻文本格式化（标题/摘要/来源）、定时到达触发推送、同天去重、抓取失败降级不崩溃、关闭时不推送、配置脏值防御。测试全程不联网、不写真实 plugin_data（mock 抓取 + 内存持久化替身 + 假 context）。

## 更新记录

### v1.0.0
- 首个版本：手动 `/新闻` 抓取推送 + 定时播报 + 同天同群去重