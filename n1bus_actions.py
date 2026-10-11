"""n1bus_actions —— 白名单动作表与参数校验器：面板能改变系统状态的全部手段都在这张表里。"""

import json
import re
import time
from pathlib import Path

import n1bus_modules
from n1bus_modules import ID_RE as MODULE_ID_RE
from n1bus_util import NAME_RE, UNIT_RE, run


def _val_unit(v):
    v = (v or "").strip()
    if not UNIT_RE.match(v):
        raise ValueError("单元名不合法")
    return v


def _val_name(v):
    v = (v or "").strip()
    if not NAME_RE.match(v):
        raise ValueError("名字不合法")
    return v


def _val_module_id(v):
    """模块 id：格式合法，且该模块确实已安装。

    「已安装」这一条也放在这里，是为了让四个 `module.*` 动作共用同一道闸门。
    校验器抛 ValueError 会被 run_action 捕获成一句明确的错误，而不是让 docker
    拿着一个不存在的路径去报一句难懂的话。
    """
    mid = (v or "").strip()
    if not MODULE_ID_RE.match(mid):
        raise ValueError("模块 id 不合法（应为 ^[a-z][a-z0-9-]{1,31}$）")
    if not (Path(n1bus_modules.modules_dir()) / mid / "manifest.json").is_file():
        raise ValueError("该模块没有安装：%s" % mid)
    return mid


def _compose_argv(mid, *sub):
    """拼出 `docker compose -p n1bus-<id> -f <模块自己的 compose> <sub...>`。

    路径由**本体**算成 `/opt/n1bus/modules/<id>/`，compose 文件名取自该模块
    **通过校验的 manifest** —— 模块影响不了命令本身，只能影响自己目录里的一个文件名。
    没有 compose 的模块（纯 iframe）不能走这几个动作，这不是缺陷。
    """
    fname = "compose.yml"
    root = Path(n1bus_modules.modules_dir())
    mf = root / mid / "manifest.json"
    try:
        comp = (json.loads(mf.read_text(encoding="utf-8")) or {}).get("compose") or {}
        fname = str(comp.get("file") or fname)
    except (OSError, ValueError):
        pass
    if "/" in fname or "\\" in fname or fname.startswith("."):
        raise ValueError("manifest 里的 compose 文件名不合法")
    path = root / mid / fname
    if not path.is_file():
        raise ValueError("该模块没有 compose 文件（%s），不需要容器编排" % fname)
    return ["docker", "compose", "-p", "n1bus-" + mid, "-f", str(path)] + list(sub)


PROTECTED_UNITS = {
    "ssh.service", "sshd.service", "systemd-networkd.service", "systemd-resolved.service",
    "systemd-timesyncd.service", "systemd-journald.service", "cron.service", "docker.service",
    "containerd.service", "n1bus.service", "ufw.service", "networking.service",
    # fail2ban 的单元由 dpkg 安装到 /lib/systemd/system，会被"发行版自带"判据过滤掉，
    # 但它是本机的安全防护组件（面板另有 fail2ban.unban 动作），必须保留在界面上。
    "fail2ban.service",
}

TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){1,2}$")


def _val_tz(v):
    v = (v or "").strip()
    if not TZ_RE.match(v) or len(v) > 64:
        raise ValueError("时区格式无效，应形如 Asia/Shanghai")
    return v


def _val_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise ValueError("布尔参数不合法")


def _val_boot_unit(v):
    u = _val_unit(v)
    if not u.endswith(".service"):
        raise ValueError("只支持 .service 单元")
    if u in PROTECTED_UNITS:
        raise ValueError("该单元受保护（关闭开机自启可能导致失联），面板不允许修改")
    return u


def _val_ip(v):
    import ipaddress
    try:
        return str(ipaddress.ip_address((v or "").strip()))
    except ValueError:
        raise ValueError("IP 不合法")


def _val_pid(v):
    n = int(str(v or "").strip())
    if not (1 <= n <= 4194304):
        raise ValueError("PID 不合法")
    return str(n)


def _val_port(v):
    """端口号：1-65535 的整数。

    不接受服务名（`ssh`、`http` 这类）：服务名由 `/etc/services` 解析，解析结果依赖
    目标机器上该文件的内容，面板显示的服务名与实际生效的端口号可能不一致。使用数字可避免这一歧义。
    """
    s = str(v if v is not None else "").strip()
    if not s.isdigit():
        raise ValueError("端口必须为 1-65535 的整数")
    n = int(s)
    if not (1 <= n <= 65535):
        raise ValueError("端口必须在 1-65535 范围内")
    return str(n)


def _val_proto(v):
    p = str(v if v is not None else "").strip().lower() or "tcp"
    if p not in ("tcp", "udp"):
        raise ValueError("协议只能是 tcp 或 udp")
    return p


def _val_dir(v):
    """规则方向。ufw 的默认方向是 in（入站），所以缺省按 in 处理。"""
    d = str(v if v is not None else "in").strip().lower()
    if d in ("", "in", "inbound"):
        return "in"
    if d in ("out", "outbound"):
        return "out"
    raise ValueError("方向只能是 in（入站）或 out（出站）")


# 白名单动作表（ADR 0002：面板不提供任意命令执行，用户能触发的只有这里）。
#
# danger 的判据：仅用于回答一个问题 —— **执行该动作前是否需要二次确认**。
#   danger=True  会改变系统运行状态，或结果不可逆且影响面大：
#                停止服务/容器、删除容器、结束进程、封禁 IP（可能导致管理端自身被拒于管理通道之外）、
#                重启面板、重启整机、取消开机自启。
#   danger=False 只是读取、幂等重载、或**回收磁盘空间**（日志/缓存/无标签镜像）：
#                清理类动作不触碰任何运行中的资源，删除的都是已失效的数据，
#                因此不额外增加二次确认环节 —— 但无论 danger 取值如何，动作**一律写入审计**。
# 新增动作时按上述判据确定 danger 取值，不要直接复制为 True。
ACTIONS = {
    "service.start": {"label": "启动服务", "desc": "systemctl start <单元>",
                      "params": {"unit": _val_unit}, "danger": False,
                      "argv": lambda p: ["systemctl", "start", p["unit"]]},
    "service.restart": {"label": "重启服务", "desc": "systemctl restart <单元>",
                        "params": {"unit": _val_unit}, "danger": False,
                        "argv": lambda p: ["systemctl", "restart", p["unit"]]},
    "service.stop": {"label": "停止服务", "desc": "systemctl stop <单元>",
                     "params": {"unit": _val_unit}, "danger": True,
                     "argv": lambda p: ["systemctl", "stop", p["unit"]]},
    "systemd.reload": {"label": "重载 systemd 单元配置", "desc": "systemctl daemon-reload",
                       "params": {}, "danger": False,
                       "argv": lambda p: ["systemctl", "daemon-reload"]},
    "docker.start": {"label": "启动容器", "desc": "docker start <容器>",
                     "params": {"name": _val_name}, "danger": False,
                     "argv": lambda p: ["docker", "start", p["name"]]},
    "docker.rm": {"label": "删除容器", "desc": "docker rm -f <名称>（强制停止并移除，不影响数据卷）",
                  "params": {"name": _val_name}, "danger": True,
                  "argv": lambda p: ["docker", "rm", "-f", p["name"]], "timeout": 60},
    "docker.restart": {"label": "重启容器", "desc": "docker restart <容器>",
                       "params": {"name": _val_name}, "danger": False,
                       "argv": lambda p: ["docker", "restart", p["name"]], "timeout": 60},
    "docker.stop": {"label": "停止容器", "desc": "docker stop <容器>",
                    "params": {"name": _val_name}, "danger": True,
                    "argv": lambda p: ["docker", "stop", p["name"]], "timeout": 60},
    "docker.prune_images": {"label": "清理无标签镜像", "desc": "docker image prune -f",
                            "params": {}, "danger": False,
                            "argv": lambda p: ["docker", "image", "prune", "-f"], "timeout": 120},
    "process.term": {"label": "结束进程（TERM）", "desc": "kill -TERM <pid>",
                     "params": {"pid": _val_pid}, "danger": True,
                     "argv": lambda p: ["kill", "-TERM", p["pid"]]},
    "process.kill": {"label": "强制结束进程（KILL）", "desc": "kill -KILL <pid>",
                     "params": {"pid": _val_pid}, "danger": True,
                     "argv": lambda p: ["kill", "-KILL", p["pid"]]},
    "ufw.deny": {"label": "防火墙封禁来源 IP", "desc": "ufw deny from <ip>",
                 "params": {"ip": _val_ip}, "danger": True,
                 "argv": lambda p: ["ufw", "deny", "from", p["ip"]], "timeout": 30},
    "ufw.undeny": {"label": "解封来源 IP", "desc": "ufw delete deny from <ip>",
                   "params": {"ip": _val_ip}, "danger": False,
                   "argv": lambda p: ["ufw", "--force", "delete", "deny", "from", p["ip"]],
                   "timeout": 30},
    # 开放 / 关闭端口本身不是命令执行，但它改变这台机器的对外暴露面：写入一条
    # ufw allow 规则，原本仅监听内网的服务即可从公网访问。因此两个都判定为危险动作。
    # 方向必须显式传给 ufw：`allow` 与 `allow out` 是两条不同的规则。
    "ufw.allow": {"label": "防火墙开放端口",
                  "desc": "ufw allow <in|out> <port>/<proto>",
                  "params": {"port": _val_port, "proto": _val_proto, "dir": _val_dir},
                  "danger": True,
                  "argv": lambda p: ["ufw", "allow", p["dir"],
                                     "%s/%s" % (p["port"], p["proto"])],
                  "timeout": 30},
    "ufw.unallow": {"label": "防火墙关闭端口",
                    "desc": "ufw delete allow <in|out> <port>/<proto>",
                    "params": {"port": _val_port, "proto": _val_proto, "dir": _val_dir},
                    "danger": True,
                    "argv": lambda p: ["ufw", "--force", "delete", "allow", p["dir"],
                                       "%s/%s" % (p["port"], p["proto"])],
                    "timeout": 30},
    "fail2ban.unban": {"label": "fail2ban 解封 IP", "desc": "fail2ban-client set <jail> unbanip <ip>",
                       "params": {"jail": _val_name, "ip": _val_ip}, "danger": False,
                       "argv": lambda p: ["fail2ban-client", "set", p["jail"], "unbanip", p["ip"]]},
    "panel.restart": {"label": "重启面板服务", "desc": "systemctl restart n1bus.service（延迟 2 秒）",
                      "params": {}, "danger": True,
                      # 延迟 2 秒：先让「已排程」响应与审计记录返回浏览器，再执行面板自身的重启
                      "argv": lambda p: ["systemd-run", "--on-active=2", "--collect",
                                         "--unit=n1bus-self-restart-%d" % int(time.time()),
                                         "systemctl", "restart", "n1bus.service"],
                      "timeout": 30},
    "system.reboot": {"label": "重启托管机", "desc": "systemctl reboot（延迟 2 秒，会断开面板与 SSH）",
                      "params": {}, "danger": True,
                      "argv": lambda p: ["systemd-run", "--on-active=2", "--collect",
                                         "--unit=n1bus-reboot-%d" % int(time.time()),
                                         "systemctl", "reboot"],
                      "timeout": 30},
    "boot.enable": {"label": "设为开机自启", "desc": "systemctl enable <单元>",
                    "params": {"unit": _val_boot_unit}, "danger": False,
                    "argv": lambda p: ["systemctl", "enable", p["unit"]], "timeout": 60},
    "boot.disable": {"label": "取消开机自启", "desc": "systemctl disable <单元>",
                     "params": {"unit": _val_boot_unit}, "danger": True,
                     "argv": lambda p: ["systemctl", "disable", p["unit"]], "timeout": 60},
    "docker.autostart": {"label": "设置容器开机自启", "desc": "docker update --restart=always|no <容器>",
                         "params": {"name": _val_name, "on": _val_bool}, "danger": False,
                         "argv": lambda p: ["docker", "update",
                                            "--restart=" + ("always" if p["on"] else "no"),
                                            p["name"]], "timeout": 60},
    "time.set_timezone": {"label": "设置时区", "desc": "timedatectl set-timezone <时区>",
                          "params": {"tz": _val_tz}, "danger": False,
                          "argv": lambda p: ["timedatectl", "set-timezone", p["tz"]], "timeout": 30},
    "time.ntp": {"label": "开关 NTP 自动校时", "desc": "timedatectl set-ntp true|false",
                 "params": {"on": _val_bool}, "danger": False,
                 "argv": lambda p: ["timedatectl", "set-ntp", "true" if p["on"] else "false"],
                 "timeout": 30},
    "backup.create": {"label": "打包备份", "desc": "tar 打包面板数据 + Docker 数据卷 + Caddy 配置",
                      "params": {}, "danger": False,
                      "argv": lambda p: ["tar", "--ignore-failed-read",
                                         "--exclude=var/lib/n1bus/backups",
                                         "--exclude=var/lib/docker/volumes/backingFsBlockDev",
                                         "-czf", "/var/lib/n1bus/backups/latest.tar.gz", "-C", "/",
                                         "var/lib/n1bus", "var/lib/docker/volumes",
                                         "opt/nimbus/docker", "etc/vlmcsd"],
                      "timeout": 300},
    "clean.journal": {"label": "收缩 journald 到 200 MB", "desc": "journalctl --vacuum-size=200M",
                      "params": {}, "danger": False,
                      "argv": lambda p: ["journalctl", "--vacuum-size=200M"], "timeout": 120},
    "clean.docker_builder": {"label": "清理 Docker 构建缓存", "desc": "docker builder prune -f",
                             "params": {}, "danger": False,
                             "argv": lambda p: ["docker", "builder", "prune", "-f"], "timeout": 300},
    "clean.apt_cache": {"label": "清理 apt 下载缓存", "desc": "apt-get clean",
                        "params": {}, "danger": False,
                        "argv": lambda p: ["apt-get", "clean"], "timeout": 120},
    "clean.old_gz_logs": {"label": "删除 30 天前的轮转日志", "desc": "仅删除 /var/log 下 *.gz 且 mtime 超过 30 天的文件",
                          "params": {}, "danger": False,
                          "argv": lambda p: ["find", "/var/log", "-type", "f", "-name", "*.gz",
                                             "-mtime", "+30", "-delete"], "timeout": 60},
    "journal.vacuum": {"label": "清理 journal 到最近 7 天", "desc": "journalctl --vacuum-time=7d",
                       "params": {}, "danger": False,
                       "argv": lambda p: ["journalctl", "--vacuum-time=7d"], "timeout": 120},
    "apt.update": {"label": "刷新软件包索引", "desc": "apt-get update",
                   "params": {}, "danger": False,
                   "argv": lambda p: ["apt-get", "update"], "timeout": 300},
    # 模块动作（ADR 0012 / docs/modules.md §6）：argv 全由本体拼死，路径来自
    # /opt/n1bus/modules/<id>/，模块**不提供**任何要执行的字符串。
    # `up -d` **不带** `--force-recreate`：compose 会给每个服务算一份 config hash
    # （环境变量来自 .env，插值后的值参与计算），只有真的变了的那几个才重建 ——
    # 于是「改一个不进 compose 的展示项」一个容器都不动。无差别强制重建会让改个域名
    # 也把整套容器推倒、整个模块跟着中断几秒。真要强制重来：module.down 之后再
    # module.install（`module.restart` 只是重启进程，不会重读 compose 文件）。
    "module.install": {"label": "安装模块（起容器）",
                       "desc": "docker compose -p n1bus-<id> up -d",
                       "params": {"id": _val_module_id}, "danger": True,
                       "argv": lambda p: _compose_argv(p["id"], "up", "-d"),
                       "timeout": 300},
    "module.start": {"label": "启动模块容器",
                     "desc": "docker compose -p n1bus-<id> start",
                     "params": {"id": _val_module_id}, "danger": False,
                     "argv": lambda p: _compose_argv(p["id"], "start"),
                     "timeout": 120},
    "module.stop": {"label": "停止模块容器",
                    "desc": "docker compose -p n1bus-<id> stop",
                    "params": {"id": _val_module_id}, "danger": True,
                    "argv": lambda p: _compose_argv(p["id"], "stop"),
                    "timeout": 120},
    "module.restart": {"label": "重启模块容器",
                       "desc": "docker compose -p n1bus-<id> restart",
                       "params": {"id": _val_module_id}, "danger": True,
                       "argv": lambda p: _compose_argv(p["id"], "restart"),
                       "timeout": 120},
    # 卸载必须先摘容器再删目录：`-f <path>` 指向模块目录，目录一没就再也操作不了这套
    # 容器了。`down` 只移除容器与网络，**不动数据卷**。
    "module.down": {"label": "移除模块容器",
                    "desc": "docker compose -p n1bus-<id> down（保留数据卷）",
                    "params": {"id": _val_module_id}, "danger": True,
                    "argv": lambda p: _compose_argv(p["id"], "down"),
                    "timeout": 180},
}


def list_actions():    return [{"name": k, "label": v["label"], "desc": v["desc"], "danger": v["danger"],
             "params": sorted((v.get("params") or {}).keys())}
            for k, v in sorted(ACTIONS.items())]


def run_action(name, params, ip, audit, dry=False):
    """执行一个白名单动作。参数逐个经校验器校验，argv 直接传给 subprocess（不经过 shell）。

    dry=True 时（演示模式）在命令组装完成后即返回：这样校验、审计与返回结构都与真实
    执行路径一致，仅最后一步不实际执行 —— 两条路径共用同一段代码。
    """
    spec = ACTIONS.get(name)
    if not spec:
        return {"ok": False, "error": "该动作不存在：%s" % (name or "（为空）")}
    clean = {}
    for key, validator in (spec.get("params") or {}).items():
        try:
            clean[key] = validator((params or {}).get(key))
        except (ValueError, TypeError, AttributeError) as e:
            return {"ok": False, "error": "参数 %s 不合法：%s" % (key, e)}
    try:
        argv = spec["argv"](clean)
    except (ValueError, TypeError, KeyError, OSError) as e:
        # argv 构造函数也会失败（例如模块缺少 compose 文件）—— 让它变成一条
        # 可读的错误返回，而不是一个把响应体带走的异常
        return {"ok": False, "error": "无法构造命令：%s" % e}
    if not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
        return {"ok": False, "error": "构造出的命令不合法，已拦截"}
    if dry:
        entry = audit.write(name, " ".join(argv[1:]) or "-", True, "演示模式：未实际执行", ip)
        return {"ok": True, "demo": True, "argv": argv, "code": 0,
                "out": "演示模式：该命令未实际执行。", "err": "", "detail": "demo",
                "audit": entry, "danger": spec.get("danger", False)}
    code, out, err = run(argv, timeout=spec.get("timeout", 30))
    detail = (out.strip() or err.strip())[:600]
    ok = code == 0
    entry = audit.write(name, " ".join(argv[1:]) or "-", ok, detail, ip)
    return {"ok": ok, "code": code, "argv": argv, "out": out.strip()[:5000],
            "err": err.strip()[:2500], "detail": detail, "audit": entry,
            "danger": spec.get("danger", False)}
