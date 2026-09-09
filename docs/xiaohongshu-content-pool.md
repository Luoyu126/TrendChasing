# 小红书账号采集与内容池

这一阶段只做账号列表、轮询、原始内容保存、去重和按日期查询，不触发 TrendRadar 的 AI 或通知。
RSSHub 负责平台访问，外层 `scripts/collect_content.py` 负责持久化。
当前支持小红书笔记、[知乎回答和文章](zhihu-content-pool.md)、[Twitter/X](twitter-content-pool.md)，共用内容与订阅来源分离的表结构。

## 1. 启动 RSSHub

需要 Docker Compose，首次启动会从子模块源码构建包含 Chromium 的镜像。
Ubuntu 且已启用 systemd 的机器可运行 `sudo bash scripts/install_docker_ubuntu.sh` 安装 Docker。
下面使用 `sudo docker`；如果已配置当前用户的 Docker 访问权限，可以省略 `sudo`。
在仓库根目录运行：

```bash
git submodule update --init --recursive
cp docker/rsshub.env.example docker/rsshub.env
chmod 600 docker/rsshub.env
```

在自己的浏览器中登录小红书，打开开发者工具的 Network，刷新主页，
从发往小红书的请求中复制请求头 `Cookie` 的完整值（不含 `Cookie:` 前缀）。
在本机编辑 `docker/rsshub.env`：

```dotenv
XIAOHONGSHU_COOKIE='完整的 Cookie 值'
```

不要将 Cookie 发到聊天、写入账号配置或提交 Git。实际环境文件已忽略。
Cookie 用于 RSSHub 尝试获取正文；失效后需在本机更新并重新创建服务。

```bash
sudo docker compose -f docker/docker-compose.rsshub.yml up -d --build
curl --fail http://127.0.0.1:1200/healthz
```

服务仅发布到本机 `127.0.0.1:1200`。此 Compose 可以独立运行，源码修改需重新构建。
RSSHub 的 Redis 只是缓存，持久内容池是下面的 SQLite 数据库。

## 2. 配置账号

```bash
cp config/content_sources.yaml config/content_sources.local.yaml
```

编辑本地配置的 `xiaohongshu.accounts`，填入完整主页链接或用户 ID：

```yaml
xiaohongshu:
  accounts:
    - "https://www.xiaohongshu.com/user/profile/593032945e87e77791e03696"
```

该 ID 只是路由格式示例，并非默认订阅。请换成自己的目标账号。
用户 ID 是主页 URL 中的 24 位标识，不是昵称或应用里显示的小红书号。
短链接请先在浏览器打开，复制跳转后的完整主页链接。

可直接验证一个账号的 RSS：

```bash
curl --fail 'http://127.0.0.1:1200/xiaohongshu/user/593032945e87e77791e03696/notes'
```

HTTP 成功只说明服务返回了内容，还需要检查笔记正文、链接与日期。

## 3. 采集与查询

Python 需要 PyYAML（已在项目依赖中）；采集脚本其余部分只用标准库。
已安装项目依赖时直接执行；若单独运行脚本，可在自己的虚拟环境中安装 PyYAML。

```bash
# 单次采集
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect

# 按配置持续轮询，默认每轮结束后等待 30 分钟，Ctrl-C 停止
python3 scripts/collect_content.py --config config/content_sources.local.yaml collect --watch

# 查看采集状态和缺少日期的条目数量
python3 scripts/collect_content.py --config config/content_sources.local.yaml status

# 查询指定自然日发布的内容，按配置 timezone 判断；输出 JSON
python3 scripts/collect_content.py --config config/content_sources.local.yaml list --date 2026-09-09
```

`--watch` 是前台进程；持续运行需使用进程管理器，或用定时任务调用单次采集。
本次未安装系统级定时任务。默认数据库位于 `output/content_pool/content.sqlite3`，不纳入 Git。

## 数据与完整性

- `items`：平台、内容 ID、原文链接、作者、标题、未经截断的正文 HTML/文本、发布时间及首次/最近发现时间。
- `observations`：内容通过哪些订阅账号被发现；同一笔记不会因为多个来源而重复保存主体。
- `fetch_runs`：每次采集状态和原始 RSS 响应，便于后续重解析、核对。无自动清理，需关注磁盘使用。

发布时间统一保存为 UTC，查询时使用配置时区的自然日起止边界。
不在入库阶段过滤旧内容；未知发布日期不会用抓取时间替代，也不会出现在指定发布日的结果中。
图片、视频保存的是 RSS 返回的链接和 HTML，不下载媒体、不做图片 OCR。

小红书路由可能降级成封面和标题。采集器保留这类原始结果，并标记：

- `missing_note_id`：未找到稳定笔记 ID，使用临时来源内标识；之后升级为真实 ID 时尚不能自动合并。
- `missing_note_link`：缺少可识别的笔记原文链接。
- `missing_published_at`：缺少有效且带时区的发布时间。
- `possibly_incomplete_body`：正文为空或仅与标题相同；这是启发式提示，不是全文完整性的证明。

状态 `success` 表示返回条目未触发上述提示，**不代表该账号当日内容已抓全**。
`partial` 表示有条目触发提示，`empty` 表示返回空列表，`failed` 表示请求或解析失败。
失败会在下一轮重试；进程中断时可能留下 `running` 记录，后续轮询会新增记录。

当前 RSSHub 路由读取最新页面，未实现按天翻页补抓。高频轮询也不能保证无遗漏；
中断期间、账号更新过多、平台拒绝访问等情况都需要后续补抓能力。
后续分类应基于完整正文和质量标记，不能将日期未知或内容不完整的记录当作完整样本。

## 本地验证

```bash
python3 -m unittest discover -s tests -p test_content_pool.py
```

离线测试覆盖重复采集、跨订阅去重、长正文保留、时区日期边界、降级内容和请求失败。
真实账号连通性仍须在部署后验证。
