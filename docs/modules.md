# 模块规范（Modules）

> 状态：**已与主人对齐**（2026-10，设计树第 1/2 轮）。
> 取代早先的 `docs/extensions.md`（那份用的是「扩展」，术语已废）。
> 相关决策：ADR 0002（不做任意命令执行）、ADR 0009（写权限收敛到白名单根）、ADR 0012（本体模块化）。

---

## 1. 它解决什么

面板现在把 KMS 与 Harness **硬编码**在本体里（`tab-ext` 下就这两个子视图）。每加一个功能
都要改 `n1bus.py` 与 `ui.html` 两处源码、重新部署一遍。

**模块**把这层变成数据：功能以声明式清单描述，从受信 registry 拉取、校验、安装，
装完由主人决定**接入**与否 —— 接入就出现在「模块」页里，不接入就只是装着。

---

## 2. 词汇（唯一事实源在 `CONTEXT.md`，这里是速查）

| 词 | 指什么 | **不指**什么 |
|---|---|---|
| **本体（core）** | N1bus 自己：认证与安全底线、观测、白名单动作、文件管理、任务、告警、审计，**加上模块框架** | 不是模块，也**不**包含 KMS/Harness（它们已搬出去） |
| **模块（module）** | 一个可安装的功能单元：manifest + 可选 compose + 可选页面声明。**它的逻辑全在自己进程里** | 不是 DSH 插件（那是 `@deepseek-ai/dsh` 的插件，另一层） |
| **模块包（package）** | 模块打成的单个 `.tar.gz`，整体 sha256 即身份 | 不是 git 快照 —— 只认归档，不认「拉某个分支 HEAD」 |
| **registry** | 模块清单的发布仓库：`Yumina0789/N1bus-modules` | 不是 PyPI/npm，不做依赖解析 |
| **商店（store）** | 本体里列出可用模块、触发安装的界面 | 不是应用市场 —— 只有你信任的那一个 registry |
| **接入（attach）** | 把**已装**模块挂进本体界面（一个开关）。不接入 = 装了但不显示 | **不是**安装；装与接是两件事 |
| **页面块（block）** | 本体渲染的一种页面元素：`status` / `kv` / `table` / `log` / `actions` / `aggregate` | 不是模块注入的 HTML/JS |
| **模块动作（module.\*）** | 本体为模块新增的受控白名单动作 | 不是模块自带的命令 |

---

## 3. 本体与模块的边界

**留本体**（主人明确要求：除 kms 与 harness 之外，面板该有的功能全留）：

- 认证与会话、服务 token、来源限制 —— 模块碰不到
- systemd / docker / ufw 的观测与 `ACTIONS` 白名单动作
- 文件管理器、任务、告警、审计
- **模块框架**（见 §6）

**搬出去**（变成模块）：

- KMS 页（`sub-kms` + `loadKms()` + `KMSService` + `KMS_WRITES` + `/api/kms*`）
- Harness 页（`sub-harness` + `#dshFrame` + `/api/dsh`）

> 搬运量实测：`n1bus.py` 里 6 处分散点、`ui.html` 里约 80 行 JS + 一个子视图。

---

## 4. registry 仓库布局

```
N1bus-modules/
├── index.json                    # 商店读这个
├── packages/
│   ├── kms-1.0.0.tar.gz          # 模块包：整体 sha256 即身份
│   └── harness-1.0.0.tar.gz
└── templates/                    # 写新模块时抄这里
```

**为什么是归档而不是散文件**：sha256 直接对*一个字节序列*算，不需要「目录哈希规范化」
那种容易出错的东西；下载 → 校验 → 解包是一条直线。

### index.json

```json
{
  "schema": 1,
  "registry": "Yumina0789/N1bus-modules",
  "modules": [
    {
      "id": "kms",
      "name": "KMS 激活服务",
      "summary": "rs-c4d：KMS wire 协议服务端（v4/v5/v6）",
      "icon": "key",
      "homepage": "https://github.com/Yumina0789/RSC4D",
      "latest": "1.0.0",
      "versions": {
        "1.0.0": {
          "url": "packages/kms-1.0.0.tar.gz",
          "sha256": "<64 位十六进制>",
          "size": 4096,
          "min_core": "1.0"
        }
      }
    }
  ]
}
```

- `sha256` 与 `size` **都是必填**：先比 size（省流量的粗筛），再比 sha256（权威）。
- `min_core` 用来拒绝「模块要求的本体版本比本机新」。

---

## 5. 模块包结构

解包后必须**恰好**包含一个 `manifest.json`，可有 `compose.yml`。

```
<解包根>/
├── manifest.json        # 必需
├── compose.yml          # 可选：需要容器栈才有
└── ...                  # 可选：图标等静态资源（本体不执行其中任何代码）
```

### manifest.json

```json
{
  "schema": 1,
  "id": "kms",
  "name": "KMS 激活服务",
  "version": "1.0.0",
  "summary": "rs-c4d：KMS wire 协议服务端（v4/v5/v6）",
  "icon": "key",
  "homepage": "https://github.com/Yumina0789/RSC4D",

  "compose": {
    "file": "compose.yml",
    "project": "n1bus-kms",
    "services": ["rsc4d"]
  },

  "ports": [
    {"port": 1688, "proto": "tcp", "expose": true, "note": "KMS 客户端（裸 TCP，不经反代）"}
  ],

  "page": {
    "kind": "declared",
    "title": "KMS",
    "sections": [
      {"type": "actions", "items": ["module.start", "module.stop", "module.restart"]},
      {"type": "aggregate", "label": "激活统计", "...": "见 §7"},
      {"type": "log", "label": "最近请求", "source": {"kind": "container-log",
                                                     "container": "n1bus-kms-rsc4d",
                                                     "tail": 200}}
    ]
  }
}
```

**字段契约**

| 字段 | 必需 | 约束 |
|---|---|---|
| `schema` | ✔ | 目前只有 `1`；不认识的值直接拒绝 |
| `id` | ✔ | `^[a-z][a-z0-9-]{1,31}$`；与 index.json 一致，否则拒绝 |
| `version` | ✔ | 与 index.json 一致 |
| `icon` | ✖ | 必须是本体**已有图标集**里的名字（不加载外部 SVG） |
| `compose.project` | ✖ | 必须等于 `n1bus-<id>`，本体不信任任意 project 名 |
| `ports` | ✖ | 仅用于展示与「该端口绕过 ufw」提示；**本体不据此改防火墙** |
| `api.upstream` | ✖ | 只允许 `http://127.0.0.1:<port>` / `http://172.17.0.1:<port>` 这类本机地址 |
| `api.writes` | ✖ | 同构于早先的 `KMS_WRITES`：`操作名 → (路径, 允许透传的参数)`；`[]` 表示不传参 |
| `page.kind` | ✔ | `declared`（本体渲染） / `iframe`（嵌模块自己的 UI） / `both` |

---

## 6. 本体的模块框架（五项能力）

主人确认全要：

| # | 能力 | 说明 |
|---|---|---|
| 1 | **编排模块的容器** | `module.install` / `.start` / `.stop` / `.restart` —— argv 全由本体拼，路径来自通过校验的 manifest |
| 2 | **读模块的容器日志** | `docker logs --tail N <container>`，给 `log` 与 `aggregate` 两个块供数 |
| 3 | **代理模块的 HTTP API** | 按 `api.upstream` + `api.writes` 白名单转发（Harness 这类用得上） |
| 4 | **渲染模块声明的页面** | §7 的六种块 |
| 5 | **iframe 嵌模块自己的 UI** | 同源路径（由 Caddy 路由），本体**不代理**、**不替它认证** |

**新增的受控动作**（写死 argv，模块无法影响）：

| 动作 | argv | danger |
|---|---|---|
| `module.install` | `docker compose -p n1bus-<id> -f <path>/compose.yml up -d` | ✔ |
| `module.start` | `docker compose -p n1bus-<id> -f <path>/compose.yml start` | ✖ |
| `module.stop` | `docker compose -p n1bus-<id> -f <path>/compose.yml stop` | ✔ |
| `module.restart` | `docker compose -p n1bus-<id> -f <path>/compose.yml restart` | ✔ |

`<id>` 只来自**通过校验的 manifest**；`<path>` 由本体算成 `/opt/n1bus/modules/<id>`
（外部装的）或**固定常量**（内置模块，见 §9），**不接受 manifest 里的路径**。

---

## 7. 页面块（六种）

| type | 数据从哪来 | 用途 |
|---|---|---|
| `status` | 本体自己（容器在不在、端口在不在听） | 一句话结论 |
| `kv` | `docker inspect` 的字段 | 键值对 |
| `table` | `api.upstream` 代理的 JSON | 结构化列表 |
| `log` | 模块容器的 stdout | 原始日志 tail |
| `actions` | — | 按钮（只能绑 `module.*` 或本体已有动作） |
| `aggregate` | 模块容器的 stdout + 正则 | **按正则提取字段并聚合** |

### `aggregate` 块（为 RSC4D 这类「没有 HTTP API」的模块准备）

```json
{
  "type": "aggregate",
  "label": "激活统计",
  "source": {"kind": "container-log", "container": "n1bus-kms-rsc4d", "tail": 5000},
  "pattern": "^\\[(?P<ip>[^\\]]+)\\] KMS (?P<ver>v\\d) KMSID=(?P<kmsid>.+?) CMID=.*? Count=(?P<count>\\d+) 产品=(?P<product>.+?) → 响应 (?P<bytes>\\d+) 字节",
  "rows": [
    {"label": "激活请求", "agg": "count"},
    {"label": "来源 IP", "agg": "distinct", "field": "ip"},
    {"label": "命中产品", "agg": "group", "field": "product", "top": 5}
  ]
}
```

- 正则必须是**命名组**形式；本体只读它声明的组。
- `agg`：`count`（匹配行数）/ `distinct`（去重计数）/ `group`（分组计数，可带 `top`）。
- **安全**：正则来自外部内容 —— 本体必须给它加长度上限并限制回溯（拒绝嵌套量词、
  限制输入行长度与 `tail` 上限），否则一个恶意正则可以拖死面板。

---

## 8. 安全模型（与 ADR 0002 共存）

主人选了「**模块的逻辑全在自己的进程里**」—— 这一点让 ADR 0002 **不需要修改**：

1. **模块不提供命令。** manifest 里没有任何「要执行的字符串」；本体执行的永远是写死的
   `module.*` 动作。模块的代码跑在**它自己的容器**里，本体只是编排它。
2. **一切都过哈希。** 只认 `index.json` 里的 sha256；解包时拒绝绝对路径、`..`、符号链接
   （与文件管理器同一套 `realpath` + 根判定）。
3. **受信 registry 是唯一来源。** 本体**不提供**「填任意仓库 URL」的入口。想加模块 →
   往 `N1bus-modules` 提 PR。这条路窄，但它是这个功能能存在的前提。
4. **正则也当不可信输入处理**（见 §7 的安全条款）。

---

## 9. 安装与接入流程

```
安装：预检 → 下载 tar.gz → size gate → sha256 gate → 防穿越解包
      → manifest 校验 → 落到 /opt/n1bus/modules/<id>/
      → module.install → 端点检查 → 失败回滚（停容器 + 移走目录 + 写审计）

接入：装完是「未接入」状态 → 主人打开开关 → 页面出现在「模块」页
      → 关掉开关就只是不显示，**容器照跑**（接入是界面层面的事，不是生命周期）
```

每一步都写审计。

### 内置模块（`kms` / `harness`）

- manifest 内置在本体代码里，**不下载**；商店里标 `builtin: true`。
- **不搬家**：服务器上这两套栈各有自己的目录（`kms` 在 `/opt/nimbus/docker/`、
  `harness` 在 `/opt/dsh/`）。为了「统一」搬目录会**重建容器**，KMS 客户端要断约 10 秒。
  所以内置模块的 compose 路径来自本体里的**固定常量**，`module.install` 退化成 `up -d`。
- registry 里若有更高版本，商店提示「可升级」（升级本身下一轮做）。

---

## 10. 第一个模块：kms

| 项 | 值 |
|---|---|
| 服务端 | **RSC4D**（`rs-c4d`，Rust，KMS wire 协议 v4/v5/v6） |
| 镜像 | `ghcr.io/yumina0789/rsc4d`（公开），由私有仓库 `Yumina0789/RSC4D-docker` 构建 |
| 端口 | `1688/tcp`（KMS 标准端口） |
| API | **没有** —— 页面数据全靠 `log` 与 `aggregate` 两个块 |
| 页面 | 状态 + 激活统计（aggregate）+ 最近请求（log）+ 启停按钮 |

---

## 11. 未决

| # | 问题 | 倾向 |
|---|---|---|
| 1 | 模块的升级/卸载 | 下一轮 |
| 2 | 模块之间的依赖 | 不做 |
| 3 | 接入开关的粒度（整个模块一个开关，还是每个页面块一个） | 整个模块一个 |
| 4 | registry 是否要求签名（minisign/GPG） | 先不做；哈希 + 单源已在可接受范围 |
