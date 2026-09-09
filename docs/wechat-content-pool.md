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
