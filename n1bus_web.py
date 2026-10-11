"""HTTP 层：Handler（全部路由）+ 页面拼装。

页面由 ui.html（骨架）+ ui.css + ui.js 拼成 —— 见 docs/modules.md。
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from n1bus_actions import _val_ip, list_actions, run_action
from n1bus_cache import clear_unit_cache
from n1bus_files import FILE_UPLOAD_MAX, _human_size, _tail_file, safe_path
from n1bus_security import PASSWORD_MIN, SESSION_TTL
from n1bus_util import VERSION, run, same_secret

HERE = Path(__file__).resolve().parent


def _trim_services(res, q):
    """服务列表按 `?limit=` 裁剪（真机上 210 个单元 ≈ 19.5 KB）。

    默认**不裁剪** —— 界面搜索框需要在全部单元中查找，静默截断会使结果与预期不符；
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


# --- 页面拼装 ----------------------------------------------------------------
PAGE_CSS_SLOT = "/*{{N1BUS_CSS}}*/"
PAGE_JS_SLOT = "/*{{N1BUS_JS}}*/"
_PAGE = {"text": None}

def build_page():
    """把骨架与 ui.css / ui.js 拼成一份完整页面（幂等，只拼一次）。

    某个分片文件缺失时将对应槽位留空 —— 在仅完成部分拆分的情况下仍可运行，
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


class Handler(BaseHTTPRequestHandler):
    server_version = "n1bus/" + VERSION
    app = None

    def log_message(self, fmt, *args):   # 访问日志：仅记录错误状态，避免访问日志淹没 journald
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # --- 基础设施 ---------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        """发送响应体。

        注意：**字符串按原样发送，不做 JSON 编码**。此前这里对任何非 bytes 都执行 json.dumps，
        于是 ui.html 被当成 JSON 字符串发出去（开头多个引号、换行变成字面 \\n、引号变成
        \\"），浏览器把整页当纯文本渲染 —— 表现就是「页面没有 CSS」。v1m-dash 那边一直是
        按类型分别处理的，因此只有 N1bus 出现该问题。
        """
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
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

        服务 token 必须保留：N1bus 每 60 秒需用它向 v1m-dash 拉取限流决策，
        该调用无法使用交互式登录。令牌存放在权限 0600 的文件中，仅供机器调用。
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

        面板通常挂在反向代理后面，直连方是那个容器（172.x）—— 那样审计里全部
        记录都会记为该代理容器地址，等同于没有区分度。因此**仅当直连方本身落在
        --allow-ip 网段内**（它才是个可信代理）时，才取 X-Forwarded-For 的第一段
        （最左 = 最原始客户端）。

        其余情况一律返回直连地址、**完全不读该头部** —— 没配 --allow-ip 时也在其列。
        该头部由客户端随便书写，谁都能伪造成任意 IP：既能把自己的失败记录记到别人头上
        （登录限流的键同样取自这个函数），也能把封禁转嫁给别人。
        """
        peer = self.client_address[0]
        nets = self.app.get("allow_nets") or []
        if not nets:
            # 没有信任网段 = 没有可信代理，谁的转发头都不采信
            return peer
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
        """可选的来源白名单（--allow-ip）。绑定回环/网桥时它只是第二层防护；绑定 0.0.0.0 时它才是关键防线。"""
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
            return self._send(403, {"error": "forbidden", "hint": "来源地址不在 --allow-ip 白名单内"})
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
                                    # 未登录时不返回用户名列表 —— 否则等于替攻击者
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
            if u.path == "/api/modules":
                mods = self.app["modules"]
                return self._send(200, {"installed": mods.installed(),
                                        "store": mods.store(),
                                        "dir": str(mods.root)})
            if u.path == "/api/modules/store":
                # 商店清单带 5 分钟缓存；`?fresh=1` 强制回源（界面的「刷新商店」用）
                return self._send(200, self.app["modules"].store(
                    force=q.get("fresh", ["0"])[0] in ("1", "true", "yes")))
            if u.path == "/api/modules/entry":
                # 页面入口每次现取：这类一次性 token 换完 cookie 就作废，
                # 缓存下来的只会是一个打不开的地址。
                return self._send(200, self.app["modules"].entry_url(
                    q.get("id", [""])[0]))
            if u.path == "/api/docker/images":
                return self._send(200, p.docker.images())
            if u.path == "/api/docker/logs":
                return self._send(200, p.docker.logs(
                    q.get("name", [""])[0], q.get("lines", ["200"])[0],
                    q.get("since", [""])[0], q.get("until", [""])[0],
                    q.get("grep", [""])[0],
                    q.get("ts", [""])[0] in ("1", "true", "yes")))
            if u.path == "/api/security":
                # 本页所需数据彼此重叠（fail2ban / ufw / auth.log），因此各计算一次
                # 再向下传递：否则同一请求会重复计算 logins 与 blocked 多次。
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
                                            "error": "路径不在允许浏览的目录内，或不是普通文件"})
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
            return self._send(403, {"error": "forbidden", "hint": "来源地址不在 --allow-ip 白名单内"})
        acc = self.app["accounts"]
        ip = self._client_ip()

        # --- 不需要登录的两个入口 -----------------------------------------
        if u.path == "/api/setup":
            if not acc.need_setup():
                return self._send(403, {"ok": False, "error": "已存在账户，请直接登录"})
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
                return self._send(429, {"ok": False, "error": "登录失败次数过多，请在 %d 秒后重试" % wait})
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
            return self._send(403, {"ok": False, "error": "用户名或密码不正确"})

        # --- 以下都要已登录 -----------------------------------------------
        if not self._authed(q):
            return self._send(401, {"error": "unauthorized"})

        # --- 面板自身的配置接口（不属于系统命令，因此不纳入 ACTIONS 表）---------
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

        # --- 模块：装与卸（多步编排，因此不走通用的 /api/action）-------------
        # 通用入口一次只跑一个动作；而「装」是「下载校验落盘 → 起容器」，
        # 「卸」是「摘容器 → 删目录」。两步之间的顺序是有意义的，所以在这里串。
        if u.path == "/api/modules/install":
            b = self._json_body()
            mid = (b.get("id") or "").strip()
            mods = self.app["modules"]
            ok, err, meta = mods.fetch(mid, b.get("version"))
            self.app["audit"].write("module.fetch", mid or "(空)", ok,
                                    err or ("已落盘 %s %s" % (meta.get("id"), meta.get("version"))),
                                    ip)
            if not ok:
                return self._send(400, {"ok": False, "error": err})
            res = {"ok": True, "module": meta}
            if meta.get("compose"):
                # 先落盘、后编排：包或清单不合法时，服务器上不会留下半个容器
                r = run_action("module.install", {"id": mid}, ip,
                               self.app["probe"].audit,
                               dry=not self.app["probe"].real)
                detail = r.get("detail") or r.get("error") or ""
                res["container"] = {"ok": bool(r.get("ok")), "detail": detail}
                if not r.get("ok"):
                    res["ok"] = False
                    res["error"] = "包已落盘，但容器没起来：%s" % detail
            # 页面路由：这是「装上即出现」的最后一环 —— 写 Caddy 片段并热重载。
            # 失败不回滚模块（容器已经起来了，先用着），但如实报出来。
            c_ok, c_detail = mods.sync_caddy()
            if c_detail:
                res["route"] = {"ok": c_ok, "detail": c_detail}
            if not c_ok:
                res["route_error"] = "模块已装好，但页面路由没同步：%s" % c_detail
            self.app["audit"].write("module.route", mid, c_ok,
                                    c_detail or "未配置 Caddy 同步", ip)
            return self._send(200 if res["ok"] else 500, res)
        if u.path == "/api/modules/uninstall":
            mid = (self._json_body().get("id") or "").strip()
            mods = self.app["modules"]
            if mid not in mods.installed_ids():
                return self._send(404, {"ok": False, "error": "没有安装这个模块：%s" % mid})
            # 先摘容器再删目录：`-f <path>` 指着模块目录，目录一没就再也操作不了那套
            # 容器了。`down` 保留数据卷。
            gone = run_action("module.down", {"id": mid}, ip,
                              self.app["probe"].audit,
                              dry=not self.app["probe"].real)
            if not gone.get("ok"):
                detail = gone.get("detail") or gone.get("error") or ""
                self.app["audit"].write("module.remove", mid, False,
                                        "容器未摘除，目录保留：%s" % detail, ip)
                return self._send(500, {"ok": False, "error":
                                        "容器没摘掉，因此没有删目录：%s" % detail})
            ok, err = mods.remove(mid)
            self.app["audit"].write("module.remove", mid, ok, err or "已移除（数据卷保留）", ip)
            # 目录已经没了，页面路由也要撤掉 —— 否则 /m/<id>/ 会转到一个不存在的容器
            c_ok, c_detail = mods.sync_caddy()
            if not c_ok:
                return self._send(500, {"ok": False,
                                        "error": "模块目录已删除，但页面路由没撤掉：%s"
                                                 % c_detail})
            return self._send(200, {"ok": ok, "error": err, "route": c_detail})

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
        # 上传使用原始请求体（不套 JSON）—— 二进制数据直接落盘，避免 base64 编码带来的体积膨胀。
        # 因此它必须排在下面 `body = self._json_body()` 之前，否则请求体会被按 JSON 解析为空对象。
        if u.path == "/api/file/upload":
            target = q.get("path", [""])[0]
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return self._send(400, {"ok": False, "error": "未收到文件内容"})
            if n > FILE_UPLOAD_MAX:
                return self._send(413, {"ok": False, "error": "文件大小超过单次上传上限（%s）"
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
            # 任何账户的密码均可修改，但必须用操作者当前密码再确认一次 —— 否则会话一旦
            # 泄露即可静默接管全部账户
            if me == "(服务 token)" or not acc.check(me, body.get("actor_password") or ""):
                self.app["audit"].write("account.password", body.get("name") or me, False,
                                        "当前密码校验失败", ip)
                return self._send(403, {"ok": False, "error": "请输入当前账户的密码以确认身份"})
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
            # 操作者输错密码导致自身来源被封禁、或正常来源被误封时：提供明确的解封入口。
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
            # 动作确实改变了系统状态（启停服务、封 IP、清日志等）时，单元属性缓存必须立即
            # 失效，否则用户刚点完「设为开机自启」，界面仍会拿旧值显示为未启用。
            # 这批表没有 TTL、只在写操作后整批作废（重新查询一批单元约 10 ms）。
            if res.get("ok") and not res.get("demo"):
                clear_unit_cache()
            return self._send(200, res)
        return self._send(404, {"error": "not found"})
