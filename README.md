# N1bus

> 名字换过几轮：显示名是 `Nimbus → N1bus`；内部标识原先是初代项目名 `Steward`
> （`steward.service` / `/opt/steward` / `/var/lib/steward` / `steward.token`），
> 2026-10 起**一并统一为 `n1bus`**。从旧部署迁过来要停旧服务、搬数据目录。


**零依赖**（只用 Python 3 标准库）的 Linux 服务器管理面板，装成宿主机上的一个 systemd
服务，默认只监听 `127.0.0.1`。源码按域拆成 12 个 `n1bus_*.py`，外加前端三件套
`ui.html` / `ui.css` / `ui.js` —— 页面由后端在启动时把三者内联进同一个 HTTP 响应，
**没有独立的静态资源路由**。

不使用 Cockpit / 宝塔 / 1Panel，是因为这台机器需要观测的内容很少，而多引入一层
运行时本身就是负担。**必要功能，自己写。**

## 它能干什么

界面共 9 个标签页（`ui.html` 的 `data-tab`）：

| 页面 | 内容 |
|---|---|
| 概览 | 状态环（CPU / 内存 / 根分区 / 负载 1·5·15）与四个统计（服务 / 容器 / 进程 / 登录失败），CPU 与网络实时曲线（内存里约 3 分钟窗口）；磁盘明细、进程与服务、登录失败来源收在折叠块里，侧栏是系统信息与网络 |
| 扩展功能 | **模块商店**：初始为空列表，点「添加模块」进商店（registry 仓库的 GitHub Pages 静态站），模块逐个安装 / 卸载，各自独立容器与 iframe 页面，**装上即出现子标签**。目前另有两个**内置**的写死子视图：**KMS 授权服务**（服务状态与激活统计、启动 / 停止 / 重启、vlmcsd 配置的编辑与回滚、激活限流与当前封禁、激活明细与清理，数据读自 v1m-dash、写操作经白名单转发给它）· **DeepSeek Harness**（独立容器中的 agent 面板，同源 iframe 内嵌；工具条有启动 / 停止 / 重启与「新标签页 / 重新载入 / 全屏」） |
| 运行 | **服务**（systemd 单元列表、详情、日志，启停重启，批量重启失败单元）· **Docker 容器**（容器含 CPU/内存/网络、镜像、日志，启停重启，重启全部容器，清理无标签镜像）· **端口 · 进程**（监听端口标出对外的、已建立连接、占用最高的进程、发信号） |
| 安全 | 三个子标签：**防护**（来源封禁与面板登录限流 / 防火墙端口管理 / 自动永久封禁与豁免名单）· **登录失败**（来源排行 / 被猜用户名 / 登录明细）· **服务状态**（SSH 配置 / fail2ban）。卡片平铺展开 |
| 系统 | 三个子标签：**维护**（系统清理 / 备份与恢复）· **网络与存储**（磁盘与挂载 / 网络）· **系统配置**（开机自启 / 系统设置）。磁盘用量以进度条呈现，虚拟网卡默认收起 |
| 文件 | **文件管理器**：白名单根内浏览 / 下载 / 上传 / 编辑文本 / 新建 / 重命名 / 删除，写操作进审计 |
| 计划任务 | 把白名单动作按周期跑起来（最小 60 秒），带启停、立即执行与「最近执行」列表 |
| 日志 · 审计 | journald 全量、白名单日志文件与单元日志的 tail；审计记录（可按动作前缀筛选）；白名单动作列表与执行结果 |
| 设置 | **账户**（列表 / 新增 / 修改密码 / 删除合并在一张卡片里）、会话与服务 token、**告警与阈值**、**关于**（版本 / 面板服务状态 / 失败单元 / 主机信息）。**重启面板与重启服务器在右上角工具条**上，与刷新并列 |

页面上的版本号来自 `n1bus_util.VERSION`（当前 `1.0`），经 `/api/meta` 下发。

## 登录

**第一次打开**面板时它会让你**设置账户**（用户名 + 密码，密码至少 8 位）—— 那时还没有
账户，界面是「设置账户」而不是「登录」。之后就用这个账户登录，会话有效期 **7 天**。

**凭证不走 Cookie**：登录成功后令牌在响应体里返回，前端存进 `localStorage`
（键 `n1bus_session`），此后每个请求带 `Authorization: Bearer <令牌>`；也可以直接用
`?token=<服务 token>` 认证。令牌因此能被页面内 JS 读到（没有 HttpOnly 挡着），
但也就没有传统 Cookie 的 CSRF 面。

之后可以在「设置 → 账户」里：新增账户、修改密码（可修改任意账户，但需先用操作者本人当前密码确认一次，
服务 token 身份不能改密码）、删除账户（最后一个账户不可删除）、退出本账户的全部会话。

**登录限流是递增的，而且重启不清零**：同一来源 IP 在 5 分钟内失败 8 次即被封禁，档位依次为
**5 分钟 → 30 分钟 → 6 小时**（封顶），记录在数据目录的 `loginguard.json` 里；成功登录会
清掉该来源的全部记录，连续 24 小时不再犯则档位归零。面板上的人工解封**不重置档位**。

密码用 `scrypt` 加盐存（解释器没编入 scrypt 时回退 `pbkdf2_hmac`），账户文件是数据目录下的
`accounts.json`（0600）。**忘了密码**就删掉它再重启：`rm /var/lib/n1bus/accounts.json && systemctl restart n1bus`，回到首次设置。

**给机器留的凭证**：`/var/lib/n1bus/n1bus.token` 仍然能调整个 API（N1bus 自己就拿
它去 v1m-dash 拉限流决策，见 `docs/adr/0003`）。前端不使用它，浏览器里不放这个 token。

## 运维功能（参考 1Panel 的最小实现）

「系统」页里那几块，以及设置页的「告警与阈值」，都是照着 1Panel 的思路做的最小版：

* **DeepSeek Harness**：一个跑在独立容器里的 agent 面板，由 Caddy 挂在 `lolicon.fans/dsh/`
  子路径下。它只有一种进场方式 —— 拿一次性启动 token 换会话 cookie，而没有 token 时
  `/dsh/` 一律回 401；该 token 每次启动时生成、仅输出到容器日志、不落盘。因此面板的
  `/api/dsh` 从容器日志中提取最新一条并拼成链接，打开即可用（换成 30 天有效的 Cookie）。
  **N1bus 不代理它的流量、也不替它认证** —— 反代与 WebSocket 都是 Caddy 的事，面板只负责
  「告诉你该从哪进」和「把界面嵌进来」。几个刻意的取舍：
  * **只挂载它自身的两个目录**（`dsh-home` 卷 + `/opt/dsh/workspace`），不挂载宿主任何敏感路径；
  * **切视图不重载 iframe**，否则正在跑的会话会被打断 —— 只在链接变了（容器重启换了 token）
    或点「重新载入」时才动它；
  * **面板起来 4 秒后后台预热一次**：DSH 首次要拉一批前端插件（实测十几秒），先热好，
    用户打开时即可直接使用。加载期间以一条加载提示作为降级保护（通过监听 iframe 内部文本判断何时收起，
    同源可读）。

  详见 ADR 0008。
* **系统清理**：先给出可回收量统计（journald 可回收多少、悬空镜像多少、apt 缓存多少），再逐项清理。
  日志只按体积收缩（`journalctl --vacuum-size=200M`）与删除 30 天前的 `*.gz`，**不碰当前日志**；
  `apt autoremove` 只列候选、面板不代为执行。
* **备份与恢复**：`tar` 打包面板数据 + 全部 Docker 卷（KMS 统计库、Caddy 证书）+ Caddy/vlmcsd
  配置，一键下载。**恢复刻意保留给 SSH** —— 自动恢复需覆盖正在运行的数据，一旦出错会导致面板自身
  不可用；页面上直接给出恢复命令。
* **开机自启**：systemd 的 enabled 单元与容器 `restart policy` 一览，可以切换；但
  **ssh / 网络 / 时间同步 / journald / docker / 面板自己**在保护名单里，前后端均不允许关闭。
* **系统设置**：时区与 NTP 开关可以修改；swap 只读展示（新建 swap 需要改动分区与文件系统，面板不代为执行）。
* **文件管理器**：在白名单根（`/var/log`、`/var/lib/n1bus`、`/opt/nimbus`、`/etc/caddy`、
  `/etc/vlmcsd`、`/etc/systemd/system`、`/opt/dsh`）里浏览、下载、上传、编辑文本、新建、重命名、删除。
  路径要 realpath 之后仍落在某个根之内，`..` 与符号链接逃逸一律拒；删除白名单**根**本身被禁止，
  删非空目录要再确认一次。**每次写都进审计**（成功失败都记），覆盖前自动留 `.bak`。
  单次上传上限 64 MB、编辑上限 2 MB、一次最多列 200 项。详见 `docs/adr/0009`。

  每个根**此刻**能不能写，还取决于 systemd 加固：N1bus 以 `ProtectSystem=full` 运行，
  只对 `/etc/ufw` 与 `/etc/systemd/system` 放开了写（前者给 ufw 动作写 `user.rules`，
  后者给「设为开机自启」建 `*.wants/` 链接）。所以 **`/etc/caddy` 与 `/etc/vlmcsd` 是只读的**
  （界面会标出来），而 `/etc/systemd/system` **可写**。可写性由 `/api/files` 的
  `roots_writable` 现场探测并如实上报，不是写死的名单 —— 这是加固边界，而非缺陷。
* **Harness 的启动 / 停止 / 重启**：「Harness」页工具条上的三个按钮操作的是 **dsh 容器**
  （这一页就是它的控制面，目标固定为 `dsh`），走已有的 `docker.start` / `docker.stop` /
  `docker.restart` 白名单动作。**重启不是重建**：容器文件系统不动，容器里的改动不会丢。
  别的容器去「运行 → Docker」操作。
  想让改过的 `/opt/dsh/.env` 生效光靠重启不够（Docker 只在**创建**容器时注入环境变量），
  那条路走 `/opt/dsh/change-key.sh`（它做的是重建）。
  四个自用插件已**打进镜像**（`docker compose build`），所以即便重建容器也不会丢。
* **计划任务**：把白名单动作按周期跑起来（最小 60 秒、最长 30 天），带启停、立即执行与执行记录。
  **不写系统 crontab、不落任何脚本文件** —— 代价是面板停着时任务也不会跑，页面上写明了。
  任务定义落在 `tasks.json`，但「最近执行」只存在内存里，面板一重启该列表就空。
* **告警与阈值**：根分区 / 内存 / 每核负载 / 失败单元超阈值时往 Webhook 发一条 JSON
  （`{ts, level, metric, value, threshold, host, text}`）；同一指标在冷却期内只发一次。
  注意两点：冷却记录**只在内存里**，面板重启后同一指标可能立即重发一次；
  真正触发的告警**不写审计**（只有保存配置与「发一条测试告警」会写）。
* **自动永久封禁**（安全页）：fail2ban 的 sshd jail 配置为「10 分钟内失败 4 次则封禁
  1 小时」，到期**自动解封**；持续低速扫描的来源不会触发该窗口，因此该策略不收敛
  （本机上 `154.18.197.29` 被反复封禁 19 次、累计失败 997 次后仍在尝试）。本功能改以
  **累计**失败次数判定：超过阈值（默认 10 次）的来源直接执行 `ufw deny` **永久**封禁，
  不自动解封。面板上可启停、调整阈值、查看「待封禁来源」预览（与真实执行使用同一份
  计算）、立即执行一轮，以及解封 / 豁免。**成功登录过的来源与私有网段永不自动封禁**；
  解封会自动加入豁免名单，否则下一轮巡检会依据同一累计次数重新封禁。
  详见 `docs/adr/0010`。
* **防火墙端口管理**（安全页）：开放与移除 ufw 端口规则，**入站与出站分开**管理。
  `ALLOW IN` 与 `ALLOW OUT` 是两条互不覆盖的规则，IPv4 与 IPv6 也是两条独立规则
  （一次 allow 写入两条），界面按「方向 + 端口 + 协议」合并后再展示。
  最有用的部分是**与监听情况的对照**：服务在监听但没放行 → 直接给一键放行；
  放行了但没有服务监听 → 提示这条规则不产生效果。
  注意 **Docker 发布的端口绕过 ufw**（docker-proxy 直接写 iptables），这类端口单独成组
  说明，不会被误报成「待放行」。端口段、不限协议的裸端口、转发方向的规则只列出，不代为执行。
  详见 `docs/adr/0011`。
* **网络 / 磁盘**：全部只读。这里有一条要紧的提示 —— **Docker 发布的端口直接写 iptables，
  ufw 拦不住**，所以"实际暴露 = ufw allow ∪ docker publish"。
* **重启**：设置页可以重启面板服务；「运行」页可以批量重启失败的单元、重启全部容器；
  整机重启也在（延迟 2 秒执行，好让响应先回到浏览器）。**面板是唯一入口，重启后起不来就只剩
  SSH/VNC**，所以这些动作都带二次确认并记审计。

写面依旧只有 `ACTIONS` 表加几个面板自身的配置接口，理由见 `docs/adr/0006`。

## 它**不**干什么

**没有 Web 终端。** 面板能改系统的全部手段就是 `n1bus_actions.py` 里那张 `ACTIONS` 表：
每个动作一段固定的 argv、参数经过严格校验、以列表形式交给 `subprocess`（不过 shell），
危险动作界面二次确认，无论成败都写审计。理由写在 `docs/adr/0002`：把一个公网可达的
root shell 挂在 token 后面，风险与收益不成比例 —— 需要执行命令时请使用 SSH。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/Yumina0789/N1bus/main/install.sh | sudo bash
```

它会装到 `/opt/n1bus`、把 token 写进 `/var/lib/n1bus/n1bus.token`（600），
并启好 `n1bus.service`。升级就是重跑一遍（旧文件会留 `.bak`）。

### 用 git 部署（推荐，升级更干净）

```bash
sudo git clone https://github.com/Yumina0789/N1bus.git /opt/n1bus-src
cd /opt/n1bus-src && sudo bash install.sh
```

之后升级只要：

```bash
cd /opt/n1bus-src && sudo bash upgrade.sh          # git pull + 重装
cd /opt/n1bus-src && sudo bash upgrade.sh --check  # 只看有没有新版本，什么都不改
```

`upgrade.sh` 会**从现有的 systemd 单元里把部署参数捞出来原样带上**（`--bind`、`--allow-ip`、
`--enforce-url`、`--enforce-token`、`--port`、`--data-dir`）。不这么做的话，重装会按默认值
重写单元，那些参数被静默丢掉 —— 表现为「升级之后反向代理连不上面板」或者「自动封禁莫名关闭」。

已经用上面 `curl` 方式装好的机器随时可以迁过来：`/opt/n1bus` 是部署目录、
`/opt/n1bus-src` 是仓库，互不干扰；升级时旧文件照旧自动留 `.bak`。

从本机访问时建议使用 SSH 隧道，无需对外暴露任何端口：

```bash
ssh -L 8402:127.0.0.1:8402 root@<服务器 IP>
# 然后浏览器打开 http://127.0.0.1:8402/?token=<安装时打印的 token>
```

如需使用域名，请交由反向代理（Caddy / Nginx）处理，**不要**将监听地址改为 `0.0.0.0`：这是一个可启停
服务、终止进程、封禁 IP 的 root 面板，公网侧唯一的防线就是 token。

卸载：`bash install.sh --uninstall`（保留数据）或 `--purge`（连数据一起删）。

## 挂在反向代理后面（可选）

面板默认只监听 `127.0.0.1`。如果反向代理跑在**容器**里（比如 Caddy/Nginx 的 compose），
它只能通过 docker 网桥的网关找到宿主机，所以要让面板多绑一份网桥地址：

```bash
sudo bash install.sh --bind 127.0.0.1,docker \
     --allow-ip 127.0.0.0/8,::1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16
```

* `docker` 会在启动时现场查出宿主在 `docker0` / `br-*` 上的地址并一起监听 —— 网段变了
  也不用改配置。
* `--allow-ip` 是第二层：只有这些网段能访问，公网直连一律 403（绑了 `0.0.0.0` 时它才是关键）。
* 还要放行网桥到面板端口的流量，否则 ufw 会把反代的请求一起丢掉：

```bash
sudo ufw allow from 172.18.0.0/16 to any port 8402 proto tcp   # 网段按实际 compose 网络填
```

反向代理那边的站点块（以 Caddy 为例）：

```caddyfile
panel.example.com {
	encode zstd gzip
	reverse_proxy 172.18.0.1:8402      # 宿主在网桥上的地址
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		X-Frame-Options DENY
		Referrer-Policy "no-referrer"
		-Server
	}
}
```

代理会带上 `X-Forwarded-For`，面板采信它的条件写在 `Handler._client_ip()` 里：
**只有直连方本身落在 `--allow-ip` 网段内**（即它确实是个可信代理）时，才取该头的第一段
（最左 = 最原始客户端）；**其余情况一律记直连方地址、根本不读这个头 —— 没配
`--allow-ip` 时也在其列**。所以上面那份配置里 `--allow-ip` 不是可选项：它既决定谁能访问，
也决定 XFF 是否被采信。这不是洁癖 —— 该头由客户端随便书写，谁都能伪造成任意 IP：
既能把自己的登录失败记到别人头上（限流用的键取自同一个函数），也能把封禁转嫁给别人。

> 提醒：这样一来面板即暴露在公网上，token 是唯一防线。若不希望如此，请移除域名并继续使用
> SSH 隧道 —— 对于可启停服务、终止进程、封禁 IP 的面板而言，隧道更为合适。

## 本地预览

没在 Linux 上也能把界面看全 —— 演示模式用一份仿真的 /proc 数据，不碰真实系统：

```bash
python3 n1bus.py --mode demo --port 8402
```

## 参数

```
--mode demo|real     演示 / 真机（默认 real）
--bind IP            监听地址，默认 127.0.0.1。逗号分隔可绑多个；写 docker 会额外绑
                     一份宿主在 docker 网桥上的地址（反向代理在容器里时需要）
--allow-ip 网段       来源白名单（逗号分隔）；留空 = 不限制
--port N             默认 8402
--data-dir DIR       默认 /var/lib/n1bus（token、账户、审计与各状态文件）
--token-file PATH    默认 <data-dir>/n1bus.token
--token STR          直接指定 token，或用环境变量 N1BUS_TOKEN
--interval SEC       采样间隔，默认 2
--enforce-url U      从哪个面板拉「该封谁」（例如 http://127.0.0.1:8099 的 v1m-dash）
--enforce-token T    上面那个面板的 token；给了才会开启自动封禁
--enforce-interval N 拉取间隔，默认 60 秒，最小 10；0 = 关闭
--auto-ban on|off    自动永久封禁，默认 on（环境变量 N1BUS_AUTO_BAN）
--auto-ban-after N   累计失败次数超过该值即永久封禁，默认 10，最小 3（N1BUS_AUTO_BAN_AFTER）
--auto-ban-interval N 巡检间隔，默认 300 秒，最小 30（N1BUS_AUTO_BAN_INTERVAL）
--nimbus-url U       KMS 授权服务（v1m-dash）的地址，默认 http://127.0.0.1:8099（NIMBUS_URL）
--nimbus-token T     上面那个面板的 token；默认自动从它的 docker 卷里读（NIMBUS_TOKEN）
--store-url U        模块商店的地址（registry 仓库的 GitHub Pages），默认
                     https://yumina0789.github.io/N1bus-modules（N1BUS_STORE_URL）
```

自动封禁执行的仍然是 `ACTIONS` 表里的白名单动作（`ufw.deny` / `ufw.undeny`），
因此与手动封禁一样会写入审计。

**这里有两条独立的自动封禁机制**，数据源不同，不要混用：

* `--enforce-*` 组**从其他面板拉取决策**（v1m-dash 的 KMS 限流），N1bus 只负责执行，
  封禁对象由 v1m-dash 决定。
* `--auto-ban*` 组由 **N1bus 自行判定**：扫描 `/var/log/auth.log` 全量记录，累计失败
  超过阈值的来源直接执行 `ufw deny` **永久**封禁，不自动解封。fail2ban 依据「时间窗口内
  的失败次数」判定，对慢速扫描无效（实测某来源被封禁 19 次、累计失败 997 次后仍持续尝试）。

  它**不会自动封禁**两类地址：**成功登录过的来源**（auth.log 中全部 `Accepted` 记录，
  可信度最高的判据）和私有网段 / `--allow-ip`。放行只能通过面板解封，且解封会
  **同时写入豁免名单** —— 否则下一轮巡检会依据同一累计次数重新封禁。
  详见 `docs/adr/0010-recidive-permanent-ban.md`。

## 设计上的几条硬性约束

* **运行在宿主机而非容器**：管理对象就是宿主机本身，容器化将被迫交出 `docker.sock`
  与多项特权挂载。见 `docs/adr/0001`。
* **不提供任意命令执行**：见上面与 `docs/adr/0002`。
* **观测数据不落库**：CPU / 网络靠 `/proc` 差值算，只留内存里的环形缓冲（90 帧 × 采样间隔，
  默认约 3 分钟）够画曲线。落盘的是**面板自身的状态与日志**：审计日志（超过 2 MB 轮转一份
  `.1`，只留一份历史）、`accounts.json`、`loginguard.json`、`defender.json`、`tasks.json`、
  `alerts.json` 与 `n1bus.token`。2 vCPU / 2 GB 的小机器上也几乎无感。
* **不伪造状态**：没有 systemctl、没有 docker、读不到 auth.log 时，界面如实显示「不可用」，
  不编造数据。
* **没有只读 TTL 缓存 —— 原先那套已经删掉了**。`n1bus_cache.py` 里现在只剩**单元属性缓存**
  （`UNIT_PATH_CACHE` / `UNIT_STATE_CACHE` / `UNIT_PROBED`：单元的 FragmentPath 与
  UnitFileState；逐个 `systemctl show` 约 8 ms、210 个约 1.5 s，所以每个进程只查一次）。
  它**没有 TTL**，而是在每次动作执行成功后整批作废 —— 否则会留下旧值，表现为
  「点了开机自启却仍显示未启用」。
  原先那套带 TTL 的数据缓存（`cached()` / `_cache_methods()` / `_CACHE`）在 `bc2f9ab`
  把单文件拆成 12 个模块时**整段漏搬了注册**，此后一直零调用、`_CACHE` 恒为空：
  `?fresh=1` 与响应里的 `_cache` 元数据都是空转。已连同 `cache_meta()` /
  `clear_data_cache()` / 前端的 `forceFresh()` 一并删除。
  代价是**每次请求都真的去跑系统命令**：一次 `/api/security` 会把 `ufw status verbose`
  跑上三遍（`blocked` / `firewall` / `allowed` 各一次），`/api/autostart`、`/api/cleanup`
  也每次都重新执行。**若要把 TTL 缓存加回来**，注意拆分前真踩过的这两个坑：
  * 带参数的只读方法必须提供缓存键：`_cache_methods` 里的 `"方法名": ttl` 仅适用于
    **无参**方法；带参数的方法必须写成 `(ttl, key_fn)`，否则全部参数共用一个缓存槽。
    这不是理论问题 —— `logins()` 曾因此出错：概览页先调用 `logins(lines=2000, top=3)`，
    安全页随后调用 `logins()` 时命中同一缓存槽，**排行榜只剩 3 条、行数显示 2000**。
  * 缓存键有两种形态，读取它的代码必须同时兼容：无参方法的键是方法名（字符串），带参数方法的
    键是 `(方法名, 参数元组)`。`clear_data_cache()` 曾假定键一定是字符串、直接调
    `.startswith()`，遇到元组键就抛 `AttributeError`；而它位于 `do_POST` 里「动作已执行完、
    审计已写完之后」，于是表现为**动作已成功执行、面板却报 JSON 解析失败**。
    用 `_cache_key_name()` 统一取名，不要假定键的类型。

## 模块系统（已实现）

「扩展功能」页现在是一个**模块商店**：

* 初始为**空列表**；点**添加模块**进商店 —— 商店是 `Yumina0789/N1bus-modules` 仓库的
  **GitHub Pages 静态站**（`--store-url` 可换），本体只负责从它取清单与包；
* 模块**逐个安装 / 卸载**，**装上即出现在子导航里** —— 没有单独的「接入」开关；
* 每个模块**有自己的容器、自己的页面**，页面以**同源 iframe** 嵌入；本体不代理它的流量、
  也不替它认证（反代与 TLS 是 Caddy 的事）；
* 包按 `index.json` 里的 `size` 与 `sha256` **双重校验**，解包拒绝绝对路径 / `..` / 符号链接
  （比 `tarfile.extractall()` 保守得多：逐个成员自己写文件）；
* 装卸、容器编排与失败原因**全程写审计**。

容器编排走五个受控动作 `module.install` / `.start` / `.stop` / `.restart` / `.down`：
argv 全由本体拼死，清单里**没有任何「要执行的字符串」** —— 模块的代码只跑在它自己的容器里。
因此 ADR 0002 的边界（面板不执行外部提供的命令）在模块系统里依然成立。

卸载 = 先 `module.down` 摘掉容器、再删模块目录，**数据卷保留**。顺序不能反：`-f <path>`
指着模块目录，目录一删就再也操作不了那套 compose 了。

### 还没做的

* **KMS 与 Harness 仍硬编码在本体里**（两个写死的子视图），尚未打包装进商店 —— 要等
  `Yumina0789/N1bus-modules` 仓库建好、模块包发布之后。它们是当前的默认内容，
  不影响模块系统的使用。
* **计划任务改成可写脚本 + 系统 crontab**：已记入 **ADR 0013** —— 那是一次安全模型倒退
  （脚本可写 = 面板重新拥有任意命令执行能力），它正式修订了 ADR 0002 与 0006。代码未动。
* 模块的**升级**、模块间依赖、registry 签名，见 `docs/modules.md` §10。

### 文档地图

| 文件 | 是什么 | 状态 |
|---|---|---|
| `README.md` | 本文，面向使用者与运维 | 描述**当前代码** |
| `CONTEXT.md` | 词汇表：项目里反复出现的名词 | 词条带实现状态标记 |
| `docs/modules.md` | 模块系统：规格与实现状态 | **框架已实现**；KMS / Harness 搬迁未做 |
| `docs/adr/` | 决策记录（为什么这么做） | 每条一个决定；0012 已落地一半，0013 待实现 |

一句话说清楚：**模块框架已经能用**（商店、装卸、iframe 页面、五个 `module.*` 动作），
但 **KMS 与 Harness 还没搬出去** —— 它们仍是本体里两个硬编码的子视图
（`n1bus_kms.py` + `tab-ext`）。

2026-10 已修掉两处：只读 TTL 缓存那套空转的机制被删除（见「设计上的几条硬性约束」），
`X-Forwarded-For` 在 `--allow-ip` 留空时被无条件采信的问题也已修（见「挂在反向代理后面」）。
目前**没有已知的实现缺陷** —— 本文里那些「做不到的事」都是刻意设计，不是缺陷。

词汇表在 `CONTEXT.md`，决策记录在 `docs/adr/`。

## 自检

```bash
python3 tools/check.py     # 语法、界面里 id 与图标、安全底线
python3 tools/smoke.py     # 拉起演示实例，把所有接口打一遍
```

CI（`.github/workflows/ci.yml`）在 Python 3.8 / 3.11 / 3.13 上跑这两条，外加 shellcheck。

## 许可

MIT，见 `LICENSE`。
