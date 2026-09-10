# GitHub Actions + Supabase 部署

本部署使用两个独立入口，复用已有分类、论文评分、主题汇总和邮件模板。代码不会自行
push 或启用仓库变量。首次提交后定时 job 仍跳过，必须按下面步骤显式启用。

## 检查结论与迁移边界

本地已存在的 `supabase_sync.py` 是完整快照初次导入器，包含正文、记录、论文缓存、
用量、冷却、批次和回执；它不是运行驱动。本次新增 PostgreSQL 运行适配，使用原有
`trendradar` schema 和表结构，保留 SQLite 本地模式及既有迁移工具，不做双向同步。

2026-09-10 本地验证：现有 `supabase.local.env` 可建立 verify-full TLS 只读连接，但
`information_schema.tables` 中 `public` / `trendradar` 应用表列表为空。**这不能证明其他
项目/凭据下没有迁移；应先核对目标项目、数据库和角色权限。没有改动远程数据库。**
运行适配在找不到 `trendradar.records` 时返回 `supabase_import_required`，不会新建另一个库。

确认正确目标后先运行：

```bash
python -m pip install . -r requirements-supabase.txt
python scripts/check_supabase.py
```

若确实尚未向这个目标迁移，停掉旧本地/systemd 写入任务，使用已有工具：

```bash
python scripts/sync_supabase.py                         # 只生成本地快照和计划
python scripts/sync_supabase.py --snapshot /path/to/reviewed.sqlite3 --validate
python scripts/sync_supabase.py --snapshot /path/to/reviewed.sqlite3 --execute
```

`--validate` 在远程事务内导入、校验后回滚；`--execute` 才提交。已有不同目标数据时工具
拒绝覆盖，不能用删除 schema 的方式强行重导。启用运行适配后也不要再用初次导入器
“同步”不断变化的数据库。若已有迁移采用不同 schema，先对齐目标，不能直接复制空库。

首次运行在已有 schema 内以 `CREATE TABLE IF NOT EXISTS` 补齐运行表，并新增
`digest_days(day PRIMARY KEY, batch_id UNIQUE REFERENCES batches)`。已有
`daily_publications` 按原来“发送日的前一天”归入内容日期，重复日期的旧批次会阻止启动，
要求人工核对，不会自行删除回执。旧批次若使用其他窗口，应在启用前人工核对该映射。
连接角色应能读写并创建这些私有运行表；迁移工具已撤销匿名角色的 schema 权限。
不要把该 schema 暴露给客户端或配置公共读取。

## Secrets 与 Variables

在 `Luoyu126/TrendRadar → Settings → Secrets and variables → Actions` 配置：

| 类型 | 名称 | 要求 |
| --- | --- | --- |
| Secret | `SUPABASE_DATABASE_URL` | 必填；PostgreSQL URI，使用 direct 或 **Session pooler**（通常 5432），不使用 Transaction pooler 6543。runner 通常需要 IPv4 Session pooler。密码按 URI 转义 |
| Secret | `AI_API_KEY` | 日报分类/评分/汇总必填；采集 job 不注入 AI Key |
| Secret | `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_PASSWORD` | 正式发送必填；收件人用逗号分隔，预览不需要 SMTP 凭据 |
| Secret | `XIAOHONGSHU_COOKIE`, `ZHIHU_COOKIES`, `TWITTER_AUTH_TOKEN` | 对应平台所需登录凭据；名称与定制 RSSHub 路由一致；缺失或过期会记录来源失败 |
| Secret | `WERSS_BASE_URL` | 公众号所需外部 HTTPS WeRSS 基地址，不带末尾 `/`、用户名密码、查询参数；支持保留服务路径前缀。未配置会记录三条公众号失败，不阻断其他来源 |
| Variable | `AI_MODEL`, `AI_API_BASE` | 可选，留空沿用 `config/config.yaml` 的现有模型配置 |
| Variable | `EMAIL_SMTP_SERVER`, `EMAIL_SMTP_PORT` | 发送时 server 必填；port 默认 465，其他端口采用 STARTTLS |
| Variable | `CONTENT_SOURCES_CONFIG` | 可选采集配置路径，默认 `config/content_sources.actions.yaml`，必须已提交到仓库 |
| Variable | `CONTENT_SCHEDULE_ENABLED` | 默认关闭；设为字面值 `true` 才运行两个 schedule job |
| Variable | `CONTENT_SEND_ENABLED` | 默认关闭；设为 `true` 才允许手动正式发送和早间定时发送 |
| Variable | `LEGACY_CRAWLER_ENABLED` | 保持未设置/false；仅用于明确恢复旧手动热榜流程 |

连接使用 `sslmode=verify-full` 和 `config/certs/supabase-ca.crt`。该文件是公开 CA 证书，
提交代码时需要包含；不含私钥。若项目更换 CA，更新证书。CLI 也支持 `PGSSLROOTCERT`。
不要把本地 `*.local.env`、Cookie 或 WeRSS 密码文件提交；Actions 不加载本机 env 文件。
新的配置保留现有公开账号订阅清单及三个公众号，未改变研究兴趣、评分阈值或报告结构。

## 从已有本地配置导入 GitHub 设置

本机已有 `supabase.local.env`、`ai.local.env`、`email.local.env` 和 `rsshub.env` 时，
先完成 GitHub CLI 登录，然后直接导入，不需要手工复制凭据：

```bash
python scripts/configure_github_actions.py                 # 仅列出名称和缺项
python scripts/configure_github_actions.py --apply         # 写入并核验
```

可用 `--gh /path/to/gh` 指定 CLI。脚本通过标准输入传递值，只打印配置名称，Secrets
由 GitHub CLI 加密后上传；不会把本地 env 文件提交。导入会明确关闭采集调度、邮件发送
和旧热榜开关，不会触发工作流。本机 WeRSS 地址不作为外部服务地址导入。

## 时间、职责和首次上线

| 工作流 | 默认时间 | 职责 |
| --- | --- | --- |
| Collect Content | 每小时第 34 分钟 | 构建临时定制 RSSHub + Chromium + Redis，RSS 与 arXiv 元数据入库，记录每源状态；不执行 AI，不发送 |
| Morning Content Digest | America/New_York 07:34 | 数据库上一自然日及更早未投递补充内容，沿用分类/评分/主题与邮件逻辑 |

日报 YAML 使用 `cron: '34 7 * * *'` 和 `timezone: America/New_York`，GitHub 当前
[工作流语法文档](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)
已支持 IANA 时区，因此自动处理夏令时。业务窗口分别计算当地两个午夜，支持 23/25 小时日。
手动 `date` 指**内容日期**，不是发送日期；空值按实际运行时纽约日期的前一天计算。
跨午夜延迟或漏跑请显式补日期，不依赖触发时间戳推测窗口。

1. 核对 Supabase 目标与表，并配置 Secrets。确认 fork `Luoyu126/RSSHub` 的固定子模块
   commit 能通过 HTTPS 读取；当前 `.gitmodules` 已改为 HTTPS，不需要个人 SSH 私钥。
   若 fork 私有，默认仓库 GITHUB_TOKEN 无法读另一个私有仓库，需提供单独只读访问方式
   或将所需 fork 可读；不能悄悄换成上游镜像。
2. 自行审阅并提交/push 这些修改（包括已有迁移文件、CA 证书、新 workflow 和订阅配置）。
   schedule 仅使用默认分支。两个启用变量先不设置。
3. Actions 手动运行 **Collect Content**。检查来源结果和 Supabase `fetch_runs`、
   `records`、`items`；再次运行确认 identities 不增加，采集尝试记录增加是正常的。
4. 手动运行 **Morning Content Digest**，`mode=preview`，可填内容 `date`。
   这会调用 AI、保存预算/缓存和固定日期批次，**不创建或确认投递记录、不发送**。
   如已有批次，预览重建相同内容；后续新内容作为下一期补充，不修改已固定快照。
5. 预览数据在 Supabase `batches.snapshot` / `summary` 和 `analysis_cache` 的
   `window:<batch_id>`、`notice:<batch_id>` 中。若需查看 HTML，在自己的机器上仅注入
   同一个 Supabase 连接、AI 配置，执行下面的 CLI；会由数据库重建本地 HTML，输出路径。
   Actions 不上传包含正文的 artifact，也不将 artifact/cache/Git 当业务库。
6. 审阅后设 `CONTENT_SEND_ENABLED=true`，手动选择 `mode=send`、同一 `date` 正式发送。
   重跑同一天会使用同一批次，已确认成功的收件人不会再发。
7. 确认后设 `CONTENT_SCHEDULE_ENABLED=true` 开启两项调度；停用旧 systemd timers、
   Render Cron 或其他仍运行旧 SQLite 程序的机器，避免双库并行和重复邮件。

本地数据库日报示例（凭据通过环境注入；不要把真实 URI 写进命令历史）：

```bash
export CONTENT_DATABASE_BACKEND=supabase
python scripts/configure_hosted.py
python scripts/run_content_daily.py --config output/hosted-config.yaml --database-only --date 2026-09-09 --dry-run
# 用户确认上线后，去掉 --dry-run 并加 --send
```

`--dry-run` 和 `--send` 互斥。已有的普通 `--skip-collect` 仍允许处理期间 arXiv 身份查询；
Actions 使用更严格的 `--database-only`，不抓社交源、不发起 arXiv 发现，仅允许 AI 调用。
论文元数据由采集阶段批量获取，并对内容中显式 arXiv 链接定向核验；只有标题、数据库尚无
匹配元数据的论文暂留待核验，不降低身份核验或推荐阈值，不凭空推荐。

## 跨 runner 状态和投递可靠性

- `CONTENT_DATABASE_BACKEND=supabase` 时业务读写直接进入 PostgreSQL，不下载本地 SQLite。
  原始正文和去重键、来源观察和抓取状态、论文元数据/评分、AI 缓存、用量及冷却、批次快照、
  窗口、逐收件人回执都跨运行保存。保留原有清理策略：确认全部投递后清理正文/快照，
  identities、投递回执、日期映射仍保留；“保存所有”指所有仍需跨运行使用的状态。
- 工作流共享 `content-supabase` concurrency，`cancel-in-progress: false`。数据库连接还
  持有同一个 session advisory lock，覆盖全部读写和 SMTP 阶段；连接失效即停止，不自动
  重连后继续发送。锁与业务使用同一连接，避免另开锁连接失效后旧进程仍写库。
  PostgreSQL session pooling 是硬性前提，不能用 transaction pooling。
- `(platform,content_id)`、来源关联和评分缓存使用主键/upsert。被投递的内容不会因重采复活。
  日报日期唯一关联一个批次；批次首次发送时固定收件人，回执主键为 `(batch_id,target,part)`。
  后来修改 EMAIL_TO 不会自动给该日期扩展收件人，避免改变已审核的投递范围。
- 发信前原子 `pending/failed → sending` 并提交。SMTP DATA 已接受但写回失败时，持久状态
  留在 `sending`；下次转换为 `uncertain`，不能盲目重发。只有确定失败的部分重试。
  `success` 仅表示 SMTP 接受，不等于到达收件箱，也不保证 exactly-once。
  对 uncertain 请核对服务商队列、Message-ID、收件箱后再用 `manage_content_pool.py acknowledge` 人工确认；
  没有证据不能把它改成 failed 以强制重发。
- 每个内容日期共用持久 AI 预算，调用前预留；预览/重跑不会重置该日期预算，崩溃时按可能
  已调用计数。原有模型间隔、冷却和指数退避保存在数据库。若耗尽预算，修改配置前先核对
  `usage_events`；不要删预算表重试。
- 单源超时及 429/5xx 有限重试；抓取状态区分 failed、empty、partial。日报提示最新来源
  的失败、空结果、不完整及过旧状态。超时中断的 running 状态也提示；下次先尝试最久
  未尝试的来源，避免长订阅列表永远只跑前几个。配置来源首次尚未尝试时，日报也会提示缺少采集记录。

人工核验不确定投递后（保持 `CONTENT_DATABASE_BACKEND=supabase`），例如：

```bash
python scripts/manage_content_pool.py --config output/hosted-config.yaml acknowledge BATCH_ID email:reader@example.com --state success --receipt '人工核验的队列或 Message-ID 证据'
```

只有明确拒收证据才选择 `--state failed`，之后手动重跑同日 send。确认 success 后重跑
同日 send 只清理已全部确认的批次，不会重发成功项。

## RSSHub / WeRSS 与排错

采集从仓库固定的定制 RSSHub 子模块构建（包含自定义小红书/知乎路由），Dockerfile
下载 Chromium 及运行库。Redis 只保存这次任务的临时 RSS 缓存；去重和进度不依赖它。
`compose up --wait --wait-timeout 240` 有界等待健康；workflow `always()` 删除容器及卷。
runner 被平台强制销毁时临时环境也消失，不影响 Supabase 状态。

WeRSS 需要扫码登录、订阅和会话持久化，现有 Compose 把 `/app/data` 挂到
`output/werss`。它**不适合每次临时启动**。保留一个外部 WeRSS 实例及持久数据卷、登录
与刷新任务，由它生成 RSS，再把 HTTPS 基地址填入 `WERSS_BASE_URL`。Actions 不登录
WeRSS、不刷新它的订阅，仍需部署和维护这部分服务。现有三个 feed ID 路径保持不变；
如外部实例重建导致 ID 改变，更新 `content_sources.actions.yaml`。订阅的
`trust_published_at: false` 保留原来行为：无法确认原始发布日期的正文等待补全，不拿
WeRSS 抓取时间充当发布日期。应先修好外部 RSS 日期再决定是否显式信任它。

常见故障：

- RSSHub 构建失败：检查 fork HTTPS 权限、子模块 commit、npm/浏览器下载网络。
- 健康检查失败：查看 step 的容器状态。避免直接将未经脱敏的 RSSHub 日志放进 Actions，
  它可能包含 Cookie、请求 URL 或响应正文。
- HTTP 401/403：Cookie 过期或登录校验；429：平台限流；网络超时：runner 出口或平台
  地区限制。GitHub 托管 runner 不保证能抓取所有社交平台，临时容器不会解除风控。
- `external_werss_not_configured`：未设置外部地址；WeRSS feed 空/缺日期：检查服务订阅、
  扫码会话、更新任务和原始文章发布时间。
- Supabase 无表：核对项目、数据库、schema 与角色权限；TLS 错误：核对 CA/主机名；
  网络不可达：使用 IPv4 Session pooler；不要关闭证书校验。
- `another_content_run_is_active`：其他实例持有数据库锁，等待其完成；不要删除回执。
- 调度可能排队、延迟，负载高时甚至丢弃。GitHub concurrency 也不是持久任务队列，
  多个 pending run 可能被替换；根据日期手动补跑。07:34 正好与小时采集冲突时会串行，
  实际邮件可能晚于 07:34。
- 公开仓库连续 60 天无活动可能自动停用 schedule；在 Actions 检查并手动恢复。
  这些是 GitHub 的[调度限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)，
  不使用自动 Git 提交制造活动，也不再沿用旧 7 天签到逻辑。

旧 `crawler.yml` 运行热榜主程序，可能独立推荐论文及邮件，已移除 schedule 并加
`LEGACY_CRAWLER_ENABLED` 手动开关。旧 `clean-crawler.yml` 会删除 workflow 运行记录，
同样加开关以保留新任务诊断；docker 发布、issue 工作流不采集/发日报，不变。

## 验证

```bash
python -m unittest discover -s tests -q
# 可选：TEST_POSTGRES_DSN 指向专用临时本机 PostgreSQL，测试会创建/删除独立测试数据库
TEST_POSTGRES_DSN='host=127.0.0.1 port=55439 user=test dbname=postgres' python -m unittest discover -s tests -p test_postgres_runtime.py -q
```

测试覆盖去重、事务回滚、跨连接恢复、数据库锁、日期批次、DST、预览投递状态不变、
成功不重发、SMTP 接受后丢回执不重发、数据库入口不发现论文、持久预算及配置。
本机 Docker socket 无权限，RSSHub 构建/真实平台采集及 GitHub runner 尚需首次手动
验证；没有调用真实 SMTP 或真实 AI 完成线上日报验证。

本次本地验证结果：119 项测试全部通过（包含 10 项真实 PostgreSQL 集成测试）；
actionlint 1.7.12 校验四个变更 workflow 通过，Compose 配置与 CLI 参数检查通过。
PostgreSQL 测试使用隔离本机数据库，SMTP/AI 为替身；测试实例验证后已停止。
