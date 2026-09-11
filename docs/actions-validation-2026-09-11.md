# 2026-09-11 Actions 部署实测

本轮目标尚未完成：工作流已部署，但没有 GitHub runner 成功执行的证据。本机真实邮件发送已通过，见末尾更新。

## 已核验

- 远端默认分支 `master`，工作流恢复正式配置的提交 `db4b048`。
- `Collect Content`：`34 * * * *`。
- `Morning Content Digest`：`34 7 * * *`，`America/New_York`（匹兹堡时区，自动处理夏令时）。
- GitHub CLI 现有 Windows 安装已登录 Luoyu126，仓库 Actions enabled、allowed_actions=all；所需八项 Secrets 名称存在。
- 用户明确确认 LLM 和邮件授权、完成本机真实链路验证后，已设置并读回核验
  `CONTENT_SCHEDULE_ENABLED=true`、`CONTENT_SEND_ENABLED=true`；`LEGACY_CRAWLER_ENABLED=false`。
- Supabase verify-full TLS 只读连接成功；实际采集入口也已成功写入同一远程业务库。
- 全量本地测试 122 项：111 项通过，11 项可选独立 PostgreSQL 测试跳过。最终 workflow 调整后 14 项 hosted 测试通过。

## 本机真实业务验证（不等于 GitHub runner 验证）

通过已有本机 RSSHub 执行 `collect_hosted.py --smoke`，退出码 0：

| 来源 | 返回数 | 结果 |
| --- | ---: | --- |
| arXiv | 5 | success，metadata_failures=0 |
| 小红书首个账号 | 32 | partial，其中 1 条有质量标记 |
| 知乎首个账号回答 | 7 | success |
| 知乎首个账号文章 | 20 | success |
| X / OpenAI | 11 | success |

重放 20 条真实社交内容后，records 数保持 1,227，事务回滚，去重检查通过。
日志：`output/actions-validation/collect-smoke.log`。

采集前通过真实数据库入口生成了内容日期 2026-09-10 的空预览，批次
`881b23b45d634473a26f18bbc4914001`。它只有缺源提示，没有入选内容；
不能据此声称 AI 筛选或有内容日报已通过。该日期批次固定，后续新内容进入之后的补充内容。
预览没有创建投递记录。日志：`output/actions-validation/daily-preview.log`。

## GitHub 执行通道

截至 2026-09-11 19:42 UTC，以下运行均为 queued，查询 jobs 为空：

- 首次采集：[34638635967](https://github.com/Luoyu126/TrendRadar/actions/runs/34638635967)
- 数据库日报预览：[34638688411](https://github.com/Luoyu126/TrendRadar/actions/runs/34638688411)
- 新提交日报预览：[34639732677](https://github.com/Luoyu126/TrendRadar/actions/runs/34639732677)
- 新共享并发组采集：[34640097984](https://github.com/Luoyu126/TrendRadar/actions/runs/34640097984)

普通取消、采集的 force-cancel 返回 HTTP 409：
`Cannot cancel a workflow run that has not been queued yet.`
日报预览的取消也返回同一错误。没有待审批 deployment。
GitHub 官方状态页当时显示 All Systems Operational，因此不能断言是全站事故。

曾按用户建议将现有日报临时增加 19:39 UTC 的预览 cron；截至 19:40 未观察到 schedule 运行。
随即移除临时 cron 并恢复正式时间。这个短观察窗口不能证明定时功能一定失效。
两个工作流换用 `content-supabase-v2`，用于排除旧并发组残留；最新任务仍无 job，
因此尚无证据证明换组能解决问题。Supabase session advisory lock 仍保护业务并发。

## 尚待明确授权和平台恢复

自动审批拒绝了本机真实 SMTP 发送，要求明确确认现有收件地址及本次邮件内容；
也拒绝了真实 AI 筛选，要求明确确认现有智谱接口接收采集内容并保存筛选结果。
已向用户提交两项确认，未执行被拒绝的操作，未启用可能间接执行它们的正式定时开关。
第三个无凭据探测工作流也曾被审批拒绝，未提交或推送，临时本地文件已移除。

下一步先读取上述运行状态并核对用户反馈的 GitHub 网页提示；收到具体 AI/邮件授权后，
再完成真实筛选、SMTP 接受及数据库回执验证。在 GitHub 采集和日报实际执行成功前，
不得把仅创建 workflow 文件、HTTP 接受 dispatch 或本机验证记作目标完成。

## 用户确认后的真实发送

用户已明确确认可自由调用现有 LLM API 和发送邮件。随后通过数据库日报正式入口
发送 2026-09-10 已生成的缺源提示报告，退出码 0；结果 `sent`、targets=1、
success=1、uncertain=0。Supabase 中该批次投递回执为 success，SMTP DATA code=250，
队列号 `8420906291646`。这证明 SMTP 服务接受邮件，不等于收件箱送达确认。
本次投递没有入选正文，不能据此宣称完整内容日报通过。真实内容的 AI 筛选另行执行，
已经观察到成功的论文评分 API 请求写入 usage_events。

截至 19:56 UTC，原四项 GitHub 运行仍 queued，最新采集的 check suite 也 queued 且
latest_check_runs_count=0。临时在现有采集工作流添加 push 小规模触发以排除手动接口异常；
提交 `77327ab` 已推送，但当时仍未观察到对应 Actions 检查套件或运行。

## 有正文邮件及正式开关

真实 AI 筛选一轮累计预留 9 次调用，成功产出论文 1 篇、知乎内容 1 条、X 内容 1 条。
过程中遇到一次 `RateLimitError`，进入既有持久冷却；其余待处理内容保留，不重置预算。
将已通过筛选的 3 条内容制作为明确标注“部署测试”的独立批次
`ae7ed8531c98414a8e6c84c2a84791c5`，按原邮件模板及投递账本发送。
该批次不写入 digest_days，不会占用次日正常日报的内容日期。
SMTP 接受：code=250，队列号 `1205316815967`；账本 success=1、uncertain=0。
相同测试再次运行返回 `already_sent`，没有新增投递。两封测试邮件均核验了远程回执。
由于模型冷却，报告预览允许沿用既有摘要降级行为；不据此宣称所有待处理内容已完成。

临时 push 触发已在 `db4b048` 移除，正式工作流只保留原有 schedule 和 workflow_dispatch。
本机工作流文件与远端 blob SHA 核验一致；两个正式开关已启用。
这完成了部署配置与本机链路验证，但四项 GitHub 手动任务仍 queued、jobs 为空，
尚未证明 GitHub runner 能完成 RSSHub 构建、抓取、评分和 SMTP 发送。
已请用户检查最新运行页面是否有账户验证、账单或启用 Actions 的提示。
