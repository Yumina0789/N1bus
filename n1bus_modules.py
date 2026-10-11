"""n1bus 的模块域：商店清单、模块包的校验、下载与解包。

规格见 `docs/modules.md`。

**边界（很重要）：本文件不执行模块提供的任何东西。** 它只做四件事 ——
从商店取清单、下载包、按 sha256 校验、安全解包到 `/opt/n1bus/modules/<id>/`。
容器的编排交给 `n1bus_actions` 里 argv 写死的 `module.*` 动作，由 HTTP 层把两步串起来。
这样 ADR 0002 的边界（面板不执行外部提供的命令）在模块系统里依然成立。
"""

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import time
import urllib.request
from pathlib import Path

from n1bus_util import VERSION, read_text


# 商店 = registry 仓库的 GitHub Pages 静态站（见 docs/modules.md §4）。
# 本体只认这一个来源，**不提供**「填任意仓库 URL」的入口。
STORE_URL = os.environ.get("N1BUS_STORE_URL") or "https://yumina0789.github.io/N1bus-modules"

STORE_TTL = 300.0            # 商店清单的内存缓存时长（秒）
MODULES_DIR = "/opt/n1bus/modules"


def modules_dir():
    """模块根目录。

    **不要**写 `from n1bus_modules import MODULES_DIR` —— 那样拿到的是导入那一刻的
    快照；n1bus.py 在演示模式下会把它改写到数据目录下（`/opt` 在开发机上不可写，
    而安装流程必须真落盘才能演练到底），改写传不过去。动作层一律走这个函数。
    """
    return MODULES_DIR


PACKAGE_MAX = 32 << 20       # 单个模块包上限 32 MB
UNPACK_MAX = 64 << 20        # 解包后总体积上限
INDEX_MAX = 512 << 10        # index.json 上限 512 KB

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
VER_RE = re.compile(r"^\d+(?:\.\d+)*$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ICON_RE = re.compile(r"^[a-z0-9-]{1,32}$")


class ModuleError(Exception):
    """模块包或清单不合法。消息会直接进审计与界面。"""


def _ver_tuple(v):
    """把版本号折成可比较的元组（`1.0.10` > `1.0.9`）。"""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def _now():
    return time.time()


# --------------------------------------------------------------------------
# 商店清单
# --------------------------------------------------------------------------
def check_index(raw, store_url=STORE_URL):
    """校验一份 index.json。返回 (ok, error, 规范化后的 modules 列表)。"""
    if not isinstance(raw, dict):
        return False, "index.json 不是对象", []
    if raw.get("schema") != 1:
        return False, "不认识的 schema：%r" % (raw.get("schema"),), []
    mods = raw.get("modules")
    if not isinstance(mods, list):
        return False, "modules 不是数组", []
    clean = []
    for i, m in enumerate(mods):
        if not isinstance(m, dict):
            return False, "modules[%d] 不是对象" % i, []
        mid = str(m.get("id") or "")
        if not ID_RE.match(mid):
            return False, "modules[%d].id 不合法：%r" % (i, mid), []
        versions = m.get("versions")
        if not isinstance(versions, dict) or not versions:
            return False, "%s 没有 versions" % mid, []
        norm = {}
        for ver, spec in versions.items():
            if not VER_RE.match(str(ver)):
                return False, "%s 的版本号不合法：%r" % (mid, ver), []
            if not isinstance(spec, dict):
                return False, "%s %s 的版本信息不是对象" % (mid, ver), []
            sha = str(spec.get("sha256") or "").lower()
            if not SHA_RE.match(sha):
                return False, "%s %s 的 sha256 不是 64 位十六进制" % (mid, ver), []
            size = spec.get("size")
            if not isinstance(size, int) or size <= 0 or size > PACKAGE_MAX:
                return False, "%s %s 的 size 不合法：%r" % (mid, ver, size), []
            url = str(spec.get("url") or "")
            if not url or url.startswith("//"):
                return False, "%s %s 的 url 不合法：%r" % (mid, ver, url), []
            if url.startswith(("http://", "https://")) and not url.startswith(
                    (store_url or "").rstrip("/") + "/"):
                # 绝对地址只接受商店站内的，避免清单把本体指到任意主机
                return False, "%s %s 的 url 不在商店站内：%r" % (mid, ver, url), []
            norm[str(ver)] = {"url": url, "sha256": sha, "size": size,
                              "min_core": str(spec.get("min_core") or "")}
        latest = str(m.get("latest") or "")
        if latest not in norm:
            return False, "%s 的 latest（%r）不在 versions 里" % (mid, latest), []
        if _ver_tuple(latest) != max(_ver_tuple(v) for v in norm):
            return False, "%s 的 latest 不是最高版本" % mid, []
        clean.append({"id": mid, "name": str(m.get("name") or mid),
                      "summary": str(m.get("summary") or ""),
                      "icon": str(m.get("icon") or ""),
                      "homepage": str(m.get("homepage") or ""),
                      "latest": latest, "versions": norm})
    return True, "", clean


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def check_manifest(raw, entry=None):
    """校验一份 manifest.json。返回 (ok, error, 规范化后的 dict)。

    字段契约见 `docs/modules.md` §5。**任何一项不通过都直接拒绝**，不做「尽力而为」的
    兼容 —— 清单是外部内容，宽松解析等于给它更多可乘之机。
    """
    if not isinstance(raw, dict):
        return False, "manifest 不是对象", {}
    if raw.get("schema") != 1:
        return False, "不认识的 schema：%r" % (raw.get("schema"),), {}
    mid = str(raw.get("id") or "")
    if not ID_RE.match(mid):
        return False, "id 不合法（应为 ^[a-z][a-z0-9-]{1,31}$）：%r" % mid, {}
    if entry is not None and entry.get("id") != mid:
        return False, "manifest 的 id（%s）与商店清单（%s）不一致" % (mid, entry.get("id")), {}
    ver = str(raw.get("version") or "")
    if not VER_RE.match(ver):
        return False, "version 不合法：%r" % ver, {}
    if entry is not None and ver not in (entry.get("versions") or {}):
        return False, "version %s 不在商店清单里" % ver, {}

    icon = str(raw.get("icon") or "")
    if icon and not ICON_RE.match(icon):
        return False, "icon 名不合法：%r" % icon, {}

    comp = raw.get("compose")
    if comp is not None:
        if not isinstance(comp, dict):
            return False, "compose 不是对象", {}
        # 本体不信任任意 project 名：它决定 docker compose 的命名空间
        if str(comp.get("project") or "") != "n1bus-" + mid:
            return False, "compose.project 必须等于 n1bus-%s" % mid, {}
        fname = str(comp.get("file") or "")
        if not fname or "/" in fname or "\\" in fname or fname.startswith("."):
            return False, "compose.file 必须是包内的简单文件名：%r" % fname, {}

    ports = raw.get("ports") or []
    if not isinstance(ports, list):
        return False, "ports 不是数组", {}
    for p in ports:
        if not isinstance(p, dict):
            return False, "ports 的元素不是对象", {}

    page = raw.get("page") or {}
    if not isinstance(page, dict):
        return False, "page 不是对象", {}
    if page.get("kind") != "iframe":
        return False, "page.kind 目前只支持 iframe（收到 %r）" % page.get("kind"), {}
    url = str(page.get("url") or "")
    if not url.startswith("/") or "//" in url or ".." in url:
        return False, "page.url 必须是站内绝对路径，例如 /m/%s/" % mid, {}

    return True, "", {
        "schema": 1, "id": mid, "version": ver,
        "name": str(raw.get("name") or mid),
        "summary": str(raw.get("summary") or ""),
        "icon": icon, "homepage": str(raw.get("homepage") or ""),
        "compose": ({"file": str(comp.get("file")), "project": str(comp.get("project"))}
                    if comp is not None else None),
        "ports": ports,
        "page": {"kind": "iframe", "url": url,
                 "title": str(page.get("title") or raw.get("name") or mid)},
    }


# --------------------------------------------------------------------------
# 下载、校验、解包
# --------------------------------------------------------------------------
def _download(url, dest, limit):
    """下载到 dest，边下边算 sha256，超过 limit 立即中断。返回 (字节数, sha256)。"""
    h = hashlib.sha256()
    n = 0
    req = urllib.request.Request(url, headers={"User-Agent": "n1bus-modules/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
        while True:
            chunk = r.read(128 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                raise ModuleError("模块包超过上限 %d 字节" % limit)
            h.update(chunk)
            fh.write(chunk)
    return n, h.hexdigest()


def safe_extract(tar_path, dest, max_total=UNPACK_MAX):
    """防穿越解包。

    `tar_path` 既可以是路径，也可以是 file-like —— 演示模式用的是内存里现造的包。

    比 `tarfile.extractall()` 更保守：**逐个成员自己写文件**，完全不依赖 tarfile 的
    路径拼接。绝对路径、`..`、符号/硬链接、设备与 FIFO 一律拒绝；解出来的权限位去掉
    组/其他可写位。导入一个模块不该有能力写到目标目录之外。
    """
    total = 0
    dest = os.path.realpath(dest)
    cm = (tarfile.open(fileobj=tar_path, mode="r:gz") if hasattr(tar_path, "read")
          else tarfile.open(tar_path, "r:gz"))
    with cm as tf:
        for m in tf.getmembers():
            name = m.name
            if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
                raise ModuleError("包里含有越界路径：%s" % name)
            if m.issym() or m.islnk():
                raise ModuleError("包里含有链接，已拒绝：%s" % name)
            if not (m.isfile() or m.isdir()):
                raise ModuleError("包里含有非常规条目，已拒绝：%s" % name)
            target = os.path.join(dest, *Path(name).parts)
            if not (target == dest or target.startswith(dest + os.sep)):
                raise ModuleError("解包目标越界：%s" % name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            total += m.size
            if total > max_total:
                raise ModuleError("解包后体积超过上限 %d 字节" % max_total)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                raise ModuleError("读不出包内文件：%s" % name)
            with src, open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)
            os.chmod(target, m.mode & 0o777 & ~0o022)
    return total


# --------------------------------------------------------------------------
# 模块管理
# --------------------------------------------------------------------------
class Modules:
    """商店清单 + 已装模块。**不碰 docker** —— 容器编排由 module.* 动作负责。"""

    def __init__(self, root=MODULES_DIR, store_url=STORE_URL, dry=False):
        self.root = Path(root)
        self.store_url = (store_url or "").rstrip("/")
        self.dry = dry
        self._index = None
        self._index_at = 0.0

    # --- 商店 ------------------------------------------------------------
    def store(self, force=False):
        """商店清单。拿不到时**如实报告不可用**，不编造模块列表。"""
        if self.dry:
            return {"available": True, "url": self.store_url, "demo": True,
                    "modules": _DEMO_INDEX}
        now = _now()
        if not force and self._index and now - self._index_at < STORE_TTL:
            return self._index
        if not self.store_url:
            return {"available": False, "url": "", "modules": [],
                    "hint": "没有配置商店地址"}
        try:
            req = urllib.request.Request(self.store_url + "/index.json",
                                        headers={"User-Agent": "n1bus-modules/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read(INDEX_MAX + 1)
            if len(raw) > INDEX_MAX:
                raise ModuleError("index.json 超过上限")
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            out = {"available": False, "url": self.store_url, "modules": [],
                   "hint": "商店不可达（%s）：%s" % (self.store_url, e)}
            # 失败也进缓存：否则界面每次轮询都要等一次超时，页面会跟着卡。
            # 用户点「刷新商店」时带 ?fresh=1，会绕过这层缓存直接回源。
            self._index, self._index_at = out, now
            return out
        good, err, mods = check_index(data, self.store_url)
        if not good:
            out = {"available": False, "url": self.store_url, "modules": [],
                   "hint": "商店清单不合法：%s" % err}
            self._index, self._index_at = out, now
            return out
        out = {"available": True, "url": self.store_url, "modules": mods,
               "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._index, self._index_at = out, now
        return out

    def entry(self, mid):
        for m in self.store().get("modules") or []:
            if m["id"] == mid:
                return m
        return None

    # --- 已装 ------------------------------------------------------------
    def installed(self):
        """已装的模块（读各自的 manifest.json）。坏目录直接跳过，不让它拖垮整页。"""
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            mf = d / "manifest.json"
            if not mf.is_file():
                continue
            try:
                raw = json.loads(read_text(mf))
            except Exception:  # noqa: BLE001
                continue
            good, _err, clean = check_manifest(raw)
            if not good:
                continue
            clean["path"] = str(d)
            clean["builtin"] = False
            out.append(clean)
        return out

    def installed_ids(self):
        return [m["id"] for m in self.installed()]

    # --- 安装 ------------------------------------------------------------
    def fetch(self, mid, version=None):
        """下载并落盘一个模块包。返回 (ok, error, manifest)。

        **只到"落盘 + manifest 校验通过"为止**；起容器是调用方的事（module.install）。
        先解到临时目录，全部校验通过后再整体搬过去 —— 失败时服务器上不会留下半个模块。
        """
        if self.dry:
            return self._demo_fetch(mid, version)
        if not ID_RE.match(mid or ""):
            return False, "模块 id 不合法：%r" % mid, {}
        entry = self.entry(mid)
        if entry is None:
            return False, "商店里没有这个模块：%s" % mid, {}
        ver = version or entry["latest"]
        spec = (entry.get("versions") or {}).get(ver)
        if spec is None:
            return False, "商店里没有 %s %s" % (mid, ver), {}
        mc = spec.get("min_core") or ""
        if mc and _ver_tuple(mc) > _ver_tuple(VERSION):
            return False, "%s %s 要求本体 ≥ %s（当前 %s）" % (mid, ver, mc, VERSION), {}
        url = spec["url"]
        if not url.startswith(("http://", "https://")):
            url = self.store_url + "/" + url.lstrip("/")

        tmp = self.root / (".tmp-" + mid)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        pkg = tmp / "package.tar.gz"
        try:
            size, sha = _download(url, str(pkg), PACKAGE_MAX)
            # 先比 size（低成本粗筛），再比 sha256（权威）
            if size != spec["size"]:
                raise ModuleError("size 不符：清单 %d，实际 %d" % (spec["size"], size))
            if sha != spec["sha256"]:
                raise ModuleError("sha256 不符：清单 %s，实际 %s" % (spec["sha256"], sha))
            unpack = tmp / "unpack"
            unpack.mkdir(parents=True, exist_ok=True)
            safe_extract(str(pkg), str(unpack))
            mfs = list(unpack.rglob("manifest.json"))
            if len(mfs) != 1:
                raise ModuleError("包里必须恰好有一个 manifest.json（找到 %d 个）" % len(mfs))
            mroot = mfs[0].parent
            good, err, clean = check_manifest(json.loads(read_text(mfs[0])), entry)
            if not good:
                raise ModuleError("manifest 校验不通过：%s" % err)
            if clean["version"] != ver:
                raise ModuleError("manifest 的 version（%s）与清单（%s）不一致"
                                  % (clean["version"], ver))
            if clean.get("compose"):
                cf = mroot / clean["compose"]["file"]
                if not cf.is_file():
                    raise ModuleError("manifest 声明的 compose 文件不存在：%s"
                                      % clean["compose"]["file"])
            # 就地落盘：把 manifest 所在目录整体当作模块根
            dest = self.root / mid
            staging = self.root / (".new-" + mid)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            shutil.move(str(mroot), str(staging))
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(staging), str(dest))
            clean["path"] = str(dest)
            return True, "", clean
        except Exception as e:  # noqa: BLE001
            return False, "%s: %s" % (type(e).__name__, e), {}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _demo_fetch(self, mid, version):
        """演示模式：造一个最小的合法模块包，把「解包 → 校验 → 落盘」真实跑一遍。

        刻意**不**跳过落盘 —— 否则演示模式下 `module.install` 的「该模块没有安装」
        校验必然失败，整条安装链在演示模式里根本无法演练。
        """
        ver = version or "1.0.0"
        man = {"schema": 1, "id": mid, "version": ver, "name": mid,
               "compose": {"file": "compose.yml", "project": "n1bus-" + mid},
               "page": {"kind": "iframe", "url": "/m/%s/" % mid}}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, text in (("manifest.json", json.dumps(man, ensure_ascii=False)),
                               ("compose.yml", "services:\n  %s:\n    image: demo\n" % mid)):
                data = text.encode("utf-8")
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        buf.seek(0)
        dest = self.root / mid
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True)
            safe_extract(buf, str(dest))
            good, err, clean = check_manifest(json.loads(read_text(dest / "manifest.json")))
            if not good:
                raise ModuleError(err)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(dest, ignore_errors=True)
            return False, "%s: %s" % (type(e).__name__, e), {}
        clean["path"] = str(dest)
        clean["demo"] = True
        return True, "", clean

    def remove(self, mid):
        """移走模块目录（不含容器的停止 —— 那由 module.stop 动作负责）。"""
        if not ID_RE.match(mid or ""):
            return False, "模块 id 不合法：%r" % mid
        d = self.root / mid
        if not d.is_dir():
            return False, "没有安装这个模块：%s" % mid
        try:
            shutil.rmtree(d)
        except OSError as e:
            return False, "删除模块目录失败：%s" % e
        return True, ""


# 演示模式下的商店清单：结构与真机一致，界面代码不用分叉。
_DEMO_INDEX = [
    {"id": "rsc4d", "name": "KMS 激活服务", "summary": "rs-c4d：KMS wire 协议服务端（v4/v5/v6）",
     "icon": "key", "homepage": "https://github.com/Yumina0789/RSC4D", "latest": "1.0.0",
     "versions": {"1.0.0": {"url": "packages/rsc4d-1.0.0.tar.gz", "sha256": "0" * 64,
                            "size": 4096, "min_core": "1.0"}}},
    {"id": "harness", "name": "DeepSeek Harness", "summary": "容器里的 agent 面板",
     "icon": "plug", "homepage": "https://github.com/deepseek-ai/dsh", "latest": "1.0.0",
     "versions": {"1.0.0": {"url": "packages/harness-1.0.0.tar.gz", "sha256": "0" * 64,
                            "size": 4096, "min_core": "1.0"}}},
]
