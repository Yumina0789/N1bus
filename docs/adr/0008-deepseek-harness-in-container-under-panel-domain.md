# 0008 DeepSeek Harness 放进容器，挂在面板域名的子路径下

## 状态

已接受（2026-10-10）

## 背景

需求是「在这台服务器上跑一个 DeepSeek Harness，放在容器里」。这台机器上已经有 N1bus
（宿主 systemd 服务）、v1m-dash/KMS（容器）、Caddy（容器，反代两个域名）。

几个硬约束：

* 服务器仅有 2 vCPU / 1.9 GB 内存 / 29 GB 磁盘，N1bus 的原则是「不让机器资源耗尽」。
* 域名 `lolicon.fans` 没有泛解析，任何新子域名都要去 DNS 服务商加记录。
* DSH 是**开发者预览**软件，官方安全说明写明它能「执行模型生成的代码与命令、访问向其开放的
  网络、进程、凭据和文件」。把它暴露出去等于暴露一台能跑命令的机器。

## 决策

**1. 独立容器，独立 compose 项目，但接进同一个网桥。**

放 `/opt/dsh`（独立的 `docker-compose.yml`），不并入 v1m-dash 的 compose 文件 —— 修改 v1m-dash 的 compose
就等于同时改动 KMS 面板。容器加入 `docker_default` 网络，Caddy 通过容器名 `dsh` 定位它。
容器端口**不 publish**，公网碰不到。

**2. 挂在 `lolicon.fans/dsh/` 子路径下，而不是开新子域名。**

不开 `dsh.lolicon.fans` 的理由是省去一道 DNS 配置步骤，也少一个独立入口。Caddy 里用
`handle_path /dsh/*` 剥掉前缀再转发 —— DSH 的 webserver 只应答 origin-root 路由
（它不知道自己被挂载），而它的页面用 `<base href="./">` 加相对路径资源，天然适配子路径。
裸路径 `/dsh` 由 `redir` 补成 `/dsh/`，因为剥掉前缀后的后端已经无法重建外部路径。

**3. 容器内自带一个 12 行的 TCP 转发。**

因为 0.2.0-rc.2 的 dsh **只绑定 127.0.0.1**（见下），而 Caddy 位于另一个容器中，无法访问其
loopback。所以容器里多跑一个 `forward.mjs`：`0.0.0.0:3081` → `127.0.0.1:3080`，
Caddy 连的是 3081。用 node 自带的 `net` 而不是装 socat，是为了让 Dockerfile 只多一个
COPY —— npm 那一层（538 个包、7 分钟）能继续走缓存。

**4. 进场链接由面板实时获取，界面直接内嵌在侧边栏的独立页面中。**

DSH 的 Web UI 只有一种进场方式：拿一次性启动 token 换会话 cookie。没有 token，
`/dsh/` 一律回一句纯文本 `401 Unauthorized`。而那个 token **每次启动现生成、只打在容器
日志里、不落盘**，所以没法硬编。N1bus 新增 `/api/dsh`：`docker inspect` 看容器状态、
`docker logs --tail 200` 捞最后一条 `token=`，拼成浏览器可直接打开的地址返回给前端。

前端将其做成侧边栏一个**独立入口**（而非嵌在某个页面角落的小卡片 —— 该功能用于实际作业，
应该与概览/运行并列），打开后即同源 iframe 中的完整界面。三条配套取舍：

* **切视图不重载 iframe**：仅在链接变化（容器重启更换 token）或点击「重新载入」时才修改 `src`。
  否则每次切回都会把 DSH 整个重载一遍，正在运行的会话即会中断。
* **面板起来 4 秒后后台预热一次**：DSH 首次要拉几十个 client 插件 bundle（实测十几秒），
  预先完成加载，打开即用。代价是面板会多一次请求和一次加载，即使用户当天不打开它。
* **加载提示通过监听 iframe 内部文本判断**，而不是 iframe 的 `load` 事件：那个事件在文档级
  onload 时就触发了，而 SPA 那时还停在「Loading plugins…」。同源，读得到 `contentDocument`。
  （另外**不要**给 iframe 挂内联 `onload` —— iframe 在 HTML 中、脚本在页面底部，初始
  `about:blank` 的 load 会赶在函数定义之前触发，只会得到一个 ReferenceError。）

**5. 容器只给它自己的地盘。**

挂 `dsh-home` 卷（配置/会话/凭据）和 `/opt/dsh/workspace`（agent 的工作区）。**不挂**宿主的
`/opt/n1bus`、`/etc`、`/var/lib/docker` 之类。它能在容器里干什么，就止步于容器。

## 后果

* 好的一面：不开新域名、不动 DNS；公网只有一个入口 `lolicon.fans/dsh/`，且它默认对所有人
  都是 401，只有拿到 token 的浏览器能进，进去后 30 天免 token。
* 好的一面：Caddy 那段自带 `X-Frame-Options "SAMEORIGIN"`（N1bus 自己的页面反而是
  `DENY`），所以同源 iframe 可以嵌入 —— 侧边栏打开即是完整界面，无需来回切换标签页。
* 差的一面：**token 会随容器重启变化**。因此「复制 URL 留待后用」不可行 —— 要么通过
  面板按钮，要么接受 30 天 Cookie 到期后重新获取。这是 DSH 的设计，不是这里能绕过的。
* 差的一面：WS 走的是 `wss://lolicon.fans/dsh/api/remote.mux`，依赖 Caddy 的
  `reverse_proxy` 正确处理升级（实测 101 正常）。如果哪天换成别的代理，要重新确认这一点。
* 差的一面：多了一层 TCP 转发。转发进程和 dsh 在同一个容器里，dsh 是 `exec` 的主进程，
  容器退出转发一起消失，不会留孤儿。

## 版本差异（本次问题最集中的部分）

**官方文档站跟的是 `master` 分支，而 npm 上的 latest 是 `0.2.0-rc.2`，两者 CLI 不一致。**

| 文档站（master）说法 | 0.2.0-rc.2 的实际情况 |
|---|---|
| `--public-url` / `--tls-cert` / `--tls-key` 可用 | **不存在**，传了直接 `unknown option` 退出 |
| 「`--host` 拒绝通配地址，请绑具体本机 IP」 | webserver 的 schema 是 `$.host expected "127.0.0.1" \| "0.0.0.0"`；绑容器网卡地址 `172.18.0.x` 会 ValidationError |
| — | 而 CLI 又主动拒绝 `0.0.0.0`：`--host 0.0.0.0 is intentionally not supported yet for safety: it would expose remote code execution to the network` |

两条路均不可行，实际只剩下 `127.0.0.1` —— 这才是需要第 3 条那个转发的真正原因。
**以后升级 DSH 版本时，先运行 `dsh --profile web --help` 核对选项，不要仅依据文档。**

（另：构建时 npm 11 的 `allowScripts` 保护拦下了 5 个包的 install scripts。实测无影响 ——
`node-pty`/`koffi`/`dsh-subprocess-local` 都能正常 `require`，node-pty 的 `linux-x64/pty.node`
预编译已在包内；而那个 postinstall 只做一件事：给 node-pty 的 **macOS** spawn-helper 补
可执行位，Linux 上 `prebuilds/linux-x64/` 里没有这个文件，本来就是空操作。）

## 备选方案

1. **开 `dsh.lolicon.fans` 子域名**：要加 DNS 记录，多一个面向公网的入口；挂子路径这些都不用。
2. **让 N1bus 自己反代 DSH**（这样能复用面板登录）：N1bus 是纯 stdlib 的 `http.server`，
   没有 WebSocket 能力，而 DSH 的前端严重依赖 WS（`/api/remote.mux`）。要么手写 WS 隧道，
   要么放弃。Caddy 干这件事是本职。
3. **用 `0.2.1-alpha.2`**（npm 的 `alpha` tag）：可能已经有 `--public-url`，但 alpha 更不稳，
   而且换不来什么实际好处 —— 公告 URL 只影响打印和提示词定位。
4. **装 socat 做转发**：一样能用，但会让 apt 层和 npm 层都失效重建（7 分钟）。node 自带的
   `net` 够了。
5. **将 DSH 并入 v1m-dash 的 compose**：可省一个 compose 文件，但每次修改 DSH 都存在影响 KMS 面板的
   风险，不值得。
