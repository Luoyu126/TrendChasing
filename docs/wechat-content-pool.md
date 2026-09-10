> GitHub 托管部署使用 [Actions + Supabase 独立入口](github-actions-supabase.md)；本文的本机定时、SQLite 与旧热榜入口说明不用于托管调度。

# 微信公众号采集

使用已有 RSSHub 的 `/wechat/sogou/:id` 公开搜狗入口，不需要 Cookie。
在 `config/content_sources.local.yaml` 添加：

```yaml
wechat:
  accounts:
    - almosthuman2014 # 机器之心
    - qbitai # 量子位
    - ai_era # 新智元
```

填写微信号，文章分享短链接不能直接作为订阅配置。

```bash
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect --platform wechat
```

默认全平台采集也会包含这些订阅。若已有长期运行的采集进程，需要重启以重新加载配置。
搜狗搜索结果可能不全或滞后，不保证当日文章齐全，也不支持历史翻页补抓。
微信可能返回验证页；返回 RSS 或有正文不代表正文完整，应核对文章原文。
短链接按短码去重，长链接按 `__biz + mid + idx` 去重；两种形式尚不能跨形式合并。
公众号内容沿用内容池的正文、发布日期和质量标记机制。

本机验证：机器之心与量子位仅返回旧文章搜索摘要，缺少微信原文链接，标记 partial；当前入口不能作为每日完整文章来源。

## WeRSS 本机部署

部署配置在 `docker/docker-compose.werss.yml`，管理员账号密码在被 Git 忽略的
`docker/werss.env`（权限 600），持久数据在 `output/werss/`。

```bash
sudo docker compose -f docker/docker-compose.werss.yml up -d
```

管理页面为 `http://127.0.0.1:8001`。扫码授权后添加公众号，并验证更新结果。
将相应微信号从 `wechat.accounts` 移到 `wechat.feeds`，例如：

```yaml
wechat:
  accounts: []
  feeds:
    - id: almosthuman2014
      url: http://127.0.0.1:8001/feed/实际订阅ID.xml
```

订阅 ID 须使用服务实际生成的值。此配置直接读取 WeRSS，绕过 RSSHub。

当前部署已完成微信扫码授权并添加三个公众号，内容池本地配置已切换为 WeRSS。
首次更新遇到微信频率限制，尚未验证全文入库。容器已启用正文采集；不要连续手动刷新。
本机源地址：`/feed/MP_WXS_3073282833.xml`（机器之心）、
`/feed/MP_WXS_3236757533.xml`（量子位）、`/feed/MP_WXS_3271041950.xml`（新智元）。
读取 RSS 只读取 WeRSS 已存文章，不等于刷新微信文章列表；后续需配置更新任务。

## 当前可用通道：微信读书

微信公众平台凭据有效，但文章接口实测返回 `200013 / freq control`。
已改用 WeRSS 内置微信读书授权，三个公众号各验证采集到一篇正文并写入内容池。
采集器按 `refresh_werss: true` 先调用微信读书采集接口，再读取 RSS，
使用 `credentials_file: docker/werss.env` 的本地管理凭据。

该版本只提供每个公众号最新一篇，无法回补历史列表，多篇连发可能漏掉。
其 RSS 发布日期实际来自抓取时间，因此本地配置设置 `trust_published_at: false`，
入库时清空发布日期并标记 `missing_published_at`（状态 partial），不伪装为当日发布文章。
正文和原文链接已保留，但日期筛选无法使用这些未核实日期的条目。

已启用用户级 `trendradar-wechat-collect.timer`，每 30 分钟刷新并入库，
只运行公众号采集；用户 Linger 已开启。首次 systemd 任务已验证退出码 0。
使用 `systemctl --user list-timers trendradar-wechat-collect.timer` 查看下次执行时间。
