#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构自检：语法、界面中使用的 id 与图标是否存在、是否存在不安全的 shell 用法。

CI 每次提交都跑；本地也能直接 python3 tools/check.py。
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}
try:                       # Windows 控制台默认 GBK，✔ 这类字符会导致编码错误；CI 上为 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:          # noqa: BLE001
    pass
fails = []


def ok(cond, msg):
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        fails.append(msg)


print("== 语法 ==")
PY_FILES = sorted(p.name for p in ROOT.glob("n1bus*.py"))
r = subprocess.run([sys.executable, "-m", "py_compile"] + [str(ROOT / f) for f in PY_FILES],
                   capture_output=True, text=True)
ok(r.returncode == 0, "%d 个 n1bus*.py 都能编译（%s）" % (len(PY_FILES), ", ".join(PY_FILES))
   + ("" if r.returncode == 0 else "：" + r.stderr[-400:]))

html = (ROOT / "ui.html").read_text(encoding="utf-8")
# 样式与脚本已拆到 ui.css / ui.js（ui.html 只是骨架 + 两个槽位）。
# 老的单文件布局仍然认 —— 拆到一半也不会把自检搞红。
js_path = ROOT / "ui.js"
if js_path.is_file():
    js = js_path.read_text(encoding="utf-8")
    ok(True, "ui.js 存在（脚本已拆出）")
else:
    m = re.search(r"<script>(.*)</script>", html, re.S)
    js = m.group(1) if m else ""
    ok(bool(m), "ui.html 里有 <script> 块")

print("== 界面自检 ==")
if js:
    ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    used = set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', js))
    ok(not (used - ids), "JS 用到的 id 都在 HTML 里：%s" % (sorted(used - ids) or "全部命中"))
    icons = set(re.findall(r'<use href="#(i-[a-z0-9-]+)"', html))
    sprite = set(re.findall(r'id="(i-[a-z0-9-]+)"', html))
    ok(not (icons - sprite), "用到的图标都定义了：%s" % (sorted(icons - sprite) or "全部命中"))
    if shutil.which("node"):
        tmp = pathlib.Path(tempfile.gettempdir()) / "n1bus-ui-check.js"
        tmp.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        ok(r.returncode == 0, "界面 JS 语法通过" + ("" if r.returncode == 0 else "：" + r.stderr[:400]))
    else:
        print("  · 没装 node，跳过 JS 语法检查")

    # 页面必须是真正的 HTML：N1bus 曾将整页作为 JSON 字符串发送，浏览器以纯文本渲染（CSS 全部失效）
    ok(html.lstrip().lower().startswith("<!doctype html>"), "页面以 <!DOCTYPE html> 开头")
    ok(html.rstrip().endswith("</html>"), "页面以 </html> 正常收尾")

    # 标签配平：手工移动面板区块时最容易漏掉一个 </div>；浏览器会静默容忍，但界面会错位
    from html.parser import HTMLParser

    class Balance(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.bad = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID_TAGS:
                self.stack.append((tag, self.getpos()[0]))

        def handle_endtag(self, tag):
            if tag in VOID_TAGS:
                return
            if not self.stack:
                self.bad.append("多出来的 </%s>（第 %d 行）" % (tag, self.getpos()[0]))
            elif self.stack[-1][0] != tag:
                top, line = self.stack.pop()
                self.bad.append("</%s>（第 %d 行）对不上 <%s>（第 %d 行）"
                                % (tag, self.getpos()[0], top, line))
            else:
                self.stack.pop()

    bal = Balance()
    bal.feed(html)
    left = ["<%s>（第 %d 行）没关" % (t, ln) for t, ln in bal.stack]
    ok(not bal.bad and not left, "标签配平：%s" % (bal.bad + left or "全部闭合"))

    # 导航按钮指向的标签页/子视图必须真的存在
    ok(not (set(re.findall(r'data-tab="([a-z]+)"', html)) -
            set(re.findall(r'id="tab-([a-z]+)"', html))),
       "每个 data-tab 都有对应的 <section id=tab-*>")
    ok(not (set(re.findall(r'data-sub="([a-z]+)"', html)) -
            set(re.findall(r'id="sub-([a-z]+)"', html))),
       "每个 data-sub 都有对应的子视图")
    ok(all(t in js for t in re.findall(r'data-tab="([a-z]+)"', html)),
       "每个标签页都在 JS 的 TABS 里注册过")

    # summary 里的 .q（说明位）在运行时会被 stripFoldNotes 移除 —— 标题行只留标题与
    # 状态数字。因此向该位置写入内容不会生效：写入后永不显示，并会让后续读者误以为
    # 「写入即应可见」。此处此前遗留了 4 个此类节点（ovDiskNote / ovProcNote /
    # ovSecNote / mtNote），长期未被发现。需要展示的摘要应写入 .foldbody。
    slots = set()
    for sm in re.finditer(r"<summary>(.*?)</summary>", html, re.S):
        slots |= set(re.findall(r'<span class="q"[^>]*\bid="([A-Za-z0-9_-]+)"', sm.group(1)))
    written = sorted(i for i in slots
                     if re.search(r'getElementById\("%s"\)' % re.escape(i), js))
    ok(not written, "没有往「会被移除的 summary 说明位」写内容：%s" % (written or "干净"))

print("== 安全底线 ==")
# 安全底线要扫**全部** n1bus*.py —— 拆分后白名单动作表在 n1bus_actions.py 里，
# 只扫描 n1bus.py 会漏掉（拆分当天即因此导致 CI 失败一次）。
srcs = {f: (ROOT / f).read_text(encoding="utf-8") for f in PY_FILES}
all_src = "\n".join(srcs.values())
ok("shell=True" not in all_src, "代码里没有 shell=True")
ok("os.system(" not in all_src, "代码里没有 os.system")
ok(any(re.search(r"ACTIONS\s*=\s*\{", s) for s in srcs.values()),
   "白名单动作表存在（n1bus_actions.py）")
ok(bool(re.search(r"0o600|0o?600", all_src)), "token 文件按 600 权限写")

print("== 安装清单 ==")
# install.sh 的 SRC_FILES 必须与磁盘上的源码一一对应。加模块时忘了改它，
# 装机时就会 404 —— 2026-10 迁移时曾发生一次（原因是 glob 在空目录中不展开），
# 已回滚一轮。将该检查置于此处，在 CI 阶段失败优于在服务器上失败。
_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
_m = re.search(r'^SRC_FILES="([^"]+)"', _sh, re.M)
_listed = set(_m.group(1).split()) if _m else set()
_on_disk = {p.name for p in ROOT.glob("n1bus*.py")} | {"ui.html", "ui.css", "ui.js"}
_diff = sorted(_listed ^ _on_disk)
ok(bool(_m), "install.sh 里有 SRC_FILES 清单")
ok(_listed == _on_disk,
   "SRC_FILES 与磁盘上的源码一一对应：%s" % ("、".join(_diff) if _diff else "一致（%d 个文件）" % len(_listed)))

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
