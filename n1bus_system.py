"""n1bus_system —— 系统观测面：systemd 服务、Docker 容器、监听端口、进程与单元属性缓存。"""

import json
import os
import re
import shutil
import threading
import time

from n1bus_util import NAME_RE, UNIT_RE, LOG_DIRS, read_text, run
from n1bus_cache import UNIT_PATH_CACHE, UNIT_STATE_CACHE, UNIT_PROBED
from n1bus_actions import PROTECTED_UNITS


class Services:
    """托管机上的 systemd 单元。没有 systemctl 时整体降级，不伪装为可用状态。"""

    def __init__(self):
        self.available = bool(shutil.which("systemctl"))
        self.hint = "" if self.available else "这台机器上没有 systemctl（通常是因为运行在容器中）"

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
            # 仅保留界面实际使用的字段：`load`（loaded / not-found）无任何读取方，
            # 210 个单元会额外占用 2 KB（P9）。如需恢复，在此处加回 f[1] 即可。
            units.append({"unit": f[0].lstrip("●").strip(), "active": f[2],
                          "sub": f[3], "desc": (f[4] if len(f) > 4 else "").strip()})
        units.sort(key=lambda u: (u["active"] != "active", u["unit"]))
        return {"available": True, "units": units, "error": err.strip()[:200],
                "running": sum(1 for u in units if u["active"] == "active"),
                "failed": sum(1 for u in units if u["active"] == "failed")}

    def detail(self, unit):
        """一个单元的关键状态。`systemctl show` 输出为机器可读格式，比 `systemctl status` 更易解析。"""
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
# 面板需要单独处理该场景的原因：dsh 的 Web UI 只有**一种**入口方式 ——
# 使用一次性启动 token 换取会话 cookie。没有 token 时，/dsh/ 一律返回 401（纯文本
# Unauthorized），因此「从面板跳转进入」这一操作必须即时获取 token。该 token 每次启动
# 时生成、仅输出到容器日志且不写入磁盘，只能从日志中提取 —— 这就是 /api/dsh 接口存在的理由。
#
# 服务拓扑（见 docs/adr/0008）：dsh 本体仅监听 127.0.0.1，容器内另有一个 TCP 转发进程
# 将 0.0.0.0:3081 转发给它；宿主上的 Caddy 去掉 https://lolicon.fans/dsh/* 的路径前缀后
# 反向代理到该转发端口。
DSH_CONTAINER = os.environ.get("N1BUS_DSH_CONTAINER", "dsh")
DSH_PUBLIC_BASE = os.environ.get("N1BUS_DSH_BASE", "https://lolicon.fans/dsh/")
DSH_ENTRY_TTL = 30.0
DSH_TOKEN_RE = re.compile(r"token=([A-Za-z0-9_-]{16,})")


class Docker:
    """Docker 容器。缺少 docker 命令时该功能整体不可用，不影响其它功能。"""

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

        耗时使其不能置于请求路径中：此前 /api/docker 在缓存冷启动时需等待 2 秒，用户可观察到
        "打开容器页转两秒"。当前策略是**绝不阻塞**：
        * 存在缓存时（即使刚过期）先返回缓存值，由后台线程刷新 —— 界面立即返回结果，
          下一个刷新周期即可取得新值（stale-while-revalidate）。
        * 从未取得过数据时（进程刚启动）先返回空 dict，同样交由后台线程处理；
          响应中会带 stats_pending 标记，前端稍后再次请求。
        TTL 15 秒：`docker stats` 本身耗时 2 秒，因此数据新鲜度的收益不足以抵消该开销。
        """
        now = time.time()
        ts, val = self._stats
        if val and (now - ts) < self.stats_ttl:
            return val
        self._refresh_stats_async()
        return val

    def _refresh_stats_async(self):
        """后台刷新 stats；同一时刻只允许一个刷新任务运行，避免每个请求都派生一个 docker 进程。"""
        with self._stats_lock:
            if self._stats_busy:
                return
            self._stats_busy = True
        threading.Thread(target=self._refresh_stats_bg, daemon=True).start()

    def _refresh_stats_bg(self):
        try:
            self._refresh_stats()
        except Exception:                     # noqa: BLE001 —— 后台线程必须吞掉异常，不得向上层传播
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

        列表本身使用只读缓存；**stats 不与列表一同缓存** —— 30 秒的列表缓存会同时
        冻结统计数字，因此缓存中始终保存不含 stats 的版本，每次请求时再合并
        最近一次 stats（其 TTL 与后台刷新相互独立）。尚未取得数据时带 stats_pending。
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
        """DeepSeek Harness 的进入链接（理由见模块顶部 DSH_* 常量的注释）。

        任何一步条件不满足都如实说明，不构造硬编码链接 —— 链接无效时用户访问只能得到 401。
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
            info["hint"] = "容器当前状态为 %s，待其运行后方可获取可用链接" % (state or "未知状态")
            return info
        # token 仅在容器重启时变化，因此缓存以 StartedAt 作为键；TTL 用于覆盖边界情况。
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
            info["hint"] = "日志中尚未出现启动 token，容器可能刚启动、日志尚未输出完毕"
            return info
        info["url"] = DSH_PUBLIC_BASE + "?token=" + found
        self._dsh = (now, (started, info["url"]))
        return info

    def logs(self, name, lines=200, since="", until="", grep="", timestamps=False):
        """查看容器日志，允许传入 docker logs 自身的参数。

        参数全部以列表形式传给 docker，不经过 shell；`--since/--until` 仅接受时间格式，
        因此这里依然不存在「任意命令执行」的入口。
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
                        "error": "%s 仅接受 30m / 2h / 1d 或 2026-10-09T05:00 格式的时间" % flag}
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


DISTRO_UNIT_DIRS = ("/lib/systemd/system/", "/usr/lib/systemd/system/", "/run/systemd/")
ETC_UNIT_DIR = "/etc/systemd/system"


def _remember_unit(name, path, state):
    if not name or "@" in name:
        return
    UNIT_PATH_CACHE[name] = path
    UNIT_STATE_CACHE[name] = state
    UNIT_PROBED.add(name)


def _unit_props_batch(units):
    """一次 `systemctl show` 查询一批单元的 UnitFileState / FragmentPath，返回查询到的单元名。"""
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
    """批量查询单元属性并写入缓存，返回 {unit: (UnitFileState, FragmentPath)}。

    采用批量查询的原因：逐个 `systemctl show` 耗时 8ms，210 个共 1.6 秒 —— 且
    「开机自启」在每次写操作导致缓存失效后都需重新承担该开销，用户可感知为"操作后长时间无响应"。
    一次性传入多个单元仅需约 10ms。

    已知问题：**模板单元**（`xxx@.service`）会使 systemctl 报
    `Failed to get properties: Unit name ... is neither a valid invocation ID nor unit name`
    并**中断整批查询**（实测 210 个仅返回前 3 条）。因此需先排除名称含 `@` 的单元。
    同一问题也见于"不存在的单元"：批量请求中若包含一个不存在的单元，其后所有单元的返回结果同样丢失 ——
    因此在批量查询后，对未取得结果的单元逐个单独查询一次；仍查不到则记录，不再重复查询。
    """
    todo = [u for u in units if "@" not in u and u not in UNIT_PROBED]
    if todo:
        got = _unit_props_batch(todo)
        for u in todo:
            if u not in got:
                _unit_props_batch([u])
                # 已单独补查过的单元即视为已探测：仍查不到则说明 systemd 不识别它（符号链接失效 / alias 名 /
                # 刚放入 /etc 但尚未执行 daemon-reload），不应在每次请求时重复查询。
                UNIT_PROBED.add(u)
    return {u: (UNIT_STATE_CACHE.get(u, ""), UNIT_PATH_CACHE.get(u, "")) for u in units}


def _unit_paths(units):
    """仅返回 FragmentPath 的旧接口（判定逻辑仍在使用）。"""
    return {u: p for u, (_, p) in _unit_props(units).items()}


def _etc_service_units():
    """用户/第三方安装的 .service 单元名；**读取目录失败时返回 None**，调用方退回全量扫描。

    自行读取目录的原因：真机实测（2 vCPU）下 `systemctl list-unit-files --type=service`
    耗时 0.95 s，带 `--state=enabled` 更慢（1.84 s）—— 该命令需逐个 stat 四百多个 unit 文件，
    且每一次写操作后缓存失效都要重新承担该开销。面板关心的"用户安装的服务"本体就在
    /etc/systemd/system 顶层这一个目录里，os.scandir 一次约 1 ms。
    `systemctl enable` 只是在 <target>.wants/ 里建符号链接，单元本体仍在顶层，
    因此只需扫描顶层目录（wants 目录本身是目录，不会被识别为单元）。
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
    """向 systemctl 全量获取单元名；仅在 /etc/systemd/system 读取失败时作为兜底（耗时较长）。"""
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
    """判断该单元是否为 Ubuntu 发行版自带。

    判据是单元文件的位置：发行版自带的在 /lib/systemd/system 或 /usr/lib/systemd/system，
    运行时动态生成的（apport / netplan-ovs-cleanup 这类）在 /run/systemd 下；
    用户/第三方装的（n1bus / v1m-dash / docker / fail2ban …）在 /etc/systemd/system。
    比按名字前缀过滤准得多 —— 前缀法会漏掉 apport / apt-daily / bolt / cryptdisks 这些。
    查不到路径的一律当作"不是自带的"（宁可多显示，也不要漏掉用户自己装的服务）。

    模板单元（`xxx@.service`，如 getty@ / systemd-fsck@）的 FragmentPath 是空的、
    而且还会让 batch show 失败，因此直接按名称排除 —— 它们全是发行版自带的。
    """
    if "@" in unit:                      # 模板单元：Ubuntu 自带的 getty@ / fsck@ / user@ 等
        return unit not in PROTECTED_UNITS
    if unit not in UNIT_PROBED:          # 单独查询该单元（未被批量路径覆盖）
        _unit_props([unit])
    p = UNIT_PATH_CACHE.get(unit, "")
    return p.startswith(DISTRO_UNIT_DIRS)


def _docker_autostart(names):
    """容器的 restart policy。

    一次 `docker inspect` 获取全部容器（原实现为每个容器派生一次 inspect，20ms × N）；
    批量失败时（例如某容器恰在期间被删除）才退化为逐个查询，行为与原先一致。
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
