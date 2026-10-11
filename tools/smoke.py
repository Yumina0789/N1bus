#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示模式冒烟测试：把面板拉起来，走一遍「首次设置账户 → 登录 → 账户管理」与所有接口。

不触碰任何真实系统状态（demo 数据源 + 临时数据目录），因此可在 CI 中安全执行。
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
try:                       # Windows 控制台默认 GBK，✔ 这类字符会导致编码错误；CI 上为 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:          # noqa: BLE001
    pass
PORT = 8439
TOKEN = "smoke-service-token"
USER = "smoke"
PASS = "smoke-password"
fails = []


def ok(cond, msg):
    print(("  ✔ " if cond else "  ✘ ") + msg)
    if not cond:
        fails.append(msg)


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
workdir = pathlib.Path(tempfile.mkdtemp())
server_log = workdir / "server.log"
log_fh = open(str(server_log), "w", encoding="utf-8")
# 自动永久封禁故意以 --auto-ban off 启动：只有「启动时是关的、后来在界面上打开」
# 这一路才测得出巡检线程有没有被拉起来（见下方「在线启用」回归）。
proc = subprocess.Popen([sys.executable, str(ROOT / "n1bus.py"), "--mode", "demo",
                         "--port", str(PORT), "--token", TOKEN, "--data-dir", str(workdir),
                         "--auto-ban", "off"],
                        stdout=log_fh, stderr=subprocess.STDOUT)

CRED = {"v": TOKEN}          # 先拿服务 token 顶着，登录后换成会话令牌


def call(path, method="GET", body=None, auth=True, cred=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data, method=method)
    if auth:
        req.add_header("Authorization", "Bearer " + (cred if cred is not None else CRED["v"]))
    req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=25) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


try:
    for _ in range(80):
        try:
            call("/api/auth", auth=False)
            break
        except Exception:
            time.sleep(0.25)
    else:
        sys.exit("面板未启动")

    print("== 首次使用：还没有账户 ==")
    st, body = call("/api/auth", auth=False)
    d = json.loads(body)
    ok(st == 200 and d["need_setup"] is True, "GET /api/auth 说要先设置账户（need_setup=true）")
    ok(d["password_min"] >= 8, "最少密码长度是 %s" % d["password_min"])

    st, body = call("/api/overview", auth=False)
    ok(st == 401, "还没登录时 /api/overview 是 401")
    st, body = call("/api/setup", "POST", {"name": USER, "password": "短"}, auth=False)
    ok(st == 400 and "至少" in json.loads(body)["error"], "密码太短会被拒：%s"
       % json.loads(body).get("error"))

    st, body = call("/api/setup", "POST", {"name": USER, "password": PASS}, auth=False)
    d = json.loads(body)
    ok(st == 200 and d.get("session") and d.get("name") == USER, "首次设置成功并直接给了会话")
    CRED["v"] = d["session"]
    st, body = call("/api/setup", "POST", {"name": "x", "password": "xxxxxxxx"}, auth=False)
    ok(st == 403, "已经有账户后再调 /api/setup 会被拒")

    st, body = call("/api/auth", auth=False)
    ok(json.loads(body)["need_setup"] is False, "现在 need_setup 变成 false 了")

    print("== 接口（用会话令牌） ==")
    # 页面内容需单独校验：仅检查状态码不足（曾经整页被 JSON 编码后发送，
    # 浏览器按纯文本渲染，表现为「没有 CSS」，而状态码始终是 200）
    st, body = call("/")
    page = body.decode("utf-8", "replace")
    ok(st == 200 and page.lstrip().lower().startswith("<!doctype html>"),
       "GET / 返回的是 HTML（不是被 JSON 编码的字符串）")
    ok(page.rstrip().endswith("</html>") and "<style>" in page and ":root{" in page,
       "页面完整：有结尾、有 <style> 与 :root 变量")
    # 账户按信息架构归到「设置」里（曾经是独立页签）—— 查设置页里有没有账户表
    ok('data-tab="settings"' in page and 'id="acBody"' in page and "新增账户" in page,
       "账户管理在设置页里（账户表 + 新增账户）")
    ok('id="ovSec"' in page and 'id="ovProc"' in page,
       "概览有「进程与服务」「登录失败来源」折叠块")

    for path, want in [("/api/meta", 200), ("/api/overview", 200),
                       ("/api/services", 200), ("/api/service?unit=ssh.service", 200),
                       ("/api/service/logs?unit=ssh.service", 200), ("/api/docker", 200),
                       ("/api/docker/images", 200), ("/api/docker/logs?name=kms", 200),
                       ("/api/security", 200), ("/api/ports", 200), ("/api/processes", 200),
                       ("/api/audit", 200), ("/api/journal", 200),
                       ("/api/cleanup", 200), ("/api/netinfo", 200), ("/api/disks", 200),
                       ("/api/backup", 200), ("/api/autostart", 200), ("/api/system_settings", 200),
                       ("/api/files", 200), ("/api/file?path=/var/log/auth.log", 200),
                       ("/api/tasks", 200), ("/api/alerts", 200),
                       ("/api/nope", 404)]:
        try:
            st, _ = call(path)
        except Exception as e:  # noqa: BLE001
            st = "%s: %s" % (type(e).__name__, e)
        ok(st == want, "%-40s → %s" % (path, st))

    print("== 数据形状 ==")
    st, body = call("/api/overview")
    d = json.loads(body)
    # 演示模式必须是确定性数据（空闲机器上真实 CPU 可能就是 0.0）
    ok(d["info"]["hostname"] == "demo-host", "演示模式用的是演示数据")
    ok(isinstance(d["cpu"]["percent"], (int, float)) and d["cpu"]["percent"] > 0,
       "概览有 CPU 数据：%s%%" % d["cpu"]["percent"])
    ok(d["mem"]["total"] > 0 and d["mem"]["percent"] > 0, "概览有内存数据：%.0f MB" % (d["mem"]["total"] >> 20))
    ok(bool(d["disk"]) and d["disk"][0]["total"] > 0, "概览有磁盘数据")
    ok(len(d["series"]) >= 1, "有曲线数据（%d 个采样点）" % len(d["series"]))

    print("== 账户管理 ==")
    st, body = call("/api/accounts")
    d = json.loads(body)
    ok([u["name"] for u in d["users"]] == [USER], "账户列表里只有 %s" % USER)
    ok(d["me"] == USER, "它知道当前登录的是谁：%s" % d["me"])

    st, body = call("/api/accounts/create", "POST", {"name": "second", "password": "second-pass"})
    ok(st == 200 and len(json.loads(body)["users"]) == 2, "能新增账户")
    st, body = call("/api/accounts/create", "POST", {"name": "second", "password": "second-pass"})
    ok(st == 400 and "已经有" in json.loads(body)["error"], "同名账户会被拒")
    st, body = call("/api/accounts/create", "POST", {"name": "带中文", "password": "whatever1"})
    ok(st == 400, "用户名格式不合法会被拒")

    # 用 second 登录（验证多账户都能登）
    st, body = call("/api/login", "POST", {"name": "second", "password": "second-pass"}, auth=False)
    ok(st == 200 and json.loads(body)["name"] == "second", "新账户能登录")
    second_session = json.loads(body).get("session", "")

    st, body = call("/api/accounts/password", "POST",
                    {"name": "second", "password": "second-pass2", "actor_password": PASS})
    ok(st == 200, "改别人的密码（用自己当前密码确认）成功")
    st, body = call("/api/accounts/password", "POST",
                    {"name": "second", "password": "second-pass3", "actor_password": "错的"})
    ok(st == 403, "当前密码不对时改不了密码")
    st, body = call("/api/overview", cred=second_session)
    ok(st == 401, "被修改密码后，那个账户的旧会话失效了")
    st, body = call("/api/login", "POST", {"name": "second", "password": "second-pass2"}, auth=False)
    ok(st == 200, "用新密码能登录")

    st, body = call("/api/accounts/delete", "POST", {"name": "second"})
    ok(st == 200 and len(json.loads(body)["users"]) == 1, "能删除账户")
    st, body = call("/api/accounts/delete", "POST", {"name": USER})
    ok(st == 400 and "最后一个" in json.loads(body)["error"], "最后一个账户删不掉")

    print("== 凭证：服务 token 仍然给机器用 ==")
    st, body = call("/api/overview", cred=TOKEN)
    ok(st == 200, "服务 token 照样能调 API（N1bus 自己就靠它拉限流决策）")
    st, body = call("/api/overview?token=" + TOKEN, cred="")
    ok(st == 200, "?token= 的老用法也还认（脚本兼容）")

    print("== 登录失败与限流 ==")
    st, body = call("/api/login", "POST", {"name": USER, "password": "错的"}, auth=False)
    ok(st == 403, "密码错 → 403：%s" % json.loads(body).get("error"))
    for _ in range(8):
        call("/api/login", "POST", {"name": USER, "password": "还是错的"}, auth=False)
    st, body = call("/api/login", "POST", {"name": USER, "password": PASS}, auth=False)
    ok(st == 429, "连续失败后会被临时拒绝（429），连正确密码也得等：%s"
       % json.loads(body).get("error"))

    print("== 安全底线 ==")
    st, _ = call("/api/overview", auth=False)
    ok(st == 401, "不带凭证一律 401")
    st, body = call("/api/action", "POST", {"name": "no.such.action"})
    ok(not json.loads(body)["ok"], "未知动作被拒绝")
    st, body = call("/api/action", "POST", {"name": "ufw.deny", "params": {"ip": "不是IP"}})
    ok(not json.loads(body)["ok"], "非法参数被拒绝")
    st, body = call("/api/log?path=/etc/passwd")
    ok(not json.loads(body)["ok"], "越界读文件被拒绝")
    st, body = call("/api/log?path=/var/log/../../etc/shadow")
    ok(not json.loads(body)["ok"], "用 .. 绕过目录限制被拒绝")
    st, body = call("/api/action", "POST", {"name": "ufw.deny", "params": {"ip": "1.2.3.4"}})
    ok(json.loads(body).get("demo") is True, "演示模式不真的执行命令")

    # 端到端回归：先让缓存里出现带参数的键（/api/security 会调 logins()），再执行一个
    # 登记了前缀作废的写动作。顺序很重要 —— 若先前跑过未登记前缀的动作，缓存会被全清，
    # 这个缺陷就被掩盖了（smoke 早期版本正是因此漏掉了它）。
    call("/api/security")
    st, body = call("/api/action", "POST", {"name": "clean.docker_builder", "params": {}})
    ok(st == 200 and bool((body or "").strip()),
       "触发前缀作废的动作仍有响应体（HTTP %s，%d 字节）" % (st, len(body or "")))
    try:
        j = json.loads(body)
        ok("ok" in j, "该响应是合法 JSON（而不是被异常吞掉变成空响应）")
    except ValueError as e:
        ok(False, "该响应不是合法 JSON：%s" % e)

    print("== 防火墙端口开放：参数校验与方向 ==")
    # 端口决定公网暴露面，参数校验必须严格：以下输入都不应通过
    for port, why in (("0", "端口 0"), ("65536", "端口越界"), ("abc", "非数字端口"),
                      ("22/tcp", "把端口和协议写在一起"), ("", "空端口")):
        st, body = call("/api/action", "POST",
                        {"name": "ufw.allow", "params": {"port": port, "proto": "tcp"}})
        ok(not json.loads(body).get("ok"), "拒绝非法端口（%s）" % why)
    st, body = call("/api/action", "POST",
                    {"name": "ufw.allow", "params": {"port": "8080", "proto": "sctp"}})
    ok(not json.loads(body).get("ok"), "拒绝非法协议")
    st, body = call("/api/action", "POST",
                    {"name": "ufw.allow", "params": {"port": "8080", "proto": "tcp",
                                                     "dir": "sideways"}})
    ok(not json.loads(body).get("ok"), "拒绝非法方向")
    st, body = call("/api/action", "POST",
                    {"name": "ufw.allow", "params": {"port": "8080", "proto": "tcp",
                                                     "dir": "in"}})
    j = json.loads(body)
    ok(j.get("demo") is True and "allow" in " ".join(j.get("argv") or []),
       "合法参数被接受（入站）：%s" % (j.get("argv"),))
    ok("in" in (j.get("argv") or []), "方向 in 被显式传给 ufw")
    st, body = call("/api/action", "POST",
                    {"name": "ufw.allow", "params": {"port": "8080", "proto": "tcp",
                                                     "dir": "out"}})
    j = json.loads(body)
    ok("out" in (j.get("argv") or []), "方向 out 被显式传给 ufw（进出是两条不同规则）")
    st, body = call("/api/action", "POST",
                    {"name": "ufw.unallow", "params": {"port": "8080", "proto": "tcp",
                                                       "dir": "out"}})
    j = json.loads(body)
    ok(j.get("demo") is True and "delete" in (j.get("argv") or []),
       "关闭端口走 delete allow：%s" % (j.get("argv"),))

    print("== /api/security 返回防火墙端口对照 ==")
    st, body = call("/api/security")
    sec = json.loads(body)
    al = sec.get("allowed") or {}
    ok("allowed" in sec, "/api/security 含 allowed 字段")
    ok(isinstance(al.get("allowed"), list), "allowed.allowed 是列表")
    ok(all(k in al for k in ("unmatched", "uncovered", "rules")),
       "含 unmatched / uncovered / rules 三组：%s" % sorted(al.keys()))
    for x in (al.get("allowed") or []):
        if not x.get("dir", "").startswith(("in", "out")):
            ok(False, "allowed 里有条目方向不合法：%s" % x)
            break
    else:
        ok(True, "所有放行条目的方向都是 in 或 out")

    st, body = call("/api/audit")
    ok(len(json.loads(body)["entries"]) >= 1, "动作写进了审计")
    st, body = call("/api/audit")
    acts = [e["action"] for e in json.loads(body)["entries"]]
    ok("account.setup" in acts and "account.login" in acts, "账户事件也进审计：%s" % acts[:6])

    print("== 退出登录 ==")
    st, body = call("/api/logout", "POST")
    ok(st == 200, "POST /api/logout 成功")
    st, body = call("/api/overview", auth=False)
    ok(st == 401, "退出后没凭证 → 401")
    st, body = call("/api/login", "POST", {"name": USER, "password": PASS}, auth=False)
    ok(st == 403 or st == 429, "还在限流冷却里，登录仍被挡（%s）" % st)

    print("== 递增封禁：一次比一次久，且重启不丢 ==")
    # 上文已退出登录，本段改用服务 token —— 也正是「管理员来源被封后如何恢复」的规范做法
    st, body = call("/api/accounts", cred=TOKEN)
    g = json.loads(body).get("guard") or {}
    ok(g.get("tiers") == [300, 1800, 21600], "档位是 5 分钟 → 30 分钟 → 6 小时：%s" % g.get("tiers"))
    first = (g.get("blocked") or [{}])[0]
    ok(first.get("level") == 1, "第一次犯满就封第 1 档：%s" % first)
    ok((workdir / "loginguard.json").exists(), "封禁状态落在 loginguard.json（重启不清零）")

    # 递增与「解封保留档位」直接拿类来试，不然得真等 5 分钟
    sys.path.insert(0, str(ROOT))
    import n1bus
    import n1bus_audit
    import n1bus_cache
    import n1bus_security
    import n1bus_util
    unit = n1bus_security.LoginGuard(workdir / "guard-unit.json", window=300, limit=2, tiers=(1, 2, 3))
    ok(unit.fail("203.0.113.5")[0] is False, "没到次数不封")
    b2 = unit.fail("203.0.113.5")
    ok(b2[0] and b2[1] == 1 and b2[2] == 1, "犯满触发第 1 档：%s" % (b2,))
    unit.unblock("203.0.113.5")
    unit.fail("203.0.113.5")
    b4 = unit.fail("203.0.113.5")
    ok(b4[0] and b4[1] == 2 and b4[2] == 2, "解封后档位保留，再犯直接第 2 档（更久）：%s" % (b4,))
    again = n1bus_security.LoginGuard(workdir / "guard-unit.json", window=300, limit=2, tiers=(1, 2, 3))
    ok(again.wait_seconds("203.0.113.5") > 0, "换一个实例重新读文件，封禁还在（重启不清零）")
    again.ok("203.0.113.5")
    ok(again.wait_seconds("203.0.113.5") == 0, "登录成功后这个来源的记录整个清掉")

    print("== 解封接口 ==")
    st, body = call("/api/accounts/unblock", "POST", {"ip": "127.0.0.1"}, cred=TOKEN)
    d = json.loads(body)
    ok(st == 200 and d.get("ok") and d.get("cleared") is True, "服务 token 能解封被封的来源：cleared=%s" % d.get("cleared"))
    st, body = call("/api/login", "POST", {"name": USER, "password": PASS}, auth=False)
    ok(st == 200 and json.loads(body).get("session"), "解封之后正确密码立刻能登录")
    st, _ = call("/api/accounts/unblock", "POST", {"ip": "不是IP"}, cred=TOKEN)
    ok(st == 400, "非法 IP 被拒")

    print("== 执法器流程（假 Nimbus） ==")
    sys.path.insert(0, str(ROOT))
    import http.server
    import threading
    import n1bus

    seen = []
    pending = {"ban": [{"ip": "203.0.113.9", "reason": "ip", "detail": "该 IP 今天激活 11 次"}],
               "unban": [], "limit": 10, "window": "day"}

    class FakeNimbus(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _out(self, obj):
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._out(pending if self.path.startswith("/api/enforce/pending") else {"error": "no"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append(json.loads(self.rfile.read(n) or b"{}"))
            self._out({"ok": True})

    fake = http.server.ThreadingHTTPServer(("127.0.0.1", PORT + 1), FakeNimbus)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    try:
        enf = n1bus_security.Enforcer("http://127.0.0.1:%d" % (PORT + 1), "tok", 60,
                               n1bus_audit.Audit(workdir / "audit2.log"))
        res = enf.poll_once()
        ok(any(s.get("action") == "ban" and s.get("ip") == "203.0.113.9" for s in seen),
           "把「封这个 IP」回报给了面板：%s" % seen)
        ok(seen and seen[0].get("ok") is False,
           "本机无法封禁（无 ufw 或非 root）时如实回报失败，不谎报成功")
        # 仅断言「失败必须带原因」，不断言具体文案 —— 不同环境差异较大
        ok(res["ok"] is False and bool(res.get("error")),
           "失败原因如实带回来了：%s" % (res.get("error") or "")[:70])
        ok((workdir / "audit2.log").exists(), "执法经过写进了审计")
        pending["ban"] = []
        pending["unban"] = [{"ip": "203.0.113.9", "reason": "ttl", "detail": "已封满 24 小时"}]
        seen.clear()
        enf.poll_once()
        ok(any(s.get("action") == "unban" for s in seen), "解封请求也会被回报回去")

        print("== 自动永久封禁：判定与保护条件 ==")
        # 该模块的风险集中在「误封」，因此测试重点是「应放过的来源是否被放过」，
        # 而不仅是「是否执行封禁」。dry=True 只记录不实际执行 ufw。
        dpath = workdir / "defender.json"
        d = n1bus_security.Defender(dpath, threshold=10, enabled=True,
                             skip=n1bus_util.parse_networks(n1bus_security.PRIVATE_NETS),
                             audit=n1bus_audit.Audit(workdir / "audit3.log"), dry=True)
        policy = {
            "top_ips": [{"ip": "198.51.100.7",  "n": 40},    # 超过阈值：应封禁
                        {"ip": "198.51.100.8",  "n": 10},    # 恰好等于阈值：不封（判据为"超过"）
                        {"ip": "198.51.100.9",  "n": 500},   # 成功登录过：可信来源，不封
                        {"ip": "192.168.1.50",  "n": 900},   # 私有网段：不封
                        {"ip": "198.51.100.10", "n": 800}],  # 已在 ufw 中：不重复封
            "ok_ips": ["198.51.100.9"],
        }
        blk = {"198.51.100.10": "ufw"}
        got = [c["ip"] for c in d.candidates(policy, blk)]
        ok(got == ["198.51.100.7"],
           "仅选出应封禁的来源（等于阈值 / 成功登录过 / 私有网段 / 已封禁 均被排除）：%s" % got)

        res2 = d.run_once(None, policy, blk)
        ok(res2["banned"] == ["198.51.100.7"], "执行后仅封禁该来源：%s" % res2["banned"])
        ok(dpath.exists(), "状态落盘 defender.json")

        d.exempt("198.51.100.7", True)
        ok(d.candidates(policy, blk) == [], "豁免后该来源不再进入待封名单")
        d.exempt("198.51.100.7", False)
        ok([c["ip"] for c in d.candidates(policy, blk)] == ["198.51.100.7"],
           "移出豁免后该来源重新进入待封名单")

        d.unban("198.51.100.7")
        ok("198.51.100.7" in d.status()["exempt"],
           "解封会自动写入豁免名单（否则下一轮巡检立即重新封禁）")

        conf2 = d.configure(enabled=True, threshold=1)
        ok(conf2["threshold"] == n1bus_security.AUTOBAN_MIN_THRESHOLD,
           "阈值被限制到下限 %d，避免过低而将正常输错密码的来源计入"
           % n1bus_security.AUTOBAN_MIN_THRESHOLD)
        d.configure(threshold=10)
        d.configure(enabled=False)
        skip = d.run_once(None, policy, blk)
        ok(bool(skip.get("skipped")), "停用后一轮直接跳过，不封禁任何来源")

        # 回归：阈值下限必须对**所有**入口生效。configure() 会夹到下限，但 __init__
        # 直接 int(threshold)，main() 又把 --auto-ban-after 原样传进来 —— 于是
        # `--auto-ban-after 1` 能让「失败 2 次即永久封禁」，恰好对应 ADR 0010 中承认的
        # 唯一实际代价（误封风险）。
        d2 = n1bus_security.Defender(None, threshold=1, enabled=True, dry=True)
        ok(d2.state["threshold"] == n1bus_security.AUTOBAN_MIN_THRESHOLD,
           "__init__ 也把阈值夹到下限 %d（--auto-ban-after 1 不再生效）"
           % n1bus_security.AUTOBAN_MIN_THRESHOLD)

        print("== 自动永久封禁：在线启用 ==")
        # 回归：启动巡检线程的代码曾经只写在 main() 的启动路径里，configure() 只改状态。
        # 于是「启动时是关的、后来在界面上打开」这一路：开关、阈值、待封禁预览都显示
        # 已启用，巡检却永不执行 —— 只有手动「立即执行一轮」有效，而且不报任何错。
        # 这里用服务 token（会话在前面的「退出登录」段已作废，服务 token 全程有效）。
        st, body = call("/api/security", cred=TOKEN)
        dd = json.loads(body).get("defender") or {}
        ok(dd.get("enabled") is False and dd.get("running") is False,
           "启动时是关的：enabled=false 且没有巡检线程")
        st, body = call("/api/security/defender", "POST", {"enabled": True}, cred=TOKEN)
        ok(st == 200 and json.loads(body)["config"]["enabled"] is True, "在界面上把它打开")
        st, body = call("/api/security", cred=TOKEN)
        dd = json.loads(body).get("defender") or {}
        ok(dd.get("running") is True, "打开之后巡检线程真的在跑（曾经只改状态、永不巡检）")

        print("== 单元属性缓存：写操作后必须整批作废 ==")
        # 这三张表没有 TTL（模块级、进程内）。留下旧值会让界面显示 enabled 而 systemctl
        # 实际已 disabled —— 表现为「点击开机自启无反应」（该故障出现过）。
        # 因此动作执行成功后必须整批清空。
        n1bus_cache.UNIT_STATE_CACHE["x.service"] = "enabled"
        n1bus_cache.UNIT_PATH_CACHE["x.service"] = "/lib/systemd/system/x.service"
        n1bus_cache.UNIT_PROBED.add("x.service")
        n1bus_cache.clear_unit_cache()
        ok(not n1bus_cache.UNIT_STATE_CACHE and not n1bus_cache.UNIT_PATH_CACHE
           and not n1bus_cache.UNIT_PROBED, "clear_unit_cache() 清空三张单元属性表")
    finally:
        fake.shutdown()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_fh.close()
    text = server_log.read_text(encoding="utf-8", errors="replace")
    if fails:
        print("\n---- 服务端日志（尾部） ----")
        print("\n".join(text.splitlines()[-20:]))

print()
if fails:
    print("失败 %d 项：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
