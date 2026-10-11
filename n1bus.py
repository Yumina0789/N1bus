#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
n1bus —— 单文件、零依赖的 Linux 服务器管理面板（Python 3 标准库，不需要 pip）

管的是什么
----------
**托管机自己**：systemd 服务、Docker 容器、进程与监听端口、日志、安全面（登录失败、
防火墙、fail2ban）。所以它装成宿主上的 systemd 服务而不是容器（见 docs/adr/0001）。

两条硬规矩
----------
* **不提供任意命令执行**。只有代码里写死的白名单动作，参数以列表形式交给
  subprocess，不经过 shell；危险动作界面二次确认 + 无论成败都写审计（见 docs/adr/0002）。
* 观测数据只活在内存里（环形缓冲够画曲线就行，不落库），落盘的只有审计日志。

用法
----
    python3 n1bus.py --mode demo --port 8402
    python3 n1bus.py --mode real --bind 127.0.0.1 --port 8402 \\
                       --data-dir /var/lib/n1bus --token-file /var/lib/n1bus/n1bus.token

安全
----
默认只绑 127.0.0.1；所有 /api/* 都要 token；token 文件 600。面板进程是 root ——
它就是半个 root shell，别裸奔在公网。
"""

import argparse
import base64
import collections
import copy
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

VERSION = "1.0"
HERE = Path(__file__).resolve().parent

# --- 页面拼装 ----------------------------------------------------------------
# ui.html 只是骨架，样式与脚本分别在 ui.css / ui.js 里。本体在**第一次要页面时**
# 拼一次，之后走缓存 —— 以前是每次请求都读一遍 ui.html，拆成三个文件后拼装更贵，
# 缓存算是顺带的改进。
PAGE_CSS_SLOT = "/*{{N1BUS_CSS}}*/"
PAGE_JS_SLOT = "/*{{N1BUS_JS}}*/"
_PAGE = {"text": None}


def build_page():
    """把骨架与 ui.css / ui.js 拼成一份完整页面（幂等，只拼一次）。

    某个分片文件缺失时就把对应槽位留空 —— 「只拆了一半」也能跑起来，
    而不是整个面板 500。
    """
    if _PAGE["text"] is None:
        html = (HERE / "ui.html").read_text(encoding="utf-8")
        for slot, name in ((PAGE_CSS_SLOT, "ui.css"), (PAGE_JS_SLOT, "ui.js")):
            part = HERE / name
            html = html.replace(slot, part.read_text(encoding="utf-8").strip("\n")
                                if part.is_file() else "")
        _PAGE["text"] = html
    return _PAGE["text"]

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
    """跑一条命令。argv 是列表，绝不经过 shell。

    返回 (返回码, stdout, stderr)。命令不存在/超时都不抛异常，返回码用负数表达：
    -1 = 起不来（缺命令），-2 = 超时。
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return -1, "", "找不到命令：%s" % argv[0]
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
    用户输入，随手发个中文就能把请求处理线程打崩（连接直接断，连 403 都回不去）。
    统一编码成 bytes 再比。
    """
    return secrets.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def docker_bridge_addresses():
    """宿主在 docker 网桥上的地址（docker0 与 br-*）。

    反向代理通常跑在容器里，只能通过网桥网关找宿主 —— 所以面板除了回环，还得在这
    个地址上听一份。用 ip 命令现场问，网段变了也不怕。
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


# --------------------------------------------------------------------------
# 账户：第一次进来先设一个，之后用户名 + 密码登录
# --------------------------------------------------------------------------
PASSWORD_MIN = 8
SESSION_TTL = 7 * 86400          # 登录态保持 7 天，过期要重新登录
LOGIN_WINDOW = 300               # 登录失败的统计窗口（秒）
LOGIN_MAX_FAIL = 8               # 窗口内失败超过这个数就封
# 递增封禁：第一次 5 分钟，再犯 30 分钟，第三次起 6 小时。
# 24 小时内没再犯就降回第一档；成功登录直接清零 —— 正常用户打错几次密码不会把自己
# 越封越久，而持续爆破的家伙会一路封到 6 小时。
LOGIN_BLOCK_TIERS = (300, 1800, 21600)
LOGIN_FORGET = 86400
USER_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,32}$")


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def hash_password(password, salt=None):
    """把密码存成可校验的字符串。

    优先 scrypt（标准库自带、内存硬，比单纯迭代 sha256 抗显卡爆破）；万一这个 Python
    没编进 scrypt，退回 pbkdf2。算法与参数都写在字符串里，所以将来调参也不会认不出老密码。
    """
    salt = salt or os.urandom(16)
    raw = password.encode("utf-8")
    if hasattr(hashlib, "scrypt"):
        n, r, p = 1 << 14, 8, 1
        dk = hashlib.scrypt(raw, salt=salt, n=n, r=r, p=p, dklen=32)
        return "scrypt$%d$%d$%d$%s$%s" % (n, r, p, _b64(salt), _b64(dk))
    iters = 200000
    dk = hashlib.pbkdf2_hmac("sha256", raw, salt, iters, dklen=32)
    return "pbkdf2$%d$%s$%s" % (iters, _b64(salt), _b64(dk))


def verify_password(password, stored):
    try:
        parts = (stored or "").split("$")
        raw = password.encode("utf-8")
        if parts[0] == "scrypt" and len(parts) == 6:
            _, n, r, p, salt_b64, want_b64 = parts
            salt, want = base64.b64decode(salt_b64), base64.b64decode(want_b64)
            got = hashlib.scrypt(raw, salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(want))
        elif parts[0] == "pbkdf2" and len(parts) == 4:
            _, iters, salt_b64, want_b64 = parts
            salt, want = base64.b64decode(salt_b64), base64.b64decode(want_b64)
            got = hashlib.pbkdf2_hmac("sha256", raw, salt, int(iters), dklen=len(want))
        else:
            return False
        return same_secret(got.hex(), want.hex())
    except (ValueError, TypeError, IndexError):
        return False


class LoginGuard:
    """登录失败的摩擦：同一来源在窗口内错太多次就封，而且**一次比一次久**。

    * 只按来源 IP 算，不按账户 —— 攻击者没法把你锁在门外，你打错密码也不连累自己。
    * 状态落盘到 loginguard.json（0600）：重启面板不该等于替攻击者清空记录。
    * 成功登录清零；24 小时没再犯也降回第一档。所以只有"持续爆破"才会被封到 6 小时。
    """

    def __init__(self, path=None, window=LOGIN_WINDOW, limit=LOGIN_MAX_FAIL,
                 tiers=LOGIN_BLOCK_TIERS, forget=LOGIN_FORGET):
        self.path = Path(path) if path else None
        self.window, self.limit, self.tiers, self.forget = window, limit, tiers, forget
        self.lock = threading.Lock()
        self.state = {"ips": {}}      # ip -> {fails: [ts], blocked_until, level, last}
        self._load()

    # --- 落盘 -------------------------------------------------------------
    def _load(self):
        if not self.path:
            return
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("ips"), dict):
                    self.state = d
        except (OSError, ValueError):
            pass

    def _save(self):
        if not self.path:
            return
        try:
            tmp = Path(str(self.path) + ".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(self.path))
        except OSError:
            pass

    # --- 判定 -------------------------------------------------------------
    def _entry(self, ip):
        e = self.state["ips"].get(ip)
        if not isinstance(e, dict):
            e = {"fails": [], "blocked_until": 0, "level": 0, "last": 0}
            self.state["ips"][ip] = e
        return e

    def wait_seconds(self, ip):
        """还要等多少秒才能再试；0 = 现在可以试。"""
        now = time.time()
        with self.lock:
            e = self._entry(ip)
            until = float(e.get("blocked_until") or 0)
            if until > now:
                return int(until - now) + 1
            e["blocked_until"] = 0
            e["fails"] = [t for t in (e.get("fails") or []) if now - t < self.window]
            if e.get("last") and now - e["last"] > self.forget:
                e["level"] = 0                                      # 很久没犯，档位降回去
            return 0

    def fail(self, ip):
        """记一次失败。返回 (是否被封, 封多久秒, 第几档)。"""
        now = time.time()
        with self.lock:
            e = self._entry(ip)
            hits = [t for t in (e.get("fails") or []) if now - t < self.window]
            hits.append(now)
            e["fails"] = hits
            e["last"] = now
            blocked, secs = False, 0
            if len(hits) >= self.limit:
                e["level"] = min(int(e.get("level") or 0) + 1, len(self.tiers))
                secs = self.tiers[e["level"] - 1]
                e["blocked_until"] = now + secs
                e["fails"] = []
                blocked = True
            self._prune(now)
            self._save()
            return blocked, secs, int(e.get("level") or 0)

    def ok(self, ip):
        """登录成功：这个来源的记录整个清掉。"""
        with self.lock:
            if self.state["ips"].pop(ip, None):
                self._save()

    def unblock(self, ip):
        """人工解封：放行这个来源，但**保留档位**——解封不等于给攻击者重置计数。"""
        with self.lock:
            e = self.state["ips"].get(ip)
            if not isinstance(e, dict):
                return False
            was = float(e.get("blocked_until") or 0) > time.time()
            e["blocked_until"] = 0
            e["fails"] = []
            if was:
                self._save()
            return was

    def _prune(self, now):
        """别让文件无限长：很久没动静、又没在封禁中的来源直接忘掉。"""
        for k in [k for k, v in self.state["ips"].items()
                  if isinstance(v, dict) and now - float(v.get("last") or 0) > self.forget * 7
                  and float(v.get("blocked_until") or 0) < now]:
            self.state["ips"].pop(k, None)

    def status(self, limit=20):
        now = time.time()
        rows = []
        with self.lock:
            for ip, e in self.state["ips"].items():
                if not isinstance(e, dict):
                    continue
                until = float(e.get("blocked_until") or 0)
                if until > now:
                    rows.append({"ip": ip, "seconds": int(until - now),
                                 "level": int(e.get("level") or 0),
                                 "fails": len(e.get("fails") or [])})
        rows.sort(key=lambda r: -r["seconds"])
        return {"blocked": rows[:limit], "tracked": len(self.state["ips"]),
                "tiers": [int(t) for t in self.tiers], "limit": self.limit,
                "window": self.window}


class Accounts:
    """账户与会话，存一个 0600 的 JSON 文件。

    不用数据库：这台机器上就一两个人的账户，几十行 JSON 足够；而且万一忘了密码，
    运维直接看文件就能明白结构、删掉重建。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {"users": {}, "sessions": {}}
        self._load()

    # --- 读写 -------------------------------------------------------------
    def _load(self):
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("users", {})
                self.data.setdefault("sessions", {})
        except (OSError, ValueError):
            self.data = {"users": {}, "sessions": {}}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(self.path) + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(self.path))
        except OSError:
            pass

    # --- 账户 -------------------------------------------------------------
    def need_setup(self):
        with self.lock:
            return not self.data["users"]

    def names(self):
        with self.lock:
            return sorted(self.data["users"])

    def create(self, name, password):
        name = (name or "").strip()
        if not USER_RE.match(name):
            return False, "用户名只能 2~32 位字母、数字、下划线、点或横线"
        if len(password or "") < PASSWORD_MIN:
            return False, "密码至少 %d 位" % PASSWORD_MIN
        with self.lock:
            if name in self.data["users"]:
                return False, "这个用户名已经有了"
            self.data["users"][name] = {"hash": hash_password(password),
                                        "created": int(time.time()), "last_login": 0}
            self._save()
        return True, ""

    def set_password(self, name, password):
        if len(password or "") < PASSWORD_MIN:
            return False, "密码至少 %d 位" % PASSWORD_MIN
        with self.lock:
            u = self.data["users"].get(name)
            if not u:
                return False, "没有这个账户"
            u["hash"] = hash_password(password)
            self._save()
        return True, ""

    def delete(self, name):
        with self.lock:
            if name not in self.data["users"]:
                return False, "没有这个账户"
            if len(self.data["users"]) <= 1:
                return False, "这是最后一个账户，删了就没人能登录了"
            self.data["users"].pop(name)
            for tok in [t for t, s in self.data["sessions"].items() if s.get("user") == name]:
                self.data["sessions"].pop(tok, None)
            self._save()
        return True, ""

    def check(self, name, password):
        with self.lock:
            u = self.data["users"].get((name or "").strip())
        if not u:
            # 不存在也照样算一次哈希，别让响应时间暴露"这个用户名存在"
            verify_password(password or "", hash_password("dummy-password"))
            return False
        return verify_password(password or "", u["hash"])

    def touch_login(self, name):
        with self.lock:
            if name in self.data["users"]:
                self.data["users"][name]["last_login"] = int(time.time())
                self._save()

    def listing(self):
        with self.lock:
            sessions = collections.Counter(s.get("user") for s in self.data["sessions"].values())
            return [{"name": n, "created": u.get("created", 0), "last_login": u.get("last_login", 0),
                     "sessions": sessions.get(n, 0)}
                    for n, u in sorted(self.data["users"].items())]

    # --- 会话 -------------------------------------------------------------
    def new_session(self, name, ttl=SESSION_TTL):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.prune_locked()
            self.data["sessions"][tok] = {"user": name, "created": int(time.time()),
                                          "expires": int(time.time()) + ttl}
            self._save()
        return tok

    def session_user(self, token):
        if not token:
            return ""
        with self.lock:
            s = self.data["sessions"].get(token)
            if not s:
                return ""
            if s.get("expires", 0) < time.time():
                self.data["sessions"].pop(token, None)
                self._save()
                return ""
            return s.get("user", "")

    def drop_session(self, token):
        with self.lock:
            if self.data["sessions"].pop(token, None):
                self._save()

    def drop_user_sessions(self, name):
        with self.lock:
            victims = [t for t, s in self.data["sessions"].items() if s.get("user") == name]
            for t in victims:
                self.data["sessions"].pop(t, None)
            if victims:
                self._save()
        return len(victims)

    def prune_locked(self):
        now = time.time()
        for t in [t for t, s in self.data["sessions"].items() if s.get("expires", 0) < now]:
            self.data["sessions"].pop(t, None)


# --------------------------------------------------------------------------
# 采样：CPU / 内存 / 网络 / 磁盘 / 负载。全部来自 /proc 与 statvfs，不落库
# --------------------------------------------------------------------------
class Sampler:
    """每隔 interval 秒读一次 /proc，算出 CPU 使用率与网络速率存进环形缓冲。"""

    def __init__(self, interval=2.0, keep=90, fake=False):
        self.interval = interval
        self.fake = fake          # 演示模式：即使有 /proc 也用仿真数据，保证演示可复现
        self.samples = collections.deque(maxlen=keep)
        self.lock = threading.Lock()
        self._prev_cpu = None
        self._prev_net = None
        self._stopped = False

    # --- /proc 解析 -------------------------------------------------------
    @staticmethod
    def _read_cpu():
        """返回 (总 jiffies, 空闲 jiffies, [每核 (总, 空闲)])。"""
        text = read_text("/proc/stat")
        total = idle = 0
        cores = []
        for line in text.splitlines():
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            if parts[0] == "cpu":
                nums = [int(x) for x in parts[1:]]
                # idle + iowait 算空闲；guest 时间已经包含在 user/nice 里，不再重复计
                total = sum(nums[:8])
                idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            elif parts[0][3:].isdigit():
                nums = [int(x) for x in parts[1:]]
                cores.append((sum(nums[:8]), nums[3] + (nums[4] if len(nums) > 4 else 0)))
        return total, idle, cores

    @staticmethod
    def _read_net():
        """返回 (rx_bytes, tx_bytes)，lo 不算。"""
        rx = tx = 0
        for line in read_text("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            if iface.strip() == "lo":
                continue
            f = rest.split()
            if len(f) >= 9:
                rx += int(f[0])
                tx += int(f[8])
        return rx, tx

    @staticmethod
    def _read_mem():
        info = {}
        for line in read_text("/proc/meminfo").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            num = v.strip().split()[0] if v.strip() else "0"
            info[k] = int(num) * 1024 if num.isdigit() else 0
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(0, total - avail)
        st, sf = info.get("SwapTotal", 0), info.get("SwapFree", 0)
        return {"total": total, "used": used, "available": avail,
                "free": info.get("MemFree", 0), "buffers": info.get("Buffers", 0),
                "cached": info.get("Cached", 0), "percent": (used / total * 100) if total else 0,
                "swap_total": st, "swap_used": max(0, st - sf),
                "swap_percent": ((st - sf) / st * 100) if st else 0}

    @staticmethod
    def _read_disk():
        """一个 (设备, 文件系统) 只留一条。

        真机上 /、/etc、/home、/root 常常是同一个分区的 bind mount，全列出来只是
        把同一个数字重复八遍。取挂载路径最短的那个（通常是 /）。
        """
        out = {}
        for line in read_text("/proc/mounts").splitlines():
            f = line.split()
            if len(f) < 3:
                continue
            dev, mount, fstype = f[0], f[1].replace("\\040", " "), f[2]
            if fstype not in REAL_FS or not dev.startswith("/dev/"):
                continue
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            if not total:
                continue
            free = st.f_bavail * st.f_frsize
            used = total - st.f_bfree * st.f_frsize
            inodes = st.f_files
            entry = {
                "mount": mount, "device": dev, "fstype": fstype,
                "total": total, "used": used, "free": free,
                "percent": (used / total * 100) if total else 0,
                "inodes_total": inodes,
                "inodes_percent": ((inodes - st.f_ffree) / inodes * 100) if inodes else 0,
            }
            key = (dev, fstype)
            old = out.get(key)
            if old is None or len(mount) < len(old["mount"]):
                out[key] = entry
        return sorted(out.values(), key=lambda d: -d["total"])

    @staticmethod
    def _read_proc_count():
        n = 0
        try:
            for name in os.listdir("/proc"):
                if name.isdigit():
                    n += 1
        except OSError:
            pass
        return n

    @staticmethod
    def _read_uptime():
        try:
            return float(read_text("/proc/uptime").split()[0])
        except (IndexError, ValueError):
            return 0.0

    # --- 采样循环 ---------------------------------------------------------
    def _fake_snapshot(self):
        """没有 /proc（比如在 Windows 上跑演示）时给一组仿真的数，别让界面全是 0。"""
        self._fake_n = getattr(self, "_fake_n", 0) + 1
        wave = (math.sin(self._fake_n / 5.0) + 1) / 2.0
        cpu = round(9 + wave * 52, 1)
        t = time.time()
        self._fake_rx = getattr(self, "_fake_rx", 1.2e9) + 40_000 + wave * 700_000
        self._fake_tx = getattr(self, "_fake_tx", 0.4e9) + 18_000 + wave * 240_000
        total = 2 << 30
        used = int(total * (0.44 + wave * 0.16))
        return {
            "t": t, "ts": now_iso(),
            "cpu": cpu, "cores": [round(max(1.0, cpu + ((i * 17) % 13) - 6), 1) for i in range(4)],
            "cpu_count": 4, "load": [round(0.3 + wave * 1.4, 2), round(0.2 + wave, 2), 0.15],
            "mem": {"total": total, "used": used, "available": total - used,
                    "free": total - used, "buffers": 60 << 20, "cached": 700 << 20,
                    "percent": used / total * 100, "swap_total": 0, "swap_used": 0,
                    "swap_percent": 0.0},
            "disk": [{"mount": "/", "device": "/dev/vda1", "fstype": "ext4",
                      "total": 30 << 30, "used": 8 << 30, "free": 22 << 30,
                      "percent": 27.0, "inodes_total": 2000000, "inodes_percent": 12.0},
                     {"mount": "/data", "device": "/dev/vdb1", "fstype": "ext4",
                      "total": 100 << 30, "used": 63 << 30, "free": 37 << 30,
                      "percent": 63.0, "inodes_total": 6000000, "inodes_percent": 41.0}],
            "procs": {"total": 138, "zombie": 1, "running": 3},
            "uptime": 2_600_000 + self._fake_n,
            "rx_total": int(self._fake_rx), "tx_total": int(self._fake_tx),
            "rx_bps": 40_000 + wave * 700_000, "tx_bps": 18_000 + wave * 240_000,
        }

    def _snapshot(self):
        if self.fake or not os.path.exists("/proc/stat"):
            snap = self._fake_snapshot()
            self._prev_cpu, self._prev_net = None, None
            return snap
        total, idle, cores = self._read_cpu()
        rx, tx = self._read_net()
        tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        now = time.time()
        snap = {"t": now, "ts": now_iso(), "mem": self._read_mem(), "disk": self._read_disk(),
                "procs": self._read_proc_count(), "uptime": self._read_uptime(),
                "rx_total": rx, "tx_total": tx}
        try:
            load = read_text("/proc/loadavg").split()
            snap["load"] = [float(load[0]), float(load[1]), float(load[2])]
        except (IndexError, ValueError):
            snap["load"] = [0.0, 0.0, 0.0]
        if self._prev_cpu and self._prev_net:
            p_total, p_idle, p_cores = self._prev_cpu
            d_total, d_idle = total - p_total, idle - p_idle
            snap["cpu"] = round(100.0 * (d_total - d_idle) / d_total, 1) if d_total else 0.0
            snap["cores"] = []
            for i, (c_total, c_idle) in enumerate(cores):
                if i < len(p_cores):
                    dt, di = c_total - p_cores[i][0], c_idle - p_cores[i][1]
                    snap["cores"].append(round(100.0 * (dt - di) / dt, 1) if dt else 0.0)
            dt = max(1e-6, now - self._prev_net[0])
            snap["rx_bps"] = max(0.0, (rx - self._prev_net[1]) / dt)
            snap["tx_bps"] = max(0.0, (tx - self._prev_net[2]) / dt)
        else:
            snap["cpu"], snap["cores"], snap["rx_bps"], snap["tx_bps"] = 0.0, [], 0.0, 0.0
        snap["cpu_count"] = len(cores) or (os.cpu_count() or 1)
        self._prev_cpu = (total, idle, cores)
        self._prev_net = (now, rx, tx)
        return snap

    def _loop(self):
        # 第一次采样只能拿到基准值（没有差值），所以先采一次再进循环
        has_proc = os.path.exists("/proc/stat") and not self.fake
        if has_proc:
            try:
                self._prev_cpu = self._read_cpu()
                rx, tx = self._read_net()
                self._prev_net = (time.time(), rx, tx)
            except Exception:  # noqa: BLE001
                pass
        while not self._stopped:
            try:
                # 先采一次再睡：否则开局几秒里 latest() 是空的，界面一片 0
                snap = self._snapshot()
                with self.lock:
                    self.samples.append(snap)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(self.interval)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        for _ in range(40):
            if self.latest():
                break
            time.sleep(0.05)

    def latest(self):
        with self.lock:
            return dict(self.samples[-1]) if self.samples else {}

    def series(self):
        with self.lock:
            return [{"t": s["t"], "cpu": s.get("cpu", 0), "rx": s.get("rx_bps", 0),
                     "tx": s.get("tx_bps", 0)} for s in self.samples]


# --------------------------------------------------------------------------
# systemd：服务列表、详情、日志
# --------------------------------------------------------------------------
class Services:
    """托管机上的 systemd 单元。没有 systemctl 就整体降级，不假装在管。"""

    def __init__(self):
        self.available = bool(shutil.which("systemctl"))
        self.hint = "" if self.available else "这台机器上没有 systemctl（多半是在容器里）"

    def list(self):
        if not self.available:
            return {"available": False, "hint": self.hint, "units": []}
        code, out, err = run(["systemctl", "list-units", "--type=service", "--all",
                              "--no-pager", "--no-legend", "--plain"], timeout=15)
        units = []
        for line in out.splitlines():
            f = line.split(None, 4)
            if len(f) < 4:
                continue
            # 只留界面真的用到的字段：`load`（loaded / not-found）从来没人读，
            # 210 个单元白扛 2 KB（P9）。要恢复的话在这里加回 f[1] 就行。
            units.append({"unit": f[0].lstrip("●").strip(), "active": f[2],
                          "sub": f[3], "desc": (f[4] if len(f) > 4 else "").strip()})
        units.sort(key=lambda u: (u["active"] != "active", u["unit"]))
        return {"available": True, "units": units, "error": err.strip()[:200],
                "running": sum(1 for u in units if u["active"] == "active"),
                "failed": sum(1 for u in units if u["active"] == "failed")}

    def detail(self, unit):
        """一个单元的关键状态。show 是机器可读的，比 status 好解析。"""
        if not self.available or not UNIT_RE.match(unit or ""):
            return {"ok": False, "error": "单元名不合法或 systemd 不可用"}
        props = ("ActiveState", "SubState", "MainPID", "ExecMainStartTimestamp",
                 "Description", "UnitFileState", "MemoryCurrent", "CPUUsageNSec",
                 "NRestarts", "FragmentPath")
        code, out, err = run(["systemctl", "show", unit, "-p", ",".join(props)], timeout=10)
        if code != 0:
            return {"ok": False, "error": (err or out).strip()[:200]}
        info = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip()
        mem, cpu_ns = info.get("MemoryCurrent", ""), info.get("CPUUsageNSec", "")
        return {
            "ok": True, "unit": unit, "desc": info.get("Description", ""),
            "active": info.get("ActiveState", ""), "sub": info.get("SubState", ""),
            "pid": info.get("MainPID", "") if info.get("MainPID") not in ("", "0") else "",
            "since": info.get("ExecMainStartTimestamp", ""),
            "enabled": info.get("UnitFileState", ""),
            "restarts": info.get("NRestarts", ""),
            "fragment": info.get("FragmentPath", ""),
            "memory": int(mem) if mem.isdigit() else None,
            "cpu_sec": round(int(cpu_ns) / 1e9, 2) if cpu_ns.isdigit() else None,
        }

    def logs(self, unit, lines=200):
        if not self.available or not UNIT_RE.match(unit or ""):
            return {"available": False, "lines": ["单元名不合法或 systemd 不可用"]}
        code, out, err = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager",
                              "-o", "short"], timeout=20)
        return {"available": code == 0, "lines": out.splitlines(),
                "error": err.strip()[:200] if code != 0 else ""}


# --------------------------------------------------------------------------
# Docker：容器列表、日志、启停
# --------------------------------------------------------------------------
# docker logs 的时间参数：相对（30m / 2h / 1d）或绝对（2026-10-09 / 2026-10-09T05:00[:00]）
LOG_TIME_RE = re.compile(r"^(\d+[smhd]|\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?)$")


# DeepSeek Harness（dsh）容器，以及它在浏览器里的入口。
#
# 为什么面板需要专门管这件事：dsh 的 Web UI 只有**一种**进场方式 ——
# 拿一次性启动 token 去换会话 cookie。没有 token，/dsh/ 一律回 401（一句纯文本
# Unauthorized），所以「点一下进面板」这个动作必须现取 token。而那个 token 每次启动
# 现生成、只打在容器日志里、不落盘，因此只能去日志里捞——这就是 /api/dsh 存在的理由。
#
# 服务拓扑（见 docs/adr/0008）：dsh 本体只肯绑 127.0.0.1，容器里另有一个小 TCP 转发
# 把 0.0.0.0:3081 转给它；宿主上的 Caddy 把 https://lolicon.fans/dsh/* 剥掉前缀后
# 反代到那个转发口。
DSH_CONTAINER = os.environ.get("N1BUS_DSH_CONTAINER", "dsh")
DSH_PUBLIC_BASE = os.environ.get("N1BUS_DSH_BASE", "https://lolicon.fans/dsh/")
DSH_ENTRY_TTL = 30.0
DSH_TOKEN_RE = re.compile(r"token=([A-Za-z0-9_-]{16,})")


class Docker:
    """Docker 容器。docker 不在就没有这一块，不影响其它功能。"""

    def __init__(self, stats_ttl=15.0):
        self.available = bool(shutil.which("docker"))
        self.hint = "" if self.available else "这台机器上没有 docker 命令"
        self._cache = (0.0, None)
        self._stats = (0.0, {})
        self._dsh = (0.0, None)
        self.stats_ttl = stats_ttl
        self._stats_lock = threading.Lock()
        self._stats_busy = False

    def _json_lines(self, argv, timeout=25):
        code, out, err = run(argv, timeout=timeout)
        items = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
        return code, items, err

    def stats(self):
        """容器实时统计（`docker stats --no-stream` 真机 1~2 秒）。

        它慢到不能进请求路径：以前 /api/docker 冷的时候要等 2 秒，用户看到的是
        "打开容器页转两秒"。现在的规矩是**绝不阻塞**：
        * 有缓存（哪怕刚过期）就先给缓存，后台线程去刷新 —— 界面立刻出结果，
          下一个 tick 拿到的就是新值（stale-while-revalidate）。
        * 一次都没取到过（进程刚起）就先返回空 dict，同样交给后台；
          响应里会带 stats_pending，前端过一会儿再要一次。
        TTL 15 秒：docker stats 本身就要 2 秒，这时候新鲜度不比那 2 秒值钱。
        """
        now = time.time()
        ts, val = self._stats
        if val and (now - ts) < self.stats_ttl:
            return val
        self._refresh_stats_async()
        return val

    def _refresh_stats_async(self):
        """后台刷新 stats；同一时刻只允许一个在跑，免得每个请求都去 fork 一个 docker。"""
        with self._stats_lock:
            if self._stats_busy:
                return
            self._stats_busy = True
        threading.Thread(target=self._refresh_stats_bg, daemon=True).start()

    def _refresh_stats_bg(self):
        try:
            self._refresh_stats()
        except Exception:                     # noqa: BLE001 —— 后台线程不许把异常抛到天上
            pass
        finally:
            with self._stats_lock:
                self._stats_busy = False

    def _refresh_stats(self):
        code, items, _ = self._json_lines(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=25)
        out = {}
        for c in items:
            out[c.get("Name", "")] = {
                "cpu": c.get("CPUPerc", ""), "mem": c.get("MemUsage", ""),
                "mem_pct": c.get("MemPerc", ""), "net": c.get("NetIO", ""),
                "block": c.get("BlockIO", ""), "pids": c.get("PIDs", ""),
            }
        self._stats = (time.time(), out)
        return out

    def list(self, with_stats=True):
        """容器列表（`docker ps -a`，真机 21 ms）。

        列表本身走只读缓存；**stats 不跟着列表一起缓存** —— 30 秒的列表缓存会把
        统计数字一起冻住，所以缓存里存的永远是不带 stats 的那份，每次请求现拼上
        最近一次 stats（它有自己的 TTL 与后台刷新）。还没取到过时带 stats_pending。
        """
        base = self._list_cached()
        if not with_stats or not base.get("available"):
            return base
        stats = self.stats()
        if not stats:
            return dict(base, stats_pending=True)
        return dict(base, containers=[dict(c, stats=stats.get(c.get("name", ""), {}))
                                      for c in base.get("containers", [])])

    def _list_cached(self):
        """容器清单（不含 stats）。"""
        if not self.available:
            return {"available": False, "hint": self.hint, "containers": []}
        code, items, err = self._json_lines(
            ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=25)
        out = []
        for c in items:
            out.append({"id": c.get("ID", "")[:12], "name": c.get("Names", ""),
                        "image": c.get("Image", ""), "state": c.get("State", ""),
                        "status": c.get("Status", ""), "ports": c.get("Ports", ""),
                        "running": c.get("State") == "running"})
        out.sort(key=lambda x: (not x["running"], x["name"]))
        return {"available": True, "containers": out,
                "error": err.strip()[:200] if code != 0 else "",
                "running": sum(1 for x in out if x["running"])}

    def dsh(self):
        """DeepSeek Harness 的进场链接（理由见模块顶部 DSH_* 常量的注释）。

        任何一步不成立都如实说明，绝不硬编一个链接出来 —— 编错了用户点进去只会看到 401。
        返回结构直接给前端用：available / state / started / url / hint。
        """
        info = {"available": False, "container": DSH_CONTAINER, "state": "",
                "started": "", "url": "", "hint": "", "error": ""}
        if not self.available:
            info["hint"] = self.hint
            return info
        code, out, err = run(["docker", "inspect", "--format",
                              "{{.State.Status}}|{{.State.StartedAt}}", DSH_CONTAINER],
                             timeout=10)
        if code != 0:
            info["hint"] = "这台机器上没有名为 %s 的容器" % DSH_CONTAINER
            return info
        info["available"] = True
        state, _, started = out.strip().partition("|")
        info["state"] = state
        info["started"] = started
        if state != "running":
            info["hint"] = "容器当前是 %s，等它跑起来才有可用的链接" % (state or "未知状态")
            return info
        # token 只在容器重启时变，所以缓存按 StartedAt 认；TTL 兜住边界情况。
        now = time.time()
        ts, cached = self._dsh
        if cached and cached[0] == started and (now - ts) < DSH_ENTRY_TTL:
            info["url"] = cached[1]
            return info
        code, out, err = run(["docker", "logs", "--tail", "200", DSH_CONTAINER], timeout=15)
        found = ""
        for line in (out + "\n" + err).splitlines():
            m = DSH_TOKEN_RE.search(line)
            if m:
                found = m.group(1)
        if not found:
            info["hint"] = "日志里还没有启动 token，容器可能刚起来还没打印完"
            return info
        info["url"] = DSH_PUBLIC_BASE + "?token=" + found
        self._dsh = (now, (started, info["url"]))
        return info

    def logs(self, name, lines=200, since="", until="", grep="", timestamps=False):
        """看容器日志，允许给 docker logs 自己的参数。

        参数全部作为列表交给 docker，不经 shell；`--since/--until` 只接受时间格式，
        所以这里依然没有「任意命令执行」的口子。
        """
        if not self.available or not NAME_RE.match(name or ""):
            return {"available": False, "lines": [], "cmd": "", "error": "容器名不合法"}
        try:
            lines = max(1, min(int(lines), 5000))
        except (TypeError, ValueError):
            lines = 200
        argv = ["docker", "logs", "--tail", str(lines)]
        for flag, val in (("--since", since), ("--until", until)):
            v = (val or "").strip()
            if not v:
                continue
            if not LOG_TIME_RE.match(v):
                return {"available": False, "lines": [], "cmd": "",
                        "error": "%s 只接受 30m / 2h / 1d 或 2026-10-09T05:00 这样的时间" % flag}
            argv += [flag, v]
        if timestamps:
            argv.append("-t")
        argv.append(name)
        code, out, err = run(argv, timeout=25)
        text = (out or "") + (err or "")
        rows = text.splitlines()
        kw = (grep or "").strip()
        if kw:
            rows = [l for l in rows if kw.lower() in l.lower()]
        return {"available": code == 0, "lines": rows[-lines:], "grep": kw,
                "matched": len(rows), "cmd": "docker " + " ".join(argv[1:]),
                "error": "" if code == 0 else text.strip()[:200]}

    def images(self):
        if not self.available:
            return {"available": False, "images": []}
        code, items, err = self._json_lines(
            ["docker", "images", "--format", "{{json .}}"], timeout=25)
        return {"available": code == 0, "error": err.strip()[:200] if code != 0 else "",
                "images": [{"repo": i.get("Repository", ""), "tag": i.get("Tag", ""),
                            "id": i.get("ID", ""), "size": i.get("Size", ""),
                            "created": i.get("CreatedSince", "")} for i in items]}


# --------------------------------------------------------------------------
# 安全面：登录失败、防火墙、fail2ban、SSH 配置
# --------------------------------------------------------------------------
class Security:
    """只读观测为主。处置动作（封 IP、解封）走白名单动作表。"""

    RE_FAIL = re.compile(
        r"(?P<ts>[A-Z][a-z]{2}\s+\d+\s+\d\d:\d\d:\d\d).*?"
        r"(?:Failed password for (?:invalid user )?(?P<u1>\S+)|Invalid user (?P<u2>\S+)|"
        r"Failed publickey for (?:invalid user )?(?P<u3>\S+)).*?"
        r"from (?P<ip>\d+\.\d+\.\d+\.\d+)")
    RE_OK = re.compile(
        r"(?P<ts>[A-Z][a-z]{2}\s+\d+\s+\d\d:\d\d:\d\d).*?"
        r"Accepted (?:password|publickey) for (?P<user>\S+) from (?P<ip>\d+\.\d+\.\d+\.\d+)")

    def logins(self, lines=0, top=0, recent=10):
        """聚合 auth.log 中的 SSH 登录成败记录。

        `lines=0` 表示读取**全部**日志（原先固定为 6000 行，而日志已有 12000 行以上，
        导致面板上的「登录失败」只有实际值的三分之一，与接口自身「来自 auth.log 全量
        统计」的说明不符）。
        `top=0` 表示排行榜**不截断**：来源 IP 通常只有几十个，完整列出才有参考价值。

        上限 20 万行，避免日志异常增长时拖慢本页响应。
        """
        text = []
        for f in ("/var/log/auth.log.1", "/var/log/auth.log"):
            if os.path.exists(f):
                rows = read_text(f).splitlines()
                if lines and lines > 0:
                    rows = rows[-lines:]
                text += rows
        if len(text) > 200000:
            text = text[-200000:]
        fails, oks = [], []
        for line in text:
            if "sshd" not in line:
                continue
            m = self.RE_FAIL.search(line)
            if m:
                fails.append({"ts": m.group("ts"), "ip": m.group("ip"),
                              "user": m.group("u1") or m.group("u2") or m.group("u3") or "?"})
                continue
            m = self.RE_OK.search(line)
            if m:
                oks.append({"ts": m.group("ts"), "ip": m.group("ip"), "user": m.group("user")})
        ips = collections.Counter(f["ip"] for f in fails)
        users = collections.Counter(f["user"] for f in fails)
        pick = (lambda c: c.most_common()) if not top else (lambda c: c.most_common(top))
        # ok_ips 是**全部**成功登录过的来源，而非 recent_ok 的最近 6 条。
        # 自动永久封禁以它作为可信来源名单：若只取最近 6 条，一个数日前登录过、
        # 之后因其他来源的扫描流量而被计入失败统计的地址会被误封。
        ok_ips = sorted({o["ip"] for o in oks})
        return {"available": bool(text), "failed": len(fails), "accepted": len(oks),
                "lines_scanned": len(text), "ok_ips": ok_ips,
                "top_ips": [{"ip": k, "n": v} for k, v in pick(ips)],
                "top_users": [{"user": k, "n": v} for k, v in pick(users)],
                "recent": fails[-max(1, recent):][::-1], "recent_ok": oks[-6:][::-1]}

    RE_UFW_DENY = re.compile(r"^\s*\S+\s+DENY\s+(?:IN\s+)?(\d{1,3}(?:\.\d{1,3}){3})", re.M)
    RE_F2B_BANNED = re.compile(r"Banned IP list:\s*(.+)")

    def ufw_raw(self):
        """`ufw status verbose` 原文（只读缓存 15 s）。

        以前一次 /api/security 里 ufw 被跑两遍：firewall() 一次 `status verbose`、
        blocked() 一次 `status`，各 ~88 ms（真机实测）。两处要的字段同一份 verbose
        输出里都有，合成一次 —— 顺带少 fork 一个进程（2 vCPU 上 fork 也是成本）。
        """
        if not shutil.which("ufw"):
            return {"available": False, "out": "", "hint": "未安装 ufw", "error": ""}
        code, out, err = run(["ufw", "status", "verbose"], timeout=15)
        return {"available": True, "out": out,
                "error": err.strip()[:200] if code != 0 else ""}

    def blocked(self, f2b=None):
        """当前真的被封着的 IP -> 封禁来源（"ufw" / "fail2ban/<jail>"）。

        手动封禁和 v1m-dash 的自动封禁都是 ufw deny from <ip>，所以查 ufw 就够；
        再叠上 fail2ban 自己 ban 的那批。前端用它给失败来源打「已封禁」，
        同一个 IP 就不该再点一次封禁。
        """
        out = {}
        u = self.ufw_raw()                   # 与 firewall() 共用同一份，不再单独跑 ufw
        if u["available"]:
            for m in self.RE_UFW_DENY.finditer(u["out"]):
                out[m.group(1)] = "ufw"
        if f2b is None:
            f2b = self.fail2ban()
        for jail, text in (f2b.get("detail") or {}).items():
            m = self.RE_F2B_BANNED.search(text or "")
            if not m:
                continue
            for ip in m.group(1).split():
                if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip):
                    out.setdefault(ip, "fail2ban/" + jail)
        return out

    def firewall(self):
        u = self.ufw_raw()
        if not u["available"]:
            return {"available": False, "hint": u.get("hint") or "未安装 ufw"}
        out = u["out"]
        rules = [l.rstrip() for l in out.splitlines()
                 if l.strip() and not l.startswith(("Status:", "Logging:", "Default:",
                                                    "New profiles:", "To ", "--"))]
        return {"available": True, "active": "Status: active" in out,
                "rules": rules[:40], "raw": out.strip()[:2500],
                "error": u.get("error", "")}

    # 一行 ufw 规则：<左侧：目标[ (v6)]>  <动作[ 方向]>  <来源>。
    # 方向只有在 `ufw status verbose` 里才出现（ALLOW IN / ALLOW OUT / ALLOW FWD），
    # 普通 `ufw status` 会省略它。ufw_raw() 用的正是 verbose，所以这里能拿到方向；
    # 万一拿不到，按 ufw 自己的默认值 in 处理。
    RE_UFW_ACT = re.compile(r"\s(ALLOW|DENY|LIMIT|REJECT)(?:\s+(IN|OUT|FWD))?\s+(.+)$")
    RE_UFW_SIMPLE = re.compile(r"^(\d{1,5})/(tcp|udp)$")

    def allowed(self, ports=None):
        """ufw 的规则，并与本机实际监听情况做双向对照。

        「已监听」与「已放行」是两件独立的事，分开看才能回答最常见的两个问题：

        * **服务在跑、端口没放行（入站）** —— 外部访问不了，需要在这里放行；
        * **端口放行了、没有服务监听** —— 一条没有作用的规则，可以清理。

        两个易错点在这里明确一下：

        1. **方向**。`ALLOW IN` 与 `ALLOW OUT` 是两条不同的规则，`ufw allow 80/tcp`
           与 `ufw allow out 80/tcp` 互不覆盖。方向从 verbose 输出里解析，缺省是 in。
        * `unmatched`（放行但无监听）**只对入站有意义** —— 出站规则不涉及本机监听。
        2. **Docker 发布的端口绕过 ufw**。docker-proxy 把转发规则直接写进 iptables 的
           DOCKER 链，流量在到达 ufw 过滤规则之前就被转发，所以「ufw 里没有这条规则」
           不等于「外面访问不了」。这类端口由 `docker` 字段标出，界面据此区分。
        """
        u = self.ufw_raw()
        if not u["available"]:
            return {"available": False, "hint": u.get("hint") or "未安装 ufw",
                    "policy": "", "rules": [], "allowed": [],
                    "uncovered": [], "unmatched": [], "error": ""}
        out = u["out"]
        # 默认策略只在 verbose 输出的 `Default:` 行里，单独取出来随规则一起返回 ——
        # 界面原先靠一段原始文本展示它，那段文本去掉之后不能把这条信息也丢了：
        # 「deny (incoming)」正是 ufw 之所以构成防线的根据。
        policy = ""
        for line in out.splitlines():
            if line.startswith("Default:"):
                policy = line.split(":", 1)[1].strip()
                break

        rules = []
        for line in out.splitlines():
            s = line.rstrip()
            if not s or s.startswith(("To ", "--", "Status:", "Logging:", "Default:")):
                continue
            m = self.RE_UFW_ACT.search(s)
            if not m:
                continue
            left = s[:m.start()].strip()
            v6 = left.endswith("(v6)")
            to = left[:-4].strip() if v6 else left
            action = m.group(1)
            direction = (m.group(2) or "IN").lower()      # 未标注方向 = ufw 默认的入站
            simple = self.RE_UFW_SIMPLE.match(to)
            rules.append({"to": to, "action": action, "dir": direction,
                          "from": m.group(3).strip(), "v6": v6,
                          "port": simple.group(1) if simple else "",
                          "proto": simple.group(2) if simple else "",
                          # 转发方向的删除语法与 in/out 不同，面板不代做
                          "simple": bool(simple) and direction in ("in", "out")})

        if ports is None:
            ports = Ports().listening()
        listen = {}
        for lp in (ports.get("ports") or []):
            listen.setdefault((str(lp.get("port")), (lp.get("proto") or "tcp").lower()),
                              []).append(lp)

        # IPv4 与 IPv6 在 ufw 里是两条独立规则（`ufw allow 80/tcp` 会一次加两条），
        # 按 (方向, 端口, 协议) 合并后再展示，否则界面上每个端口都会出现两遍。
        merged = {}
        for r in rules:
            if r["action"] != "ALLOW" or not r["simple"]:
                continue
            key = (r["dir"], r["port"], r["proto"])
            e = merged.setdefault(key, {"port": r["port"], "proto": r["proto"],
                                        "dir": r["dir"], "from": r["from"],
                                        "v4": False, "v6": False, "count": 0})
            e["v6" if r["v6"] else "v4"] = True
            e["count"] += 1
            # 来源不同的规则不合并语义，取第一条并在界面上如实呈现
        allowed = []
        for k in sorted(merged, key=lambda k: (k[0] != "in", int(k[1]))):
            e = merged[k]
            lp = listen.get((e["port"], e["proto"])) or []
            # 顺带把「这条规则背后有没有服务在听」带上，界面就不必自己去交叉两张表
            e["listen"] = (lp[0].get("process") or "") if lp else ""
            e["docker"] = bool(lp) and (lp[0].get("process") or "") == "docker-proxy"
            allowed.append(e)

        in_keys = {(r["port"], r["proto"]) for r in allowed if r["dir"] == "in"}
        # 放行了但没有服务监听：规则不产生实际效果（只对入站有意义）
        unmatched = [r for r in allowed
                     if r["dir"] == "in" and (r["port"], r["proto"]) not in listen]
        uncovered = []
        for (port, proto), items in sorted(listen.items(), key=lambda kv: int(kv[0][0])):
            if (port, proto) in in_keys:
                continue
            # 只报告对外可达的那些：只绑 127.0.0.1 的服务本来就只给本机用
            if not any(i.get("exposed") for i in items):
                continue
            uncovered.append({
                "port": port, "proto": proto,
                "process": items[0].get("process") or "",
                "pid": items[0].get("pid") or "",
                "addr": items[0].get("addr") or "",
                # docker-proxy 说明这条监听来自容器的端口发布：它的转发规则由 Docker
                # 直接写进 iptables，不受 ufw 约束，因此「ufw 里没有」是正常的。
                "docker": (items[0].get("process") or "") == "docker-proxy",
            })
        return {"available": True,
                "active": "Status: active" in out,
                "policy": policy,
                "rules": rules,
                "allowed": allowed,
                "unmatched": unmatched,
                "uncovered": uncovered,
                "error": u.get("error", "")}


    def fail2ban(self):
        if not shutil.which("fail2ban-client"):
            return {"available": False, "hint": "未安装 fail2ban"}
        code, out, err = run(["fail2ban-client", "status"], timeout=15)
        jails = []
        m = re.search(r"Jail list:\s*(.+)", out)
        if m:
            jails = [j.strip() for j in m.group(1).split(",") if j.strip()]
        detail = {}
        for j in jails[:6]:
            c, o, _ = run(["fail2ban-client", "status", j], timeout=15)
            if c == 0:
                detail[j] = o.strip()[:1200]
        return {"available": code == 0, "jails": jails, "detail": detail,
                "error": err.strip()[:200] if code != 0 else ""}

    def sshd(self):
        keys = {}
        for line in read_text("/etc/ssh/sshd_config").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or " " not in s:
                continue
            k, v = s.split(None, 1)
            keys[k] = v.strip()
        want = ("Port", "PermitRootLogin", "PasswordAuthentication", "PubkeyAuthentication",
                "PermitEmptyPasswords", "MaxAuthTries", "ChallengeResponseAuthentication",
                "AllowUsers", "KbdInteractiveAuthentication")
        cfg = {k: keys.get(k, "(默认)") for k in want}
        auth = Path("/root/.ssh/authorized_keys")
        n = 0
        if auth.exists():
            n = len([l for l in read_text(str(auth)).splitlines()
                     if l.strip() and not l.lstrip().startswith("#")])
        return {"config": cfg, "authorized_keys": n,
                "note": "authorized_keys 为空时不要关密码登录，否则会把自己锁在外面" if not n else ""}


# --------------------------------------------------------------------------
# 端口 / 连接 / 进程
# --------------------------------------------------------------------------
class Ports:
    RE_SS = re.compile(
        r"^(?P<proto>tcp|udp)\s+(?P<state>\S+)\s+\S+\s+\S+\s+(?P<local>\S+)\s+(?P<peer>\S+)"
        r"(?:\s+users:\(\((?P<proc>\"[^\"]+\",pid=\d+|[^)]*)\))?")
    RE_PROC = re.compile(r"\"(?P<name>[^\"]+)\",pid=(?P<pid>\d+)")

    def listening(self):
        if not shutil.which("ss"):
            return {"available": False, "hint": "没有 ss 命令", "ports": []}
        code, out, err = run(["ss", "-lntup"], timeout=15)
        ports = []
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) < 5 or f[0] not in ("tcp", "udp"):
                continue
            m = self.RE_PROC.search(line)
            ports.append({
                "proto": f[0], "local": f[4],
                "addr": f[4].rsplit(":", 1)[0] or "*", "port": f[4].rsplit(":", 1)[-1],
                "process": m.group("name") if m else "",
                "pid": m.group("pid") if m else "",
                "exposed": not f[4].startswith(("127.", "[::1]", "::1")),
            })
        ports.sort(key=lambda p: (not p["exposed"], p["port"]))
        return {"available": True, "ports": ports,
                "exposed": sum(1 for p in ports if p["exposed"]),
                "error": err.strip()[:200] if code != 0 else ""}

    def established(self, limit=40):
        code, out, err = run(["ss", "-tun"], timeout=15)
        rows = []
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) < 6 or "ESTAB" not in f[1]:
                continue
            rows.append({"proto": f[0], "local": f[4], "peer": f[5]})
            if len(rows) >= limit:
                break
        return {"connections": rows, "error": err.strip()[:200] if code != 0 else ""}


# 单元属性缓存：FragmentPath（判断"发行版自带"还是"用户/第三方装的"）与
# UnitFileState（enabled / disabled / static …）。
# 逐个 systemctl show 约 8ms，210 个约 1.5s；这份缓存让它每个进程只付出一次代价。
UNIT_PATH_CACHE = {}
UNIT_STATE_CACHE = {}
UNIT_PROBED = set()          # 问过 systemctl 的单元（问不到也算问过，别反复问）
DISTRO_UNIT_DIRS = ("/lib/systemd/system/", "/usr/lib/systemd/system/", "/run/systemd/")
ETC_UNIT_DIR = "/etc/systemd/system"


def _remember_unit(name, path, state):
    if not name or "@" in name:
        return
    UNIT_PATH_CACHE[name] = path
    UNIT_STATE_CACHE[name] = state
    UNIT_PROBED.add(name)


def _unit_props_batch(units):
    """一次 `systemctl show` 问一批单元的 UnitFileState / FragmentPath，返回查到的名字。"""
    code, out, _ = run(["systemctl", "show", "-p", "Id", "-p", "UnitFileState",
                        "-p", "FragmentPath"] + list(units), timeout=25)
    got, cur = set(), {}
    for line in list(out.splitlines()) + [""]:
        if not line.strip():
            if cur:
                _remember_unit(cur.get("Id", ""), cur.get("FragmentPath", ""),
                               cur.get("UnitFileState", ""))
                got.add(cur.get("Id", ""))
                cur = {}
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
    return got


def _unit_props(units):
    """批量查单元属性（进缓存），返回 {unit: (UnitFileState, FragmentPath)}。

    为什么要批量：逐个 `systemctl show` 一个 8ms，210 个就是 1.6 秒 —— 而
    「开机自启」每次写操作后缓存失效都要重付一遍，用户感觉就是"点完要等好久"。
    一次性传多个单元只要 ~10ms。

    坑：**模板单元**（`xxx@.service`）会让 systemctl 报
    `Failed to get properties: Unit name ... is neither a valid invocation ID nor unit name`
    并且**中断整批**（实测 210 个只返回前 3 条）。所以先把带 `@` 的挑出去。
    同一个坑还有"不存在的单元"：批量里混进一个，排在它后面的也全丢 ——
    所以批查询之后对没拿到的逐个补问一次，问不到就记下来，不再重复问。
    """
    todo = [u for u in units if "@" not in u and u not in UNIT_PROBED]
    if todo:
        got = _unit_props_batch(todo)
        for u in todo:
            if u not in got:
                _unit_props_batch([u])
                # 单个补查过就认了：还查不到就是 systemd 不认它（坏链接 / alias 名 /
                # 刚放进 /etc 还没 daemon-reload），别每次请求都再问一遍。
                UNIT_PROBED.add(u)
    return {u: (UNIT_STATE_CACHE.get(u, ""), UNIT_PATH_CACHE.get(u, "")) for u in units}


def _unit_paths(units):
    """只要 FragmentPath 的老接口（判据在用）。"""
    return {u: p for u, (_, p) in _unit_props(units).items()}


def _etc_service_units():
    """用户/第三方装的 .service 单元名；**读不到目录时返回 None**，调用方退回全量扫描。

    为什么自己读目录：真机实测（2 vCPU）`systemctl list-unit-files --type=service`
    要 0.95 s，带 `--state=enabled` 更慢（1.84 s）—— 它得挨个 stat 四百多个 unit 文件，
    而每一次写操作后缓存失效都要重付。面板关心的"用户装的服务"本体就在
    /etc/systemd/system 顶层这一个目录里，os.scandir 一次 ~1 ms。
    `systemctl enable` 只是在 <target>.wants/ 里建符号链接，单元本体仍在顶层，
    所以扫顶层就够（wants 目录是目录，不会被当单元）。
    """
    units = set()
    try:
        with os.scandir(ETC_UNIT_DIR) as it:
            for e in it:
                if e.name.endswith(".service") and "@" not in e.name:
                    try:
                        if e.is_file():          # 跟随符号链接；断链与目录不算
                            units.add(e.name)
                    except OSError:
                        continue
    except OSError:
        return None
    return units


def _all_service_units():
    """全量问 systemctl 要单元名；只在 /etc/systemd/system 读不到时兜底（很慢）。"""
    code, out, _ = run(["systemctl", "list-unit-files", "--type=service",
                        "--no-legend", "--no-pager"], timeout=25)
    names = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[0])
    _unit_props(names)
    return [u for u in names if u in PROTECTED_UNITS or not _is_distro_unit(u)]


def _is_distro_unit(unit):
    """这个单元是不是 Ubuntu 自带的？

    判据是单元文件的位置：发行版自带的在 /lib/systemd/system 或 /usr/lib/systemd/system，
    运行时动态生成的（apport / netplan-ovs-cleanup 这类）在 /run/systemd 下；
    用户/第三方装的（n1bus / v1m-dash / docker / fail2ban …）在 /etc/systemd/system。
    比按名字前缀过滤准得多 —— 前缀法会漏掉 apport / apt-daily / bolt / cryptdisks 这些。
    查不到路径的一律当作"不是自带的"（宁可多显示，也不要漏掉用户自己装的服务）。

    模板单元（`xxx@.service`，如 getty@ / systemd-fsck@）的 FragmentPath 是空的、
    而且还会让 batch show 失败，所以直接按名字挡掉 —— 它们全是发行版自带的。
    """
    if "@" in unit:                      # 模板单元：Ubuntu 那堆 getty@ / fsck@ / user@
        return unit not in PROTECTED_UNITS
    if unit not in UNIT_PROBED:          # 单独补问一个（批量路径没覆盖到）
        _unit_props([unit])
    p = UNIT_PATH_CACHE.get(unit, "")
    return p.startswith(DISTRO_UNIT_DIRS)


def _docker_autostart(names):
    """容器的 restart policy。

    一次 `docker inspect` 拿全部（原来是每个容器 fork 一次 inspect，20ms × N）；
    批量失败（某个容器刚好被删掉）才逐个退回，行为与从前一致。
    """
    code, out, _ = run(["docker", "inspect", "--format",
                        "{{.Name}}|{{.HostConfig.RestartPolicy.Name}}"] + list(names),
                       timeout=25)
    boxes = []
    if code == 0:
        for line in out.splitlines():
            if "|" not in line:
                continue
            nm, pol = line.split("|", 1)
            p = pol.strip() or "no"
            boxes.append({"name": nm.strip().lstrip("/"), "policy": p,
                          "autostart": p in ("always", "unless-stopped")})
    if len(boxes) == len(names):
        return boxes
    boxes = []
    for name in names:
        c, pol, _ = run(["docker", "inspect", name, "--format",
                         "{{.HostConfig.RestartPolicy.Name}}"], timeout=20)
        p = pol.strip() if c == 0 else "?"
        boxes.append({"name": name, "policy": p or "no",
                      "autostart": p in ("always", "unless-stopped")})
    return boxes


class Processes:
    def top(self, by="cpu", n=15):
        flag = "-pmem" if by == "mem" else "-pcpu"
        code, out, err = run(["ps", "-eo", "pid,ppid,user:14,pcpu,pmem,rss,etimes,stat,comm,args",
                              "--sort=" + flag, "--no-headers"], timeout=15)
        rows = []
        for line in out.splitlines()[:max(0, n)]:
            f = line.split(None, 9)
            if len(f) < 10:
                continue
            try:
                rows.append({"pid": f[0], "ppid": f[1], "user": f[2], "cpu": float(f[3]),
                             "mem": float(f[4]), "rss": int(f[5]) * 1024, "etimes": int(f[6]),
                             "stat": f[7], "comm": f[8], "args": f[9][:180]})
            except ValueError:
                continue
        return {"top": rows, "error": err.strip()[:200] if code != 0 else ""}

    def counts(self):
        total = zombie = 0
        for name in os.listdir("/proc") if os.path.isdir("/proc") else []:
            if not name.isdigit():
                continue
            total += 1
            try:
                if read_text("/proc/%s/stat" % name).split(") ", 1)[1][0] == "Z":
                    zombie += 1
            except (IndexError, OSError):
                pass
        return {"total": total, "zombie": zombie}


# --------------------------------------------------------------------------
# 白名单动作：面板能改变系统状态的全部手段，就这一张表
# --------------------------------------------------------------------------
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


class Audit:
    """审计日志：所有改变系统状态的尝试都追加一行 JSON。落盘的只有这个。"""

    def __init__(self, path, max_bytes=2 << 20):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()

    def write(self, action, target, ok, detail, ip):
        entry = {"ts": now_iso(), "t": int(time.time()), "ip": ip, "action": action,
                 "target": target, "ok": bool(ok), "detail": (detail or "")[:600]}
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    shutil.copy2(str(self.path), str(self.path) + ".1")
                    open(self.path, "w", encoding="utf-8").close()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return entry

    def tail(self, n=200):
        out = []
        for line in read_text(str(self.path)).splitlines()[-n:][::-1]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


class Enforcer:
    """从 v1m-dash 拉「该封谁 / 该解封谁」，用本机的白名单动作执行，再把结果回报。

    为什么是它拉、而不是 v1m-dash 推：防火墙的权柄本来就该留在宿主机上；而且 v1m-dash
    的面板已经发布在宿主的 127.0.0.1:8099，宿主直接就能连 —— 不用开新端口，也不用
    给容器任何特权。执行仍然走 ACTIONS 那张表（ufw.deny / ufw.undeny），所以自动封禁
    和手动封禁一样有审计。
    """

    def __init__(self, url, token, interval, audit, timeout=15):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.interval = max(10, int(interval or 60))
        self.audit = audit
        self.timeout = timeout
        self.enabled = bool(self.url and self.token)
        self.last = {"ts": 0, "ok": None, "banned": [], "unbanned": [], "error": ""}
        self.lock = threading.Lock()

    # --- 与 v1m-dash 的两次对话 ---------------------------------------------
    def _call(self, path, method="GET", body=None):
        if not self.enabled:
            return False, {"error": "未配置外部封禁决策源"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
            "User-Agent": "n1bus-enforcer"})
        try:
            # 这是本机回环调用，必须绕开环境里的 HTTP(S)_PROXY
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return True, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            return False, {"error": "HTTP %d：%s" % (e.code, e.read().decode("utf-8", "replace")[:200])}
        except Exception as e:  # noqa: BLE001
            return False, {"error": "%s: %s" % (type(e).__name__, e)}

    def poll_once(self):
        """跑一轮：拉取 → 执行 → 回报。返回这一轮的结果（也给界面看）。"""
        result = {"ts": int(time.time()), "ok": True, "banned": [], "unbanned": [], "error": ""}
        ok, data = self._call("/api/enforce/pending")
        if not ok:
            result.update(ok=False, error=data.get("error", "拉不到待执行列表"))
            self._remember(result)
            return result
        for item in (data.get("ban") or []):
            ip, reason, detail = item.get("ip"), item.get("reason", ""), item.get("detail", "")
            run = run_action("ufw.deny", {"ip": ip}, "v1m-dash", self.audit)
            self._call("/api/enforce/report", "POST", {
                "ip": ip, "action": "ban", "ok": run.get("ok"), "reason": reason,
                "detail": detail, "response": (run.get("detail") or run.get("error") or "")[:300]})
            if run.get("ok"):
                result["banned"].append(ip)
            else:
                result.update(ok=False, error="封 %s 失败：%s" % (ip, run.get("error") or run.get("err")))
        for item in (data.get("unban") or []):
            ip, reason, detail = item.get("ip"), item.get("reason", ""), item.get("detail", "")
            run = run_action("ufw.undeny", {"ip": ip}, "v1m-dash", self.audit)
            self._call("/api/enforce/report", "POST", {
                "ip": ip, "action": "unban", "ok": run.get("ok"), "reason": reason,
                "detail": detail, "response": (run.get("detail") or run.get("error") or "")[:300]})
            if run.get("ok"):
                result["unbanned"].append(ip)
            else:
                result.update(ok=False, error="解封 %s 失败：%s" % (ip, run.get("error") or run.get("err")))
        self._remember(result)
        return result

    def _remember(self, result):
        with self.lock:
            self.last = result

    def status(self):
        with self.lock:
            last = dict(self.last)
        return {"enabled": self.enabled, "url": self.url, "interval": self.interval,
                "last": last,
                "note": "" if self.enabled else "未接入外部封禁决策源，按来源封禁功能未启用"}

    def loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001
                self._remember({"ts": int(time.time()), "ok": False, "banned": [], "unbanned": [],
                                "error": "%s: %s" % (type(e).__name__, e)})
            time.sleep(self.interval)


# --------------------------------------------------------------------------
# 重复失败来源永久封禁
#
# fail2ban 的判据是「窗口内的失败次数」，到期自动解封。对持续低速扫描的来源，
# 该策略不会收敛 —— 原因与实测数据见 Defender 的文档字符串及 docs/adr/0010。
# --------------------------------------------------------------------------
AUTOBAN_THRESHOLD = 10        # 累计失败次数超过该值即永久封禁
AUTOBAN_MIN_THRESHOLD = 3     # 阈值下限：低于该值无法区分密码输错与暴力破解
AUTOBAN_MAX_THRESHOLD = 100000
AUTOBAN_INTERVAL = 300        # 巡检间隔（秒）
AUTOBAN_KEEP_LOG = 200        # 落盘保留的处置记录条数（面板只展示最近若干条）
AUTOBAN_FIRST_DELAY = 30      # 启动后延迟执行首轮，留出人工确认的时间窗口


def _clamp_threshold(t):
    """把阈值夹到 [下限, 上限] 之内。

    **所有**入口都必须经过这里。只在 configure() 里夹是不够的：__init__ 直接
    int(threshold)、main() 又把 --auto-ban-after 原样传进来，于是
    `--auto-ban-after 1` 能让「失败 2 次即永久封禁」真的生效 —— 那正好是 ADR 0010
    自己认下的唯一实际代价（误封风险）。
    """
    try:
        n = int(t)
    except (TypeError, ValueError):
        n = AUTOBAN_THRESHOLD
    return max(AUTOBAN_MIN_THRESHOLD, min(AUTOBAN_MAX_THRESHOLD, n))


# 排除网段：回环、私有网段、链路本地。这些地址永不自动封禁。
PRIVATE_NETS = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16"


class Defender:
    """重复失败来源永久封禁：累计失败次数超过阈值的来源执行 ufw deny，不自动解封。

    为什么 fail2ban 不足以覆盖该场景：sshd jail 的配置是
    maxretry=4 / findtime=600 / bantime=3600，即「10 分钟内失败 4 次则封禁 1 小时」，
    **到期自动解封**。持续低速扫描的来源，其失败记录分散在时间窗口之外，不会触发
    maxretry；即便触发，解封后仍会继续。生产环境实测：

        154.18.197.29    累计失败 37 次，被 fail2ban 封禁 19 次
        177.104.140.2    累计失败 52 次，被 fail2ban 封禁 16 次
        45.148.10.141    累计失败 45 次，被 fail2ban 封禁 13 次

    本类补充的是另一半：把累计失败次数超过阈值、且从未成功登录过的来源写入 ufw 规则。
    ufw 规则持久化在 /etc/ufw/user.rules，重启后仍然生效。放行只有一条路径 —— 在面板上
    解除封禁；该操作会同时把 IP 写入豁免名单，因为解封不会重置累计失败次数，否则下一轮
    巡检会立即重新封禁。

    两条约束，避免把管理员自己挡在门外：
      1. **成功登录过的 IP 永不自动封禁**（auth.log 中全部 Accepted 记录的来源）。
         管理员、运维与任何真实账户的来源都在该集合内，这是可信度最高的信号。
      2. 私有网段与回环不封禁；无法解析的地址同样不封禁（保守处理）。

    执行仍走 ACTIONS 白名单（ufw.deny），因此自动封禁与手动封禁的审计格式一致。
    """

    def __init__(self, path=None, threshold=AUTOBAN_THRESHOLD, enabled=True,
                 skip=None, audit=None, interval=AUTOBAN_INTERVAL, dry=False):
        self.path = Path(path) if path else None
        self.audit = audit
        self.dry = bool(dry)          # 演示模式：动作只生成命令，不实际执行
        self.skip = list(skip or [])
        self.interval = max(30, int(interval or AUTOBAN_INTERVAL))
        self.lock = threading.Lock()
        self.busy = False
        self._thread = None           # 巡检线程；由 start() 幂等拉起
        self.state = {"enabled": bool(enabled), "threshold": _clamp_threshold(threshold),
                      "exempt": [], "log": [], "last": {}}
        self._load()

    # --- 落盘 -------------------------------------------------------------
    def _load(self):
        if not self.path:
            return
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k in self.state:
                            self.state[k] = v
                    self.state["exempt"] = [str(x) for x in (self.state.get("exempt") or [])]
                    self.state["log"] = list(self.state.get("log") or [])[-AUTOBAN_KEEP_LOG:]
                    # 落盘值同样要夹：手上的 defender.json 可能是旧版本、或被人手改过
                    self.state["threshold"] = _clamp_threshold(self.state.get("threshold"))
        except (OSError, ValueError):
            pass

    def _save(self):
        if not self.path:
            return
        try:
            tmp = Path(str(self.path) + ".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(self.path))
        except OSError:
            pass

    # --- 配置与豁免 -------------------------------------------------------
    def configure(self, enabled=None, threshold=None, security=None):
        with self.lock:
            if enabled is not None:
                self.state["enabled"] = bool(enabled)
            if threshold is not None:
                self.state["threshold"] = _clamp_threshold(threshold)
            self._save()
            conf = {"enabled": bool(self.state["enabled"]),
                    "threshold": int(self.state["threshold"])}
        # 在线开启：巡检线程可能**从未启动过**（实例启动时是关的，或 defender.json 里
        # 存着 false）。所以这里必须补一次幂等启动，否则界面显示已启用、阈值与待封禁
        # 预览也都正常，巡检却永不执行 —— 只有手动「立即执行一轮」有效，且不报任何错。
        if conf["enabled"] and security is not None:
            self.start(security)
        return conf

    def start(self, security):
        """确保巡检线程在跑（幂等），返回本次是否真的拉起了它。

        巡检循环是常驻的，开关只由 state["enabled"] 决定 —— 所以「关掉再打开」
        既不必重启服务，也不必重启线程。这里只补「还没起来」的那一次。
        """
        if security is None:
            return False
        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self.loop, args=(security,),
                                            name="n1bus-autoban", daemon=True)
            self._thread.start()
            return True

    @property
    def running(self):
        """巡检线程是否活着。界面据此区分「已启用」与「已启用且真的在巡检」。"""
        return bool(self._thread is not None and self._thread.is_alive())

    def exempt(self, ip, on=True):
        """把 IP 加入 / 移出豁免名单。名单内的地址永不被自动封禁。"""
        with self.lock:
            ex = set(self.state.get("exempt") or [])
            if on:
                ex.add(ip)
            else:
                ex.discard(ip)
            self.state["exempt"] = sorted(ex)
            self._save()
            return sorted(ex)

    def is_exempt(self, ip):
        return ip in set(self.state.get("exempt") or [])

    def _own(self, ip):
        """地址属于排除网段、或无法解析时返回 True（两者都不自动封禁）。"""
        import ipaddress
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return True
        return any(a in n for n in self.skip)

    # --- 判定：本轮应封禁哪些来源 -----------------------------------------
    def candidates(self, logins, blocked):
        """计算本轮应封禁的来源，**只计算、不执行**。

        面板上的「将会封禁」预览与真实执行调用的是同一个方法，因此预览结果与实际
        行为一致，不存在两套判断逻辑。
        """
        lg = logins or {}
        fails = {x.get("ip"): int(x.get("n") or 0) for x in (lg.get("top_ips") or [])}
        good = set(lg.get("ok_ips") or [])          # 成功登录过的来源：永不自动封禁
        blk = blocked or {}
        ex = set(self.state.get("exempt") or [])
        th = int(self.state.get("threshold") or AUTOBAN_THRESHOLD)
        out = []
        for ip, n in fails.items():
            if not ip or n <= th:
                continue
            if ip in good or ip in ex or ip in blk:
                continue
            if self._own(ip):
                continue
            out.append({"ip": ip, "n": n})
        out.sort(key=lambda x: -x["n"])
        return out

    # --- 执行 -------------------------------------------------------------
    def run_once(self, security, logins=None, blocked=None, actor="autoban"):
        """执行一轮：判定 → 封禁 → 记录。返回值同时也是面板展示用的结果。"""
        if not self.state.get("enabled"):
            return {"ts": int(time.time()), "ok": True, "skipped": "自动封禁已关闭",
                    "banned": [], "failed": [], "count": 0}
        if self.busy:
            return {"ts": int(time.time()), "ok": True, "skipped": "上一轮尚未结束",
                    "banned": [], "failed": [], "count": 0}
        self.busy = True
        try:
            lg = logins if logins is not None else security.logins()
            blk = blocked if blocked is not None else security.blocked()
            todo = self.candidates(lg, blk)
            result = {"ts": int(time.time()), "ok": True, "skipped": "",
                      "banned": [], "failed": [], "count": len(todo)}
            for c in todo:
                r = run_action("ufw.deny", {"ip": c["ip"]}, actor, self.audit, dry=self.dry)
                row = {"ts": int(time.time()), "ip": c["ip"], "n": c["n"],
                       "ok": bool(r.get("ok")), "actor": actor,
                       "detail": (r.get("detail") or r.get("error") or "")[:200]}
                if row["ok"]:
                    result["banned"].append(c["ip"])
                else:
                    result["failed"].append(c["ip"])
                    result["ok"] = False
                with self.lock:
                    self.state["log"] = (list(self.state.get("log") or []) + [row])[-AUTOBAN_KEEP_LOG:]
            with self.lock:
                self.state["last"] = result
                self._save()
            return result
        finally:
            self.busy = False

    def unban(self, ip, actor="manual", jail=""):
        """面板解封：撤销 ufw 规则（提供 jail 时同时撤销 fail2ban 封禁），**并加入豁免名单**。

        豁免是必要的：解封不会重置累计失败次数，若不加豁免，下一轮巡检会立即依据同一
        次数重新封禁，表现为「解封按钮无效」。
        """
        out = []
        r = run_action("ufw.undeny", {"ip": ip}, actor, self.audit, dry=self.dry)
        out.append(r)
        # 仅被 fail2ban 封禁的地址在 ufw 中没有对应规则，上面那次调用必然失败。
        # 因此不能用 ufw 的结果决定是否解 fail2ban：两条路径各自撤销，任一成功即视为解封。
        if jail:
            out.append(run_action("fail2ban.unban", {"jail": jail, "ip": ip}, actor,
                                  self.audit, dry=self.dry))
        self.exempt(ip, True)
        ok = any(x.get("ok") for x in out)
        with self.lock:
            row = {"ts": int(time.time()), "ip": ip, "n": 0, "ok": bool(ok), "actor": actor,
                   "detail": "解除封禁并加入豁免名单：" + (out[0].get("detail")
                                                          or out[0].get("error") or "")[:160]}
            self.state["log"] = (list(self.state.get("log") or []) + [row])[-AUTOBAN_KEEP_LOG:]
            self._save()
        return {"ok": bool(ok), "ip": ip, "runs": out,
                "detail": out[0].get("detail") or out[0].get("error") or ""}

    # --- 供界面读取的状态快照 ---------------------------------------------
    def status(self, security=None, logins=None, blocked=None):
        st = {"enabled": bool(self.state.get("enabled")),
              "running": self.running,
              "threshold": int(self.state.get("threshold") or AUTOBAN_THRESHOLD),
              "interval": self.interval,
              "min_threshold": AUTOBAN_MIN_THRESHOLD,
              "max_threshold": AUTOBAN_MAX_THRESHOLD,
              "exempt": sorted(self.state.get("exempt") or []),
              "log": list(self.state.get("log") or [])[-20:][::-1],
              "last": dict(self.state.get("last") or {}),
              "skip": [str(n) for n in self.skip],
              "pending": []}
        if security is not None:
            lg = logins if logins is not None else security.logins()
            blk = blocked if blocked is not None else security.blocked()
            st["pending"] = self.candidates(lg, blk)
            st["own_ips"] = len(lg.get("ok_ips") or [])
        return st

    def loop(self, security):
        # 首轮延迟：若启动后立即按历史日志批量封禁，管理员没有机会先查看将受影响的来源。
        # 延迟一个窗口，使「将会封禁」列表先在界面上可见。
        time.sleep(min(AUTOBAN_FIRST_DELAY, self.interval))
        while True:
            try:
                self.run_once(security)
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.state["last"] = {"ts": int(time.time()), "ok": False, "banned": [],
                                          "failed": [], "count": 0,
                                          "error": "%s: %s" % (type(e).__name__, e)}
            time.sleep(self.interval)


# --------------------------------------------------------------------------
# KMS 授权服务（v1m-dash 的数据）
#
# 合并之后对外只有一个面板，但 KMS 的统计与限流决策仍留在原来的进程里：它同时管着
# vlmcsd 的 ini、supervisord 的进程控制、以及激活日志的解析与归档。把这些全搬过来
# 收益不大、风险不小。所以这里**只读取**它的数据，界面上不再有两套面板的感觉。
#
# 这与 DeepSeek Harness 是同一个思路：Harness 也是独立容器，在面板里只是一个页面。
# --------------------------------------------------------------------------
NIMBUS_URL = os.environ.get("NIMBUS_URL") or "http://127.0.0.1:8099"
# token 落在 docker 卷里。卷名取决于 compose 的项目名，所以按常见位置依次找一遍。
NIMBUS_TOKEN_PATHS = (
    "/var/lib/docker/volumes/docker_kms-data/_data/nimbus.token",
    "/var/lib/docker/volumes/kms-data/_data/nimbus.token",
    "/var/lib/nimbus/nimbus.token",
)

# KMS 的写操作白名单：操作名 → (对方的路径, 允许透传的参数；None 表示全部参数)。
# 不开放任意转发：那个面板还带着自己的账户接口（建账户、改密码），全量转发等于把
# 它的登录体系一并交给这里的任何使用者。所以每个操作只挑自己需要的参数。
KMS_WRITES = {
    "service":         ("/api/service",        ("action",)),
    "config":          ("/api/config",         None),
    "config_rollback": ("/api/config/rollback", ()),
    "unban":           ("/api/limits/unban",   ("ip",)),
    "stats_prune":     ("/api/stats/prune",    ()),
    "stats_clear":     ("/api/stats/clear",    ("confirm",)),
}


class KMSService:
    """读 v1m-dash 的 KMS 数据，并把白名单内的写操作转过去。"""

    def __init__(self, url=NIMBUS_URL, token="", timeout=8):
        self.url = (url or "").rstrip("/")
        self.timeout = timeout
        self._token = token or ""
        self._token_at = 0.0

    def token(self):
        """token 可能被轮换，所以带 60 秒缓存地重新读，而不是只在启动时读一次。"""
        if self._token and time.time() - self._token_at < 60:
            return self._token
        for p in NIMBUS_TOKEN_PATHS:
            try:
                if os.path.exists(p):
                    t = read_text(p).strip()
                    if t:
                        self._token, self._token_at = t, time.time()
                        return t
            except OSError:
                continue
        return self._token

    def _get(self, path):
        if not self.url:
            return None
        try:
            req = urllib.request.Request(
                "%s%s?token=%s" % (self.url, path, self.token()),
                headers={"User-Agent": "nimbus-panel"})
            # 本机回环调用要绕开环境里的 HTTP(S)_PROXY，否则会被代理掉
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except Exception:  # noqa: BLE001
            return None

    def summary(self):
        """服务状态 + 激活统计。取不到就如实说不可用，不编数据。"""
        s = self._get("/api/summary")
        if s is None:
            return {"available": False,
                    "hint": "读不到 KMS 服务（v1m-dash 未运行，或 token 不可读）"}
        return {"available": True, "service": s.get("service") or {},
                "stats": s.get("stats") or {}}

    def limits(self):
        """限流与封禁现状。"""
        d = self._get("/api/limits")
        if d is None:
            return {"available": False, "hint": "读不到限流状态"}
        return dict(d, available=True)

    def config(self):
        """vlmcsd.ini 的可写配置项。

        注意它与 summary 是两个接口：summary 只给服务状态与统计，配置在 /api/config。
        """
        d = self._get("/api/config")
        return (d or {}).get("config") or {}

    def events(self, per=50):
        """最近的激活明细。分页接口，这里只取第一页用来展示。"""
        d = self._get("/api/stats?per=%d" % per)
        page = (d or {}).get("page") or {}
        for key in ("rows", "items", "records", "events"):
            if isinstance(page.get(key), list):
                return page[key]
        return []

    def all(self):
        s = self.summary()
        # 配置与激活明细在另外两个接口里；取不到就留空，不让整页变成不可用
        if s.get("available"):
            s["config"] = self.config()
            s["events"] = self.events()
        else:
            s["config"], s["events"] = {}, []
        return {"summary": s, "limits": self.limits()}

    def _post(self, path, body):
        if not self.url:
            return None
        try:
            data = json.dumps(body or {}).encode("utf-8")
            req = urllib.request.Request(
                "%s%s?token=%s" % (self.url, path, self.token()), data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "nimbus-panel"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    def action(self, op, params, audit=None, client=""):
        """把一次写操作转给原面板。

        **走白名单，不开放任意转发。** 那个面板除了 KMS 还带着自己的账户管理接口
        （创建账户、改密码），全量转发等于把它的登录体系一并交给这里的任何使用者。
        所以每个操作只挑它自己需要的参数，多余的参数直接丢掉。

        params 为 None 的操作（config）是个例外：vlmcsd 的配置项由对方自己的
        validate() 逐项校验，这里不重复实现一遍规则。
        """
        spec = KMS_WRITES.get(op)
        if not spec:
            return {"ok": False, "error": "不支持的操作：%s" % (op or "(空)")}
        path, allow = spec
        if allow is None:
            body = dict(params or {})
        else:
            body = {k: (params or {}).get(k) for k in allow if k in (params or {})}
        if op == "service" and body.get("action") not in ("start", "stop", "restart"):
            return {"ok": False, "error": "服务动作只能是 start / stop / restart"}
        r = self._post(path, body)
        if r is None:
            r = {"ok": False, "error": "KMS 面板不可达（%s）" % self.url}
        if audit is not None:
            errs = r.get("errors") or []
            detail = r.get("error") or ("；".join(str(x) for x in errs) if errs
                                        else json.dumps(r, ensure_ascii=False)[:300])
            audit.write("kms." + op, json.dumps(body, ensure_ascii=False)[:200],
                        bool(r.get("ok")), detail, client)
        return r


class _DemoKMS:
    """演示用的 KMS 数据，结构与真机一致，界面代码不用分叉。"""

    def action(self, op, params, audit=None, client=""):
        """演示模式：只回成功，不真的改变任何东西（与 run_action 的 dry 同一思路）。"""
        return {"ok": True, "demo": True, "op": op, "applied": params or {}}

    def all(self):
        return {
            "summary": {"available": True,
                        "service": {"running": True, "unit": "vlmcsd",
                                    "manager": "supervisord", "pid": 10,
                                    "since": "2026-10-09 15:34:40 (up 1 天 9 小时)",
                                    "version": "vlmcsd 5978b33 64-bit",
                                    "binary": "/usr/local/bin/vlmcsd", "reload_pending": False},
                        "stats": {"total": 1284, "today": 37, "today_ok": 35,
                                  "clients_30d": 42, "first": "2026-09-01",
                                  "last": "2026-10-11 00:41:07",
                                  "storage_bytes": 391144},
                        "config": {"Port": "1688", "Listen": "0.0.0.0:1688",
                                   "MaxWorkers": "256", "ConnectionTimeout": "30",
                                   "WhiteListingLevel": "0", "CheckClientTime": "0",
                                   "LogVerbose": "1"},
                        "events": [
                            {"t": "2026-10-11 00:41:07", "ip": "39.144.169.199",
                             "cmid": "a1b2c3d4e5f6", "ok": True},
                            {"t": "2026-10-10 22:13:51", "ip": "59.63.43.227",
                             "cmid": "0f1e2d3c4b5a", "ok": True},
                            {"t": "2026-10-10 19:02:14", "ip": "203.0.113.9",
                             "cmid": "998877665544", "ok": False}]},
            "limits": {"available": True, "enabled": True, "limit": 10, "window": "day",
                       "window_text": "今天", "ban_hours": 24,
                       "skip": ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12",
                                "192.168.0.0/16", "169.254.0.0/16"],
                       "banned": ["203.0.113.9", "198.51.100.7"],
                       "pending_ban": [{"ip": "192.0.2.44"}], "pending_unban": [],
                       "top_ips": [{"ip": "203.0.113.9", "n": 26},
                                   {"ip": "198.51.100.7", "n": 14}]},
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


# --------------------------------------------------------------------------
# 数据聚合：把采集器拼成界面要的几块
# --------------------------------------------------------------------------
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


def _tail_file(path, lines=200):
    """只允许 /var/log 与面板数据目录下的普通文件；解析真实路径挡符号链接逃逸。"""
    if not path:
        return {"ok": False, "error": "没给路径"}
    real = os.path.realpath(path)
    if not any(real.startswith(d) for d in LOG_DIRS):
        return {"ok": False, "error": "只允许读 %s 下的文件" % "、".join(LOG_DIRS)}
    if not os.path.isfile(real):
        return {"ok": False, "error": "不是普通文件：%s" % real}
    try:
        size = os.path.getsize(real)
        # 大文件只读尾巴，别把 1 GB 的日志拖进内存
        with open(real, "r", encoding="utf-8", errors="replace") as fh:
            if size > 4 << 20:
                fh.seek(max(0, size - (1 << 20)))
                fh.readline()
            data = fh.readlines()[-lines:]
    except OSError as e:
        return {"ok": False, "error": "读不了：%s" % e}
    return {"ok": True, "path": real, "size": size,
            "lines": [l.rstrip("\n") for l in data]}


FILE_ROOTS = ("/var/log", "/var/lib/n1bus", "/opt/nimbus", "/etc/caddy", "/etc/vlmcsd",
              "/etc/systemd/system", "/opt/dsh")
FILE_MAX_ENTRIES = 200
FILE_TEXT_LIMIT = 512 * 1024
# 单次上传上限：再大就别走面板了，用 scp。
FILE_UPLOAD_MAX = 64 * 1024 * 1024
# 可在面板里直接编辑的文本上限：再大只给下载。
FILE_EDIT_MAX = 2 * 1024 * 1024


def safe_path(p, want_dir=None):
    """把路径限制在白名单根之下；realpath 之后判断，`..` 与符号链接都逃不掉。"""
    if not p:
        return None
    real = os.path.realpath(str(p))
    for root in FILE_ROOTS:
        rr = os.path.realpath(root)
        if real == rr or real.startswith(rr + os.sep):
            if want_dir is True and not os.path.isdir(real):
                return None
            if want_dir is False and not os.path.isfile(real):
                return None
            return real
    return None


def safe_write_path(p, must_exist=False):
    """写操作的路径闸门。

    和 `safe_path` 的差别：那个要求**目标本身**已在白名单内；新建文件时目标还不存在，
    所以这里判的是**父目录**（同样先 realpath，符号链接照旧逃不掉），并额外挡住两件事：

    * 直接对学生白名单**根**下手（删掉 /var/log 这种整棵树）；
    * `must_exist` 时目标必须真的在。

    返回 (realpath, 错误文案)；成功时错误文案为空串。
    """
    if not str(p or "").strip():
        return None, "路径为空"
    real = os.path.realpath(str(p))
    parent = os.path.dirname(real)
    for root in FILE_ROOTS:
        rr = os.path.realpath(root)
        if parent == rr or parent.startswith(rr + os.sep):
            if real == rr:
                return None, "不能直接操作白名单根目录"
            if must_exist and not os.path.lexists(real):
                return None, "目标不存在"
            return real, ""
    return None, "路径不在允许管理的目录里"


def safe_name(name):
    """文件名合法性：只能是单段名字，不许带路径分隔符或穿越。"""
    s = str(name or "").strip()
    if not s or s in (".", ".."):
        return None, "名字不能为空"
    if os.sep in s or (os.altsep and os.altsep in s) or "/" in s or "\\" in s:
        return None, "名字里不能带路径分隔符"
    if "\x00" in s:
        return None, "名字里有非法字符"
    return s, ""


def _human_size(n):
    """把字节数说成人话（审计与报错文案里用）。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%d %s" % (round(n), unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n



def _size_bytes(text):
    """把 "1.2GB" / "524.3M" / "12kB" 这类输出里的体积转成字节数；认不出就 0。"""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP])(?:i?B)?", str(text or "").upper())
    if not m:
        return 0
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
    return int(float(m.group(1)) * mult[m.group(2)])


class HostProbe:
    """真机数据源。"""

    real = True

    def __init__(self, sampler, audit):
        self.sampler = sampler
        self.audit = audit
        self.services = Services()
        self.docker = Docker()
        self.security = Security()
        self.ports = Ports()
        self.processes = Processes()
        self.info = host_info()

    def overview(self):
        snap = self.sampler.latest()
        procs = self.processes.counts()
        svc = self.services.list() if self.services.available else {"units": [], "available": False}
        dk = self.docker.list(with_stats=False) if self.docker.available else {"containers": [], "available": False}
        logins = self.security.logins(lines=2000, top=3) if os.path.exists("/var/log/auth.log") else {}
        return {
            "info": self.info, "ts": snap.get("ts", ""), "uptime": snap.get("uptime", 0),
            "uptime_text": dur_text(snap.get("uptime", 0)),
            "cpu": {"percent": snap.get("cpu", 0), "cores": snap.get("cores", []),
                    "count": snap.get("cpu_count", 1), "load": snap.get("load", [0, 0, 0])},
            "mem": snap.get("mem", {}), "disk": snap.get("disk", []),
            "net": {"rx_bps": snap.get("rx_bps", 0), "tx_bps": snap.get("tx_bps", 0),
                    "rx_total": snap.get("rx_total", 0), "tx_total": snap.get("tx_total", 0)},
            "procs": procs, "series": self.sampler.series(),
            "services": {"available": svc.get("available", False),
                         "total": len(svc.get("units", [])),
                         "running": svc.get("running", 0), "failed": svc.get("failed", 0)},
            "docker": {"available": dk.get("available", False),
                       "total": len(dk.get("containers", [])),
                       "running": dk.get("running", 0)},
            "security": {"failed": logins.get("failed", 0),
                         "top": (logins.get("top_ips") or [{}])[0].get("ip", ""),
                         "failed_24h_note": "来自 auth.log 全量统计"},
        }

    # ---------------- 系统清理 / 网络 / 磁盘（1Panel 参考切片） ----------------
    def cleanup(self):
        """回收空间的账本：这里只统计，不动手；真正删除一律走白名单动作。"""
        items = []
        code, out, _ = run(["journalctl", "--disk-usage"], timeout=20)
        jb = _size_bytes(out) if code == 0 else 0
        items.append({"key": "journald", "label": "系统日志 journald", "current": jb,
                      "reclaimable": max(0, jb - 200 * 1024 * 1024),
                      "action": "clean.journal", "note": "收缩到 200 MB"})
        code, out, _ = run(["docker", "image", "ls", "--filter", "dangling=true",
                            "--format", "{{.ID}}|{{.Size}}"], timeout=25)
        dang = [l.split("|", 1) for l in out.splitlines() if "|" in l]
        items.append({"key": "docker_images", "label": "悬空镜像", "count": len(dang),
                      "current": 0, "reclaimable": sum(_size_bytes(p[1]) for p in dang),
                      "action": "docker.prune_images", "note": "只删无标签镜像"})
        code, out, _ = run(["docker", "system", "df", "--format",
                            "{{.Type}}|{{.Reclaimable}}"], timeout=25)
        builder = 0
        for line in out.splitlines():
            if line.lower().startswith("build cache"):
                builder = _size_bytes(line.split("|", 1)[-1])
        items.append({"key": "docker_builder", "label": "Docker 构建缓存", "current": 0,
                      "reclaimable": builder, "action": "clean.docker_builder",
                      "note": "通常为 0"})
        code, out, _ = run(["du", "-sk", "/var/cache/apt/archives"], timeout=20)
        kb = 0
        if code == 0 and out.split():
            try:
                kb = int(out.split()[0])
            except ValueError:
                kb = 0
        items.append({"key": "apt_cache", "label": "apt 下载缓存", "current": kb * 1024,
                      "reclaimable": kb * 1024, "action": "clean.apt_cache",
                      "note": "已下载的 .deb"})
        code, out, _ = run(["find", "/var/log", "-type", "f", "-name", "*.gz",
                            "-mtime", "+30", "-printf", "%s\n"], timeout=25)
        sizes = [int(x) for x in out.split() if x.isdigit()]
        items.append({"key": "old_logs", "label": "30 天前的轮转日志", "count": len(sizes),
                      "current": sum(sizes), "reclaimable": sum(sizes),
                      "action": "clean.old_gz_logs",
                      "note": "30 天前的 *.gz"})
        code, out, _ = run(["apt-get", "-s", "autoremove"], timeout=45)
        cand = [l.split()[1] for l in out.splitlines()
                if l.startswith("Remv ") and len(l.split()) > 1]
        snap = self.sampler.latest()
        root = next((d for d in snap.get("disk", []) if d.get("mount") == "/"),
                    (snap.get("disk") or [{}])[0])
        return {"disk": root, "items": items,
                "reclaimable_total": sum((i.get("reclaimable") or 0) for i in items),
                "autoremove_candidates": cand[:40],
                "note": "autoremove 仅列候选，不执行"}

    BACKUP_DIR = "/var/lib/n1bus/backups"
    BACKUP_ARCHIVE = BACKUP_DIR + "/latest.tar.gz"

    def backup_info(self):
        """备份账本：有哪些数据、多大、上次什么时候打的。

        恢复故意不自动化：那要覆盖正在运行的面板数据与容器卷，出事就是自己把自己弄挂。
        页面给出 SSH 恢复命令，人要动手时自己动手。
        """
        try:
            os.makedirs(self.BACKUP_DIR, exist_ok=True)
        except OSError:
            pass
        srcs = [("n1bus", "面板数据（账户 / 审计 / 服务 token）", "/var/lib/n1bus"),
                ("caddy-conf", "Caddy 配置与站点文件", "/opt/nimbus/docker"),
                ("vlmcsd", "vlmcsd 配置", "/etc/vlmcsd")]
        # Docker 卷名带 compose 项目前缀（docker_kms-data…），不能写死 —— 按实际目录列
        code, out, _ = run(["ls", "-1", "/var/lib/docker/volumes"], timeout=20)
        for name in out.split():
            if name in ("backingFsBlockDev", "metadata.db"):
                continue
            srcs.append(("vol:" + name, "Docker 卷 " + name,
                         "/var/lib/docker/volumes/" + name))
        items = []
        for key, label, path in srcs:
            code, out, _ = run(["du", "-sb", path], timeout=25)
            size = 0
            if code == 0 and out.split():
                try:
                    size = int(out.split()[0])
                except ValueError:
                    size = 0
            items.append({"key": key, "label": label, "path": path,
                          "exists": os.path.exists(path), "size": size})
        arc = {"exists": os.path.exists(self.BACKUP_ARCHIVE),
               "path": self.BACKUP_ARCHIVE, "size": 0, "mtime": ""}
        if arc["exists"]:
            try:
                st = os.stat(self.BACKUP_ARCHIVE)
                arc["size"] = st.st_size
                arc["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
            except OSError:
                pass
        return {"items": items, "archive": arc, "action": "backup.create",
                "restore_hint": "tar -xzf latest.tar.gz -C /  然后 systemctl restart n1bus"}

    def files(self, path=""):
        """只读列目录：白名单根之下，最多 200 条。"""
        if not path:
            target = os.path.realpath(FILE_ROOTS[0])
        else:
            # 分三种情况给三种话：越界 / 不存在 / 不是目录。混成一句
            # "不在允许浏览的目录里" 会让人以为是自己打错，其实可能是真越界。
            inside = safe_path(path)
            if inside is None:
                return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                        "truncated": False,
                        "error": "这个位置不在允许浏览的白名单里，一律不可达：%s" % str(path)[:120]}
            if not os.path.lexists(inside):
                return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                        "truncated": False, "error": "没有这个路径：%s" % inside}
            if not os.path.isdir(inside):
                return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                        "truncated": False, "error": "这是个文件，不是目录：%s" % inside}
            target = inside
        if target is None:
            return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                    "truncated": False, "error": "路径不在允许浏览的目录里"}
        entries = []
        try:
            with os.scandir(target) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        entries.append({"name": e.name, "dir": e.is_dir(),
                                        "size": 0 if e.is_dir() else st.st_size,
                                        "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                               time.localtime(st.st_mtime)),
                                        "link": e.is_symlink()})
                    except OSError:
                        continue
        except OSError as ex:
            return {"path": target, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                    "truncated": False, "error": str(ex)[:140]}
        entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        parent = os.path.dirname(target)
        if safe_path(parent, want_dir=True) is None:
            parent = ""
        # 每个白名单根此刻能不能写。服务是 systemd 跑的、带 ProtectSystem=full，
        # /etc 下是只读绑定挂载 —— os.access 能如实反映（那层挂载真的不可写）。
        # 前端据此把"只读"的根标出来，省得用户点了半天才发现存不了。
        roots_writable = {}
        for root in FILE_ROOTS:
            rr = os.path.realpath(root)
            roots_writable[root] = bool(os.path.isdir(rr) and os.access(rr, os.W_OK))
        return {"path": target, "parent": parent, "roots": list(FILE_ROOTS),
                "roots_writable": roots_writable,
                "entries": entries[:FILE_MAX_ENTRIES],
                "truncated": len(entries) > FILE_MAX_ENTRIES, "error": ""}

    def file_tail(self, path="", lines=200):
        """只读预览文本文件末尾若干行；大文件只读尾部，二进制拒绝。"""
        real = safe_path(path, want_dir=False)
        if real is None:
            return {"path": path, "size": 0, "lines": [], "truncated": False,
                    "error": "路径不在允许浏览的目录里，或者不是普通文件"}
        try:
            size = os.path.getsize(real)
            with open(real, "rb") as fh:
                if size > FILE_TEXT_LIMIT:
                    fh.seek(-FILE_TEXT_LIMIT, os.SEEK_END)
                blob = fh.read()
        except OSError as ex:
            return {"path": real, "size": 0, "lines": [], "truncated": False,
                    "error": str(ex)[:140]}
        if b"\x00" in blob[:4096]:
            return {"path": real, "size": size, "lines": [], "truncated": False,
                    "error": "二进制文件，不予预览"}
        try:
            want = max(1, min(5000, int(lines)))
        except (TypeError, ValueError):
            want = 200
        rows = blob.decode("utf-8", "replace").splitlines()[-want:]
        return {"path": real, "size": size, "lines": rows,
                "truncated": size > FILE_TEXT_LIMIT, "error": ""}

    # --- 文件管理（写）----------------------------------------------------
    #
    # 和只读浏览共用同一套白名单闸门（safe_write_path 判**父目录**，因为新建时目标还不存在）。
    # 面板不提供任意命令执行，但"往某个文件写东西"本身就是改变系统状态，所以每个写操作
    # 无论成败都进审计，与动作表同等对待。

    def file_read(self, path=""):
        """读完整文本供编辑。超上限或二进制一律拒绝 —— 让用户去下载，别把面板拖死。"""
        real = safe_path(path, want_dir=False)
        if real is None:
            return {"ok": False, "error": "路径不在允许浏览的目录里，或者不是普通文件"}
        try:
            size = os.path.getsize(real)
            if size > FILE_EDIT_MAX:
                return {"ok": False, "error": "文件 %s，超过面板可编辑上限（%s），请用下载"
                        % (_human_size(size), _human_size(FILE_EDIT_MAX))}
            with open(real, "rb") as fh:
                blob = fh.read()
        except OSError as ex:
            return {"ok": False, "error": str(ex)[:140]}
        if b"\x00" in blob[:8192]:
            return {"ok": False, "error": "二进制文件，不能在这编辑（可以下载）"}
        return {"ok": True, "path": real, "size": size,
                "text": blob.decode("utf-8", "replace")}

    def _backup(self, real):
        """覆盖前留一份 .bak。失败不致命 —— 不该因为备份不了就不让用户存。"""
        try:
            if os.path.isfile(real):
                shutil.copy2(real, real + ".bak")
                return True
        except OSError:
            pass
        return False

    def file_write(self, path="", text="", ip=""):
        """保存文本（新建或覆盖）。"""
        real, err = safe_write_path(path)
        if err:
            return {"ok": False, "error": err}
        blob = str(text).encode("utf-8")
        if len(blob) > FILE_EDIT_MAX:
            return {"ok": False, "error": "内容超过面板可写上限（%s）" % _human_size(FILE_EDIT_MAX)}
        existed = os.path.isfile(real)
        backed = self._backup(real)
        try:
            with open(real, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as ex:
            self.audit.write("file.write", real, False, str(ex)[:120], ip)
            return {"ok": False, "error": str(ex)[:140]}
        self.audit.write("file.write", real, True,
                         "%s %d 字节%s" % ("覆盖" if existed else "新建", len(blob),
                                           "（已留 .bak）" if backed else ""), ip)
        return {"ok": True, "path": real, "size": len(blob), "backed": backed}

    def file_upload(self, path="", data=b"", ip=""):
        """上传（或覆盖）一个文件。父目录不存在就连带建出来。"""
        real, err = safe_write_path(path)
        if err:
            return {"ok": False, "error": err}
        data = bytes(data or b"")
        if len(data) > FILE_UPLOAD_MAX:
            return {"ok": False, "error": "超过单次上传上限（%s）" % _human_size(FILE_UPLOAD_MAX)}
        parent = os.path.dirname(real)
        existed = os.path.isfile(real)
        backed = self._backup(real)
        try:
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(real, "wb") as fh:
                fh.write(data)
        except OSError as ex:
            self.audit.write("file.upload", real, False, str(ex)[:120], ip)
            return {"ok": False, "error": str(ex)[:140]}
        self.audit.write("file.upload", real, True,
                         "%s %d 字节" % ("覆盖" if existed else "新建", len(data)), ip)
        return {"ok": True, "path": real, "size": len(data), "overwrote": existed}

    def file_mkdir(self, path="", ip=""):
        """新建目录（只建一层，父目录必须在白名单内）。"""
        real, err = safe_write_path(path)
        if err:
            return {"ok": False, "error": err}
        try:
            os.mkdir(real)
        except OSError as ex:
            self.audit.write("file.mkdir", real, False, str(ex)[:120], ip)
            return {"ok": False, "error": str(ex)[:140]}
        self.audit.write("file.mkdir", real, True, "", ip)
        return {"ok": True, "path": real}

    def file_touch(self, path="", ip=""):
        """新建空文件（已存在就只更新时间戳）。"""
        real, err = safe_write_path(path)
        if err:
            return {"ok": False, "error": err}
        existed = os.path.lexists(real)
        try:
            if existed:
                os.utime(real, None)
            else:
                with open(real, "a", encoding="utf-8"):
                    pass
        except OSError as ex:
            self.audit.write("file.touch", real, False, str(ex)[:120], ip)
            return {"ok": False, "error": str(ex)[:140]}
        self.audit.write("file.touch", real, True, "已存在" if existed else "", ip)
        return {"ok": True, "path": real, "existed": existed}

    def file_rename(self, path="", new_name="", ip=""):
        """原地改名（不移动目录）。目标已存在则拒绝，不静默覆盖。"""
        real, err = safe_write_path(path, must_exist=True)
        if err:
            return {"ok": False, "error": err}
        name, nerr = safe_name(new_name)
        if nerr:
            return {"ok": False, "error": nerr}
        dst = os.path.join(os.path.dirname(real), name)
        dst_real, derr = safe_write_path(dst)
        if derr:
            return {"ok": False, "error": derr}
        if os.path.lexists(dst_real):
            return {"ok": False, "error": "同名目标已存在"}
        try:
            os.rename(real, dst_real)
        except OSError as ex:
            self.audit.write("file.rename", real, False, str(ex)[:120], ip)
            return {"ok": False, "error": str(ex)[:140]}
        self.audit.write("file.rename", "%s → %s" % (real, dst_real), True, "", ip)
        return {"ok": True, "path": dst_real}

    def file_delete(self, path="", recursive=False, ip=""):
        """删除。目录默认只允许空目录 —— 要整棵删掉必须显式 recursive。"""
        real, err = safe_write_path(path, must_exist=True)
        if err:
            return {"ok": False, "error": err}
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                if recursive:
                    shutil.rmtree(real)
                else:
                    if os.listdir(real):
                        return {"ok": False, "error": "目录非空；要连内容一起删得勾选「连同内容」"}
                    os.rmdir(real)
            else:
                os.remove(real)
        except OSError as ex:
            self.audit.write("file.delete", real, False, str(ex)[:120], ip)
            return {"ok": False, "error": str(ex)[:140]}
        self.audit.write("file.delete", real, True,
                         "连同子目录" if recursive and os.path.isdir(real) else "", ip)
        return {"ok": True, "path": real}

    def autostart(self):
        """开机自启项：用户关心的 systemd 服务 + 容器的 restart policy。

        两个要点：
        * 查"全部服务"而不是只查 --state=enabled —— 否则刚被停掉自启的单元会从列表里消失，
          用户看到的不是"按钮变成「自启」"，而是"这一行没了"。
        * 只列**用户/第三方装的**单元（判据不是名字前缀，漏得多，而是**单元文件在哪**）：
          直接从 /etc/systemd/system 读，一次 scandir ~1 ms；以前跑
          `systemctl list-unit-files` 要 0.95 s（带 --state filter 1.84 s），
          而每一次写操作后缓存失效都要重付一遍 —— 那才是"点完按钮等一秒"的真凶。
          受保护单元始终保留，免得它们从界面上消失。
        """
        names = _etc_service_units()
        if names is None:                    # 目录读不到：退回全量扫描，慢但不会瞎
            names = set(_all_service_units())
        else:
            names |= PROTECTED_UNITS
        props = _unit_props(sorted(names))
        units = []
        for unit in sorted(names):
            state, path = props.get(unit, ("", ""))
            # path 空 = systemd 不认这个单元：/etc/systemd/system 里的坏链接、alias 名
            # （dbus-org.freedesktop.resolve1 这种）、或者刚放进去还没 daemon-reload。
            # 面板上不该出现 systemd 自己都不认的东西（旧版走 list-unit-files 也看不到）。
            if not path:
                continue
            if unit not in PROTECTED_UNITS and path.startswith(DISTRO_UNIT_DIRS):
                continue                     # 本体在 /lib、/usr/lib、/run：发行版自带，不列
            units.append({"unit": unit, "state": state, "enabled": state == "enabled",
                          # 只有 enabled / disabled 才真能用 systemctl 开关。
                          # static（snap.lxd.daemon 这种没有 [Install] 段的）enable 会失败；
                          # masked 同理。给它们画按钮 = 点了必然失败 + 状态跳回去，纯误导。
                          "togglable": state in ("enabled", "disabled"),
                          "protected": unit in PROTECTED_UNITS})
        _, act, _ = run(["systemctl", "list-units", "--type=service", "--state=running",
                         "--no-legend", "--no-pager", "--plain"], timeout=25)
        running = set()
        for line in act.splitlines():
            sp = line.split()
            if sp:
                running.add(sp[0])
        for u in units:
            u["running"] = u["unit"] in running
        # 已启用的排前面（那是用户要管的），其余按名字
        units.sort(key=lambda u: (not u["enabled"], not u["running"], u["unit"]))
        _, cnames, _ = run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=25)
        boxes = _docker_autostart(cnames.split()) if cnames.split() else []
        return {"units": units, "containers": boxes,
                "note": "ssh / 网络 / docker / 面板等受保护，不允许取消自启"}

    def sys_settings(self):
        """时区 / NTP / swap。swap 只读：新建 swap 是分区与文件系统级操作，面板不做。"""
        kv = {}
        code, out, _ = run(["timedatectl", "show"], timeout=20)
        if code == 0:
            for line in out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
        swaps = []
        _, sw, _ = run(["swapon", "--show", "--bytes", "--noheadings"], timeout=20)
        for line in sw.splitlines():
            p = line.split()
            if len(p) >= 4:
                swaps.append({"device": p[0], "type": p[1],
                              "size": int(p[2]) if p[2].isdigit() else 0,
                              "used": int(p[3]) if p[3].isdigit() else 0})
        snap = self.sampler.latest()
        mem = snap.get("mem", {})
        root = next((d for d in snap.get("disk", []) if d.get("mount") == "/"), {})
        return {"timezone": kv.get("Timezone", ""),
                "ntp": kv.get("NTP", "") == "yes",
                "ntp_synced": kv.get("NTPSynchronized", "") == "yes",
                "rtc_local": kv.get("LocalRTC", "") == "yes",
                "now": kv.get("TimeUSec", ""),
                "swaps": swaps, "swap_total": mem.get("swap_total", 0),
                "swap_used": mem.get("swap_used", 0),
                "swapfile_exists": os.path.exists("/swapfile"),
                "root_free": root.get("free", 0),
                "timezones_note": "常见值：Asia/Shanghai、UTC、Asia/Tokyo",
                "note": "swap 只读（建 swap 要动分区）"}

    def netinfo(self):
        """网络只读：网卡 / 路由 / DNS / Docker 发布端口。改网络会断线，本期不做写。"""
        def jrun(argv, timeout=20):
            code, out, _ = run(argv, timeout=timeout)
            if code != 0:
                return None
            try:
                return json.loads(out or "null")
            except ValueError:
                return None

        ifaces = []
        for l in (jrun(["ip", "-j", "addr"]) or []):
            name = l.get("ifname", "")
            addrs = ["%s/%s" % (a.get("local", ""), a.get("prefixlen", ""))
                     for a in (l.get("addr_info") or [])]
            # 分三类，因为它们在界面上的价值差得很远：物理网卡要一眼看到，
            # 回环是常识不必强调，而 veth* 是 Docker 给每个容器拉的一根线 ——
            # 数量会随容器数增长、每次重建都换名字、地址栏还常常是空的。
            # 不分类就全部平铺，一屏里大半是这种噪音。
            if name == "lo":
                kind = "loopback"
            elif name.startswith(("veth", "br-", "docker", "virbr", "tun", "tap")):
                kind = "virtual"
            else:
                kind = "physical"
            ifaces.append({"name": name, "up": l.get("operstate") == "UP", "kind": kind,
                           "mac": l.get("address", ""), "mtu": l.get("mtu", 0),
                           "addrs": addrs})
        routes = [{"dst": r.get("dst", ""), "gateway": r.get("gateway", ""),
                   "dev": r.get("dev", ""), "metric": r.get("metric", 0)}
                  for r in (jrun(["ip", "-j", "route"]) or [])]
        rc = read_text("/etc/resolv.conf")
        ns = [l.split()[-1] for l in rc.splitlines()
              if l.strip().startswith("nameserver") and l.split()]
        dk = self.docker.list(with_stats=False) if self.docker.available else {}
        pub = [{"container": c.get("name", ""), "ports": c.get("ports", "")}
               for c in dk.get("containers", []) if c.get("ports")]
        return {"interfaces": ifaces, "routes": routes, "nameservers": ns,
                "resolv_conf": rc.strip()[:600], "docker_published": pub,
                "note": "Docker 发布的端口不经过 ufw，实际对外暴露 = ufw 放行 ∪ Docker 发布"}

    def disks(self):
        """磁盘与挂载只读：lsblk 树 + df；SMART 要 smartmontools，没装就说明。"""
        tree = None
        code, out, _ = run(["lsblk", "-J", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,MODEL"],
                           timeout=20)
        if code == 0:
            try:
                tree = json.loads(out).get("blockdevices", [])
            except ValueError:
                tree = None
        snap = self.sampler.latest()
        has_smart = shutil.which("smartctl") is not None
        return {"blockdevices": tree or [], "mounts": snap.get("disk", []),
                "smart_available": has_smart,
                # 只说"未安装 smartmontools"，用户不知道这意味着少了什么。
                # 写清代价：这个面板不做硬盘健康预测，仅此而已。
                "smart_note": "" if has_smart else
                "未安装 smartmontools，因此不提供硬盘健康（SMART）信息"}

        


class _DemoServices:
    """演示用的假 systemd。数据结构与真机一致，界面代码完全不用分叉。"""

    available = True
    hint = ""
    UNITS = [
        ("ssh.service", "active", "running", "OpenBSD Secure Shell server"),
        ("cron.service", "active", "running", "Regular background program processing daemon"),
        ("docker.service", "active", "running", "Docker Application Container Engine"),
        ("containerd.service", "active", "running", "containerd container runtime"),
        ("fail2ban.service", "failed", "failed", "Fail2Ban Service"),
        ("nginx.service", "inactive", "dead", "A high performance web server"),
    ]

    def list(self):
        units = [{"unit": u, "load": "loaded", "active": a, "sub": s, "desc": d}
                 for u, a, s, d in self.UNITS]
        return {"available": True, "units": units, "error": "",
                "running": sum(1 for u in units if u["active"] == "active"),
                "failed": sum(1 for u in units if u["active"] == "failed")}

    def detail(self, unit):
        u = next((x for x in self.UNITS if x[0] == unit), self.UNITS[0])
        return {"ok": True, "unit": u[0], "desc": u[3], "active": u[1], "sub": u[2],
                "pid": "1371" if unit == "ssh.service" else "602", "since": "2026-10-09 03:12:41 CST",
                "enabled": "enabled", "restarts": "0", "fragment": "/lib/systemd/system/" + u[0],
                "memory": 6 << 20, "cpu_sec": 1.42}

    def logs(self, unit, lines=200):
        return {"available": True, "lines": [
            "2026-10-09T03:12:41+0800 %s Starting %s..." % (unit, unit),
            "2026-10-09T03:12:41+0800 %s Started %s." % (unit, unit),
            "2026-10-09T03:15:02+0800 %s 演示模式：这里会是真实日志" % unit,
        ], "error": ""}


class _DemoDocker:
    available = True
    hint = ""

    def list(self, with_stats=True):
        cs = [("kms", "v1m-dash:local", "Up 3 minutes (healthy)", True),
              ("caddy", "caddy:2-alpine", "Up 26 minutes", True),
              ("old-test", "alpine:3.19", "Exited (0) 2 days ago", False)]
        out = []
        for name, image, status, running in cs:
            out.append({"id": (name + "0123456789ab")[:12], "name": name, "image": image,
                        "state": "running" if running else "exited", "status": status,
                        "ports": "1688:1688" if name == "kms" else ("80:80, 443:443" if name == "caddy" else ""),
                        "running": running,
                        "stats": {"cpu": "0.42%", "mem": "48MiB / 1.94GiB", "mem_pct": "2.4%",
                                  "net": "1.2MB / 860kB", "pids": "3"} if running else {}})
        return {"available": True, "containers": out, "error": "",
                "running": sum(1 for c in out if c["running"])}

    def dsh(self):
        # 演示模式给一个一眼假的 token，别让它在真机上能用
        return {"available": True, "container": DSH_CONTAINER, "state": "running",
                "started": "2026-10-10T10:35:00Z",
                "url": DSH_PUBLIC_BASE + "?token=demo-not-a-real-token-0000",
                "hint": "", "error": ""}

    def logs(self, name, lines=200, since="", until="", grep="", timestamps=False):
        # 校验与真机一致：演示模式也不能对非法参数、非法容器名睁一只眼
        if not NAME_RE.match(name or ""):
            return {"available": False, "lines": [], "cmd": "", "error": "容器名不合法"}
        for flag, val in (("--since", since), ("--until", until)):
            v = (val or "").strip()
            if v and not LOG_TIME_RE.match(v):
                return {"available": False, "lines": [], "cmd": "",
                        "error": "%s 只接受 30m / 2h / 1d 或 2026-10-09T05:00 这样的时间" % flag}
        try:
            lines = max(1, min(int(lines), 5000))
        except (TypeError, ValueError):
            lines = 200
        rows = [
            "[entrypoint] reusing token from /var/lib/nimbus/nimbus.token",
            "nimbus 0.2.0  [real 模式]",
            "vlmcsd 5978b33 started successfully",
            "(演示模式：%s 的真实日志会出现在这里)" % name,
        ]
        kw = (grep or "").strip()
        if kw:
            rows = [l for l in rows if kw.lower() in l.lower()]
        tail = " ".join(x for x in [("--since " + since.strip()) if since.strip() else "",
                                    ("--until " + until.strip()) if until.strip() else "",
                                    "-t" if timestamps else "",
                                    "--tail %s" % lines] if x)
        return {"available": True, "lines": rows[-lines:], "grep": kw, "matched": len(rows),
                "cmd": "docker logs %s %s" % (tail, name), "error": ""}

    def images(self):
        return {"available": True, "error": "", "images": [
            {"repo": "v1m-dash", "tag": "local", "id": "487970c6a7a8", "size": "72.4MB", "created": "10 minutes ago"},
            {"repo": "caddy", "tag": "2-alpine", "id": "1f534b35d111", "size": "51.2MB", "created": "3 days ago"},
            {"repo": "<none>", "tag": "<none>", "id": "9a8b7c6d5e4f", "size": "7.8MB", "created": "2 weeks ago"},
        ]}


class _DemoSecurity:
    def logins(self, lines=0, top=0, recent=10):
        # 演示数据刻意混合"已封禁 / 未封禁"两种状态，使默认的"只看未封禁"视图有内容；
        # 另外保留若干远超阈值的未封禁来源，用于展示「自动永久封禁」的待封名单。
        fails = [{"ts": "Oct  9 03:19:48", "ip": "109.160.32.137", "user": "s10emmanuel"},
                 {"ts": "Oct  9 03:19:50", "ip": "109.160.32.110", "user": "kernbm"},
                 {"ts": "Oct  9 03:20:11", "ip": "109.160.32.137", "user": "postgres"},
                 {"ts": "Oct  9 03:20:33", "ip": "45.148.10.76", "user": "admin"}]
        return {"available": True, "failed": 1284, "accepted": 6, "lines_scanned": 12470,
                "ok_ips": ["39.144.169.199", "192.168.1.9"],
                "top_ips": [{"ip": "109.160.32.137", "n": 742}, {"ip": "109.160.32.110", "n": 388},
                            {"ip": "45.148.10.76", "n": 154}, {"ip": "177.104.140.2", "n": 52},
                            {"ip": "27.102.116.43", "n": 46}, {"ip": "62.60.130.253", "n": 43}],
                "top_users": [{"user": "s10emmanuel", "n": 210}, {"user": "kernbm", "n": 190},
                              {"user": "admin", "n": 120}, {"user": "postgres", "n": 88},
                              {"user": "root", "n": 64}, {"user": "ubuntu", "n": 41}],
                "recent": fails[-max(1, recent):][::-1], "recent_ok": [
                    {"ts": "Oct  9 03:02:10", "ip": "39.144.169.199", "user": "root"}]}

    def firewall(self):
        return {"available": True, "active": True,
                "rules": ["80/tcp                     ALLOW IN    Anywhere",
                          "443/tcp                    ALLOW IN    Anywhere",
                          "1688/tcp                   ALLOW IN    Anywhere",
                          "8402/tcp                   ALLOW IN    172.18.0.0/16",
                          "11451/tcp                  ALLOW IN    Anywhere",
                          "Anywhere                   DENY IN     109.160.32.137"],
                "raw": "Status: active\n\nTo                         Action      From\n"
                       "--                         ------      ----\n"
                       "80/tcp                     ALLOW IN    Anywhere\n"
                       "443/tcp                    ALLOW IN    Anywhere\n"
                       "1688/tcp                   ALLOW IN    Anywhere\n"
                       "8402/tcp                   ALLOW IN    172.18.0.0/16\n"
                       "11451/tcp                  ALLOW IN    Anywhere\n"
                       "\n（演示数据）",
                "error": ""}

    def allowed(self, ports=None):
        """演示数据：入站与出站都有，IPv4/IPv6 已合并，并各留一条待处理项。"""
        return {"available": True, "active": True,
                "policy": "deny (incoming), allow (outgoing), deny (routed)",
                "rules": [
                    {"to": "80/tcp", "action": "ALLOW", "dir": "in", "from": "Anywhere",
                     "v6": False, "port": "80", "proto": "tcp", "simple": True},
                    {"to": "80/tcp", "action": "ALLOW", "dir": "in", "from": "Anywhere (v6)",
                     "v6": True, "port": "80", "proto": "tcp", "simple": True},
                    {"to": "8402/tcp", "action": "ALLOW", "dir": "in", "from": "172.18.0.0/16",
                     "v6": False, "port": "8402", "proto": "tcp", "simple": True},
                    {"to": "53", "action": "ALLOW", "dir": "out", "from": "Anywhere",
                     "v6": False, "port": "", "proto": "", "simple": False},
                ],
                "allowed": [
                    {"port": "80", "proto": "tcp", "dir": "in", "from": "Anywhere",
                     "v4": True, "v6": True, "count": 2, "listen": "docker-proxy",
                     "docker": True},
                    {"port": "8402", "proto": "tcp", "dir": "in", "from": "172.18.0.0/16",
                     "v4": True, "v6": False, "count": 1, "listen": "python3",
                     "docker": False},
                    {"port": "9000", "proto": "tcp", "dir": "in", "from": "Anywhere",
                     "v4": True, "v6": False, "count": 1, "listen": "", "docker": False},
                    {"port": "123", "proto": "udp", "dir": "out", "from": "Anywhere",
                     "v4": True, "v6": False, "count": 1, "listen": "", "docker": False},
                ],
                # 放行了但本机没有服务在听
                "unmatched": [{"port": "9000", "proto": "tcp", "dir": "in",
                               "from": "Anywhere", "v4": True, "v6": False, "count": 1}],
                # 有服务对外监听但没有放行规则
                "uncovered": [
                    {"port": "11451", "proto": "tcp", "process": "sshd", "pid": "1371",
                     "addr": "0.0.0.0", "docker": False},
                    {"port": "3000", "proto": "tcp", "process": "docker-proxy", "pid": "41022",
                     "addr": "0.0.0.0", "docker": True},
                ],
                "error": ""}

    def fail2ban(self):
        # 给出一个 jail 的真实格式输出：这张卡片的正文靠解析这些计数行来生成
        return {"available": True, "jails": ["sshd"], "error": "",
                "detail": {"sshd": (
                    "Status for the jail: sshd\n"
                    "|- Filter\n"
                    "|  |- Currently failed:\t2\n"
                    "|  |- Total failed:\t1809\n"
                    "|  `- Journal matches:\t_SYSTEMD_UNIT=sshd.service + _COMM=sshd\n"
                    "`- Actions\n"
                    "   |- Currently banned:\t7\n"
                    "   |- Total banned:\t249\n"
                    "   `- Banned IP list:\t154.18.197.29 45.148.10.157 193.47.62.69 "
                    "62.60.130.242 92.118.39.50 45.148.10.141 183.242.200.142")}}

    def blocked(self, f2b=None):
        return {"109.160.32.137": "ufw"}

    def sshd(self):
        return {"config": {"Port": "22", "PermitRootLogin": "yes", "PasswordAuthentication": "yes",
                           "PubkeyAuthentication": "yes", "PermitEmptyPasswords": "no",
                           "MaxAuthTries": "6", "ChallengeResponseAuthentication": "(默认)",
                           "AllowUsers": "(默认)", "KbdInteractiveAuthentication": "(默认)"},
                "authorized_keys": 0,
                "note": "authorized_keys 为空时不要关密码登录，否则会把自己锁在外面"}


class _DemoPorts:
    PORTS = [("tcp", "0.0.0.0:22", "22", "sshd", "1371", True),
             ("tcp", "0.0.0.0:80", "80", "docker-proxy", "27973", True),
             ("tcp", "0.0.0.0:443", "443", "docker-proxy", "27979", True),
             ("tcp", "0.0.0.0:1688", "1688", "docker-proxy", "37348", True),
             ("tcp", "127.0.0.1:8099", "8099", "docker-proxy", "37355", False),
             ("tcp", "127.0.0.1:8402", "8402", "python3", "41022", False)]

    def listening(self):
        ports = [{"proto": p, "local": l, "addr": l.rsplit(":", 1)[0] or "*", "port": port,
                  "process": proc, "pid": pid, "exposed": exp}
                 for p, l, port, proc, pid, exp in self.PORTS]
        return {"available": True, "ports": ports, "error": "",
                "exposed": sum(1 for p in ports if p["exposed"])}

    def established(self, limit=40):
        return {"connections": [{"proto": "tcp", "local": "38.76.207.46:22", "peer": "39.144.169.199:51234"}],
                "error": ""}


class _DemoProcesses:
    def top(self, by="cpu", n=15):
        rows = [("41022", "root", 4.2, 1.8, 42 << 20, 320, "R", "python3", "/usr/bin/python3 /opt/n1bus/n1bus.py"),
                ("27979", "root", 0.8, 0.4, 18 << 20, 1560, "S", "docker-proxy", "/usr/bin/docker-proxy -proto tcp"),
                ("602", "systemd+", 0.3, 0.6, 24 << 20, 4200, "S", "systemd-resolve", "/lib/systemd/systemd-resolved")]
        return {"top": [{"pid": r[0], "ppid": "1", "user": r[1], "cpu": r[2], "mem": r[3], "rss": r[4],
                         "etimes": r[5], "stat": r[6], "comm": r[7], "args": r[8]} for r in rows],
                "error": ""}

    def counts(self):
        return {"total": 138, "zombie": 1}


class DemoProbe:
    """演示数据源：没在 Linux 上也能把界面看全。"""

    real = False

    def __init__(self, sampler, audit):
        self.sampler = sampler
        self.audit = audit
        self.services = _DemoServices()
        self.docker = _DemoDocker()
        self.security = _DemoSecurity()
        self.ports = _DemoPorts()
        self.processes = _DemoProcesses()
        self.info = {"hostname": "demo-host", "os": "Ubuntu 22.04.4 LTS (演示)",
                     "kernel": "Linux 5.15.0-generic", "arch": "x86_64",
                     "python": sys.version.split()[0]}

    def overview(self):
        snap = self.sampler.latest()
        return {
            "info": self.info, "ts": snap.get("ts", ""), "uptime": snap.get("uptime", 0),
            "uptime_text": dur_text(snap.get("uptime", 0)),
            "cpu": {"percent": snap.get("cpu", 0), "cores": snap.get("cores", []),
                    "count": snap.get("cpu_count", 1), "load": snap.get("load", [0, 0, 0])},
            "mem": snap.get("mem", {}), "disk": snap.get("disk", []),
            "net": {"rx_bps": snap.get("rx_bps", 0), "tx_bps": snap.get("tx_bps", 0),
                    "rx_total": snap.get("rx_total", 0), "tx_total": snap.get("tx_total", 0)},
            "procs": snap.get("procs", {}) if isinstance(snap.get("procs"), dict)
                     else {"total": snap.get("procs", 0), "zombie": 0},
            "series": self.sampler.series(),
            "services": {"available": True, "total": 6, "running": 5, "failed": 1},
            "docker": {"available": True, "total": 3, "running": 2},
            "security": {"failed": 1284, "top": "109.160.32.137", "failed_24h_note": "演示数据"},
        }

    def cleanup(self):
        return {"disk": {"mount": "/", "device": "/dev/vda1", "total": 31 * 1024 ** 3,
                         "used": 5 * 1024 ** 3, "free": 25 * 1024 ** 3, "percent": 17.5,
                         "inodes_percent": 8.0},
                "items": [
                    {"key": "journald", "label": "系统日志 journald", "current": 524288000,
                     "reclaimable": 314572800, "action": "clean.journal",
                     "note": "收缩到 200 MB"},
                    {"key": "docker_images", "label": "悬空镜像", "count": 3, "current": 0,
                     "reclaimable": 104857600, "action": "docker.prune_images",
                     "note": "只删无标签镜像"},
                    {"key": "docker_builder", "label": "Docker 构建缓存", "current": 0,
                     "reclaimable": 0, "action": "clean.docker_builder",
                     "note": "通常为 0"},
                    {"key": "apt_cache", "label": "apt 下载缓存", "current": 52428800,
                     "reclaimable": 52428800, "action": "clean.apt_cache",
                     "note": "已下载的 .deb"},
                    {"key": "old_logs", "label": "30 天前的轮转日志", "count": 4,
                     "current": 8388608, "reclaimable": 8388608, "action": "clean.old_gz_logs",
                     "note": "30 天前的 *.gz"}],
                "reclaimable_total": 480 * 1024 ** 2,
                "autoremove_candidates": ["linux-image-5.4.0-109-generic",
                                          "linux-headers-5.4.0-109"],
                "note": "autoremove 仅列候选，不执行"}

    def files(self, path=""):
        p = path or "/var/log"
        return {"path": p, "parent": "/var" if p != "/var/log" else "",
                "roots": list(FILE_ROOTS),
                "roots_writable": {r: True for r in FILE_ROOTS},
                "truncated": False, "error": "",
                "entries": [{"name": "auth.log", "dir": False, "size": 524288,
                             "mtime": "2026-10-09 06:30", "link": False},
                            {"name": "syslog", "dir": False, "size": 1048576,
                             "mtime": "2026-10-09 06:31", "link": False},
                            {"name": "journal", "dir": True, "size": 0,
                             "mtime": "2026-10-08 12:00", "link": False}]}

    def file_tail(self, path="", lines=200):
        return {"path": path or "/var/log/auth.log", "size": 524288,
                "lines": ["Oct  9 06:31:15 demo-host sshd[1234]: Failed password for root "
                          "from 109.160.32.137 port 51234 ssh2",
                          "Oct  9 06:31:20 demo-host sshd[1234]: Accepted password for natsusa "
                          "from 39.144.169.199 port 51235 ssh2"],
                "truncated": False, "error": ""}

    # --- 文件管理（demo：只回话，绝不碰真文件系统）----------------------

    def file_read(self, path=""):
        return {"ok": True, "path": path or "/var/log/auth.log", "size": 96,
                "text": "Oct  9 06:31:15 demo-host sshd[1234]: Failed password for root\n"
                        "Oct  9 06:31:20 demo-host sshd[1234]: Accepted password for natsusa\n"}

    def file_write(self, path="", text="", ip=""):
        return {"ok": True, "path": path, "size": len(str(text).encode("utf-8")), "backed": False}

    def file_upload(self, path="", data=b"", ip=""):
        return {"ok": True, "path": path, "size": len(data or b""), "overwrote": False}

    def file_mkdir(self, path="", ip=""):
        return {"ok": True, "path": path}

    def file_touch(self, path="", ip=""):
        return {"ok": True, "path": path, "existed": False}

    def file_rename(self, path="", new_name="", ip=""):
        return {"ok": True, "path": os.path.join(os.path.dirname(path or "/var/log"),
                                                 new_name or "demo")}

    def file_delete(self, path="", recursive=False, ip=""):
        return {"ok": True, "path": path}

    def autostart(self):
        return {"units": [
            {"unit": "ssh.service", "state": "enabled", "enabled": True, "protected": True,
             "running": True},
            {"unit": "docker.service", "state": "enabled", "enabled": True, "protected": True,
             "running": True},
            {"unit": "n1bus.service", "state": "enabled", "enabled": True, "protected": True,
             "running": True},
            {"unit": "fail2ban.service", "state": "enabled", "enabled": True, "protected": False,
             "running": True},
            # 一个"未启用"的单元 —— 界面上它应该显示「自启」按钮
            {"unit": "nginx.service", "state": "disabled", "enabled": False, "protected": False,
             "running": False}],
            "containers": [{"name": "caddy", "policy": "unless-stopped", "autostart": True},
                           {"name": "kms", "policy": "always", "autostart": True}],
            "note": "ssh / 网络 / docker / 面板等受保护，不允许取消自启"}

    def sys_settings(self):
        return {"timezone": "Etc/UTC", "ntp": True, "ntp_synced": True, "rtc_local": False,
                "now": "Fri 2026-10-09 06:40:00 UTC", "swaps": [], "swap_total": 0,
                "swap_used": 0, "swapfile_exists": False, "root_free": 26 * 1024 ** 3,
                "timezones_note": "常见值：Asia/Shanghai、UTC、Asia/Tokyo",
                "note": "swap 只读（建 swap 要动分区）"}

    def backup_info(self):
        return {"items": [
            {"key": "n1bus", "label": "面板数据（账户 / 审计 / 服务 token）",
             "path": "/var/lib/n1bus", "exists": True, "size": 262144},
            {"key": "vol:docker_kms-data", "label": "Docker 卷 docker_kms-data",
             "path": "/var/lib/docker/volumes/docker_kms-data", "exists": True, "size": 1048576},
            {"key": "vol:docker_caddy-data", "label": "Docker 卷 docker_caddy-data",
             "path": "/var/lib/docker/volumes/docker_caddy-data", "exists": True, "size": 4194304},
            {"key": "caddy-conf", "label": "Caddy 配置与站点文件",
             "path": "/opt/nimbus/docker", "exists": True, "size": 40960},
            {"key": "vlmcsd", "label": "vlmcsd 配置", "path": "/etc/vlmcsd",
             "exists": True, "size": 8192}],
            "archive": {"exists": True, "path": "/var/lib/n1bus/backups/latest.tar.gz",
                        "size": 1372160, "mtime": "2026-10-09 06:40:00"},
            "action": "backup.create",
            "restore_hint": "tar -xzf latest.tar.gz -C /  然后 systemctl restart n1bus"}

    def netinfo(self):
        # 演示数据也带上 kind，好让界面默认收起虚拟网卡这条逻辑也能被看到
        return {"interfaces": [
            {"name": "eth0", "up": True, "kind": "physical", "mac": "00:16:3e:1f:2a:3b",
             "mtu": 1500, "addrs": ["10.0.0.4/24", "fe80::1/64"]},
            {"name": "lo", "up": True, "kind": "loopback", "mac": "00:00:00:00:00:00",
             "mtu": 65536, "addrs": ["127.0.0.1/8"]},
            {"name": "docker0", "up": True, "kind": "virtual", "mac": "02:42:1a:2b:3c:4d",
             "mtu": 1500, "addrs": ["172.17.0.1/16"]},
            {"name": "veth1a2b3c4d", "up": True, "kind": "virtual", "mac": "aa:bb:cc:dd:ee:01",
             "mtu": 1500, "addrs": []},
            {"name": "veth5e6f7a8b", "up": True, "kind": "virtual", "mac": "aa:bb:cc:dd:ee:02",
             "mtu": 1500, "addrs": []}],
                "routes": [{"dst": "default", "gateway": "10.0.0.1", "dev": "eth0", "metric": 100}],
                "nameservers": ["8.8.8.8", "1.1.1.1"],
                "resolv_conf": "nameserver 8.8.8.8\nnameserver 1.1.1.1",
                "docker_published": [{"container": "caddy", "ports": "0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp"},
                                     {"container": "kms", "ports": "0.0.0.0:1688->1688/tcp"}],
                "note": "Docker 发布的端口不经过 ufw，实际对外暴露 = ufw 放行 ∪ Docker 发布"}

    def disks(self):
        return {"blockdevices": [
            {"name": "vda", "type": "disk", "size": "30G", "model": "VIRTUAL DISK", "children": [
                {"name": "vda1", "type": "part", "size": "29.9G", "fstype": "ext4",
                 "mountpoint": "/"},
                {"name": "vda15", "type": "part", "size": "105M", "fstype": "vfat",
                 "mountpoint": "/boot/efi"}]}],
            "mounts": self.sampler.latest().get("disk", []),
            "smart_available": False, "smart_note": "演示模式没有 SMART"}

        


# --------------------------------------------------------------------------
# 计划任务与告警：面板自己起后台线程，不碰系统 crontab
# --------------------------------------------------------------------------
class Tasks:
    """面板内的计划任务表。

    任务只能调用 ACTIONS 里已有的动作 —— 不写系统 crontab、不落任何脚本文件。
    代价是：面板停着的时候任务也不会跑（页面上写明了）。
    """

    MIN_EVERY = 60
    TICK = 30

    def __init__(self, data_dir, audit, probe):
        self.path = os.path.join(str(data_dir), "tasks.json")
        self.audit = audit
        self.probe = probe
        self.lock = threading.Lock()
        self.items = self._load()
        self.recent = []
        self.stop = threading.Event()
        self._seq = max([int(t.get("id") or 0) for t in self.items] or [0])

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        return [t for t in (data if isinstance(data, list) else [])
                if isinstance(t, dict) and t.get("action") in ACTIONS]

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def listing(self):
        return {"tasks": self.items,
                "actions": [{"name": n, "label": s.get("label", n),
                             "params": sorted((s.get("params") or {}).keys()),
                             "danger": bool(s.get("danger"))}
                            for n, s in sorted(ACTIONS.items())],
                "recent": self.recent[-20:][::-1], "min_every": self.MIN_EVERY,
                "now": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "仅执行白名单动作；不写入系统 crontab"}

    def add(self, action, params, every, ip, enabled=True):
        spec = ACTIONS.get(action or "")
        if not spec:
            return False, "没有这个动作"
        try:
            every = int(every)
        except (TypeError, ValueError):
            return False, "周期必须是秒数"
        if not (self.MIN_EVERY <= every <= 30 * 86400):
            return False, "周期要在 %d 秒到 30 天之间" % self.MIN_EVERY
        clean = {}
        for key, validator in (spec.get("params") or {}).items():
            try:
                clean[key] = validator((params or {}).get(key))
            except (ValueError, TypeError, AttributeError) as e:
                return False, "参数 %s 不合法：%s" % (key, e)
        with self.lock:
            self._seq += 1
            self.items.append({"id": self._seq, "action": action, "params": clean,
                               "every": every, "enabled": bool(enabled),
                               "next_at": time.time() + every, "last_run": "",
                               "last_ok": None, "last_result": ""})
            self._save()
        self.audit.write("task.add", "%s 每 %d 秒" % (action, every), True, "", ip)
        return True, ""

    def remove(self, tid, ip):
        with self.lock:
            before = len(self.items)
            self.items = [t for t in self.items if str(t.get("id")) != str(tid)]
            if len(self.items) == before:
                return False, "没有这个任务"
            self._save()
        self.audit.write("task.del", str(tid), True, "", ip)
        return True, ""

    def toggle(self, tid, ip):
        with self.lock:
            for t in self.items:
                if str(t.get("id")) == str(tid):
                    t["enabled"] = not t.get("enabled")
                    t["next_at"] = time.time() + int(t.get("every") or 300)
                    self._save()
                    self.audit.write("task.toggle", str(tid), True,
                                     "现在 %s" % ("启用" if t["enabled"] else "停用"), ip)
                    return True, ""
        return False, "没有这个任务"

    def run_now(self, tid, ip):
        target = next((t for t in self.items if str(t.get("id")) == str(tid)), None)
        if not target:
            return False, {"error": "没有这个任务"}
        return True, self._run(target, ip)

    def _run(self, t, ip):
        res = run_action(t.get("action"), t.get("params") or {}, ip, self.audit,
                         dry=not self.probe.real)
        ok = bool(res.get("ok"))
        t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        t["last_ok"] = ok
        t["last_result"] = (res.get("error") or res.get("detail") or "")[:200]
        self.recent.append({"ts": t["last_run"], "action": t.get("action"), "ok": ok,
                            "detail": t["last_result"], "demo": bool(res.get("demo"))})
        with self.lock:
            self._save()
        return {"ok": ok, "detail": t["last_result"], "demo": bool(res.get("demo"))}

    def tick(self):
        now = time.time()
        for t in list(self.items):
            if not t.get("enabled") or now < float(t.get("next_at") or 0):
                continue
            t["next_at"] = now + int(t.get("every") or 300)
            try:
                self._run(t, "task")
            except Exception:
                continue

    def start(self):
        def loop():
            while not self.stop.wait(self.TICK):
                try:
                    self.tick()
                except Exception:
                    continue
        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self):
        self.stop.set()


class Alerts:
    """阈值告警：超阈值发 Webhook，同指标冷却期内只发一次。"""

    TICK = 60
    DEFAULTS = {"enabled": False, "webhook": "", "cooldown_min": 30,
                "rules": {"disk_percent": 90, "mem_percent": 92, "load_per_core": 2.0,
                          "failed_units": 1}}

    def __init__(self, data_dir, audit, probe):
        self.path = os.path.join(str(data_dir), "alerts.json")
        self.audit = audit
        self.probe = probe
        self.lock = threading.Lock()
        self.cfg = self._load()
        self.fired = {}
        self.recent = []
        self.stop = threading.Event()

    def _load(self):
        cfg = json.loads(json.dumps(self.DEFAULTS))
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return cfg
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in cfg})
            if isinstance(saved.get("rules"), dict):
                cfg["rules"].update(saved["rules"])
        return cfg

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def save(self, body, ip):
        body = body or {}
        webhook = str(body.get("webhook") or "").strip()
        if webhook and not webhook.startswith(("http://", "https://")):
            return False, "Webhook 必须以 http:// 或 https:// 开头"
        if len(webhook) > 300:
            return False, "Webhook 地址太长"
        try:
            cooldown = int(body.get("cooldown_min") or 30)
        except (TypeError, ValueError):
            return False, "冷却时间必须是分钟数"
        rules = {}
        for key, default in self.DEFAULTS["rules"].items():
            try:
                rules[key] = float((body.get("rules") or {}).get(key, default) or 0)
            except (TypeError, ValueError):
                return False, "阈值 %s 不是数字" % key
        if body.get("enabled") and not webhook:
            return False, "启用告警需先配置 Webhook"
        with self.lock:
            self.cfg = {"enabled": bool(body.get("enabled")), "webhook": webhook,
                        "cooldown_min": max(1, min(1440, cooldown)), "rules": rules}
            self._save()
        self.audit.write("alert.save", webhook or "(未设置)", True,
                         "冷却 %d 分钟" % self.cfg["cooldown_min"], ip)
        return True, ""

    def status(self):
        return {"config": self.cfg, "recent": self.recent[-20:][::-1],
                "note": "同一指标冷却期内只发一次"}

    def _post(self, payload):
        import urllib.request
        url = self.cfg.get("webhook") or ""
        if not url:
            return False, "没有配置 Webhook"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "n1bus-alert/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300, "HTTP %s" % resp.status
        except Exception as e:  # noqa: BLE001
            return False, "%s: %s" % (type(e).__name__, str(e)[:120])

    def test(self, ip):
        host = os.uname().nodename if hasattr(os, "uname") else "n1bus"
        ok, detail = self._post({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "test",
                                 "metric": "test", "value": 0, "threshold": 0, "host": host,
                                 "text": "N1bus 测试告警：通道正常"})
        self.recent.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "metric": "test",
                            "ok": ok, "detail": detail})
        self.audit.write("alert.test", "test", ok, detail, ip)
        return ok, detail

    def check(self):
        if not self.cfg.get("enabled"):
            return
        try:
            snap = self.probe.overview()
        except Exception:  # noqa: BLE001
            return
        rules = self.cfg.get("rules") or {}
        cpu = snap.get("cpu") or {}
        mem = snap.get("mem") or {}
        svc = snap.get("services") or {}
        disk = snap.get("disk") or []
        root = next((d for d in disk if d.get("mount") == "/"), (disk or [{}])[0])
        cores = max(1, int(cpu.get("count") or 1))
        checks = [("disk_percent", float(root.get("percent") or 0), "根分区使用率"),
                  ("mem_percent", float(mem.get("percent") or 0), "内存使用率"),
                  ("load_per_core", float((cpu.get("load") or [0])[0] or 0) / cores, "每核负载"),
                  ("failed_units", float(svc.get("failed") or 0), "失败的服务单元")]
        cooldown = max(1, int(self.cfg.get("cooldown_min") or 30)) * 60
        now = time.time()
        for metric, value, label in checks:
            threshold = float(rules.get(metric) or 0)
            if threshold <= 0 or value < threshold:
                continue
            if now - float(self.fired.get(metric) or 0) < cooldown:
                continue
            self.fired[metric] = now
            host = (snap.get("info") or {}).get("hostname") or "n1bus"
            ok, detail = self._post({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "warn",
                                     "metric": metric, "value": round(value, 2),
                                     "threshold": threshold, "host": host,
                                     "text": "%s %s 超过阈值 %s" % (label, round(value, 2),
                                                                   threshold)})
            self.recent.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "metric": metric,
                                "value": round(value, 2), "threshold": threshold, "ok": ok,
                                "detail": detail})

    def start(self):
        def loop():
            while not self.stop.wait(self.TICK):
                try:
                    self.check()
                except Exception:
                    continue
        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self):
        self.stop.set()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 只读 TTL 缓存：给"昂贵但幂等"的观测结果去重
# --------------------------------------------------------------------------
# 为什么需要它（真机实测，2 vCPU 的托管机）：
#   systemctl list-unit-files --type=service --state=enabled   ≈ 1.8 s   ← /api/autostart
#   apt-get -s autoremove                                      ≈ 0.9 s   ← /api/cleanup
#   fail2ban-client status[+jail] + ufw status verbose         ≈ 0.3 s   ← /api/security
# 这些命令的输出在几秒内不会变，但前端每次轮询/切页都重跑一次。机器只有 2 vCPU，
# 代价不只是慢，还有每次 fork 的 CPU 抖动。
#
# 规矩（与"不假装"那条设计原则对齐）：
#   * **只缓存只读观测**。任何写操作、任何随参数变化的结果都不进这里。
#   * TTL 一律短（默认 15 s，最多 30 s）。数据新鲜度有界，且响应里会带 cache 元数据，
#     界面能如实说明"这批数据可能是 N 秒前的"。
#   * 任何缓存命中都可以被 `?fresh=1` 强制绕过 —— 用户点"刷新"就该拿到当下的真值。
#   * 缓存只在内存里，进程重启即空（和采样曲线一样，不落库）。
DATA_CACHE_TTL = 15.0   # 常规观测
SLOW_CACHE_TTL = 30.0   # 要等子进程的那几条（systemctl / docker ps / ip route）
CLEANUP_CACHE_TTL = 300.0   # 清理账本：apt 模拟 + journald/镜像体积，几分钟内不会变

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LOCAL = threading.local()   # 每个请求线程各自的命中记录，用来给响应附元数据


def cache_meta():
    """当前线程最近一次缓存决策。没命中过就返回 None。"""
    hit, ttl, ts = getattr(_CACHE_LOCAL, "last", (False, 0.0, 0.0))
    if not hit:
        return None
    return {"stale": True, "ttl": ttl, "age": max(0, int(time.time() - ts))}


def _cache_key_name(k):
    """取出缓存键里用于前缀匹配的那部分。

    无参方法的键就是方法名本身（字符串）；**带参数方法的键是 `(方法名, 参数元组)`**
    —— 见 cached()。所以作废缓存时不能假定键一定是字符串，否则一旦缓存里存在带参数的
    条目，这里就会抛 AttributeError。

    这个错误的代价不小：它发生在 `do_POST` 里、动作**已经执行完并写过审计之后**，
    于是现象是「动作明明成功了，面板却报 JSON 解析失败」—— 因为异常把响应体也带走了。
    """
    if isinstance(k, tuple) and k and isinstance(k[0], str):
        return k[0]
    return k if isinstance(k, str) else ""


def clear_data_cache(prefixes=None):
    """作废只读缓存。

    给 prefixes 就只删以这些前缀开头的缓存键（写操作按影响面精确作废）；
    不给（None）就全清 —— 老调用与"拿不准影响面"的情况都走这条。
    返回被删掉的键，方便自检与排查。
    """
    with _CACHE_LOCK:
        if prefixes is None:
            gone = list(_CACHE)
        elif prefixes:
            pre = tuple(prefixes)
            gone = [k for k in _CACHE if _cache_key_name(k).startswith(pre)]
        else:
            gone = []
        for k in gone:
            _CACHE.pop(k, None)
    # 单元属性缓存**无条件作废**，不看 prefixes。
    # 这些是模块级的、**没有过期时间**的进程内缓存（为了省 systemctl show 的 8ms/个）。
    # 任何写操作都可能改 UnitFileState / ActiveState，留下旧值就会出现：
    #   systemctl 明明已经 disabled，面板还坚持显示 enabled —— 于是「点自启没反应」。
    # 重查一批量只要 ~10ms，宁可清掉。
    UNIT_STATE_CACHE.clear()
    UNIT_PATH_CACHE.clear()
    UNIT_PROBED.clear()
    return gone


# 写操作 → 要作废哪些只读缓存。
# 以前是"任何动作成功后清空全部"：点一下「取消自启」，docker / 安全 / 网络那些
# 毫不相干的观测也一起被打掉，下一个 tick 全部重查 —— 2 vCPU 上就是白烧一秒多。
# 现在按下表精确作废；**没登记的动作一律全清**（宁可多清，不可留陈旧值）。
# 注意字典顺序即匹配顺序：更具体的前缀写在前面（docker.autostart vs docker.*）。
CACHE_KEYS_FOR_ACTION = {
    "docker.autostart": ("HostProbe.autostart",),
    "docker.prune_images": ("Docker.", "HostProbe.cleanup"),
    "docker.": ("Docker.", "HostProbe.cleanup", "HostProbe.netinfo", "HostProbe.autostart"),
    "service.": ("Services.", "HostProbe.autostart"),
    "systemd.": ("Services.", "HostProbe.autostart"),
    "boot.": ("HostProbe.autostart", "Services."),
    "process.": ("Ports.",),
    "ufw.": ("Security.",),
    "fail2ban.": ("Security.",),
    "clean.": ("HostProbe.cleanup",),
    "journal.": ("HostProbe.cleanup",),
    "apt.": ("HostProbe.cleanup",),
    # 下面这些要么只动面板自己的文件、要么页面随后就断了，没有只读观测需要作废
    "backup.": (),
    "time.": (),
    "panel.": (),
    "system.": (),
    "account.": (),
}


def _cache_prefixes_for(action):
    """这个动作影响到哪些只读缓存？没登记就返回 None（调用方按全清处理）。"""
    for prefix, keys in CACHE_KEYS_FOR_ACTION.items():
        if action.startswith(prefix):
            return keys
    return None


def cached(ttl, key=None):
    """把一个只读方法的结果缓存 ttl 秒。

    要求被装饰的方法：只读、幂等、**参数可枚举**。
    * 无参方法：不传 key，整份结果一个缓存槽。
    * 带参方法：必须传 key=lambda self, *a, **kw: "..."，把参数折进缓存键。
      参数空间不可控的方法（比如按关键字搜日志）**不要**用它 —— 会撑爆缓存。

    返回的是拷贝：调用方随便改，不会污染缓存里那份。
    """
    def deco(fn):
        name = getattr(fn, "__qualname__", fn.__name__)

        def slot(k):
            with _CACHE_LOCK:
                _CACHE.setdefault(k, (0.0, None))

        if key is None:
            slot(name)

        def wrapper(self, *args, **kwargs):
            # 带参数时把**方法名也并入缓存键**：key_fn 通常返回一个纯参数元组
            # （例如 (0, 0, 10)），若两个不同类的方法参数形式相同，会命中同一缓存槽
            # 并相互读到对方的结果 —— 这类问题比「参数被忽略」更难定位。
            k = (name, key(self, *args, **kwargs)) if key else name
            if key is not None:
                slot(k)
            if getattr(_CACHE_LOCAL, "fresh", False):   # ?fresh=1：这一趟必须取真值
                val = fn(self, *args, **kwargs)
                with _CACHE_LOCK:
                    _CACHE[k] = (time.time(), copy.deepcopy(val))
                _CACHE_LOCAL.last = (False, ttl, time.time())
                return val
            now = time.time()
            with _CACHE_LOCK:
                ts, val = _CACHE.get(k, (0.0, None))
                hit = val is not None and (now - ts) < ttl
            if hit:
                _CACHE_LOCAL.last = (True, ttl, ts)
                return copy.deepcopy(val)
            val = fn(self, *args, **kwargs)
            with _CACHE_LOCK:
                _CACHE[k] = (time.time(), copy.deepcopy(val))
            _CACHE_LOCAL.last = (False, ttl, time.time())
            return val

        wrapper.__name__ = getattr(fn, "__name__", "cached")
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return deco


def _cache_methods(cls, mapping):
    """类定义完再打补丁：免得到处改类体，也让"哪些被缓存了"一眼看得全。

    mapping 的值：ttl 秒数，或 (ttl, key_fn) —— 带参数的方法必须走后者。
    """
    for meth, spec in mapping.items():
        ttl, key = spec if isinstance(spec, tuple) else (spec, None)
        fn = getattr(cls, meth)
        setattr(cls, meth, cached(ttl, key)(fn))


def _trim_services(res, q):
    """服务列表按 `?limit=` 裁剪（P9：真机上 210 个单元 ≈ 19.5 KB）。

    默认**不裁** —— 界面的搜索框要在全部单元里找，悄悄截断等于骗人；
    只有调用方明确给了 limit 才截，并如实带上 total / truncated。
    """
    try:
        limit = int(q.get("limit", ["0"])[0])
    except (TypeError, ValueError):
        limit = 0
    units = res.get("units") or []
    if 0 < limit < len(units):
        res = dict(res, units=units[:limit], truncated=True, total=len(units))
    return res


class Handler(BaseHTTPRequestHandler):
    server_version = "n1bus/" + VERSION
    app = None

    def log_message(self, fmt, *args):   # 访问日志：只留错误，别把 journald 刷满
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # --- 基础设施 ---------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        """把东西发出去。

        注意：**字符串原样发，不要 JSON 编码**。以前这里对任何非 bytes 都 json.dumps，
        于是 ui.html 被当成 JSON 字符串发出去（开头多个引号、换行变成字面 \\n、引号变成
        \\"），浏览器把整页当纯文本渲染 —— 表现就是「页面没有 CSS」。v1m-dash 那边一直是
        分类型处理的，所以只有 N1bus 中招。

        另外：如果 body 是 dict 且这一趟走了只读缓存，就附一句 `_cache` 元数据，
        让界面能如实说出「这批数据是 N 秒前的」——而不是拿旧值冒充实时值。
        """
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            meta = cache_meta()
            if meta and isinstance(body, dict):
                body = dict(body, _cache=meta)   # 新 dict：绝不写回缓存里那份
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:].strip() if auth.startswith("Bearer ") else ""

    def _authed(self, q):
        """两种凭证：① 登录后发的会话令牌（浏览器）② 服务 token（机器）。

        服务 token 必须留着：N1bus 每 60 秒要拿它去 v1m-dash 拉限流决策，
        那种调用没法让人登录。它存在 0600 的文件里，只给机器用。
        """
        if q.get("token", [None])[0] == self.app["token"]:
            return True
        cred = self._bearer()
        if not cred:
            return False
        if self.app["accounts"].session_user(cred):
            return True
        return same_secret(cred, self.app["token"])

    def _user(self):
        """当前登录的账户名；用服务 token 进来的是 "(服务 token)"。"""
        cred = self._bearer()
        if not cred:
            return ""
        u = self.app["accounts"].session_user(cred)
        if u:
            return u
        return "(服务 token)" if same_secret(cred, self.app["token"]) else ""

    def _client_ip(self):
        """审计里记的客户端 IP。

        面板通常挂在反向代理后面，直连方是那个容器（172.x）—— 那样审计里全是
        "172.18.0.3 干了所有事"，等于没有。所以当直连方在信任网段内时，取
        X-Forwarded-For 的第一段（最左 = 最原始客户端）。直连方不在信任网段里就
        完全不看这个头，免得有人伪造。
        """
        peer = self.client_address[0]
        nets = self.app.get("allow_nets") or []
        if nets:
            import ipaddress
            try:
                addr = ipaddress.ip_address(peer)
            except ValueError:
                return peer
            if not any(addr in n for n in nets):
                return peer
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
        return peer

    def _client_ok(self):
        """可选的来源白名单（--allow-ip）。绑回环/网桥时它只是第二层；绑 0.0.0.0 时它才是关键。"""
        nets = self.app.get("allow_nets") or []
        if not nets:
            return True
        import ipaddress
        try:
            addr = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(addr in n for n in nets)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not n or n > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # --- GET --------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._client_ok():
            return self._send(403, {"error": "forbidden", "hint": "来源不在 --allow-ip 白名单里"})
        # ?fresh=1：本次请求绕过只读缓存，取当下的真值（界面的「强制刷新」用它）
        _CACHE_LOCAL.fresh = q.get("fresh", ["0"])[0] in ("1", "true", "yes")
        _CACHE_LOCAL.last = (False, 0.0, 0.0)
        if u.path in ("/", "/index.html"):
            return self._send(200, build_page(), "text/html; charset=utf-8")
        if u.path == "/api/meta":
            return self._send(200, {"version": VERSION, "mode": self.app["mode"],
                                    "real": self.app["probe"].real, "started": self.app["started"],
                                    "interval": self.app["sampler"].interval,
                                    "actions": list_actions()})
        if u.path == "/api/auth":
            acc = self.app["accounts"]
            me = self._user()
            return self._send(200, {"need_setup": acc.need_setup(),
                                    "password_min": PASSWORD_MIN, "session_ttl": SESSION_TTL,
                                    "service_token": True, "me": me,
                                    # 没登录就不告诉你都有哪些用户名 —— 否则等于替攻击者
                                    # 把"猜用户名"这一步做了
                                    "users": acc.names() if me else []})
        if not self._authed(q):
            return self._send(401, {"error": "unauthorized", "hint": "需要登录"})
        p = self.app["probe"]
        try:
            if u.path == "/api/overview":
                return self._send(200, p.overview())
            if u.path == "/api/services":
                return self._send(200, _trim_services(p.services.list(), q))
            if u.path == "/api/service":
                return self._send(200, p.services.detail(q.get("unit", [""])[0]))
            if u.path == "/api/service/logs":
                return self._send(200, p.services.logs(q.get("unit", [""])[0],
                                                       int(q.get("lines", ["200"])[0])))
            if u.path == "/api/docker":
                return self._send(200, p.docker.list())
            if u.path == "/api/dsh":
                return self._send(200, p.docker.dsh())
            if u.path == "/api/kms":
                return self._send(200, self.app["kms"].all())
            if u.path == "/api/docker/images":
                return self._send(200, p.docker.images())
            if u.path == "/api/docker/logs":
                return self._send(200, p.docker.logs(
                    q.get("name", [""])[0], q.get("lines", ["200"])[0],
                    q.get("since", [""])[0], q.get("until", [""])[0],
                    q.get("grep", [""])[0],
                    q.get("ts", [""])[0] in ("1", "true", "yes")))
            if u.path == "/api/security":
                # 这一页要的几样东西彼此重叠（fail2ban / ufw / auth.log），先各算一次
                # 再往下传：否则 logins 与 blocked 会被同一个请求多跑好几遍。
                f2b = p.security.fail2ban()
                lg = p.security.logins()
                blk = p.security.blocked(f2b)
                return self._send(200, {"logins": lg,
                                        "firewall": p.security.firewall(),
                                        "fail2ban": f2b,
                                        "blocked": blk,
                                        "defender": self.app["defender"].status(p.security, lg, blk),
                                        "allowed": p.security.allowed(p.ports.listening()),
                                        "sshd": p.security.sshd()})
            if u.path == "/api/ports":
                return self._send(200, {"listening": p.ports.listening(),
                                        "established": p.ports.established()})
            if u.path == "/api/processes":
                return self._send(200, {"top": p.processes.top(q.get("by", ["cpu"])[0]),
                                        "counts": p.processes.counts()})
            if u.path == "/api/audit":
                return self._send(200, {"entries": p.audit.tail(int(q.get("lines", ["200"])[0]))})
            if u.path == "/api/files":
                return self._send(200, p.files(q.get("path", [""])[0]))
            if u.path == "/api/file":
                return self._send(200, p.file_tail(q.get("path", [""])[0],
                                                   q.get("lines", ["200"])[0]))
            if u.path == "/api/file/read":
                return self._send(200, p.file_read(q.get("path", [""])[0]))
            if u.path == "/api/file/download":
                real = safe_path(q.get("path", [""])[0], want_dir=False)
                if real is None:
                    return self._send(404, {"ok": False,
                                            "error": "路径不在允许浏览的目录里，或者不是普通文件"})
                try:
                    size = os.path.getsize(real)
                except OSError as ex:
                    return self._send(404, {"ok": False, "error": str(ex)[:140]})
                name = os.path.basename(real) or "download"
                # 文件名进 Content-Disposition 前要消毒：只留安全字符，防头部注入
                safe_fn = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "download"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % safe_fn)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    with open(real, "rb") as fh:
                        shutil.copyfileobj(fh, self.wfile, 256 * 1024)
                except OSError:
                    pass
                return
            if u.path == "/api/tasks":
                return self._send(200, self.app["tasks"].listing())
            if u.path == "/api/alerts":
                return self._send(200, self.app["alerts"].status())
            if u.path == "/api/autostart":
                return self._send(200, p.autostart())
            if u.path == "/api/system_settings":
                return self._send(200, p.sys_settings())
            if u.path == "/api/backup":
                return self._send(200, p.backup_info())
            if u.path == "/api/backup/file":
                arc = p.BACKUP_ARCHIVE
                if not os.path.exists(arc):
                    return self._send(404, {"error": "备份不存在，请先执行打包"})
                with open(arc, "rb") as fh:
                    blob = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="n1bus-backup.tar.gz"')
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(blob)
                return
            if u.path == "/api/cleanup":
                return self._send(200, p.cleanup())
            if u.path == "/api/netinfo":
                return self._send(200, p.netinfo())
            if u.path == "/api/disks":
                return self._send(200, p.disks())
            if u.path == "/api/enforce":
                return self._send(200, self.app["enforcer"].status())
            if u.path == "/api/accounts":
                return self._send(200, {"users": self.app["accounts"].listing(),
                                        "me": self._user(),
                                        "guard": self.app["guard"].status()})
            if u.path == "/api/log":
                return self._send(200, _tail_file(q.get("path", [""])[0],
                                                  int(q.get("lines", ["200"])[0])))
            if u.path == "/api/journal":
                code, out, err = run(["journalctl", "-n", str(int(q.get("lines", ["200"])[0])),
                                      "--no-pager", "-o", "short"], timeout=20)
                return self._send(200, {"available": code == 0, "lines": out.splitlines(),
                                        "error": err.strip()[:200] if code != 0 else ""})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        return self._send(404, {"error": "not found"})

    # --- POST -------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._client_ok():
            return self._send(403, {"error": "forbidden", "hint": "来源不在 --allow-ip 白名单里"})
        # POST 一律取真值：写操作前的判断不该建立在陈旧观测上
        _CACHE_LOCAL.fresh = True
        _CACHE_LOCAL.last = (False, 0.0, 0.0)
        acc = self.app["accounts"]
        ip = self._client_ip()

        # --- 不需要登录的两个入口 -----------------------------------------
        if u.path == "/api/setup":
            if not acc.need_setup():
                return self._send(403, {"ok": False, "error": "已经有账户了，请直接登录"})
            body = self._json_body()
            name = (body.get("name") or "").strip()
            ok, err = acc.create(name, body.get("password") or "")
            if not ok:
                return self._send(400, {"ok": False, "error": err})
            acc.touch_login(name)
            self.app["audit"].write("account.setup", name, True, "首次设置账户", ip)
            return self._send(200, {"ok": True, "session": acc.new_session(name), "name": name})
        if u.path == "/api/login":
            wait = self.app["guard"].wait_seconds(ip)
            if wait:
                return self._send(429, {"ok": False, "error": "登录失败次数太多，请 %d 秒后再试" % wait})
            body = self._json_body()
            name = (body.get("name") or "").strip()
            if acc.check(name, body.get("password") or ""):
                self.app["guard"].ok(ip)
                acc.touch_login(name)
                self.app["audit"].write("account.login", name, True, "", ip)
                return self._send(200, {"ok": True, "session": acc.new_session(name), "name": name})
            blocked, secs, level = self.app["guard"].fail(ip)
            self.app["audit"].write("account.login", name or "(空)", False,
                                    "密码错误" + ("，已封禁 %d 秒（第 %d 档）" % (secs, level)
                                                  if blocked else ""), ip)
            return self._send(403, {"ok": False, "error": "用户名或密码不对"})

        # --- 以下都要已登录 -----------------------------------------------
        if not self._authed(q):
            return self._send(401, {"error": "unauthorized"})

        # --- 面板自身的配置接口（不是系统命令，所以不进 ACTIONS 表）---------
        if u.path == "/api/tasks/add":
            b = self._json_body()
            ok, err = self.app["tasks"].add(b.get("action"), b.get("params") or {},
                                            b.get("every"), ip, b.get("enabled", True))
            return self._send(200 if ok else 400, {"ok": ok, "error": err})
        if u.path == "/api/tasks/del":
            ok, err = self.app["tasks"].remove(self._json_body().get("id"), ip)
            return self._send(200 if ok else 404, {"ok": ok, "error": err})
        if u.path == "/api/tasks/toggle":
            ok, err = self.app["tasks"].toggle(self._json_body().get("id"), ip)
            return self._send(200 if ok else 404, {"ok": ok, "error": err})
        if u.path == "/api/tasks/run":
            ok, res = self.app["tasks"].run_now(self._json_body().get("id"), ip)
            return self._send(200 if ok else 404, {"ok": ok, "result": res})
        if u.path == "/api/alerts":
            ok, err = self.app["alerts"].save(self._json_body(), ip)
            return self._send(200 if ok else 400, {"ok": ok, "error": err})
        if u.path == "/api/alerts/test":
            ok, detail = self.app["alerts"].test(ip)
            return self._send(200, {"ok": ok, "detail": detail})

        # --- 自动永久封禁 --------------------------------------------------
        # 策略本身不是系统命令，因此不进入 ACTIONS 表；但实际封禁仍通过 ufw.deny
        # 这一白名单动作执行，因此审计记录与手动封禁格式一致。
        if u.path == "/api/security/defender":
            b = self._json_body()
            conf = self.app["defender"].configure(b.get("enabled"), b.get("threshold"),
                                                  self.app["probe"].security)
            self.app["audit"].write("defender.config", "threshold=%d" % conf["threshold"], True,
                                    "自动永久封禁已%s，阈值 %d 次"
                                    % ("开启" if conf["enabled"] else "关闭", conf["threshold"]), ip)
            return self._send(200, {"ok": True, "config": conf})
        if u.path == "/api/security/defender/run":
            res = self.app["defender"].run_once(self.app["probe"].security)
            return self._send(200, {"ok": bool(res.get("ok")), "result": res})
        if u.path == "/api/security/defender/exempt":
            b = self._json_body()
            try:
                target = _val_ip(b.get("ip"))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            on = bool(b.get("on", True))
            ex = self.app["defender"].exempt(target, on)
            self.app["audit"].write("defender.exempt", target, True,
                                    "从豁免名单" + ("加入（不再自动封这个地址）" if on else "移除"), ip)
            return self._send(200, {"ok": True, "exempt": ex})

        # --- KMS 授权服务的写操作 ------------------------------------------
        # 面板这边不自己实现，转发给原面板（它管着 vlmcsd 的 ini 与 supervisord）。
        # 操作名走 KMS_WRITES 白名单，每次调用都写审计。
        if u.path == "/api/kms/action":
            b = self._json_body()
            r = self.app["kms"].action((b.get("op") or "").strip(),
                                       b.get("params") or {}, self.app["audit"], ip)
            return self._send(200 if r.get("ok") else 400, r)

        # 解封的唯一入口：撤销规则 + 写入豁免。若拆成两个接口，前端漏调其中一个会导致
        # "解封后下一轮巡检立即重新封禁"。
        if u.path == "/api/security/unban":
            b = self._json_body()
            try:
                target = _val_ip(b.get("ip"))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            r = self.app["defender"].unban(target, "panel", (b.get("jail") or "").strip())
            return self._send(200 if r.get("ok") else 400, r)

        # --- 文件管理：写类操作 -------------------------------------------
        #
        # 上传走原始 body（不套 JSON）—— 二进制直接落盘，省掉 base64 那份膨胀。
        # 所以它必须排在下面 `body = self._json_body()` 之前，否则请求体会被当 JSON 读空。
        if u.path == "/api/file/upload":
            target = q.get("path", [""])[0]
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return self._send(400, {"ok": False, "error": "没有收到文件内容"})
            if n > FILE_UPLOAD_MAX:
                return self._send(413, {"ok": False, "error": "文件超过单次上传上限（%s）"
                                        % _human_size(FILE_UPLOAD_MAX)})
            try:
                data = self.rfile.read(n)
            except OSError as ex:
                return self._send(400, {"ok": False, "error": "读取请求体失败：%s" % str(ex)[:100]})
            r = self.app["probe"].file_upload(target, data, ip)
            return self._send(200 if r.get("ok") else 400, r)

        body = self._json_body()
        me = self._user()

        if u.path == "/api/file/write":
            r = self.app["probe"].file_write(body.get("path", ""), body.get("text", ""), ip)
            return self._send(200 if r.get("ok") else 400, r)
        if u.path == "/api/file/mkdir":
            r = self.app["probe"].file_mkdir(body.get("path", ""), ip)
            return self._send(200 if r.get("ok") else 400, r)
        if u.path == "/api/file/touch":
            r = self.app["probe"].file_touch(body.get("path", ""), ip)
            return self._send(200 if r.get("ok") else 400, r)
        if u.path == "/api/file/rename":
            r = self.app["probe"].file_rename(body.get("path", ""), body.get("name", ""), ip)
            return self._send(200 if r.get("ok") else 400, r)
        if u.path == "/api/file/delete":
            r = self.app["probe"].file_delete(body.get("path", ""),
                                              bool(body.get("recursive")), ip)
            return self._send(200 if r.get("ok") else 400, r)

        if u.path == "/api/logout":
            acc.drop_session(self._bearer())
            return self._send(200, {"ok": True})
        if u.path == "/api/accounts/create":
            name = (body.get("name") or "").strip()
            ok, err = acc.create(name, body.get("password") or "")
            self.app["audit"].write("account.create", name, ok, err or "", ip)
            return self._send(200 if ok else 400,
                              {"ok": ok, "error": err, "users": acc.listing()})
        if u.path == "/api/accounts/password":
            # 改谁的密码都行，但必须拿"你自己当前的密码"确认一次 —— 免得一个会话被
            # 偷走就能静默接管所有账户
            if me == "(服务 token)" or not acc.check(me, body.get("actor_password") or ""):
                self.app["audit"].write("account.password", body.get("name") or me, False,
                                        "当前密码校验失败", ip)
                return self._send(403, {"ok": False, "error": "请输入你自己当前的密码"})
            target = (body.get("name") or me).strip()
            ok, err = acc.set_password(target, body.get("password") or "")
            if ok:
                acc.drop_user_sessions(target)      # 改完密码让旧会话全部失效
            self.app["audit"].write("account.password", target, ok, err or "", ip)
            return self._send(200 if ok else 400, {"ok": ok, "error": err})
        if u.path == "/api/accounts/delete":
            target = (body.get("name") or "").strip()
            ok, err = acc.delete(target)
            self.app["audit"].write("account.delete", target, ok, err or "", ip)
            return self._send(200 if ok else 400,
                              {"ok": ok, "error": err, "users": acc.listing()})
        if u.path == "/api/accounts/logout-all":
            n = acc.drop_user_sessions(me) if me and me != "(服务 token)" else 0
            self.app["audit"].write("account.logout_all", me, True, "退出 %d 个会话" % n, ip)
            return self._send(200, {"ok": True, "dropped": n})
        if u.path == "/api/accounts/unblock":
            # 自己手滑输错密码把自己封了，或者误封了好人：给一条明确的解封路。
            # 档位不清零，所以解封不等于给攻击者重置计数。
            try:
                who = _val_ip(body.get("ip"))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            cleared = self.app["guard"].unblock(who)
            self.app["audit"].write("account.unblock", who, True,
                                    "解封登录限流" if cleared else "本来就没封", ip)
            return self._send(200, {"ok": True, "cleared": cleared,
                                    "guard": self.app["guard"].status()})
        if u.path == "/api/action":
            action = body.get("name", "")
            res = run_action(action, body.get("params") or {}, ip,
                             self.app["probe"].audit,
                             dry=not self.app["probe"].real)
            # 动作真的改了系统状态（启停服务、封 IP、清日志…），只读缓存要立刻失效，
            # 否则用户刚点完"启动"，界面还拿旧值告诉他没启动。
            # 只作废**这个动作影响到的**那几项（见 CACHE_KEYS_FOR_ACTION）：全清会让
            # 下一个 tick 把 docker / 安全 / 网络那些无关观测一起重查。
            if res.get("ok") and not res.get("demo"):
                clear_data_cache(_cache_prefixes_for(action))
            return self._send(200, res)
        return self._send(404, {"error": "not found"})


# --------------------------------------------------------------------------
# 打完补丁：哪些只读方法进缓存、各给多长 TTL
# --------------------------------------------------------------------------
# 挑的全是"输出要等一个子进程、而且那个子进程本身就得几百毫秒到一秒"的观测。
# 没挑进来的（overview / ports / processes / disks / posts 那些）本来就 10~55 ms，
# 加缓存只会平白引入陈旧度，不划算。
_cache_methods(Services, {
    "list": SLOW_CACHE_TTL,        # systemctl list-units，12 ms —— 顺带压住 /api/overview 里的重复调用
})
_cache_methods(Docker, {
    # 只缓存**不带 stats** 的清单：stats 有自己的 TTL 与后台刷新（见 Docker.list）。
    # 跟列表一起冻 30 秒的话，界面上的 CPU / 内存就是一张旧照片了。
    "_list_cached": SLOW_CACHE_TTL,
    "images": SLOW_CACHE_TTL,      # docker images
})
_cache_methods(Security, {
    # logins 是**带参数**的方法：概览页请求「最近 2000 行、前 3 名」，安全页请求
    # 「全量、全部来源」。若不给 key，两者会共用同一个缓存槽 —— 先进概览页（默认首页）
    # 会使用于安全页的排行被压缩为 3 条、行数被压成 2000，而接口本身返回的是全量数据。
    # 这比 most_common(10) 更隐蔽：后者能一眼看出是截断，前者看起来像"日志本来就不多"。
    "logins": (DATA_CACHE_TTL,
               lambda self, lines=0, top=0, recent=10: (lines, top, recent)),
    "ufw_raw": DATA_CACHE_TTL,     # ufw status verbose ≈ 90 ms：firewall 与 blocked 共用这一份
    "firewall": DATA_CACHE_TTL,    # 解析上面那份，不再单独 fork 一次 ufw
    "fail2ban": DATA_CACHE_TTL,    # fail2ban-client status + 每个 jail ≈ 90 ms × N
    "sshd": DATA_CACHE_TTL,        # 纯读两个文件，顺手一起缓存
})
_cache_methods(Ports, {
    "listening": DATA_CACHE_TTL,   # ss -lntup
})
_cache_methods(HostProbe, {
    "autostart": SLOW_CACHE_TTL,   # 读 /etc/systemd/system + 两次 systemctl：1.07 s → ~0.03 s
    "cleanup": CLEANUP_CACHE_TTL,  # apt-get -s autoremove ≈ 0.9 s，而这个账本变得极慢
    "netinfo": SLOW_CACHE_TTL,     # ip -j addr + route + docker ps
})


def main():
    # 控制台编码可能是 GBK / ASCII（Windows、LANG=C 的 Linux 都是），一句中文横幅就能
    # 让整个面板起不来。先把输出通道统一成 UTF-8 且遇错不炸。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    def _env_int(name, fallback):
        """读取环境变量中的整数；解析失败时返回回退值。

        这些参数由 systemd 单元的 Environment= 配置。若取值非法，int() 会抛出异常，
        而该调用发生在 argparse 计算默认值的阶段，会直接导致面板无法启动。
        """
        try:
            return int(os.environ.get(name) or fallback)
        except (TypeError, ValueError):
            return fallback

    ap = argparse.ArgumentParser(description="N1bus —— Linux 服务器管理面板")
    ap.add_argument("--mode", choices=["demo", "real"], default="real")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="监听地址，逗号分隔可绑多个；写 docker 表示再绑一份宿主在 docker 网桥上的地址")
    ap.add_argument("--allow-ip", default=os.environ.get("N1BUS_ALLOW_IP", ""),
                    help="来源白名单（逗号分隔的网段）；留空 = 不限制，靠绑定地址与 token 守门")
    ap.add_argument("--port", type=int, default=8402)
    ap.add_argument("--token", default="", help="直接给 token（默认从 token 文件读或生成）")
    ap.add_argument("--token-file", default="", help="默认 <data-dir>/n1bus.token")
    ap.add_argument("--data-dir", default="/var/lib/n1bus", help="token 与审计日志的目录")
    ap.add_argument("--interval", type=float, default=2.0, help="采样间隔（秒）")
    ap.add_argument("--enforce-url", default=os.environ.get("N1BUS_ENFORCE_URL", ""),
                    help="从哪个面板拉「该封谁」，例如 http://127.0.0.1:8099（v1m-dash）")
    ap.add_argument("--enforce-token", default=os.environ.get("N1BUS_ENFORCE_TOKEN", ""),
                    help="上面那个面板的访问 token")
    ap.add_argument("--enforce-interval", type=int, default=60,
                    help="拉取间隔（秒），最小 10；0 表示关闭自动封禁")
    ap.add_argument("--auto-ban", choices=["on", "off"],
                    default=os.environ.get("N1BUS_AUTO_BAN") or "on",
                    help="重复失败来源永久封禁：累计失败次数超过 --auto-ban-after 的来源"
                         "执行 ufw deny，且不自动解封（on/off，默认 on）")
    ap.add_argument("--auto-ban-after", type=int,
                    default=_env_int("N1BUS_AUTO_BAN_AFTER", AUTOBAN_THRESHOLD),
                    help="累计失败次数超过该值即永久封禁（默认 %d，最小 %d）"
                         % (AUTOBAN_THRESHOLD, AUTOBAN_MIN_THRESHOLD))
    ap.add_argument("--auto-ban-interval", type=int,
                    default=_env_int("N1BUS_AUTO_BAN_INTERVAL", AUTOBAN_INTERVAL),
                    help="巡检间隔（秒），最小 30（默认 %d）" % AUTOBAN_INTERVAL)
    ap.add_argument("--nimbus-url", default=os.environ.get("NIMBUS_URL") or NIMBUS_URL,
                    help="KMS 授权服务（v1m-dash）的地址，默认 %s" % NIMBUS_URL)
    ap.add_argument("--nimbus-token", default=os.environ.get("NIMBUS_TOKEN", ""),
                    help="上面那个面板的 token；默认自动从它的 docker 卷里读")
    args = ap.parse_args()

    if args.mode == "demo":
        data_dir = Path(args.data_dir if args.data_dir != "/var/lib/n1bus"
                        else HERE / ".n1bus-demo-data")
    else:
        data_dir = Path(args.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit("数据目录建不了：%s" % e)

    token = args.token or os.environ.get("N1BUS_TOKEN") or ""
    token_file = Path(args.token_file) if args.token_file else data_dir / "n1bus.token"
    if not token:
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            token = secrets.token_urlsafe(24)
            try:
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text(token, encoding="utf-8")
                os.chmod(token_file, 0o600)
            except OSError as e:
                sys.exit("token 文件写不了：%s" % e)

    sampler = Sampler(interval=args.interval, fake=(args.mode == "demo"))
    audit = Audit(data_dir / "audit.log")
    accounts = Accounts(data_dir / "accounts.json")
    enforcer = Enforcer(args.enforce_url, args.enforce_token, args.enforce_interval, audit)
    if args.mode == "demo" or not args.enforce_interval:
        enforcer.enabled = False
    # 排除网段 = 私有网段 + 面板访问白名单。两者都不应被自动封禁，尤其是 --allow-ip：
    # 它是管理员访问面板所使用的来源。
    defender = Defender(data_dir / "defender.json", threshold=args.auto_ban_after,
                        enabled=(args.auto_ban == "on"),
                        skip=dedup_nets(parse_networks(PRIVATE_NETS)
                                        + parse_networks(args.allow_ip)),
                        audit=audit, interval=args.auto_ban_interval,
                        dry=(args.mode == "demo"))
    probe = DemoProbe(sampler, audit) if args.mode == "demo" else HostProbe(sampler, audit)
    tasks = Tasks(data_dir, audit, probe)
    alerts = Alerts(data_dir, audit, probe)
    app = {"probe": probe, "sampler": sampler, "token": token, "mode": args.mode,
           "started": now_iso(), "enforcer": enforcer, "defender": defender,
           "kms": _DemoKMS() if args.mode == "demo"
                  else KMSService(args.nimbus_url, args.nimbus_token),
           "accounts": accounts, "audit": audit,
           "guard": LoginGuard(data_dir / "loginguard.json"),
           "tasks": tasks, "alerts": alerts,
           "allow_nets": parse_networks(args.allow_ip)}
    Handler.app = app

    # 先把端口占住，再起采样线程：反过来的话 systemd 已经报 active、端口却还没监听，
    # 健康检查会踩空（install.sh 的健康检查就踩过一次）。
    servers = []
    for addr in expand_binds(args.bind):
        try:
            srv = ThreadingHTTPServer((addr, args.port), Handler)
        except OSError as e:
            print("  !! 绑不上 %s:%d（%s）" % (addr, args.port, e))
            continue
        srv.daemon_threads = True
        servers.append((addr, srv))
    if not servers:
        sys.exit("一个地址都没绑上，退出")
    for _, srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    sampler.start()
    if enforcer.enabled:
        threading.Thread(target=enforcer.loop, daemon=True).start()
    if defender.state["enabled"]:
        defender.start(probe.security)
    tasks.start()
    alerts.start()
    print("=" * 72)
    print("  N1bus %s  [%s 模式]" % (VERSION, args.mode))
    for addr, _ in servers:
        print("  监听 %s:%d" % (addr, args.port))
    print("  打开这个地址（已带 token）：http://%s:%d/?token=%s"
          % (servers[0][0] if servers[0][0] != "0.0.0.0" else "127.0.0.1", args.port, token))
    print("  纯 token：%s（机器凭证，浏览器改用账户登录）" % token)
    print("  忘了 token 就跑：cat %s" % token_file)
    print("  数据目录：%s（审计日志 %s）" % (data_dir, data_dir / "audit.log"))
    if accounts.need_setup():
        print("  注意：还没有账户，打开页面先设置账户名与密码")
    else:
        print("  账户：%s（改密码在面板的「账户」页）" % "、".join(accounts.names()))
    if app["allow_nets"]:
        print("  来源白名单：%s" % "、".join(str(n) for n in app["allow_nets"]))
    if args.mode == "real":
        print("  systemd：%s   docker：%s" % (
            "可用" if probe.services.available else "不可用",
            "可用" if probe.docker.available else "不可用"))
        print("  自动封禁：%s" % ("%s（每 %d 秒拉一次）" % (enforcer.url, enforcer.interval)
                                 if enforcer.enabled else "关闭"))
        print("  重复失败来源永久封禁：%s" % (
            "开启 —— 累计失败 > %d 次即执行 ufw deny，每 %d 秒巡检一次%s"
            % (defender.state["threshold"], defender.interval,
               "（演示模式：仅记录，不实际执行）" if defender.dry else "")
            if defender.state["enabled"] else "关闭"))
    print("=" * 72)
    sys.stdout.flush()
    try:
        servers[0][1].serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
