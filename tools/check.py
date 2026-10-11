#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构自检：语法、界面里用到的 id 与图标是否都存在、有没有 shell 里跑的坏习惯。

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
try:                       # Windows 控制台默认 GBK，✔ 这种字符会炸；CI 上是 UTF-8
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

    # 页面必须真的是 HTML：N1bus 曾经把整页当 JSON 字符串发出去，浏览器渲染成纯文本（CSS 全丢）
    ok(html.lstrip().lower().startswith("<!doctype html>"), "页面以 <!DOCTYPE html> 开头")
    ok(html.rstrip().endswith("</html>"), "页面以 </html> 正常收尾")

    # 标签配平：手搬面板区块时最容易漏掉一个 </div>，浏览器会默默容忍、界面却错位
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
    # 状态数字。所以**往那儿写内容是白写**：写过、永远不会显示，而且让下一个读者以为
    # 「写了就该看得见」。这块以前就埋了 4 个这样的节点（ovDiskNote / ovProcNote /
    # ovSecNote / mtNote），一直没人发现。要展示的摘要写进 .foldbody。
    slots = set()
    for sm in re.finditer(r"<summary>(.*?)</summary>", html, re.S):
        slots |= set(re.findall(r'<span class="q"[^>]*\bid="([A-Za-z0-9_-]+)"', sm.group(1)))
    written = sorted(i for i in slots
                     if re.search(r'getElementById\("%s"\)' % re.escape(i), js))
    ok(not written, "没有往「会被移除的 summary 说明位」写内容：%s" % (written or "干净"))

print("== 安全底线 ==")
# 安全底线要扫**全部** n1bus*.py —— 拆分后白名单动作表在 n1bus_actions.py 里，
# 只扫 n1bus.py 会漏掉（拆分当天就因此红过一次）。
srcs = {f: (ROOT / f).read_text(encoding="utf-8") for f in PY_FILES}
all_src = "\n".join(srcs.values())
ok("shell=True" not in all_src, "代码里没有 shell=True")
ok("os.system(" not in all_src, "代码里没有 os.system")
ok(any(re.search(r"ACTIONS\s*=\s*\{", s) for s in srcs.values()),
   "白名单动作表存在（n1bus_actions.py）")
ok(bool(re.search(r"0o600|0o?600", all_src)), "token 文件按 600 权限写")

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
