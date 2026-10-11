# 模块规范（Modules）

> **状态：已实现并上线（2026-10-11）**
>
> 落地的东西：
>
> * `n1bus_modules.py` —— 商店清单、manifest 与模块包的校验、防穿越解包、安装与卸载；
> * 五个受控动作 `module.install` / `.start` / `.stop` / `.restart` / `.down`
>   （`module.install` 对没有 compose 的模块退化成 `up -d`）；
> * 四个端点 `/api/modules`、`/api/modules/store`、`/api/modules/install`、
>   `/api/modules/uninstall`，外加 `/api/modules/entry`（动态入口）；
> * **装 / 卸模块时自动重写 Caddy 路由并热重载**，页面「装上即出现」；
> * 模块页：列表 + 「模块商店」+ 逐个装卸，装上即出现子标签（iframe 页面）；
> * registry 仓库 [`Yumina0789/N1bus-modules`](https://github.com/Yumina0789/N1bus-modules)
>   已建好并启用 Pages，里面是 **`rsc4d`** 与 **`harness`** 两个模块。
>
> 还没做：模块升级、模块间依赖、registry 签名（见 §10）。

---

## 1. 它解决什么

面板一开始把 KMS 与 Harness **硬编码**在本体里（`tab-ext` 下就这两个子视图）。每加一个
功能都要改本体源码与 `ui.html` 两处、重新部署一遍。

**模块**把这层变成数据：功能以一份清单描述，从受信任的商店拉取、校验、安装，
**装上即出现在模块页** —— 加功能不再需要动本体源码。

---

## 2. 词汇（唯一事实源在 `CONTEXT.md`，这里是速查）

| 词 | 指什么 | **不指**什么 |
|---|---|---|
| **本体（core）** | N1bus 自己：认证与安全底线、观测、白名单动作、文件管理、任务、告警、审计，**加上模块框架** | 不是模块；**不**包含 KMS / Harness，它们已经是模块 |
| **模块（module）** | 一个可安装的功能单元：manifest + 可选 compose + **一个自己的页面**。**它的逻辑全在自己进程里** | 不是 DSH 插件（那是 `@deepseek-ai/dsh` 的插件，另一层） |
| **模块包（package）** | 模块打成的单个 `.tar.gz`，整体 sha256 即身份 | 不是 git 快照 —— 只认归档，不认「拉某个分支 HEAD」 |
| **registry** | 模块清单与包的发布仓库：`Yumina0789/N1bus-modules` | 不是 PyPI/npm，不做依赖解析 |
| **商店（store）** | 上面那个仓库的 **GitHub Pages 静态站**；本体从它取清单与包 | 不在本体里；也不是应用市场 —— 只有受信任的那一个 registry |
| **模块动作（module.\*）** | 本体为模块新增的受控白名单动作 | 不是模块自带的命令 |

> 已删除的词：**接入（attach）**、**页面块（block）**、**扩展（extension）**。

---

## 3. 本体与模块的边界

**留本体**：

- 认证与会话、服务 token、来源限制 —— 模块碰不到
- systemd / docker / ufw 的观测与 `ACTIONS` 白名单动作
- 文件管理器、任务、告警、审计
- **模块框架**（§6）

**已经搬出去**（2026-10，写死的两个视图连同后端一起删了，现在以模块形式回归）：

- KMS 页：`sub-kms` + `loadKms()` + `n1bus_kms.py` + `/api/kms*` 全套删除。
  KMS 现在由容器 `rs-c4d` 自己承担，面板不代取数据
- Harness 页：`sub-harness` + `#dshFrame` + 前端那一整块逻辑删除；
  后端 `/api/dsh` 也**已下线** —— 它被 `page.entry` 这个通用机制取代（§6），
  本体不再认识任何具体模块

---

## 4. registry 与商店

```
N1bus-modules/                    # 仓库 = registry
├── index.json                    # 商店与本体读的清单（由 tools/pack.py 回填）
├── packages/
│   ├── rsc4d-1.0.1.tar.gz        # 模块包：整体 sha256 即身份
│   └── harness-1.0.1.tar.gz
├── src/<id>/                     # 模块源文件（人在这里改）
├── templates/                    # 编写新模块时抄这里
└── tools/pack.py                 # 打包 + 算哈希 + 回填 index.json
```

它的 **GitHub Pages 就是商店**：`https://yumina0789.github.io/N1bus-modules/`。
**安装动作由本体执行**，商店页本身不碰服务器。

- 本体**只认这一个仓库**，**不提供**「填任意仓库 URL」的入口（`--store-url` 是安装期
  的部署选项，不是界面功能）。想加模块 → 往 `N1bus-modules` 提 PR。
- **为什么用归档而非散文件**：sha256 直接对一个字节序列计算，无需「目录哈希规范化」
  这类易错机制；下载 → 校验 → 解包是一条线性流程。
- `pack.py` 是**确定性打包**（固定 mtime / uid / gid，gzip 头 mtime 归零）：
  同样的源打两次得到同样的哈希，否则「哈希即身份」名存实亡。
- **改了包内容必须升版本号**：覆盖同版本号会让已缓存清单的部署校验失败。
  下架某个版本同理 —— 缓存还没过期（5 分钟）的部署会拿到 404。

### index.json

```json
{
  "schema": 1,
  "registry": "Yumina0789/N1bus-modules",
  "modules": [
    {
      "id": "rsc4d",
      "name": "KMS 激活服务",
      "summary": "rs-c4d：KMS wire 协议服务端（v4 / v5 / v6），自带一个状态页",
      "icon": "key",
      "homepage": "https://github.com/Yumina0789/RSC4D",
      "latest": "1.0.1",
      "versions": {
        "1.0.1": {
          "url": "packages/rsc4d-1.0.1.tar.gz",
          "sha256": "<64 位十六进制>",
          "size": 4243,
          "min_core": "1.0"
        }
      }
    }
  ]
}
```

- `sha256` 与 `size` **均为必填**：先比 size（低成本粗筛），再比 sha256（权威）。
- `min_core` 用来拒绝「模块要求的本体版本比本机新」。
- `url` 若是绝对地址，必须落在商店站内 —— 清单不能把本体指到别的主机。

---

## 5. 模块包结构

解包后必须**恰好**包含一个 `manifest.json`（且**位于包根**），可带其它文件。

```
<解包根>/
├── manifest.json        # 必需
├── compose.yml          # 可选：需要容器栈才有；文件名由 manifest 指定
└── ...                  # 可选：页面、静态资源、模块自己的配置
```

### manifest.json

```json
{
  "schema": 1,
  "id": "rsc4d",
  "name": "KMS 激活服务",
  "version": "1.0.1",
  "summary": "rs-c4d：KMS wire 协议服务端（v4 / v5 / v6），自带一个状态页",
  "icon": "key",
  "homepage": "https://github.com/Yumina0789/RSC4D",

  "compose": {"file": "compose.yml", "project": "n1bus-rsc4d"},

  "ports": [
    {"port": 1688, "proto": "tcp", "expose": true, "note": "KMS 客户端：裸 TCP"}
  ],

  "page": {
    "kind": "iframe",
    "title": "KMS 激活服务",
    "url": "/m/rsc4d/",
    "upstream": "n1bus-rsc4d-web:80"
  }
}
```

| 字段 | 必需 | 约束 |
|---|---|---|
| `schema` | ✔ | 目前只有 `1`；不认识的值直接拒绝 |
| `id` | ✔ | `^[a-z][a-z0-9-]{1,31}$`；与 index.json 一致 |
| `version` | ✔ | 与 index.json 一致 |
| `compose.project` | ✖ | 必须等于 `n1bus-<id>` |
| `compose.file` | ✖ | 包内的简单文件名（不含路径分隔符） |
| `icon` | ✖ | 必须是本体**已有图标集**里的名字（不加载外部 SVG） |
| `ports` | ✖ | 仅用于展示与「该端口绕过 ufw」提示；本体**不据此改防火墙** |
| `page.kind` | ✔ | **只有 `iframe`** |
| `page.url` | ✔ | 站内绝对路径，如 `/m/<id>/`。字符集卡得很紧，因为它会被写进 Caddy 配置 |
| `page.upstream` | ✖ | `host:port`。Caddy 把 `page.url` 这个前缀转给谁；没有它就不生成路由 |
| `page.entry` | ✖ | 入口地址不是固定值时的取法，见 §6 |

### 可配置项（`config`）

模块可以把「值得在面板上改的参数」声明出来，面板据此在模块页生成一张表单：

```json
"config": [
  {"key": "KMS_RATE_LIMIT", "label": "每来源连接数上限", "type": "number",
   "default": 60, "min": 1, "max": 100000,
   "help": "同一来源在「拒绝时长」内最多发起多少次连接，超出即丢弃"},
  {"key": "KMS_DOMAIN", "label": "对外域名", "type": "text",
   "default": "", "placeholder": "kms.example.com"}
]
```

| 字段 | 说明 |
|---|---|
| `key` | 必填，`^[A-Z][A-Z0-9_]{1,31}$`。它会成为 `.env` 里的一行 |
| `type` | `text` / `number` / `bool` / `select` |
| `default` | `bool` 用 `"0"` / `"1"`；`select` 必须落在 `options` 里 |
| `min` / `max` | 仅 `number` |
| `options` | 仅 `select` |
| `label` / `help` / `placeholder` | 界面文案 |

保存时本体做三件事，顺序不能反：

1. **校验**（类型、范围、字符集）
2. 写 `<模块目录>/.env`（compose 的变量来源）**与** `config.json`（模块页面读它——
   页面是它自己容器里的静态页，读不到 `.env`）
3. `docker compose up -d` —— 环境变量变了，compose 会**重建**受影响的容器

> **`.env` 是行式格式，一个换行就能把一个值变成两个变量。** 所以值只接受可见 ASCII、
> 长度 ≤ 200，键名也不许出现小写与连字符 —— 否则一个「域名」框就能往 `.env` 里塞变量。
>
> 用户改过的 `.env` / `config.json` 在**重装模块时会被保留**：升级不该冲掉配置。

---

## 6. 本体的模块框架

| # | 能力 | 说明 |
|---|---|---|
| 1 | **编排模块的容器** | `module.install` / `.start` / `.stop` / `.restart` / `.down` —— argv 全由本体拼，路径由本体算成 `/opt/n1bus/modules/<id>/` |
| 2 | **iframe 嵌模块自己的页面** | 站内路径 `/m/<id>/`，**由 Caddy 转给模块的容器**；本体不代理、不替它认证 |
| 3 | **维护 Caddy 路由** | 装 / 卸模块后重写路由片段并热重载 |
| 4 | **取动态入口** | `page.entry`，见下 |
| 5 | **收集并落盘模块配置** | `config` 声明 → 模块页表单 → `.env` + `config.json` → 重建容器（见 §5） |

### 动作

| 动作 | argv | danger |
|---|---|---|
| `module.install` | `docker compose -p n1bus-<id> -f <path>/<file> up -d` | ✔ |
| `module.start` | … `start` | ✖ |
| `module.stop` | … `stop` | ✔ |
| `module.restart` | … `restart` | ✔ |
| `module.down` | … `down` | ✔ |

`<id>` 只来自**通过校验的 manifest**；`<path>` 由本体算，**不接受 manifest 里的路径**。

`module.down` 只用于**卸载**：`-f <path>` 指着模块目录，目录一删就再也操作不了那套容器了，
所以必须先摘容器。它**保留数据卷**（不带 `-v`）。

### Caddy 路由怎么接上

本体把已装模块的路由渲染成一份片段，写到
`<caddy-sites>/modules/routes.caddy`，整份覆盖：

```caddy
handle_path /m/rsc4d/* {
	reverse_proxy n1bus-rsc4d-web:80
}
```

站点块里只需要一行把它收进来：

```caddy
lolicon.fans {
	import /etc/caddy/sites/modules/*.caddy

	reverse_proxy 172.18.0.1:8402
}
```

> **踩过的坑：站点块的响应头会一并应用到模块页面上。**
> 如果那里写着 `X-Frame-Options DENY`（面板自己确实不需要被谁嵌入），浏览器就会
> **拒绝把模块页面显示在 iframe 里** —— 表现是 iframe 一片空白或报「拒绝连接」，
> 而服务端日志干干净净、`curl /m/<id>/` 还是 200。改成 `SAMEORIGIN` 即可：
> 模块页面与面板同源，既允许面板嵌它，又不允许外部站点嵌面板。

写完执行 `docker exec <caddy容器> caddy reload --config /etc/caddy/Caddyfile`。
**两个配置项 `--caddy-sites` / `--caddy-container` 都给全才启用**；都不给就完全不做同步
（面板不假设服务器上一定有 Caddy，也不去猜它的目录在哪）。

两侧约定：

- **glob 必须始终能匹配到文件** —— Caddy 的 `import` 找不到文件会直接报错。
  所以本体至少会写一份带注释的空片段。
- **模块容器必须挂在 Caddy 所在的那个 docker 网络上**，否则 `upstream` 解析不到。

### 动态入口（`page.entry`）

有的模块入口地址**每次启动才生成**、只打印在容器日志里（用一次性 token 换会话 cookie 的那类）。
manifest 用 `page.entry` 声明，前端在打开页面时先取再嵌 iframe：

```json
"page": {
  "kind": "iframe", "url": "/m/harness/", "upstream": "n1bus-harness:3081",
  "entry": {"type": "container-log-token", "container": "n1bus-harness"}
}
```

`type` 是**白名单，目前只有 `container-log-token`**：本体读该容器最近 400 行日志，
用**本体自己内置的**正则提取 token，拼成 `page.url?token=…`。
**模块给不了正则、也给不了命令** —— 与 ADR 0002 的边界一致。

结果**不缓存**：这类 token 换完 cookie 就作废，缓存下来的只会是一个打不开的地址。
容器名会作为 `docker logs` 的一个参数，因此按 `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$` 校验。

---

## 7. 安装与卸载流程

```
安装：预检 → 从商店取 index.json → 下载 tar.gz → size gate → sha256 gate
      → 防穿越解包 → manifest 校验 → 落到 /opt/n1bus/modules/<id>/
      → module.install → 重写 Caddy 路由 + 热重载
      → 页面出现在模块页（没有「接入」这一步）
      失败时：包或清单不合法 → 服务器上不留半个模块；
              容器起不来 → 目录保留（便于排障），如实报错，不回滚 Caddy

卸载：module.down（摘容器，保留数据卷）→ 移走目录 → 重写路由（撤掉该模块）
```

每一步都写审计（`module.fetch` / `module.install` / `module.route` / `module.remove`）。

---

## 8. 安全模型（与 ADR 0002 共存）

用户选定「**模块的逻辑全在自己的进程里**」—— 这一点使 ADR 0002 **无需修改**：

1. **模块不提供命令。** manifest 里没有任何「要执行的字符串」；本体执行的永远是写死的
   `module.*` 动作。模块的代码跑在**它自己的容器**里，本体只是编排它。
2. **一切都过哈希。** 只认 `index.json` 里的 sha256；解包时拒绝绝对路径、`..`、
   符号 / 硬链接（与文件管理器同一套 `realpath` + 根判定）。
3. **受信 registry 是唯一来源**（见 §4）。
4. **动态入口的类型是白名单**，正则写死在本体里；模块影响不了怎么解析（§6）。

> 模块页面与面板**同源**（`/m/<id>/` 与 `/` 同一个域名）。这是有意的取舍：
> 模块来自受信 registry，同源让它能直接用面板的会话，代价是模块页面在原理上
> 也能碰到面板的存储。跨源（子域名）能消除这一点，但要给每个模块一个 DNS 记录
> 与证书 —— 现阶段不值当。

---

## 9. 商店里的两个模块

| 模块 | 内容 | 页面 |
|---|---|---|
| **rsc4d** | KMS 服务端 `rs-c4d`（协议 v4 / v5 / v6）。容器暴露 `1688`（裸 TCP，给客户端）与 `8080`（rs-c4d-web，供探测）；另有一个 `caddy:2-alpine` 容器承载状态页并把 `healthz` 转给 KMS 容器 | `/m/rsc4d/`：实时探测 KMS 可达性 + 各平台的客户端配置命令 |
| **harness** | DeepSeek Harness —— 容器里的 agent 面板（与 ADR 0008 同一套部署）。本体只监听容器内的 127.0.0.1，靠镜像里的 TCP 转发把 `3081` 露出来 | `/m/harness/`：dsh 自己的界面，入口用 `page.entry` 取一次性 token |

**为什么 rsc4d 的页面不是 rs-c4d 自带的那个 web**：它把链接写成根绝对路径（`/status`），
挂在 `/m/rsc4d/` 前缀下点开会打到面板自己的路由上。所以状态页由模块自带，
探测请求走相对路径 `healthz`，两边对得上。

两个模块的镜像都**没有发布到公开 registry**（`rs-c4d:local` / `dsh:local`），
compose 里的 `image:` 写成 `${VAR:-默认值}`，可用模块目录下的 `.env` 覆盖。

RSC4D 上线后，**vlmcsd 退役**，激活记录不迁移（见 ADR 0012）——
注意**退役的是 vlmcsd，不是那套面板**。

---

## 10. 未决

| # | 问题 | 倾向 |
|---|---|---|
| 1 | 模块的**升级**（装上更高版本） | 下一轮。目前要升级得先卸载再装 |
| 2 | 模块之间的依赖 | 不做 |
| 3 | registry 是否要求签名（minisign/GPG） | 先不做；哈希 + 单源已在可接受范围 |
| 4 | 读模块容器日志（本体内置一个「模块日志」块） | 有用，但用 `docker logs` 也能看，优先级低 |
| 5 | 模块页面的同源路径规则（`/m/<id>/` 还是别的） | 已定为 `/m/<id>/` |
| 6 | 模块根目录（`/opt/n1bus/modules`）是否做成可配置 | 目前真机固定、演示模式指到数据目录下 |
| 7 | 模块镜像的公开分发 | 待定：需要能把镜像推到 ghcr（token 需 `write:packages`） |
