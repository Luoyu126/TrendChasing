# 外部服务

`RSSHub/` 是 Git 子模块，指向 [Luoyu126/RSSHub](https://github.com/Luoyu126/RSSHub)。
TrendRadar 记录它的具体提交版本；RSSHub 的源码和修改历史由其独立仓库管理。

## 职责

- RSSHub：获取外部平台内容，通过 HTTP 提供 RSS。
- TrendRadar：读取订阅源，保存、筛选内容并推送。
- `docker/`：TrendRadar 部署配置；后续可在这里统一编排 RSSHub 服务。

添加子模块不会自动启动 RSSHub，也不会自动启用小红书、公众号等订阅。
每个平台的路由、账号参数和所需凭据仍需单独配置、验证。
小红书的独立部署与内容池配置见 [小红书采集说明](../docs/xiaohongshu-content-pool.md)。

## 获取源码

以下命令在 TrendRadar 仓库根目录执行。

新机器克隆整个项目：

```bash
git clone --recurse-submodules https://github.com/Luoyu126/TrendRadar.git
```

已克隆 TrendRadar，补齐子模块，或在拉取主仓库后对齐所记录的版本：

```bash
git submodule update --init --recursive
```

子模块使用 SSH，需要可访问 GitHub 的 SSH 密钥。若密钥文件名不是默认名称，
初始化时显式指定本机密钥路径：

```bash
GIT_SSH_COMMAND='ssh -i /path/to/github_key -o IdentitiesOnly=yes' git submodule update --init --recursive
```

`/path/to/github_key` 应替换为本机实际路径。机器专用路径仅保存到本地 Git 配置，
不写入共享的 `.gitmodules`。

## 更新 RSSHub 版本

初始化默认检出主仓库固定的提交，可能处于 detached HEAD 状态；这是正常行为。
只有准备主动升级时，才执行下面的更新流程：

```bash
git -C external/RSSHub fetch origin
git -C external/RSSHub switch --detach <已验证的提交SHA>
# 验证目标路由和部署后，记录新版本
git add external/RSSHub
git commit -m "chore: update RSSHub submodule"
```

请将 `<已验证的提交SHA>` 替换为真实提交。升级前先处理子模块内未提交的修改。

## 修改 RSSHub

先阅读子模块自己的 `AGENTS.md` 和贡献说明。在子模块中创建分支，独立提交并推送：

```bash
git -C external/RSSHub switch -c feature/custom-route
# 修改并按照 RSSHub 的要求验证后，暂存具体改动文件
git -C external/RSSHub add <修改的文件路径>
git -C external/RSSHub commit -m "feat: add custom route"
git -C external/RSSHub push -u origin feature/custom-route

# RSSHub 提交已推送后，再在主仓库记录版本
git add external/RSSHub
git commit -m "chore: use custom RSSHub route"
```

这样其他机器才能拉取到 TrendRadar 所引用的 RSSHub 提交。

## 同步官方更新

在 RSSHub 子模块内将官方仓库设为 upstream（只需添加一次）：

```bash
git -C external/RSSHub remote add upstream https://github.com/DIYgod/RSSHub.git
git -C external/RSSHub fetch upstream
```

在自己的开发分支合并适当的上游提交，解决冲突、验证后推送到 fork，最后更新主仓库中的子模块版本。

## 部署约定

RSSHub 作为独立服务运行，TrendRadar 通过 `config/config.yaml` 中的 `rss.feeds` 访问它。
同一 Compose 网络内可使用 `http://rsshub:1200/<路由>`；本机进程则使用实际映射的地址和端口。
具体路由需验证后填写，不要直接使用占位符。

若需要让子模块中的修改生效，RSSHub 服务必须从 `external/RSSHub/` 构建。
直接运行官方镜像不会包含本地源码改动。根目录 `.dockerignore` 排除了 `external/`，
避免将外部源码发送到 TrendRadar 镜像的构建上下文；RSSHub 应使用自己的独立构建上下文。

平台 Cookie、Token 等凭据通过运行环境注入，不要提交到任一仓库。

## Paper-Pulse 参考仓库

`Paper-Pulse/` 是 [yangjunx21/Paper-Pulse](https://github.com/yangjunx21/Paper-Pulse)
的独立本地克隆，固定到参考提交 `f05147ac12eab8ffd7b84a7d44e819678d512e03`。
它不是 Git 子模块，也不是 TrendRadar 的运行依赖。论文功能独立实现在
`trendradar/papers/`；配置、使用与许可核查说明见
[论文推荐说明](../docs/paper-recommendations.md)。

## WeRSS 参考仓库

`we-mp-rss/` 是 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss)
的独立本地克隆，参考提交为 `f54aba50cbf349ed7e4ee1dae8bfe9990d0c5894`。
当前部署使用 `docker/docker-compose.werss.yml` 中的发布镜像，不依赖该目录。

这两个未修改的参考克隆由主仓库 `.gitignore` 排除，不随主仓库上传。
需要查阅对应源码时可单独克隆，再检出上述参考提交。
