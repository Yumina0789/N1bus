"""n1bus 通用工具层：版本号、白名单正则、子进程/文本/密钥/网段辅助函数。"""

import os
import re
import secrets
import socket
import subprocess
import sys
from datetime import datetime

VERSION = "1.0"


# 真实文件系统才列进磁盘面板；overlay/tmpfs 那些是容器与内存的，列出来只会干扰
REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "vfat"}
# 单元名与容器名的白名单形状：systemd 与 docker 都只认这套字符，先挡住注入
NAME_RE = re.compile(r"^[A-Za-z0-9_.@:\\-]{1,128}$")
# 允许 tail 的日志文件：必须落在这些目录下，且不含 ..（真实路径还要再校验一次）
LOG_DIRS = ("/var/log/", "/var/lib/n1bus/")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:\\-]{1,128}$")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def dur_text(sec):
    sec = int(sec or 0)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return "%d 天 %d 小时" % (d, h)
    if h:
        return "%d 小时 %d 分" % (h, m)
    return "%d 分" % m


def run(argv, timeout=15, cwd=None):
    """执行一条命令。argv 为列表，不经过 shell。

    返回 (返回码, stdout, stderr)。命令不存在/超时都不抛异常，返回码用负数表达：
    -1 = 无法启动（命令缺失），-2 = 超时。
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return -1, "", "未找到命令：%s" % argv[0]
    except subprocess.TimeoutExpired:
        return -2, "", "命令超时（%ss）：%s" % (timeout, " ".join(argv))
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        return -3, "", "%s: %s" % (type(e).__name__, e)


def read_text(path, limit=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


def same_secret(a, b):
    """定时安全比较。

    compare_digest 只吃 ASCII 的 str，碰上非 ASCII 会抛 TypeError —— 而 token 是
    用户输入，一个中文字符即可让请求处理线程异常中断（连接直接断开，连 403 都返回不了）。
    统一编码成 bytes 再比。
    """
    return secrets.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def docker_bridge_addresses():
    """宿主在 docker 网桥上的地址（docker0 与 br-*）。

    反向代理通常跑在容器里，只能通过网桥网关找宿主 —— 所以面板除了回环，还得在这
    个地址上监听一份。用 ip 命令实时查询，网段变化时仍能正确获取。
    """
    code, out, _ = run(["ip", "-4", "-o", "addr", "show"], timeout=10)
    addrs = []
    for line in out.splitlines():
        f = line.split()
        if len(f) > 3 and f[2] == "inet" and (f[1].startswith("br-") or f[1] == "docker0"):
            addrs.append(f[3].split("/")[0])
    return addrs


def expand_binds(spec):
    """--bind 支持逗号分隔的多个地址；其中 "docker" 展开成上面的网桥地址。"""
    out = []
    for part in str(spec or "127.0.0.1").split(","):
        part = part.strip()
        if not part:
            continue
        if part == "docker":
            out += docker_bridge_addresses()
        else:
            out.append(part)
    uniq = []
    for a in out:
        if a not in uniq:
            uniq.append(a)
    return uniq or ["127.0.0.1"]


def parse_networks(text):
    import ipaddress
    nets = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return nets


def parse_skip(text):
    """限流永不封禁的网段（默认回环 + 内网）。"""
    return parse_networks(text)


def dedup_nets(nets):
    """按字符串去重，保持原有顺序。

    --allow-ip 通常已包含 127.0.0.0/8、10.0.0.0/8 等私有网段，与内置的 PRIVATE_NETS
    直接拼接会使同一网段在界面上重复出现。
    """
    seen, out = set(), []
    for n in nets:
        s = str(n)
        if s not in seen:
            seen.add(s)
            out.append(n)
    return out


def host_info():
    rel = {}
    for line in read_text("/etc/os-release").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            rel[k] = v.strip().strip('"')
    uname = os.uname() if hasattr(os, "uname") else None
    return {"hostname": socket.gethostname(), "os": rel.get("PRETTY_NAME", ""),
            "kernel": ("%s %s" % (uname.sysname, uname.release)) if uname else "",
            "arch": uname.machine if uname else "", "python": sys.version.split()[0]}
