> GitHub 托管部署使用 [Actions + Supabase 独立入口](github-actions-supabase.md)；本文的本机定时、SQLite 与旧热榜入口说明不用于托管调度。

# 内容候选、正式池与日报

此流程沿用本地 `output/content_pool/content.sqlite3`。采集脚本只写候选；只有完成分类或论文筛选的内容才进入正式池。现已接通统一运行入口、按收件人记录的邮件投递，以及本机用户级定时任务。

WeRSS RSS、其他社交来源和 arXiv 原始论文现在先进入同一个候选池，再由
`Processor` 统一处理。论文元数据与评分缓存也保存在该数据库；旧版本已有论文库的
部署需要先执行[本地统一数据库迁移](unified-database.md)。

## 部署与试运行入口

```bash
# 自动读取本地 AI / 邮件配置，抓取与筛选，不发送
.venv/bin/python scripts/run_content_daily.py --collect-only
# 小批量真实采集、评分与日报预览
.venv/bin/python scripts/run_content_daily.py --trial
# 检查预览后，发送已有批次；不重复采集、评分
.venv/bin/python scripts/run_content_daily.py --send --deliver-batch BATCH_ID
# 早间日报，只发送已准备的内容
.venv/bin/python scripts/run_content_daily.py --send-ready --send
# 安装当前 checkout 的用户级定时任务
.venv/bin/python scripts/install_content_timers.py
```

默认按匹兹堡时区 `America/New_York`（自动夏令时）夜间 02:00 采集、串行处理并准备日报，早上 08:00 只投递已准备的日报。两个任务串行运行，失败或断线后的候选保持可恢复。已成功发送的日历日不会重复生成常规邮件；试运行独立于常规日报。机器及用户服务需要持续运行，休眠期间无法采集。

日报为适合手机阅读的 HTML 邮件：第一部分最多六个主题，每个主题有总结和逐篇作者/平台/观点/链接；第二部分为论文标题、作者、推荐理由和社交解读。完整原始摘要保留在池与 JSON 中，不在邮件正文展开。抓取失败仅在页脚用一行汇总，例如“采集暂缺：小红书 2 个订阅；后续补抓。”

当前本地配置使用 `email_link_mode: plain_address`：邮件保留 HTML 排版，将来源链接显示为不带 `http(s)://` 的可复制地址，不生成 `href`。本地 HTML/JSON 报告仍保留完整链接。此配置源于收件人参与的投递对照：无网址 HTML 和去协议前缀的完整纯文本均能收到，带 HTTPS 网址的样本未收到；具体过滤发生在哪一侧尚未确认。设置为 `full` 可恢复普通邮件链接，未配置时默认 `full`。

SMTP 每次只投递给一个收件人，确认 DATA 被服务器接受后记录成功；这不等同于最终进入收件箱。部分收件人失败只重试失败者，发送后连接中断等无法确定结果的情况保留为 `uncertain`，不自动重发。全部目标成功才清理本批正文、报告及关联缓存。详细运行信息在 journal 和 SQLite 中，错误细节不会占用邮件篇幅。

## 处理规则

- Paper：通过 `Processor.import_approved(existing_pipeline_result)` 或 `import-papers` 接收现有论文流程的结果，检查结果结构、完整摘要和当前阈值后直接入 `low_level`，不重复分类或评分。JSON 使用论文模块 `run()` 的返回格式。此入口是受信任的内部接口，不应接收未经筛选的外部 JSON。
- 社交内容：检查永久处理记录、正文完整性和来源标识；清理 HTML、导航和重复行，提取明确 arXiv 链接，再分类。
- `high_level`：观点、时事及非论文的模型、项目、产品发布；分类调用同时生成主要观点，直接入池。
- `low_level`：以具体论文为主要目的。第一版自动核验 arXiv ID；没有链接时只用模型提供的原文论文标题检索，必须唯一匹配。摘要始终来自 arXiv 接口或论文模块，不让模型编造摘要。不能确定身份时保持待处理。
- 社交论文调用原有 `prefilter`、`score_paper`、`cache_key` 和 `State`，但不套用新论文采集时间窗。同一论文多个平台分享只评分一次，合并展示来源解读。
- 相关性不足的候选标为 `filtered`，保留供研究兴趣变化后重新筛选；明确正文不足、缺少来源标识或无法归类的内容记录原因后移除正文。网络、模型和身份补全故障保持待处理。

论文配置仍来自 `config/papers.yaml`。社交论文筛选需要其中 `enabled: true` 和真实研究兴趣。分类及汇总默认复用现有 `config/config.yaml` 的 AI 配置与环境变量；可在 `config/content_pool.yaml` 单独指定模型。不会修改论文评分模型。

## 命令

先按仓库的 `pyproject.toml` 安装运行依赖。以下命令在仓库根目录执行：

```bash
# 备份旧库并迁移；新库无需备份。已经迁移时只显示状态。
python3 scripts/manage_content_pool.py migrate

# 原采集入口继续使用；只产生候选。
python3 scripts/collect_content.py --help

# 处理新候选；retry 与 process 相同，会跳过已完成的当前版本。
python3 scripts/manage_content_pool.py process
python3 scripts/manage_content_pool.py retry

# 历史数据必须显式选择，不会默认混入新日报。
python3 scripts/manage_content_pool.py process --history

# 接收已有论文模块 run() 的 JSON 输出。
python3 scripts/manage_content_pool.py import-papers /path/to/approved-papers.json

# 创建或恢复固定批次，返回 batch_id。
python3 scripts/manage_content_pool.py prepare
python3 scripts/manage_content_pool.py preview BATCH_ID

# 手动改类后必须重新处理；改成 low_level 不绕过论文筛选。
python3 scripts/manage_content_pool.py correct twitter CONTENT_ID low_level
python3 scripts/manage_content_pool.py process

python3 scripts/manage_content_pool.py stats
python3 scripts/manage_content_pool.py cleanup BATCH_ID --dry-run
# 只有发送器确认全部目标的全部分段成功，下面命令才会成功。
python3 scripts/manage_content_pool.py cleanup BATCH_ID --execute
```

历史日报使用 `prepare --history`。输出的 HTML/JSON 在 `output/content_pool/reports/`。两部分分别展示主题与逐篇来源、论文推荐理由与完整原始摘要。HTML 对来源文本转义。`preview` 不发送、不登记推送成功，重复预览不调用模型。

`prepare` 在 SQLite 写事务中固定内容及版本，优先恢复尚未完成的批次。按内容池配置的 `America/New_York` 计算每日论文名额，可在内容池配置中设置 `timezone`。多个批次共享论文配置的 `daily_output_limit`，清理后名额也不会重置；超额论文保持待选。历史批次独立计数，新内容不混入已创建的快照。

## 投递接口

发送器构造 `Digests(store, meter, paper_config, report_dir)`，使用以下接口。`delivery.deliver()` 已将这些接口接入真实 SMTP 发送。

```python
# 必须先生成成功的 preview；目标及各目标分段数固定后不可更改。
digests.set_targets(batch_id, {"email:reader": 2, "webhook:team": 1})
# part 从 0 开始。发送前写 sending；进程中断后不可假定已经成功。
digests.acknowledge(batch_id, "email:reader", 0, "sending")
# 收到真实投递确认后写 success，提供提供商消息 ID 或确认凭证。
digests.acknowledge(batch_id, "email:reader", 0, "success", "provider-message-id")
# failed 可重试；uncertain 必须先对账确定 success 或 failed，不能直接重发。
# 所有指定目标的所有 part 都 success 后，才允许执行清理。
digests.cleanup(batch_id, execute=False)
```

失败和不确定状态都阻止清理。清理默认 dry-run，执行时在事务中删除该批正文和官方池条目，保留内容 ID、发现关系、处理状态、论文投递身份、日期名额及投递回执。仅当一个原始 RSS 响应不再关联任何保留正文时才删除它；未完成的失败响应不随其他批次删除。相关分类、分块和汇总缓存没有其他候选引用后删除，报告文件按登记的受限路径删除。文件删除失败可重复执行清理。

论文元数据 `papers` 和评分缓存 `judgments` 已与内容池共库，不属于日报正文清理范围。
旧 `output/papers/state.sqlite3` 仅作为迁移来源保留；迁移备份也不会自动删除。

## 成本与恢复

- 分类按稳定 ID 返回，每批默认最多 10 条，同时受输入上限控制。成功结果逐条保存；漏项或无效项只单独重试。没有默认第二轮审核。
- 分类缓存包含标题、清理后正文、必要链接、模型及提示词/规则版本；作者、平台或发现账号变化不重复语义分析，各自来源仍单独存储。
- 长文逐段压缩并缓存中间证据，再综合判断。所有段落都会处理；无法在预算内完成时保留候选，不静默截断。**完整论文摘要超过输入上限时也只保留待处理，不截断评分。**
- 主题归纳使用已经生成的主要观点，必要时分组归纳后合并；验证所有文章 ID 恰好出现一次，最多 6 类。日报快照及汇总版本用于缓存。
- `run_call_budget`、`run_input_budget`、`input_limit`、`max_output_tokens`、`retries` 可配置。预算按保守 UTF-8 字节量加消息开销估算，**不是提供商实际计费 token**。预算耗尽后再次运行即可续跑，已完成结果保留。
- 用量按运行、阶段、模型分别记录请求、重试、缓存命中、估算输入及实际输入/输出 token。实际用量未返回则为 null。仅提供商返回费用或配置了模型单价时记录费用。
- 修改研究兴趣会复用分类、失效对应论文评分；修改分类提示词/规则版本会重新分类。已冻结日报保持原版本。调整配置后先 `process` 再 `prepare`。

## 验证

```bash
python3 -m unittest discover -s tests -q
```

测试使用模拟模型、arXiv 响应及发送器，覆盖分流、完整摘要、跨平台缓存、批量部分失败、预算续跑、长文尾段、缓存失效、历史迁移、日报恢复、每日限额、部分投递失败、缓存与 RSS 清理、永久去重。固定观点/时事/论文/无链接论文/产品发布样例比较单条和批量的结构与来源一致性及请求数；**模拟测试不代表真实模型分类准确率已经得到验证**。上线前应使用实际选定模型对同一组人工标注样例做质量评估。

## 单模型低频运行

取消模型分层，分类、分块、论文评分和主题归纳均复用现有 AI 接口。所有内容池进程共享文件锁，模型调用并发为 1；每次完成后至少间隔 30 秒，间隔状态保存在 SQLite，重启和换阶段也不能跳过。普通技术错误等待至少 60 秒后有限重试；429 / 1302 / 1305 触发持久化冷却，初始 5 分钟，连续限流指数延长至最多 1 小时，并尊重 Retry-After。冷却期间不继续逐条请求模型，未完成候选后续续跑。

30 秒是保守初始配置，不是智谱官方固定额度。官方说明当前主要按账号与模型的在途并发请求限流，实际额度需在账号控制台查看；平台过载也可能影响串行请求：[智谱速率限制](https://docs.bigmodel.cn/cn/api/rate-limit)。

主题归纳不可用时，日报使用已生成的逐篇观点，在页脚一行注明归纳暂缺；不会因模型临时故障阻断已完成内容的投递。

## 日报日期口径

日常运行按 `America/New_York` 的上一自然日 `[00:00, 次日00:00)` 选择主要内容，以来源的发布时间判断，而非服务器 UTC 日期或抓取时刻。例：匹兹堡 9 月 10 日早上 08:00 的日报主要覆盖当地 9 月 9 日。夏令时切换日可能为 23 或 25 小时，按日历边界计算。

当天凌晨发布的内容留待次日报；没有可信发布时间的候选保留待补全。晚到、重试成功或此前超额留池的旧内容可作为补充，邮件标记“补充”，不会伪装为昨日内容。arXiv 可继续回看最近几日来发现晚到论文，但筛选和日报入选同样受当地日期上界限制。显式 `--trial` 的近期样本演示不套用正式日报窗口，原有历史候选仍需显式选择。
