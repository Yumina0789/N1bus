# 模块规范（Modules）

> **状态：已与用户逐项确认的目标形态，代码尚未实现**（2026-10-11）
>
> 本文件描述的是**要做成什么样**。以下东西在当前代码里**一处都没有**：
> 模块系统本身、registry 与商店的任何界面或路由、`module.*` 动作。
> KMS 与 Harness 当前仍是本体里两个硬编码的子视图（`n1bus_kms.py` + `ui.html` 的 `tab-ext`）。
>
> **2026-10-11 用户确认的五条形态**（覆盖本文件早先的写法）：
>
> 1. **没有「接入」这一步** —— 模块**装上即出现在扩展功能页**，只有「装 / 卸」两态。
>    早先「装与接分离、由一个开关控制显不显示」的设计作废。
> 2. **商店是 `Yumina0789/N1bus-modules` 仓库的 GitHub Pages 静态站** —— 不在本体里；
>    本体只负责从它拉取与安装，只有仓库所有者能上传功能。
> 3. **只留 iframe 页面** —— 模块自带页面，本体只负责嵌。早先那套「声明式页面块」
>    （`status` / `kv` / `table` / `log` / `actions` / `aggregate`）**整体作废**。
> 4. 扩展功能页**初始为空列表**，点**加号**进商店，逐个安装 / 卸载，各自独立。
> 5. 规划的模块是 **RSC4D** 与 **harness**。
>
> 取代早先的 `docs/extensions.md`（该文件使用「扩展」一词，术语已废弃）。
> 相关决策：ADR 0002（不做任意命令执行）、ADR 0009（写权限收敛到白名单根）、
> ADR 0012（本体模块化）。

---

## 1. 它解决什么

面板现在把 KMS 与 Harness **硬编码**在本体里（`tab-ext` 下就这两个子视图）。每加一个功能
都要改本体源码（写这份规格时是 `n1bus.py`；模块化重构后是 `n1bus_*.py` 里对应的域文件）
与 `ui.html` 两处、重新部署一遍。

**模块**把这层变成数据：功能以一份清单描述，从受信任的商店拉取、校验、安装，
**装上即出现在扩展功能页** —— 加功能不再需要动本体源码。

---

## 2. 词汇（唯一事实源在 `CONTEXT.md`，这里是速查）

| 词 | 指什么 | **不指**什么 |
|---|---|---|
| **本体（core）** | N1bus 自己：认证与安全底线、观测、白名单动作、文件管理、任务、告警、审计，**加上模块框架** | 不是模块；目标形态下**不**包含 KMS/Harness（它们要搬出去） |
| **模块（module）** | 一个可安装的功能单元：manifest + 可选 compose + **一个自己的页面**。**它的逻辑全在自己进程里** | 不是 DSH 插件（那是 `@deepseek-ai/dsh` 的插件，另一层） |
| **模块包（package）** | 模块打成的单个 `.tar.gz`，整体 sha256 即身份 | 不是 git 快照 —— 只认归档，不认「拉某个分支 HEAD」 |
| **registry** | 模块清单与包的发布仓库：`Yumina0789/N1bus-modules` | 不是 PyPI/npm，不做依赖解析 |
| **商店（store）** | 上面那个仓库的 **GitHub Pages 静态站**；本体从它取清单与包 | 不在本体里；也不是应用市场 —— 只有受信任的那一个 registry |
| **模块动作（module.\*）** | 本体为模块新增的受控白名单动作 | 不是模块自带的命令 |

> 已删除的两个词：**接入（attach）**、**页面块（block）** —— 见文件顶部第 1、3 条。

---

## 3. 本体与模块的边界

**留本体**（用户明确要求：除 kms 与 harness 之外，面板应有的功能全部保留）：

- 认证与会话、服务 token、来源限制 —— 模块碰不到
- systemd / docker / ufw 的观测与 `ACTIONS` 白名单动作
- 文件管理器、任务、告警、审计
- **模块框架**（见 §6）

**搬出去**（变成模块）：

- KMS 页（`sub-kms` + `loadKms()` + `KMSService` + `KMS_WRITES` + `/api/kms*`）
- Harness 页（`sub-harness` + `#dshFrame` + `/api/dsh`）

> 搬运量实测：`n1bus.py` 里 6 处分散点、`ui.html` 里约 80 行 JS + 一个子视图。

---

## 4. registry 与商店

```
N1bus-modules/                    # 仓库 = registry
├── index.json                    # 商店页与本体的清单来源
├── packages/
│   ├── rsc4d-1.0.0.tar.gz        # 模块包：整体 sha256 即身份
│   └── harness-1.0.0.tar.gz
└── templates/                    # 编写新模块时参考此处
```

它的 **GitHub Pages 就是商店**：一个静态站，展示可用模块并给出包地址。
**安装动作由本体执行**，商店页本身不碰服务器。

- 本体**只认这一个仓库**，**不提供**「填任意仓库 URL」的入口。想加模块 → 往
  `N1bus-modules` 提 PR。这条路窄，但它是这个功能能存在的前提。
- **为什么采用归档而非散文件**：sha256 直接对*一个字节序列*计算，无需「目录哈希规范化」
  这类易错机制；下载 → 校验 → 解包是一条线性流程。

### index.json

```json
{
  "schema": 1,
  "registry": "Yumina0789/N1bus-modules",
  "modules": [
    {
      "id": "rsc4d",
      "name": "KMS 激活服务",
      "summary": "rs-c4d：KMS wire 协议服务端（v4/v5/v6）",
      "icon": "key",
      "homepage": "https://github.com/Yumina0789/RSC4D",
      "latest": "1.0.0",
      "versions": {
        "1.0.0": {
          "url": "packages/rsc4d-1.0.0.tar.gz",
          "sha256": "<64 位十六进制>",
          "size": 4096,
          "min_core": "1.0"
        }
      }
    }
  ]
}
```

- `sha256` 与 `size` **均为必填**：先比较 size（低成本粗筛），再比较 sha256（权威校验）。
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
  "id": "rsc4d",
  "name": "KMS 激活服务",
  "version": "1.0.0",
  "summary": "rs-c4d：KMS wire 协议服务端（v4/v5/v6）",
  "icon": "key",
  "homepage": "https://github.com/Yumina0789/RSC4D",

  "compose": {
    "file": "compose.yml",
    "project": "n1bus-rsc4d",
    "services": ["rsc4d"]
  },

  "ports": [
    {"port": 1688, "proto": "tcp", "expose": true, "note": "KMS 客户端（裸 TCP，不经反代）"}
  ],

  "page": {
    "kind": "iframe",
    "title": "KMS 激活服务",
    "url": "/m/rsc4d/"
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
| `page.kind` | ✔ | **只有 `iframe`** |
| `page.url` | ✔ | 模块页面的**同源路径**，由 Caddy 路由到模块自己的容器（与现在 `/dsh/` 同一套做法）；本体不代理、不替它认证 |

---

## 6. 本体的模块框架

**核心能力（两项）**：

| # | 能力 | 说明 |
|---|---|---|
| 1 | **编排模块的容器** | `module.install` / `.start` / `.stop` / `.restart` —— argv 全由本体拼，路径来自通过校验的 manifest |
| 2 | **iframe 嵌模块自己的页面** | 同源路径（由 Caddy 路由），本体**不代理**、**不替它认证** |

**新增的受控动作**（写死 argv，模块无法影响）：

| 动作 | argv | danger |
|---|---|---|
| `module.install` | `docker compose -p n1bus-<id> -f <path>/compose.yml up -d` | ✔ |
| `module.start` | `docker compose -p n1bus-<id> -f <path>/compose.yml start` | ✖ |
| `module.stop` | `docker compose -p n1bus-<id> -f <path>/compose.yml stop` | ✔ |
| `module.restart` | `docker compose -p n1bus-<id> -f <path>/compose.yml restart` | ✔ |

`<id>` 只来自**通过校验的 manifest**；`<path>` 由本体算成 `/opt/n1bus/modules/<id>`，
**不接受 manifest 里的路径**。

**两项待确认的辅助能力**：早先的「读模块容器日志」与「代理模块的 HTTP API」原本是给
**声明式页面块**供数的。既然只留 iframe（第 3 条），这两项是否还需要：

- **读容器日志**：仍有用 —— 模块出问题时面板上能直接看它的日志排障；
- **代理 HTTP API**：多半不再需要，模块自己的页面可以直接调自己的后端。

倾向是**保留读日志、去掉 API 代理**，待实现时确认。

---

## 7. 安装与卸载流程

```
安装：预检 → 从商店取 index.json → 下载 tar.gz → size gate → sha256 gate
      → 防穿越解包 → manifest 校验 → 落到 /opt/n1bus/modules/<id>/
      → module.install → 端点检查 → 失败回滚（停容器 + 移走目录 + 写审计）
      → 页面出现在扩展功能页（没有「接入」这一步）

卸载：module.stop → 移走目录 → 从列表移除（数据卷是否保留由用户在界面上选）
```

每一步都写审计。

### 既有部署不搬家

服务器上这两套栈各有自己的目录（KMS 在 `/opt/nimbus/docker/`、harness 在 `/opt/dsh/`）。
为了「统一」搬目录会**重建容器**，KMS 客户端要断约 10 秒。所以这两个模块的 compose 路径
来自本体里的**固定常量**，`module.install` 退化成 `up -d`。

registry 里若有更高版本，商店提示「可升级」（升级本身下一轮做）。

---

## 8. 安全模型（与 ADR 0002 共存）

用户选定「**模块的逻辑全在自己的进程里**」—— 这一点使 ADR 0002 **无需修改**：

1. **模块不提供命令。** manifest 里没有任何「要执行的字符串」；本体执行的永远是写死的
   `module.*` 动作。模块的代码跑在**它自己的容器**里，本体只是编排它。
2. **一切都过哈希。** 只认 `index.json` 里的 sha256；解包时拒绝绝对路径、`..`、符号链接
   （与文件管理器同一套 `realpath` + 根判定）。
3. **受信 registry 是唯一来源**（见 §4）。

> 附带的一处简化：早先的 `aggregate` 块要拿**外部提供的正则**去解析容器日志，
> 必须按不可信输入处理（长度上限、限制回溯、限制输入行数）。声明式块作废后，
> 这条攻击面**随之消失** —— 页面改由 iframe 承载，本体不再解析模块的任何内容。

---

## 9. 商店里规划的两个模块

| 模块 | 内容 |
|---|---|
| **rsc4d** | 服务端 **RSC4D**（`rs-c4d`，Rust，KMS wire 协议 v4/v5/v6）；镜像 `ghcr.io/yumina0789/rsc4d`（公开），由私有仓库 `Yumina0789/RSC4D-docker` 构建；端口 `1688/tcp`；页面自带（激活统计、最近请求、启停按钮） |
| **harness** | DeepSeek Harness —— 独立容器里的 agent 面板，页面以 iframe 嵌 `/dsh/`（与现在同一套做法，见 ADR 0008）；compose 仍由 `/opt/dsh/` 管理 |

RSC4D 上线后，**v1m-dash 与 vlmcsd 退役**，激活记录不迁移（见 ADR 0012）。

---

## 10. 未决

| # | 问题 | 倾向 |
|---|---|---|
| 1 | 模块的升级 / 卸载 | 下一轮 |
| 2 | 模块之间的依赖 | 不做 |
| 3 | registry 是否要求签名（minisign/GPG） | 先不做；哈希 + 单源已在可接受范围 |
| 4 | 除读日志外，是否还要「代模块调本机 HTTP API」 | 倾向不要（见 §6） |
| 5 | 模块页面的同源路径规则（`/m/<id>/` 还是别的） | 实现时定 |
