# 2026-09-10 托管部署实测记录

本轮只验证，不启用定时发送，不发送真实邮件。公众号按用户要求暂停。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| Supabase 目标核对 | 完成 | 原先不存在业务 schema；只有 Supabase 系统表 |
| 快照迁移 | 通过 | 21 表、5,148 行；事务试导入哈希核验后提交 |
| 迁移重跑 | 通过 | 同一快照返回 already_imported |
| 实际运行连接 | 通过 | 1,214 条永久身份，1,201 条保留正文 |
| 实际写入/读取/回滚 | 通过 | 验证用缓存行回滚后不可见 |
| 跨连接数据库锁 | 通过 | 第二连接被 another_content_run_is_active 拒绝 |
| 重复入库 | 通过 | 20 篇内容重放两遍，身份数仍为 1,214；测试写入回滚 |
| GitHub runner / RSSHub 构建 | 未验证完成 | 两次采集及一次最小 Docker 探测均显示 queued、没有 job |
| GitHub RSSHub 启动及三平台 Cookie | 未验证完成 | 尚未执行到容器启动及抓取阶段 |
| GitHub 日报预览 / AI / 邮件 | 未执行 | 等待新采集；发送开关保持 false |

GitHub Actions 运行：

- [首次小规模采集](https://github.com/Luoyu126/TrendRadar/actions/runs/34518791172)
- [无业务凭据、无并发锁的最小 runner 探测](https://github.com/Luoyu126/TrendRadar/actions/runs/34519634162)
- [最新代码有限重试](https://github.com/Luoyu126/TrendRadar/actions/runs/34520187160)

排查中确认：仓库 Actions 已启用，允许全部 Actions，没有待审批 deployment。
但运行查询持续返回 queued，取消 API 返回 HTTP 409：
`Cannot cancel a workflow run that has not been queued yet.`
因此没有把 GitHub 平台未执行的任务记为构建或抓取成功。临时探测 workflow 文件已移除。

本轮修复：Supabase 默认 extra_float_digits=0 导致浮点文本截短、快照校验误报，
迁移和运行连接现设为 3；增加手动 smoke 模式；暂停公众号采集、筛选及缺源提示。
本地回归 122 项中 111 项通过、11 项可选本机 PostgreSQL 测试跳过；以上远程
Supabase 验证为真实执行，使用独立事务回滚测试写入。

后续先读取上述运行的真实最终结果和日志。如果 runner 开始执行，逐项核对构建、
健康检查、来源返回数/质量标记/错误状态、Supabase 入库；再手动运行日报 preview。
只有这些步骤通过之后才考虑正式发送及启用调度。
