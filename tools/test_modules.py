#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块配置落盘的回归测试：纯函数级，不起服务、不碰 docker。

    python3 tools/test_modules.py

这里锁住的每一条都对应一次**踩过的坑**或一个已经写进规格的契约：

  1. `config.json` 在宿主上被 Docker 建成**目录** → 保存永远 Errno 21，配置再也存不进去
     （rsc4d 因此断过几分钟；上游第一版修复只覆盖「安装 / 升级」，早已装好的模块不会自愈）
  2. 保存时整体重写 `.env` → README 让人手工设的 `RSC4D_IMAGE` 被抹掉，
     之后重建容器回退到默认镜像名，起不来
  3. 改一个**纯展示项**（`apply: file`）也把整套容器 `--force-recreate` → KMS 端口跟着断
  4. 写 `config.json` 失败、`.env` 却已经改了 → 半截状态，会在下一次重建时突然生效
  5. `config.json` 必须**原地改写**（inode 不变）—— 单文件 bind mount 认的是创建时的
     那个 inode，换成「写临时文件再 rename」的原子写法，容器里就永远读不到新内容
"""
import hashlib
import io
import json
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:                       # Windows 控制台默认 GBK，✔ 这类字符会导致编码错误；CI 上为 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:          # noqa: BLE001
    pass

import n1bus_actions as NA       # noqa: E402
import n1bus_modules as NM       # noqa: E402

fails = []


def ok(cond, msg):
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        fails.append(msg)


MID = "tmod"

MANIFEST = {
    "schema": 1, "id": MID, "version": "1.0.0", "name": "配置落盘测试模块",
    "summary": "只用于回归测试",
    "compose": {"file": "compose.yml", "project": "n1bus-" + MID},
    "config": [
        # 纯展示项：值只被模块容器当场按请求读，写文件就够了
        {"key": "TMOD_HOST", "label": "对外域名", "type": "text",
         "default": "", "apply": "file"},
        # 默认（restart）：值进容器，必须让容器按新值重来
        {"key": "TMOD_LIMIT", "label": "上限", "type": "number",
         "default": 60, "min": 1, "max": 1000},
        {"key": "TMOD_ON", "label": "开关", "type": "bool", "default": "0"},
    ],
    "page": {"kind": "iframe", "url": "/m/" + MID + "/"},
}

# 单文件挂载 —— 正是「Docker 会替你建目录」的那个形状
COMPOSE = (
    "services:\n"
    "  web:\n"
    "    image: caddy:2-alpine\n"
    "    volumes:\n"
    "      - ./config.json:/cfg/config.json:ro\n"
)


def make_module(root, manifest=None):
    d = root / MID
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps(manifest or MANIFEST, ensure_ascii=False), encoding="utf-8")
    (d / "compose.yml").write_text(COMPOSE, encoding="utf-8")
    return d


def env_text(d):
    return (d / NM.CONFIG_ENV).read_text(encoding="utf-8")


work = pathlib.Path(tempfile.mkdtemp())


# --------------------------------------------------------------------------
print("== manifest：config 的 apply 字段 ==")
# --------------------------------------------------------------------------
def cfg_item(**kw):
    base = {"key": "TMOD_X", "label": "x", "type": "text", "default": ""}
    base.update(kw)
    return base


base = dict(MANIFEST)
good, err, clean = NM.check_manifest(dict(base, config=[cfg_item()]))
ok(good and clean["config"][0]["apply"] == "restart",
   "没写 apply 时缺省是 restart（老 manifest 语义不变）：%s" % (err or "ok"))

good, err, clean = NM.check_manifest(dict(base, config=[cfg_item(apply="file")]))
ok(good and clean["config"][0]["apply"] == "file", "apply=file 通过：%s" % (err or "ok"))

good, err, _c = NM.check_manifest(dict(base, config=[cfg_item(apply="hot")]))
ok(not good, "不认识的 apply 被拒绝：%s" % err)


# --------------------------------------------------------------------------
print("== save_config：正常路径 ==")
# --------------------------------------------------------------------------
# store_url="" —— 这个测试一个网络请求都不发：update_for 由我们直接喂 store，
# 而 fetch 的降级闸门在它去查商店**之前**就会拦下来。
mods = NM.Modules(root=str(work), store_url="")
d = make_module(work)

ok4, err, saved, apply_ = mods.save_config(MID, {})
ok(ok4, "首次按默认值落盘成功：%s" % (err or "ok"))
ok(apply_ == "restart",
   "首次（.env 还不存在）每一项都算「变了」→ restart：容器得先按这些变量起来")
ok((d / NM.CONFIG_JSON).is_file(), "config.json 是**文件**（不是 Docker 建出来的目录）")
ok(isinstance(saved, dict) and saved.get("TMOD_LIMIT") == "60",
   "saved 回给界面的是写下去的值：%s" % (saved,))

cfg = json.loads((d / NM.CONFIG_JSON).read_text(encoding="utf-8"))
ok(cfg.get("TMOD_LIMIT") == "60" and cfg.get("TMOD_ON") == "0",
   "config.json 与 .env 是同一个来源：%s" % (cfg,))


# --------------------------------------------------------------------------
print("== 回归：config.json 被 Docker 建成了目录 ==")
# --------------------------------------------------------------------------
cf = d / NM.CONFIG_JSON
cf.unlink()
cf.mkdir()
ok(cf.is_dir(), "先把 config.json 造成目录，复现线上那个状态")
ok4, err, saved, apply_ = mods.save_config(MID, {"TMOD_HOST": "kms.example.com"})
ok(ok4 and cf.is_file(),
   "保存成功，并把它换回了文件（不再要用户卸载重装自救）：%s" % (err or "ok"))
ok(json.loads(cf.read_text(encoding="utf-8")).get("TMOD_HOST") == "kms.example.com",
   "换回文件后内容是对的")


# --------------------------------------------------------------------------
print("== 回归：.env 里非本模块声明的变量必须保留 ==")
# --------------------------------------------------------------------------
(d / NM.CONFIG_ENV).write_text(
    env_text(d) + "RSC4D_IMAGE=registry.local:5000/rs-c4d:v9\n", encoding="utf-8")
ok4, err, saved, apply_ = mods.save_config(MID, {"TMOD_HOST": "kms.example.com"})
env = NM._read_env(d / NM.CONFIG_ENV)
ok(env.get("RSC4D_IMAGE") == "registry.local:5000/rs-c4d:v9",
   "手工设的 RSC4D_IMAGE 没被保存动作抹掉：%s" % (env.get("RSC4D_IMAGE"),))
ok(env.get("TMOD_HOST") == "kms.example.com", "声明的项照常写下去")
ok("RSC4D_IMAGE" not in json.loads((d / NM.CONFIG_JSON).read_text(encoding="utf-8")),
   "它没有被塞进 config.json（那是模块声明的项的地盘）")


# --------------------------------------------------------------------------
print("== apply 判定：只改纯展示项时不该动容器 ==")
# --------------------------------------------------------------------------
ok4, err, saved, apply_ = mods.save_config(MID, {"TMOD_HOST": "kms2.example.com"})
ok(ok4 and apply_ == "none", "只改 apply=file 的项 → none（原来是 restart，改个域名也重建三个容器）")

ok4, err, saved, apply_ = mods.save_config(MID, {"TMOD_HOST": "kms2.example.com",
                                                 "TMOD_LIMIT": "120"})
ok(ok4 and apply_ == "restart", "同时改了要进容器的项 → restart")

ok4, err, saved, apply_ = mods.save_config(MID, {"TMOD_HOST": "kms2.example.com",
                                                 "TMOD_LIMIT": "120"})
ok(ok4 and apply_ == "none", "什么都没变 → none（不该白重建一次）")

ok4, err, saved, apply_ = mods.save_config(MID, {"TMOD_LIMIT": "120", "TMOD_ON": "1"})
ok(ok4 and apply_ == "restart",
   "纯 bool 项只有当它真的翻转时才要重启；这里它变了 → restart")


# --------------------------------------------------------------------------
print("== 回归：写 config.json 失败时不许留下半截 .env ==")
# --------------------------------------------------------------------------
cf.unlink()
cf.mkdir()                       # 先造成目录，下面让「删掉它」这一步失败
before_env = env_text(d)
_real_rmtree = shutil.rmtree


def _boom(*a, **k):
    raise OSError("模拟删不掉（例如权限不足）")


# NM.shutil 就是 shutil 模块本身，改完必须还原
NM.shutil.rmtree = _boom
try:
    ok4, err, saved, apply_ = mods.save_config(MID, {"TMOD_HOST": "should-not-land.example.com"})
finally:
    NM.shutil.rmtree = _real_rmtree
ok(not ok4 and "写配置文件失败" in err, "config.json 写不进去 → 如实报错：%s" % err)
ok(before_env == env_text(d),
   "此时 .env 一个字都没改 —— 否则下一次 `up -d` 会悄悄用上这份没保存成功的值")
ok(apply_ == "none", "失败时 apply 也是 none（调用方不会去动容器）")


# --------------------------------------------------------------------------
print("== 契约：config.json 必须原地改写（单文件挂载认 inode） ==")
# --------------------------------------------------------------------------
cf = d / NM.CONFIG_JSON
if cf.is_dir():
    shutil.rmtree(str(cf))
mods.save_config(MID, {"TMOD_HOST": "inode.example.com"})
before = os.stat(str(cf))
mods.save_config(MID, {"TMOD_HOST": "inode2.example.com"})
after = os.stat(str(cf))
if before.st_ino and after.st_ino:
    ok(before.st_ino == after.st_ino,
       "改写之后 inode 不变（换成 tmp+rename 的原子写法，容器里就永远读不到新内容）")
else:
    print("  · 这个文件系统不提供 st_ino，跳过 inode 断言")


# --------------------------------------------------------------------------
print("== 回归：module.install 不再无差别 --force-recreate ==")
# --------------------------------------------------------------------------
class _Audit:
    def write(self, *a, **k):
        return {}


_old_dir = NM.MODULES_DIR
NM.MODULES_DIR = str(work)
try:
    r = NA.run_action("module.install", {"id": MID}, "127.0.0.1", _Audit(), dry=True)
finally:
    NM.MODULES_DIR = _old_dir
argv = r.get("argv") or []
ok(r.get("ok") is True and argv[:3] == ["docker", "compose", "-p"], "动作能构造出命令：%s" % (argv,))
ok(argv[-2:] == ["up", "-d"] and "--force-recreate" not in argv,
   "argv 是 `up -d`（compose 自己按 config hash 只重建变了的那几个服务）：%s" % (" ".join(argv),))


# --------------------------------------------------------------------------
print("== 升级判定：update_for ==")
# --------------------------------------------------------------------------
def _store(latest, min_core="1.0", mid=MID):
    return {"available": True, "modules": [
        {"id": mid, "latest": latest,
         "versions": {latest: {"url": "packages/x.tar.gz", "sha256": "0" * 64,
                               "size": 4096, "min_core": min_core}}}]}


up = mods.update_for(MID, "1.0.0", _store("1.1.0"))
ok(up == {"version": "1.1.0", "available": True, "reason": ""},
   "商店有更高版本 → 给出可升级到的版本号：%s" % (up,))
ok(mods.update_for(MID, "1.1.0", _store("1.1.0")) is None, "版本相同 → None（不提示升级）")
ok(mods.update_for(MID, "1.2.0", _store("1.1.0")) is None,
   "已装比商店里的还新 → None（这里不是提示降级的地方）")
up = mods.update_for(MID, "1.0.0", _store("1.1.0", min_core="99.0"))
ok(up and up["available"] is False and "99.0" in up["reason"],
   "新版要求更高的面板版本 → available=False 带原因（界面给说明，不给能点的按钮）：%s" % (up,))
ok(mods.update_for(MID, "1.0.0", {"available": False, "modules": []}) is None,
   "商店不可达 → None：不猜「大概有新版」，否则那是个点了必然失败的按钮")
ok(mods.update_for(MID, "1.0.0", _store("9.9.9", mid="other")) is None,
   "商店里没有这个模块 → None")
up = mods.update_for(MID, "1.0.9", _store("1.0.10"))
ok(up and up["version"] == "1.0.10",
   "版本号按数字段比大小，1.0.10 > 1.0.9（不是字符串比）：%s" % (up,))


# --------------------------------------------------------------------------
print("== 回归：降级要先把模块卸掉，不该由一个版本号触发 ==")
# --------------------------------------------------------------------------
ok3, err, _m = mods.fetch(MID, "0.9.0")
ok(not ok3 and "不支持降级" in err, "已装 v1.0.0 时请求 v0.9.0 被拦下：%s" % err)
ok3, err, _m = mods.fetch(MID, "1.0.0")
ok("不支持降级" not in err,
   "同版本重装不被闸门拦（修坏掉的文件要用它）：%s" % err)


# --------------------------------------------------------------------------
print("== 升级：真机 fetch 路径（下载 → 校验 → 原地替换 → 保留配置） ==")
# --------------------------------------------------------------------------
# 把 _download 换成「把预先造好的包写到目标路径」，于是没有网络也能跑通 fetch 的
# **全部**逻辑。升级保不保留配置、会不会把模块目录整个换掉，全由这段决定。
up_dir = work / "upgrade"
make_module(up_dir)
up_mods = NM.Modules(root=str(up_dir), store_url="")
PKGS = {}


def _make_pkg(mid, ver):
    """造一个合法的模块包（与真机同构：包根就是 manifest.json）。"""
    man = {"schema": 1, "id": mid, "version": ver, "name": MANIFEST["name"],
           "summary": MANIFEST["summary"],
           "compose": {"file": "compose.yml", "project": "n1bus-" + mid},
           "config": MANIFEST["config"],
           "page": {"kind": "iframe", "url": "/m/" + mid + "/"}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in (("manifest.json", json.dumps(man, ensure_ascii=False)),
                           ("compose.yml", COMPOSE)):
            data = text.encode("utf-8")
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _spec(ver):
    data = _make_pkg(MID, ver)
    PKGS[ver] = data
    return {"url": "packages/%s-%s.tar.gz" % (MID, ver),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "min_core": "1.0"}


# 手工塞一份「已缓存的商店清单」：store() 认它，于是整个测试不发网络请求
up_mods._index = {"available": True, "url": "https://example.invalid", "modules": [
    {"id": MID, "name": MANIFEST["name"], "summary": MANIFEST["summary"],
     "icon": "", "homepage": "", "latest": "1.1.0",
     "versions": {"1.1.0": _spec("1.1.0")}}]}
up_mods._index_at = time.time()

d2 = up_dir / MID
up_mods.save_config(MID, {"TMOD_HOST": "keep.example.com", "TMOD_LIMIT": "77"})
# 再手工加一个不由 config 声明的变量（README 让人设 RSC4D_IMAGE 就是这种）
with open(str(d2 / NM.CONFIG_ENV), "a", encoding="utf-8") as fh:
    fh.write("RSC4D_IMAGE=registry.local:5000/rs-c4d:v9\n")

before_dir = os.stat(str(d2))
_real_download = NM._download


def _fake_download(url, dest, limit):
    ver = str(url).rsplit("-", 1)[-1][:-len(".tar.gz")]
    data = PKGS[ver]
    pathlib.Path(dest).write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()


NM._download = _fake_download
try:
    ok3, err, meta = up_mods.fetch(MID, "1.1.0")
finally:
    NM._download = _real_download

ok(ok3 and meta.get("version") == "1.1.0", "升级成功并返回新版的 manifest：%s" % (err or "ok"))
env2 = NM._read_env(d2 / NM.CONFIG_ENV)
ok(env2.get("TMOD_HOST") == "keep.example.com" and env2.get("TMOD_LIMIT") == "77",
   "用户改过的配置在升级后原样还在：%s" % (env2,))
ok(env2.get("RSC4D_IMAGE") == "registry.local:5000/rs-c4d:v9",
   "手工加的非托管变量也还在（升级不该顺手清掉它）")
ok(json.loads((d2 / NM.CONFIG_JSON).read_text(encoding="utf-8")).get("TMOD_LIMIT") == "77",
   "config.json 也保住了（模块页面读的是它）")
after_dir = os.stat(str(d2))
if before_dir.st_ino and after_dir.st_ino:
    ok(before_dir.st_ino == after_dir.st_ino,
       "升级是**原地同步**：模块目录 inode 不变 —— bind mount 认它，换掉目录会让"
       "正在跑的容器看到一片空白")


# --------------------------------------------------------------------------
print("== 兼容：save_config 永远返回四元组 ==")
# --------------------------------------------------------------------------
res = mods.save_config("no-such-module", {})
ok(isinstance(res, tuple) and len(res) == 4,
   "没装的模块也返回 (ok, err, saved, apply)：%r" % (res,))


shutil.rmtree(str(work), ignore_errors=True)
print()
if fails:
    print("失败 %d 项：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
