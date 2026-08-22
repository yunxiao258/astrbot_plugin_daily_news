# AstrBot 每日新闻聚合播报插件

AstrBot 插件：手动或定时抓取并推送今日聚合新闻（标题+摘要+来源）。v1.1.0 新增分类订阅、关键词过滤与历史回看。

- 作者：云晓
- 版本：1.1.0
- 许可证：MIT（详见 LICENSE）

## 功能

### 手动抓取 (`/新闻`)
发送 `/新闻` 立即抓取并推送今日聚合新闻（默认最多 5 条，含时间、标题、摘要、来源）；`/新闻 <关键词>` 则按关键词 Bing 搜索并展示结果（如 `/新闻 AI 新闻`）。

### 分类订阅（v1.1.0）
- `/新闻 <分类名>`：按预置分类关键词搜索，如 `/新闻 科技`、`/新闻 财经`
- 预置 12 类：科技 / 互联网 / 财经 / 股票 / 体育 / 足球 / 娱乐 / 游戏 / 汽车 / 健康 / 教育 / 国际
- `/新闻 分类`：列出全部可用分类
- 也支持任意自定义关键词直接搜索

### 关键词过滤（v1.1.0）
- `news_filter_keywords`（黑名单）：标题或摘要命中任一词的新闻剔除
- `news_required_keywords`（白名单）：设置后仅保留命中任一词的新闻
- 全部被过滤时不强制清空，自动放弃过滤避免推送空白

### 历史回看（v1.1.0）
- 每次成功抓取（手动 / 定时 / 搜索）自动把当日快照存档到 `plugin_data`
- `/新闻 历史`：回看最近一天的新闻快照
- `/新闻 历史 2026-08-15`：回看指定日期（支持 `MM-DD` 简写）
- 快照保留 `news_archive_days` 天（默认 7，上限 90），过期自动清理

### 定时播报
- 后台每 30 秒检查一次，到达配置时间 `news_push_time` 时向目标群自动推送今日聚合新闻
- 同一天同一群只推送一次（去重记录持久化到 plugin_data），避免重复骚扰
- 抓取失败返回空时本次跳过推送，不影响插件运行

### 新闻时效
- 自动解析 RSS/Atom 的发布时间（`pubDate`/`published`），每条新闻带时间标签（当天显示 `[HH:MM]`，跨天显示 `[MM-DD HH:MM]`），统一按北京时间显示
- 按 `news_max_age_hours` 过滤旧新闻（默认 24 小时，0=不过滤）；严格过滤为空时自动放宽到 2 倍时长，仍为空则全部保留并标注时间，避免推送为空

## 依赖

- 仅依赖 AstrBot 核心库 + Python 标准库（`urllib.request` / `xml.etree`），无需第三方包

## 配置

见 `_conf_schema.json`。关键项：

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `news_push_enable` | bool | `false` | 是否启用定时播报 |
| `news_push_time` | string | `"08:00"` | 每日播报时间（HH:MM，非法值回退 08:00） |
| `news_push_groups` | string | `""` | 目标群号，英文逗号分隔，留空则不推送 |
| `news_push_platform` | string | `""` | 平台实例 ID（留空自动学习，见上文推送平台说明） |
| `news_max_age_hours` | int | `24` | 新闻最大时效（小时），0=不过滤 |
| `news_bing_query` | string | `""` | 定时播报的 Bing 搜索关键词（建议用具体词，如「AI 科技」；泛词如「新闻」匹配质量差） |
| `news_filter_keywords` | string | `""` | 过滤黑名单关键词，英文逗号分隔，命中即剔除 |
| `news_required_keywords` | string | `""` | 过滤白名单关键词，设置后仅保留命中项 |
| `news_archive_days` | int | `7` | 历史快照保留天数（1~90） |

定时播报推送目标 UMO 由 `平台ID:GroupMessage:群号` 组成（平台 ID 如 `云晓`，可通过群内 `/新闻` 自动学习，或手动在 `news_push_platform` 指定）。

## 新闻来源

`main.py` 中的 `NEWS_SOURCES` 定义了可替换的抓取源，默认优先使用国内可访问的免费 RSS：

- 36氪 RSS：`https://36kr.com/feed`
- cnBeta RSS：`https://www.cnbeta.com.tw/backend.php`
- 新浪新闻 RSS：`https://rss.sina.com.cn/news/china/focus15.xml`

`_fetch_news()` 依次尝试各源（单源失败自动降级到下一源），兼容 RSS（`<item>`）与 Atom（`<entry>`），用标准库 `urllib.request` 抓取、`xml.etree.ElementTree` 解析。全部失败返回空列表，不影响插件运行。

### Bing 搜索（无 API Key）
配置 `news_bing_query`（或手动 `/新闻 <关键词>`）后，优先通过 `cn.bing.com/search?format=rss` 的 RSS 输出抓取与关键词相关的搜索结果（无需 Bing API Key），失败自动降级到 RSS 源。

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
D:\uv-tools\astrbot\Scripts\python.exe -m unittest discover -s . -p "test_*.py"
```

测试覆盖：手动推送、新闻文本格式化（标题/摘要/来源）、定时到达触发推送、同天去重、抓取失败降级不崩溃、关闭时不推送、关键词黑/白名单过滤、全过滤兜底、历史快照存档与按日期回看（含 MM-DD 简写）、过期清理、损坏归档重置、分类订阅查询转换、配置脏值防御。测试全程不联网、不写真实 plugin_data（mock 抓取 + 临时目录持久化 + 假 context）。

## 更新记录

### v1.1.0
- 新增**分类订阅**：`/新闻 <分类名>` 按预置 12 类关键词搜索，`/新闻 分类` 列出全部分类
- 新增**关键词过滤**：`news_filter_keywords` 黑名单剔除 + `news_required_keywords` 白名单保留，全过滤时自动放弃避免空白
- 新增**历史回看**：每次成功抓取自动存档当日快照，`/新闻 历史 [日期]` 回看，支持 `MM-DD` 简写，保留天数可配（默认 7 天）

### v1.0.0
- 首个版本：手动 `/新闻` 抓取推送 + 定时播报 + 同天同群去重