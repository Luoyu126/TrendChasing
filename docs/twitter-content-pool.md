# Twitter / X 内容池

复用现有 RSSHub 服务和 SQLite 内容池。个人配置放在 `config/content_sources.local.yaml`：

```yaml
twitter:
  include_replies: false
  include_retweets: true
  accounts:
    - "https://x.com/OpenAI"
    - "https://x.com/karpathy"
```

账号支持主页链接、用户名或 `@用户名`；统一转小写并去重。
每个账号使用 `/twitter/user/<用户名>/includeReplies=0&includeRts=1` 路由。
若需要回复，设置 `include_replies: true`。

## 登录配置

只提取用户 Cookie 中的 `auth_token` 值，在本机 `docker/rsshub.env` 配置：

```dotenv
TWITTER_AUTH_TOKEN='auth_token 的值'
```

现有 RSSHub 实现会用这个令牌建立 Cookie 会话；不要把整个 Cookie 请求头填入该变量。
更新后执行：

```bash
sudo docker compose -f docker/docker-compose.rsshub.yml up -d --no-build
```

凭据仅保存在被 Git 忽略、权限为 600 的本地文件，不写入共享配置。
不要公开容器完整日志：RSSHub 某些调试日志可能包含认证令牌。

## 采集

```bash
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect --platform twitter
python3 scripts/collect_content.py --config config/content_sources.local.yaml status
```

遇到限流时保留已入库数据，等待平台/RSSHub 的冷却时间结束后再补采：

```bash
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect --platform twitter --resume
```

`--resume` 跳过最近状态为 `success` 或 `partial` 的来源，重试空结果、失败、中断和尚未采集的账号。
它只用于本轮补采，不能与 `--watch` 一起使用；常规刷新不要加此参数，否则会跳过旧的成功账号。
不要清空 Redis 令牌锁来绕过冷却。空结果可能来自限流，不能直接认定账号无内容。

不带 `--platform` 会采集全部已配置平台；加 `--watch` 会前台持续轮询。
本次没有安装后台定时任务。

推文按 `platform=twitter` 和 `content_id=tweet:<数字ID>` 保存。
`x.com` 与 `twitter.com` 地址中的同一 ID 会合并；不同订阅来源的发现关系单独保存。
普通短推文的标题和正文可以相同，因此不会仅因二者相同就判断为正文缺失。
原始 RSS、完整 HTML 和文本均保存；媒体只保存链接，不下载、不识别图片文字。

## 日期和完整性

当前 RSSHub 用户 Web API 请求中 `count` 被固定为 20，路由不提供按天完整补抓。
首次采集是近期窗口，不保证返回条目数为 20，也不保证当天内容抓全。
现有路由可能捕获上游异常后返回空 RSS；`empty` 不能证明账号没有发帖，应结合诊断检查。

RSSHub 的转发条目可能使用转发事件链接、原帖发布时间和内容。这里保留源数据：
不同转发事件可能分别入库；按日查询使用 RSS 发布时间，不能解释为当天转发的完整记录。
若要按原帖合并或按转发发生时间统计，需要后续增加事件与原帖的结构化关联。

验证某个账号能返回内容，不等于其余账号的权限、改名状态或内容窗口已经验证。
