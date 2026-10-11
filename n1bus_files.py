"""n1bus 文件读取与写入闸门：日志尾巴、路径白名单校验、体积换算。"""

import os
import re

from n1bus_util import LOG_DIRS


def _tail_file(path, lines=200):
    """只允许 /var/log 与面板数据目录下的普通文件；解析真实路径挡符号链接逃逸。"""
    if not path:
        return {"ok": False, "error": "未提供路径"}
    real = os.path.realpath(path)
    if not any(real.startswith(d) for d in LOG_DIRS):
        return {"ok": False, "error": "只允许读 %s 下的文件" % "、".join(LOG_DIRS)}
    if not os.path.isfile(real):
        return {"ok": False, "error": "不是普通文件：%s" % real}
    try:
        size = os.path.getsize(real)
        # 大文件仅读取末尾部分，避免将 1 GB 日志整体载入内存
        with open(real, "r", encoding="utf-8", errors="replace") as fh:
            if size > 4 << 20:
                fh.seek(max(0, size - (1 << 20)))
                fh.readline()
            data = fh.readlines()[-lines:]
    except OSError as e:
        return {"ok": False, "error": "无法读取：%s" % e}
    return {"ok": True, "path": real, "size": size,
            "lines": [l.rstrip("\n") for l in data]}


FILE_ROOTS = ("/var/log", "/var/lib/n1bus", "/opt/nimbus", "/etc/caddy", "/etc/vlmcsd",
              "/etc/systemd/system", "/opt/dsh")
FILE_MAX_ENTRIES = 200
FILE_TEXT_LIMIT = 512 * 1024
# 单次上传上限：超过此值请改用 scp，不要经由面板。
FILE_UPLOAD_MAX = 64 * 1024 * 1024
# 可在面板中直接编辑的文本上限：超过此值仅提供下载。
FILE_EDIT_MAX = 2 * 1024 * 1024


def safe_path(p, want_dir=None):
    """把路径限制在白名单根之下；realpath 之后判断，`..` 与符号链接均无法绕过。"""
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
    所以这里判断的是**父目录**（同样先 realpath，符号链接依然无法绕过），并额外拦截两种情况：

    * 直接对受管白名单**根目录**操作（例如删除 /var/log 整个目录树）；
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
    """文件名合法性：仅允许单段名称，不得包含路径分隔符或穿越序列。"""
    s = str(name or "").strip()
    if not s or s in (".", ".."):
        return None, "名称不能为空"
    if os.sep in s or (os.altsep and os.altsep in s) or "/" in s or "\\" in s:
        return None, "名称不能包含路径分隔符"
    if "\x00" in s:
        return None, "名称包含非法字符"
    return s, ""


def _human_size(n):
    """将字节数转换为人类可读格式（用于审计与报错文案）。"""
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
