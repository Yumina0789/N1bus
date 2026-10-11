"""n1bus_actions —— 白名单动作表与参数校验器：面板能改变系统状态的全部手段都在这张表里。"""

import re
import time

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


PROTECTED_UNITS = {
    "ssh.service", "sshd.service", "systemd-networkd.service", "systemd-resolved.service",
    "systemd-timesyncd.service", "systemd-journald.service", "cron.service", "docker.service",
    "containerd.service", "n1bus.service", "ufw.service", "networking.service",
    # fail2ban 的单元是 dpkg 装到 /lib/systemd/system 的，会被"发行版自带"那套判据滤掉，
    # 但它是这台机器的安全防线（面板还有 fail2ban.unban 动作），必须留在界面上。
    "fail2ban.service",
}

TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){1,2}$")


def _val_tz(v):
    v = (v or "").strip()
    if not TZ_RE.match(v) or len(v) > 64:
        raise ValueError("时区格式不对，应该像 Asia/Shanghai")
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
        raise ValueError("这个单元受保护（关掉自启可能失联），面板不允许改")
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
    目标机器上那份文件，面板上显示的名字和实际生效的端口号就可能对不上。数字最明确。
    """
    s = str(v if v is not None else "").strip()
    if not s.isdigit():
        raise ValueError("端口要是 1-65535 的整数")
    n = int(s)
    if not (1 <= n <= 65535):
        raise ValueError("端口要在 1-65535 之间")
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
# danger 的判据 —— 它现在只回答一个问题：**点下去要不要先弹二次确认**。
#   danger=True  会改变系统运行状态，或结果不可逆且影响面大：
#                停服务/容器、删容器、杀进程、封 IP（可能把自己关在门外）、
#                重启面板、重启整机、取消开机自启。
#   danger=False 只是读取、幂等重载、或**回收磁盘空间**（日志/缓存/无标签镜像）：
#                清理类不碰任何在跑的东西，删掉的都是已经没用的字节，
#                所以不给二次确认添堵 —— 但无论 danger 与否，动作**一律写审计**。
# 新增动作时按上面的判据标，别随手抄 True。
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
    "docker.rm": {"label": "删除容器", "desc": "docker rm -f <名称>（强制停止并移除，数据卷不动）",
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
    "process.kill": {"label": "强杀进程（KILL）", "desc": "kill -KILL <pid>",
                     "params": {"pid": _val_pid}, "danger": True,
                     "argv": lambda p: ["kill", "-KILL", p["pid"]]},
    "ufw.deny": {"label": "防火墙封禁来源 IP", "desc": "ufw deny from <ip>",
                 "params": {"ip": _val_ip}, "danger": True,
                 "argv": lambda p: ["ufw", "deny", "from", p["ip"]], "timeout": 30},
    "ufw.undeny": {"label": "解封来源 IP", "desc": "ufw delete deny from <ip>",
                   "params": {"ip": _val_ip}, "danger": False,
                   "argv": lambda p: ["ufw", "--force", "delete", "deny", "from", p["ip"]],
                   "timeout": 30},
    # 开放 / 关闭端口本身不是命令执行，但它改变这台机器的对外暴露面：把一条
    # ufw allow 写下去，一个原本只监听内网的服务就上了公网。所以两个都算危险动作。
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
                      # 延迟 2 秒：先让「已排程」的响应与审计回到浏览器，再动手重启自己
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
    "docker.autostart": {"label": "设置容器自启", "desc": "docker update --restart=always|no <容器>",
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
    "backup.create": {"label": "打包备份", "desc": "tar 打包面板数据 + KMS 数据卷 + Caddy 配置",
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
    "clean.old_gz_logs": {"label": "删除 30 天前的轮转日志", "desc": "只删 /var/log 下 *.gz 且 mtime > 30 天",
                          "params": {}, "danger": False,
                          "argv": lambda p: ["find", "/var/log", "-type", "f", "-name", "*.gz",
                                             "-mtime", "+30", "-delete"], "timeout": 60},
    "journal.vacuum": {"label": "清理 journal 到最近 7 天", "desc": "journalctl --vacuum-time=7d",
                       "params": {}, "danger": False,
                       "argv": lambda p: ["journalctl", "--vacuum-time=7d"], "timeout": 120},
    "apt.update": {"label": "刷新软件包索引", "desc": "apt-get update",
                   "params": {}, "danger": False,
                   "argv": lambda p: ["apt-get", "update"], "timeout": 300},
}


def list_actions():    return [{"name": k, "label": v["label"], "desc": v["desc"], "danger": v["danger"],
             "params": sorted((v.get("params") or {}).keys())}
            for k, v in sorted(ACTIONS.items())]


def run_action(name, params, ip, audit, dry=False):
    """执行一个白名单动作。参数逐个过校验器，argv 直接交给 subprocess（不经 shell）。

    dry=True 时只走到"命令已拼好"这一步就返回（演示模式）：这样校验、审计、返回
    结构都和真机一致，只有最后一步不真的执行 —— 两条路的代码不分叉。
    """
    spec = ACTIONS.get(name)
    if not spec:
        return {"ok": False, "error": "没有这个动作：%s" % (name or "(空)")}
    clean = {}
    for key, validator in (spec.get("params") or {}).items():
        try:
            clean[key] = validator((params or {}).get(key))
        except (ValueError, TypeError, AttributeError) as e:
            return {"ok": False, "error": "参数 %s 不合法：%s" % (key, e)}
    argv = spec["argv"](clean)
    if not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
        return {"ok": False, "error": "拼出来的命令不合法，已拦下"}
    if dry:
        entry = audit.write(name, " ".join(argv[1:]) or "-", True, "演示模式：未真的执行", ip)
        return {"ok": True, "demo": True, "argv": argv, "code": 0,
                "out": "演示模式：这条命令没有真的执行。", "err": "", "detail": "demo",
                "audit": entry, "danger": spec.get("danger", False)}
    code, out, err = run(argv, timeout=spec.get("timeout", 30))
    detail = (out.strip() or err.strip())[:600]
    ok = code == 0
    entry = audit.write(name, " ".join(argv[1:]) or "-", ok, detail, ip)
    return {"ok": ok, "code": code, "argv": argv, "out": out.strip()[:5000],
            "err": err.strip()[:2500], "detail": detail, "audit": entry,
            "danger": spec.get("danger", False)}
