# 知乎账号采集

沿用 [小红书部署步骤](xiaohongshu-content-pool.md)的 RSSHub 和 SQLite 内容池。
本次无需修改 RSSHub 源码或数据库结构。

在 `config/content_sources.local.yaml` 添加账号主页链接或 URL 标识：

```yaml
zhihu:
  accounts:
    - "https://www.zhihu.com/people/su-jian-lin-22"
    - "https://www.zhihu.com/people/tian-yuan-dong"
```

每个账号采集两种来源：

- 回答：`/zhihu/people/answers/<账号标识>`。
- 文章：`/zhihu/posts/people/<账号标识>`。

这两类属于作者发布内容；暂不包含想法、提问、点赞、关注和收藏动态。
内容 ID 分别使用 `answer:<ID>` 和 `article:<ID>`，防止不同类型数字 ID 冲突。
不同来源发现同一内容时共享主体记录，保留各自的发现关系。

## 登录配置

在本机 `docker/rsshub.env` 中添加：

```dotenv
ZHIHU_COOKIES='你的知乎请求 Cookie'
```

本机已配置用户提供的 `_xsrf`、`d_c0`、`__zse_ck` 和 `z_c0`。
路由用 `d_c0` 和 `__zse_ck` 初始化已有会话，`z_c0` 提供登录状态。
不要将 Cookie 放入公开配置、提交或聊天。更新后重新创建容器：

```bash
sudo docker compose -f docker/docker-compose.rsshub.yml up -d --no-build
```

## 执行

```bash
# 只采集知乎
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect --platform zhihu

# 采集全部已配置平台
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect

# 持续轮询全部平台（前台运行；本次未安装后台定时任务）
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect --watch

# 统计和按发布日期查询共用内容池
python3 scripts/collect_content.py --config config/content_sources.local.yaml status
python3 scripts/collect_content.py --config config/content_sources.local.yaml list --date 2026-09-09
```

查询结果的 `platform` 字段可区分知乎和小红书；日期以配置的时区为准。
缺失稳定 ID 或原文链接时会标记 `missing_content_id` / `missing_content_link`。
正文保留 RSSHub 返回的 HTML 和提取文本，不做截断或 AI 筛选。

## 当前限制

当前子模块回答路由的请求参数为 `limit=7`，文章为 `limit=20`；平台实际返回数量可能不同，没有历史翻页补抓。
首次入库包含旧内容，不代表都是当天发布；持续轮询也不能证明无遗漏。
RSSHub 的回答路由直接访问返回数组的第一项，如果账号没有回答，可能报错而不是返回空 RSS。
采集器将这种响应记为失败，不能仅凭失败推断账号没有内容。
登录 Cookie 失效或平台拒绝访问时同样需要实测排查；不会把请求失败记成成功的空列表。

`quality_flags` 仅是结构检查，不证明登录成功、正文完整或当天内容已抓全。
