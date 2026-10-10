# 扩展规范（Extensions）

> 状态：**草案，待主人确认**。本轮只定规范，不写面板代码。
> 相关决策：ADR 0002（不做任意命令执行）、ADR 0009（写权限收敛到白名单根）。

---

## 1. 它解决什么

面板现在把 KMS 与 Harness 硬编码成「扩展功能」页里的两个子标签。要加第三个功能，
就得改 `n1bus.py` 和 `ui.html` 两处源码再重新部署一遍。

**扩展**把这层变成数据：功能以**声明式清单**描述，从受信的 registry 拉取、校验、
安装，装完自动出现在「扩展功能」页里。

---

## 2. 词汇

| 词 | 指什么 | **不指**什么 |
|---|---|---|
| **扩展（extension）** | 一个可安装的功能单元：一份 manifest + 可选容器编排 + 可选页面声明 | 不是 DSH 插件（那是 `@deepseek-ai/dsh` 的插件，另一层东西） |
| **扩展包（package）** | 扩展被打成的单个归档（`.tar.gz`），带整体 sha256 | 不是 Git 仓库快照 —— 只认归档，不认「拉某个分支 HEAD」 |
| **registry** | 扩展清单的发布仓库：`Yumina0789/N1bus-extensions` | 不是 PyPI/npm，不带依赖解析 |
| **商店（store）** | 面板里列出可用扩展、并触发安装的界面 | 不是应用市场 —— 里面只有你信任的那一个 registry |
| **内置扩展（builtin）** | 随面板发布、无需安装就存在的扩展（`kms`、`harness`） | 不是「装的」扩展；但商店里同样列出 |
| **声明式页面（declared page）** | manifest 描述、由面板自己渲染的功能页 | 不是扩展注入的 HTML/JS |
| **扩展动作（ext.\*）** | 面板为扩展新增的受控白名单动作 | 不是扩展自带的命令 |

---

## 3. registry 仓库布局

```
N1bus-extensions/
├── index.json                    # 商店读这个
└── packages/
    ├── kms-1.0.0.tar.gz          # 扩展包：整体 sha256 即身份
    └── dsh-1.0.0.tar.gz
```

**为什么是归档而不是散文件**：sha256 直接对**一个字节序列**算，不需要「目录哈希规范化」
那种容易出错的东西。且下载 → 校验 → 解包是一条直线。

### index.json

```json
{
  "schema": 1,
  "registry": "Yumina0789/N1bus-extensions",
  "extensions": [
    {
      "id": "kms",
      "name": "KMS 激活服务",
      "summary": "vlmcsd + 管理面板，为 Windows / Office 提供 KMS 激活",
      "icon": "key",
      "homepage": "https://github.com/Yumina0789/V1mc4d",
      "latest": "1.0.0",
      "versions": {
        "1.0.0": {
          "url": "packages/kms-1.0.0.tar.gz",
          "sha256": "<64 位十六进制>",
          "size": 123456,
          "min_panel": "1.0"
        }
      }
    }
  ]
}
```

- `sha256` 与 `size` **都是必填**：面板先比 size（省流量的粗筛），再比 sha256（权威）。
- `min_panel` 用来拒绝「扩展要求的面板版本比本机新」。

---

## 4. 扩展包内容

解包后必须**恰好**包含一个 `manifest.json`，可选的 `compose.yml`、页面资源。

```
<解包根>/
├── manifest.json        # 必需
├── compose.yml          # 可选：需要容器栈才有
└── ...                  # 可选：图标、说明等静态资源（面板不执行其中任何代码）
```

### manifest.json

```json
{
  "schema": 1,
  "id": "kms",
  "name": "KMS 激活服务",
  "version": "1.0.0",
  "summary": "vlmcsd + 管理面板，为 Windows / Office 提供 KMS 激活",
  "icon": "key",

  "compose": {
    "file": "compose.yml",
    "project": "n1bus-kms",
    "services": ["vlmcsd", "v1m-dash"]
  },

  "ports": [
    {"port": 1688, "proto": "tcp", "expose": true,  "note": "KMS 客户端"},
    {"port": 8099, "proto": "tcp", "expose": false, "note": "管理 API（仅回环）"}
  ],

  "api": {
    "upstream": "http://127.0.0.1:8099",
    "token_file": "/var/lib/n1bus/extensions/kms/token",
    "writes": {
      "service":         ["/api/service",        ["action"]],
      "config_rollback": ["/api/config/rollback", []],
      "unban":           ["/api/limits/unban",   ["ip"]]
    }
  },

  "page": {
    "kind": "both",
    "title": "KMS",
    "sections": [
      {"type": "status",  "label": "服务状态", "from": "service"},
      {"type": "kv",      "label": "统计",     "from": "stats"},
      {"type": "actions", "items": ["ext.start", "ext.stop", "ext.restart"]}
    ],
    "iframe": {"label": "工作台", "path": "/dsh/"}
  }
}
```

**字段契约**

| 字段 | 必需 | 约束 |
|---|---|---|
| `schema` | ✔ | 目前只有 `1`；不认识的值直接拒绝 |
| `id` | ✔ | `^[a-z][a-z0-9-]{1,31}$`；与 index.json 里的一致，否则拒绝 |
| `version` | ✔ | 与 index.json 里的一致 |
| `icon` | ✖ | 必须是面板**已有图标集**里的名字，否则回落默认图标（不加载外部 SVG） |
| `compose.project` | ✖ | 必须等于 `n1bus-<id>`，面板不信任任意 project 名 |
| `ports` | ✖ | 仅用于展示与「该端口绕过 ufw」提示；**面板不据此改防火墙** |
| `api.upstream` | ✖ | 只允许 `http://127.0.0.1:<port>` / `http://172.17.0.1:<port>` 这类本机地址 |
| `api.writes` | ✖ | 与现有 `KMS_WRITES` 同构：`操作名 → (路径, 允许透传的参数)`；`[]` 表示不传参 |
| `page.sections[].type` | ✔ | 只能是 `status` / `kv` / `table` / `log` / `actions` 五种 |
| `page.iframe.path` | ✖ | 必须是 `/` 开头的同源绝对路径（由 Caddy 路由），面板不代理它 |

---

## 5. 安全模型（与 ADR 0002 共存）

面板的核心主张是**不提供任意命令执行**。扩展机制必须不破坏它，靠三条：

1. **扩展不提供命令。**
   manifest 里没有任何「要执行的字符串」。面板执行的永远是**写死的白名单动作**，
   扩展只能**选择**用哪几个、以及容器编排文件名。

2. **一切都过哈希。**
   面板只认 `index.json` 里那个 sha256。下载后 size 与 sha256 都对上才解包；
   解包时拒绝绝对路径、`..`、符号链接（与文件管理器同一套 `realpath` + 根判定）。

3. **受信 registry 是唯一来源。**
   面板**不提供**「填任意仓库 URL」的入口。registry 地址是面板内置的常量。
   想加扩展 → 往 `N1bus-extensions` 提 PR。这条路窄，但它是这个功能能存在的前提。

### 新增的受控动作

现有 `ACTIONS` 表里没有 compose 类动作，所以必须新增（argv 全部由面板拼，扩展无法影响）：

| 动作 | argv（不可由扩展改写） | danger |
|---|---|---|
| `ext.install` | `docker compose -p n1bus-<id> -f <ext_dir>/compose.yml up -d` | ✔ |
| `ext.start` | `docker compose -p n1bus-<id> -f <ext_dir>/compose.yml start` | ✖ |
| `ext.stop` | `docker compose -p n1bus-<id> -f <ext_dir>/compose.yml stop` | ✔ |
| `ext.restart` | `docker compose -p n1bus-<id> -f <ext_dir>/compose.yml restart` | ✔ |

- `<id>` 只来自**通过校验的 manifest**，且必须匹配白名单正则。
- `<ext_dir>` 由面板算成 `/opt/n1bus/extensions/<id>`，**不接受** manifest 里的路径。
- 没有 `compose` 字段的扩展（纯声明式）没有这些按钮。

---

## 6. 安装流程（沿用本项目既有的部署纪律）

```
预检（面板版本 / 磁盘 / 是否已装）
  → 下载 tar.gz 到临时目录
  → size gate
  → sha256 gate          ← 不通过就中止，什么都不改
  → 解包（防穿越：绝对路径 / .. / 符号链接一律拒绝）
  → manifest 校验（schema / id / version / 各字段契约）
  → 落到 /opt/n1bus/extensions/<id>/
  → 执行 ext.install
  → 端点检查（manifest 声明的 expose 端口是否在听）
  → 失败：停容器 + 移走目录 + 写审计，面板回到装之前的状态
```

**每一步都写审计**（与白名单动作同等对待）。安装、启动、停止、卸载都是安全相关事件。

---

## 7. 功能页

`page.kind` 三选一：

| 值 | 表现 |
|---|---|
| `declared` | 面板用已有渲染器画 `sections`（kv / status / table / log / actions） |
| `iframe` | 整页就是 `iframe` 指向扩展自己的 Web UI（Harness 现在就是这么嵌的） |
| `both` | 声明式状态页作为默认子标签，iframe 作为额外子标签 |

**只读数据**来自面板代理：`GET /api/ext/<id>/<section.from>`，
面板按 `api.upstream` 转发（与现有 `/api/kms` 同一套模式）。
**写操作**只走 `api.writes` 白名单 + `ext.*` 动作，绝不开放任意转发。

---

## 8. 内置扩展

`kms` 与 `harness` 随面板发布，是「已装」状态的内置扩展：

- manifest 内置在面板里（不下载）；商店里 `builtin: true`。
- 状态从**本机实际情况**读（容器在不在、端口在不在听）——不假装。
- registry 里若有更高版本，商店提示「可升级」（升级本身下一轮做）。

### ⚠️ 它们**不搬家**

这两套栈已经在服务器上跑着，各有自己的目录与 compose project：

| 扩展 | 现状目录 | 内容 |
|---|---|---|
| `kms` | `/opt/nimbus/docker/` | vlmcsd + v1m-dash + supervisord |
| `harness` | `/opt/dsh/` | `dsh:local` 容器 |

**不要**为了「统一」把它们搬进 `/opt/n1bus/extensions/`：`docker compose` 换目录会**重建容器**，
KMS 客户端要断约 10 秒（见 [OPERATIONS.md](../../../OPERATIONS.md)）。

所以 `ext.*` 动作取 compose 文件的路径有两条来源，**各自写死、扩展无法影响**：

1. **外部装的扩展** → `/opt/n1bus/extensions/<id>/compose.yml`（面板算出来）
2. **内置扩展** → 面板里的**固定路径常量**（`/opt/nimbus/docker/docker-compose.yml`、`/opt/dsh/docker-compose.yml`）

对内置扩展，`ext.install` 退化成 `up -d`（栈已存在，只保证它在跑），**不会销毁已有容器**。

---

## 9. 待确认 / 未决

| # | 问题 | 当前倾向 |
|---|---|---|
| 1 | `page.sections` 的五种块类型是否够用？KMS 页的深度集成表达得了吗？ | 先用这五种，不够再补 |
| 2 | 纯声明式扩展（没有 compose）第一版要不要支持？ | 要（Harness 就是这种：容器另外装） |
| 3 | 卸载与更新 | **下一轮**，本轮不做 |
| 4 | 扩展之间的依赖 | 不做 |
| 5 | 同一扩展多版本并存 | 不做 |
| 6 | registry 要不要签名（minisign/GPG） | 先不做；哈希 + 单源已经在可接受范围 |
