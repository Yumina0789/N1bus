"""n1bus 的数据源层：真机采样器 Sampler、真机数据源 HostProbe，以及演示模式的假数据源 _DemoServices / _DemoDocker / _DemoSecurity / _DemoPorts / _DemoProcesses / DemoProbe。"""

import collections
import json
import math
import os
import shutil
import sys
import threading
import time

from n1bus_util import (LOG_DIRS, NAME_RE, REAL_FS, UNIT_RE, dur_text, host_info,
                        now_iso, read_text, run)
from n1bus_actions import PROTECTED_UNITS
from n1bus_files import (FILE_EDIT_MAX, FILE_MAX_ENTRIES, FILE_ROOTS, FILE_TEXT_LIMIT,
                         FILE_UPLOAD_MAX, _human_size, _size_bytes, _tail_file,
                         safe_name, safe_path, safe_write_path)
from n1bus_system import (DISTRO_UNIT_DIRS,
                          ETC_UNIT_DIR, LOG_TIME_RE, Docker, Ports, Processes,
                          Services, _all_service_units, _docker_autostart,
                          _etc_service_units, _is_distro_unit, _unit_props)
from n1bus_security import Security


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

        真机上 /、/etc、/home、/root 常为同一分区的 bind mount，逐一列出只会
        重复同一个数值。取挂载路径最短的那个（通常是 /）。
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
        """在无 /proc 的环境（例如 Windows 上运行演示模式）下返回一组仿真数据，避免界面各项显示为 0。"""
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
                # 先采样再进入 sleep：否则启动后最初几秒 latest() 为空，界面各项为 0
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

    # ---------------- 系统清理 / 网络 / 磁盘（参考 1Panel 的实现） ----------------
    def cleanup(self):
        """空间回收统计：此处只做统计，不执行删除；实际删除一律走白名单动作。"""
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
        """备份清单：包含哪些数据、占用大小与上次备份时间。

        恢复流程刻意不自动化：它会覆盖正在运行的面板数据与容器卷，一旦出错将导致面板自身不可用。
        页面提供 SSH 恢复命令，由管理员手动执行。
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
                "restore_hint": "tar -xzf latest.tar.gz -C / && systemctl restart n1bus"}

    def files(self, path=""):
        """只读列目录：白名单根之下，最多 200 条。"""
        if not path:
            target = os.path.realpath(FILE_ROOTS[0])
        else:
            # 分三种情况给出三种提示：越界 / 不存在 / 不是目录。统一返回一句
            # 「不在允许浏览的目录里」会让用户误以为是自己输入错误，而实际可能是越权访问。
            inside = safe_path(path)
            if inside is None:
                return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                        "truncated": False,
                        "error": "该路径不在允许浏览的白名单内，不可访问：%s" % str(path)[:120]}
            if not os.path.lexists(inside):
                return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                        "truncated": False, "error": "路径不存在：%s" % inside}
            if not os.path.isdir(inside):
                return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                        "truncated": False, "error": "目标不是目录，而是文件：%s" % inside}
            target = inside
        if target is None:
            return {"path": path, "parent": "", "roots": list(FILE_ROOTS), "entries": [],
                    "truncated": False, "error": "路径不在允许浏览的目录内"}
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
        # 前端据此标记只读白名单根，避免用户在多次尝试后才发现无法写入。
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
                    "error": "路径不在允许浏览的目录内，或不是普通文件"}
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
        """读取完整文本用于编辑。超过上限或为二进制时一律拒绝，并引导用户改用下载，避免面板进程被大文件拖慢。"""
        real = safe_path(path, want_dir=False)
        if real is None:
            return {"ok": False, "error": "路径不在允许浏览的目录内，或不是普通文件"}
        try:
            size = os.path.getsize(real)
            if size > FILE_EDIT_MAX:
                return {"ok": False, "error": "文件 %s 超过面板可编辑上限（%s），请改用下载"
                        % (_human_size(size), _human_size(FILE_EDIT_MAX))}
            with open(real, "rb") as fh:
                blob = fh.read()
        except OSError as ex:
            return {"ok": False, "error": str(ex)[:140]}
        if b"\x00" in blob[:8192]:
            return {"ok": False, "error": "二进制文件不支持在线编辑（可下载）"}
        return {"ok": True, "path": real, "size": size,
                "text": blob.decode("utf-8", "replace")}

    def _backup(self, real):
        """覆盖前保存一份 .bak。备份失败不构成阻断：不应因备份失败而拒绝写入。"""
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
            return {"ok": False, "error": "文件大小超过单次上传上限（%s）" % _human_size(FILE_UPLOAD_MAX)}
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
        """删除。目录默认仅允许空目录 —— 如需连同内容递归删除，必须显式传入 recursive。"""
        real, err = safe_write_path(path, must_exist=True)
        if err:
            return {"ok": False, "error": err}
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                if recursive:
                    shutil.rmtree(real)
                else:
                    if os.listdir(real):
                        return {"ok": False, "error": "目录非空；如需连同内容一并删除，请勾选「连同内容」"}
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
          用户看到的不是"按钮变成「开机自启」"，而是"这一行没了"。
        * 只列**用户/第三方装的**单元（判据不是名字前缀，漏得多，而是**单元文件在哪**）：
          直接从 /etc/systemd/system 读，一次 scandir ~1 ms；以前跑
          `systemctl list-unit-files` 要 0.95 s（带 --state filter 1.84 s），
          而每次写操作后缓存失效都会重新付出该开销 —— 这才是「点击按钮后等待约一秒」的根本原因。
          受保护单元始终保留，以免它们从界面上消失。
        """
        names = _etc_service_units()
        if names is None:                    # 目录不可读：回退到全量扫描，较慢但结果完整
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
                          # masked 同理。为其提供切换按钮会导致操作必然失败且状态回退，属于误导性界面。
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
                "note": "ssh / 网络 / docker / 面板等受保护，不允许取消开机自启"}

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

        # timedatectl 不可用（容器里常见）时，NTP 与同步状态都读不到。
        # 读不到必须与「读了、结果是 no」区分开：否则界面会把「无法判断」印成「已关闭」，
        # 用户按这个结论去改配置，改的是一件本来没坏的事。所以这里给三态。
        def _tri(v):
            v = (v or "").strip().lower()
            return True if v == "yes" else (False if v == "no" else None)

        return {"timezone": kv.get("Timezone", ""),
                "ntp": _tri(kv.get("NTP")),
                "ntp_synced": _tri(kv.get("NTPSynchronized")),
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
            # operstate 原样带上：只有它等于 DOWN 才算真的断开。
            # 回环、bridge、tun 这类接口的 operstate 是 UNKNOWN（内核不实现载波检测），
            # 若把「不是 UP」一律当成断开，界面就会把 lo 标成「已断开」。
            ifaces.append({"name": name, "up": l.get("operstate") == "UP", "kind": kind,
                           "operstate": l.get("operstate", ""),
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
        """磁盘与挂载只读：lsblk 树 + df；SMART 依赖 smartmontools，未安装时在结果中说明。"""
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
        cs = [("caddy", "caddy:2-alpine", "Up 26 minutes", True),
              ("redis", "redis:7-alpine", "Up 3 minutes (healthy)", True),
              ("old-test", "alpine:3.19", "Exited (0) 2 days ago", False)]
        out = []
        for name, image, status, running in cs:
            out.append({"id": (name + "0123456789ab")[:12], "name": name, "image": image,
                        "state": "running" if running else "exited", "status": status,
                        "ports": "6379:6379" if name == "redis" else ("80:80, 443:443" if name == "caddy" else ""),
                        "running": running,
                        "stats": {"cpu": "0.42%", "mem": "48MiB / 1.94GiB", "mem_pct": "2.4%",
                                  "net": "1.2MB / 860kB", "pids": "3"} if running else {}})
        return {"available": True, "containers": out, "error": "",
                "running": sum(1 for c in out if c["running"])}

    def dsh(self):
        # 该接口已下线，入口由模块系统提供。演示数据保留一个明显无效的返回值。
        return {"available": False,
                "hint": "该接口已下线，模块入口见 /api/modules/entry"}

    def logs(self, name, lines=200, since="", until="", grep="", timestamps=False):
        # 校验与真机保持一致：演示模式同样不接受非法参数或非法容器名
        if not NAME_RE.match(name or ""):
            return {"available": False, "lines": [], "cmd": "", "error": "容器名不合法"}
        for flag, val in (("--since", since), ("--until", until)):
            v = (val or "").strip()
            if v and not LOG_TIME_RE.match(v):
                return {"available": False, "lines": [], "cmd": "",
                        "error": "%s 只接受 30m / 2h / 1d 或 2026-10-09T05:00 格式的时间" % flag}
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
             ("tcp", "0.0.0.0:6379", "6379", "docker-proxy", "37348", True),
             ("tcp", "127.0.0.1:5432", "5432", "postgres", "37355", False),
             ("tcp", "127.0.0.1:8402", "8402", "python3", "41022", False)]

    def listening(self):
        ports = [{"proto": p, "local": l, "addr": l.rsplit(":", 1)[0] or "*", "port": port,
                  "process": proc, "pid": pid, "exposed": exp}
                 for p, l, port, proc, pid, exp in self.PORTS]
        return {"available": True, "ports": ports, "error": "",
                "exposed": sum(1 for p in ports if p["exposed"])}

    def established(self, limit=40):
        return {"available": True,
                "connections": [{"proto": "tcp", "local": "38.76.207.46:22",
                                 "peer": "39.144.169.199:51234"}],
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

    # --- 文件管理（演示：仅返回响应，不访问真实文件系统）------------------

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
            # 一个"未启用"的单元 —— 界面上它应该显示「开机自启」按钮
            {"unit": "nginx.service", "state": "disabled", "enabled": False, "protected": False,
             "running": False}],
            "containers": [{"name": "caddy", "policy": "unless-stopped", "autostart": True},
                           {"name": "redis", "policy": "always", "autostart": True}],
            "note": "ssh / 网络 / docker / 面板等受保护，不允许取消开机自启"}

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
            {"key": "vol:docker_redis-data", "label": "Docker 卷 docker_redis-data",
             "path": "/var/lib/docker/volumes/docker_redis-data", "exists": True, "size": 1048576},
            {"key": "vol:docker_caddy-data", "label": "Docker 卷 docker_caddy-data",
             "path": "/var/lib/docker/volumes/docker_caddy-data", "exists": True, "size": 4194304},
            {"key": "caddy-conf", "label": "Caddy 配置与站点文件",
             "path": "/opt/nimbus/docker", "exists": True, "size": 40960},
            {"key": "vlmcsd", "label": "vlmcsd 配置", "path": "/etc/vlmcsd",
             "exists": True, "size": 8192}],
            "archive": {"exists": True, "path": "/var/lib/n1bus/backups/latest.tar.gz",
                        "size": 1372160, "mtime": "2026-10-09 06:40:00"},
            "action": "backup.create",
            "restore_hint": "tar -xzf latest.tar.gz -C / && systemctl restart n1bus"}

    def netinfo(self):
        # 演示数据也带上 kind，好让界面默认收起虚拟网卡这条逻辑也能被看到
        return {"interfaces": [
            {"name": "eth0", "up": True, "kind": "physical", "operstate": "UP",
             "mac": "00:16:3e:1f:2a:3b", "mtu": 1500, "addrs": ["10.0.0.4/24", "fe80::1/64"]},
            {"name": "lo", "up": False, "kind": "loopback", "operstate": "UNKNOWN",
             "mac": "00:00:00:00:00:00", "mtu": 65536, "addrs": ["127.0.0.1/8"]},
            {"name": "docker0", "up": True, "kind": "virtual", "operstate": "UP",
             "mac": "02:42:1a:2b:3c:4d", "mtu": 1500, "addrs": ["172.17.0.1/16"]},
            {"name": "veth1a2b3c4d", "up": True, "kind": "virtual", "operstate": "UP",
             "mac": "aa:bb:cc:dd:ee:01", "mtu": 1500, "addrs": []},
            {"name": "veth5e6f7a8b", "up": True, "kind": "virtual", "operstate": "UP",
             "mac": "aa:bb:cc:dd:ee:02", "mtu": 1500, "addrs": []}],
                "routes": [{"dst": "default", "gateway": "10.0.0.1", "dev": "eth0", "metric": 100}],
                "nameservers": ["8.8.8.8", "1.1.1.1"],
                "resolv_conf": "nameserver 8.8.8.8\nnameserver 1.1.1.1",
                "docker_published": [{"container": "caddy", "ports": "0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp"},
                                     {"container": "redis", "ports": "0.0.0.0:6379->6379/tcp"}],
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
