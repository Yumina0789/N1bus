"""n1bus 安全面：账户凭证、登录摩擦、防火墙观测、封禁执行与自动封禁。"""

import base64
import collections
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from n1bus_util import read_text, run, same_secret
from n1bus_actions import run_action
from n1bus_system import Ports


# --------------------------------------------------------------------------
# 账户：首次启动时创建一个账户，此后以用户名 + 密码登录
# --------------------------------------------------------------------------
PASSWORD_MIN = 8
SESSION_TTL = 7 * 86400          # 登录态保持 7 天，过期要重新登录
LOGIN_WINDOW = 300               # 登录失败的统计窗口（秒）
LOGIN_MAX_FAIL = 8               # 窗口内失败超过这个数就封
# 递增封禁：第一次 5 分钟，再犯 30 分钟，第三次起 6 小时。
# 24 小时内未再失败则降回第一档；成功登录直接清零 —— 正常用户多次输错密码不会导致
# 自身封禁时长递增，而持续暴力破解的来源会逐级升至 6 小时。
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
    """登录失败节流：同一来源在统计窗口内失败次数超限即封禁，且封禁时长逐级递增。

    * 仅按来源 IP 计数，不按账户计数 —— 攻击者无法借此锁死合法账户，合法用户输错密码也不会波及该账户。
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
                e["level"] = 0                                      # 距上次失败已超过 forget 窗口：封禁档位重置
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
        """登录成功：删除该来源的全部记录。"""
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
        """限制状态文件增长：长时间无活动且当前不在封禁中的来源予以清除。"""
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
                return False, "该账户不存在"
            u["hash"] = hash_password(password)
            self._save()
        return True, ""

    def delete(self, name):
        with self.lock:
            if name not in self.data["users"]:
                return False, "该账户不存在"
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
            # 用户不存在时同样执行一次哈希运算，避免响应时间泄露该用户名是否存在
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
        输出中均已包含所需字段，因此合并为一次调用，同时减少一次进程 fork（在 2 vCPU 上 fork 亦有开销）。
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
    # 若无法获取，按 ufw 自身的默认值 in 处理。
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
                          # 转发方向的删除语法与 in/out 不同，面板不代为执行
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
            # 同时返回该规则对应的监听服务，界面无需自行交叉比对两张表
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
                "note": "authorized_keys 为空时请勿关闭密码登录，否则将无法登录该主机" if not n else ""}


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
        """执行一轮：拉取 → 执行 → 回报。返回值同时供界面展示。"""
        result = {"ts": int(time.time()), "ok": True, "banned": [], "unbanned": [], "error": ""}
        ok, data = self._call("/api/enforce/pending")
        if not ok:
            result.update(ok=False, error=data.get("error", "无法获取待执行列表"))
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
                result.update(ok=False, error="封禁 %s 失败：%s" % (ip, run.get("error") or run.get("err")))
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
                    # 落盘值同样需要经过 clamp：本机 defender.json 可能来自旧版本或被人工修改
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
