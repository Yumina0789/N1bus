"""n1bus 的 KMS 接入层：外部 KMS 面板的地址与令牌位置（NIMBUS_URL、NIMBUS_TOKEN_PATHS）、写操作白名单 KMS_WRITES、只读数据源与白名单转发 KMSService，以及演示模式假数据源 _DemoKMS。"""

import json
import os
import time
import urllib.request

from n1bus_util import read_text


# --------------------------------------------------------------------------
# KMS 授权服务（v1m-dash 的数据）
#
# 合并之后对外只有一个面板，但 KMS 的统计与限流决策仍留在原来的进程里：它同时管着
# vlmcsd 的 ini、supervisord 的进程控制、以及激活日志的解析与归档。把这些全搬过来
# 收益不大、风险不小。所以这里**只读取**它的数据，界面上不再有两套面板的感觉。
#
# 这与 DeepSeek Harness 是同一个思路：Harness 也是独立容器，在面板里只是一个页面。
# --------------------------------------------------------------------------
NIMBUS_URL = os.environ.get("NIMBUS_URL") or "http://127.0.0.1:8099"
# token 落在 docker 卷里。卷名取决于 compose 的项目名，所以按常见位置依次找一遍。
NIMBUS_TOKEN_PATHS = (
    "/var/lib/docker/volumes/docker_kms-data/_data/nimbus.token",
    "/var/lib/docker/volumes/kms-data/_data/nimbus.token",
    "/var/lib/nimbus/nimbus.token",
)

# KMS 的写操作白名单：操作名 → (对方的路径, 允许透传的参数；None 表示全部参数)。
# 不开放任意转发：那个面板还带着自己的账户接口（建账户、改密码），全量转发等于把
# 它的登录体系一并交给这里的任何使用者。所以每个操作只挑自己需要的参数。
KMS_WRITES = {
    "service":         ("/api/service",        ("action",)),
    "config":          ("/api/config",         None),
    "config_rollback": ("/api/config/rollback", ()),
    "unban":           ("/api/limits/unban",   ("ip",)),
    "stats_prune":     ("/api/stats/prune",    ()),
    "stats_clear":     ("/api/stats/clear",    ("confirm",)),
}


class KMSService:
    """读 v1m-dash 的 KMS 数据，并把白名单内的写操作转过去。"""

    def __init__(self, url=NIMBUS_URL, token="", timeout=8):
        self.url = (url or "").rstrip("/")
        self.timeout = timeout
        self._token = token or ""
        self._token_at = 0.0

    def token(self):
        """token 可能被轮换，所以带 60 秒缓存地重新读，而不是只在启动时读一次。"""
        if self._token and time.time() - self._token_at < 60:
            return self._token
        for p in NIMBUS_TOKEN_PATHS:
            try:
                if os.path.exists(p):
                    t = read_text(p).strip()
                    if t:
                        self._token, self._token_at = t, time.time()
                        return t
            except OSError:
                continue
        return self._token

    def _get(self, path):
        if not self.url:
            return None
        try:
            req = urllib.request.Request(
                "%s%s?token=%s" % (self.url, path, self.token()),
                headers={"User-Agent": "nimbus-panel"})
            # 本机回环调用要绕开环境里的 HTTP(S)_PROXY，否则会被代理掉
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except Exception:  # noqa: BLE001
            return None

    def summary(self):
        """服务状态 + 激活统计。取不到就如实说不可用，不编数据。"""
        s = self._get("/api/summary")
        if s is None:
            return {"available": False,
                    "hint": "读不到 KMS 服务（v1m-dash 未运行，或 token 不可读）"}
        return {"available": True, "service": s.get("service") or {},
                "stats": s.get("stats") or {}}

    def limits(self):
        """限流与封禁现状。"""
        d = self._get("/api/limits")
        if d is None:
            return {"available": False, "hint": "读不到限流状态"}
        return dict(d, available=True)

    def config(self):
        """vlmcsd.ini 的可写配置项。

        注意它与 summary 是两个接口：summary 只给服务状态与统计，配置在 /api/config。
        """
        d = self._get("/api/config")
        return (d or {}).get("config") or {}

    def events(self, per=50):
        """最近的激活明细。分页接口，这里只取第一页用来展示。"""
        d = self._get("/api/stats?per=%d" % per)
        page = (d or {}).get("page") or {}
        for key in ("rows", "items", "records", "events"):
            if isinstance(page.get(key), list):
                return page[key]
        return []

    def all(self):
        s = self.summary()
        # 配置与激活明细在另外两个接口里；取不到就留空，不让整页变成不可用
        if s.get("available"):
            s["config"] = self.config()
            s["events"] = self.events()
        else:
            s["config"], s["events"] = {}, []
        return {"summary": s, "limits": self.limits()}

    def _post(self, path, body):
        if not self.url:
            return None
        try:
            data = json.dumps(body or {}).encode("utf-8")
            req = urllib.request.Request(
                "%s%s?token=%s" % (self.url, path, self.token()), data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "nimbus-panel"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    def action(self, op, params, audit=None, client=""):
        """把一次写操作转给原面板。

        **走白名单，不开放任意转发。** 那个面板除了 KMS 还带着自己的账户管理接口
        （创建账户、改密码），全量转发等于把它的登录体系一并交给这里的任何使用者。
        所以每个操作只挑它自己需要的参数，多余的参数直接丢掉。

        params 为 None 的操作（config）是个例外：vlmcsd 的配置项由对方自己的
        validate() 逐项校验，这里不重复实现一遍规则。
        """
        spec = KMS_WRITES.get(op)
        if not spec:
            return {"ok": False, "error": "不支持的操作：%s" % (op or "(空)")}
        path, allow = spec
        if allow is None:
            body = dict(params or {})
        else:
            body = {k: (params or {}).get(k) for k in allow if k in (params or {})}
        if op == "service" and body.get("action") not in ("start", "stop", "restart"):
            return {"ok": False, "error": "服务动作只能是 start / stop / restart"}
        r = self._post(path, body)
        if r is None:
            r = {"ok": False, "error": "KMS 面板不可达（%s）" % self.url}
        if audit is not None:
            errs = r.get("errors") or []
            detail = r.get("error") or ("；".join(str(x) for x in errs) if errs
                                        else json.dumps(r, ensure_ascii=False)[:300])
            audit.write("kms." + op, json.dumps(body, ensure_ascii=False)[:200],
                        bool(r.get("ok")), detail, client)
        return r


class _DemoKMS:
    """演示用的 KMS 数据，结构与真机一致，界面代码不用分叉。"""

    def action(self, op, params, audit=None, client=""):
        """演示模式：只回成功，不真的改变任何东西（与 run_action 的 dry 同一思路）。"""
        return {"ok": True, "demo": True, "op": op, "applied": params or {}}

    def all(self):
        return {
            "summary": {"available": True,
                        "service": {"running": True, "unit": "vlmcsd",
                                    "manager": "supervisord", "pid": 10,
                                    "since": "2026-10-09 15:34:40 (up 1 天 9 小时)",
                                    "version": "vlmcsd 5978b33 64-bit",
                                    "binary": "/usr/local/bin/vlmcsd", "reload_pending": False},
                        "stats": {"total": 1284, "today": 37, "today_ok": 35,
                                  "clients_30d": 42, "first": "2026-09-01",
                                  "last": "2026-10-11 00:41:07",
                                  "storage_bytes": 391144},
                        "config": {"Port": "1688", "Listen": "0.0.0.0:1688",
                                   "MaxWorkers": "256", "ConnectionTimeout": "30",
                                   "WhiteListingLevel": "0", "CheckClientTime": "0",
                                   "LogVerbose": "1"},
                        "events": [
                            {"t": "2026-10-11 00:41:07", "ip": "39.144.169.199",
                             "cmid": "a1b2c3d4e5f6", "ok": True},
                            {"t": "2026-10-10 22:13:51", "ip": "59.63.43.227",
                             "cmid": "0f1e2d3c4b5a", "ok": True},
                            {"t": "2026-10-10 19:02:14", "ip": "203.0.113.9",
                             "cmid": "998877665544", "ok": False}]},
            "limits": {"available": True, "enabled": True, "limit": 10, "window": "day",
                       "window_text": "今天", "ban_hours": 24,
                       "skip": ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12",
                                "192.168.0.0/16", "169.254.0.0/16"],
                       "banned": ["203.0.113.9", "198.51.100.7"],
                       "pending_ban": [{"ip": "192.0.2.44"}], "pending_unban": [],
                       "top_ips": [{"ip": "203.0.113.9", "n": 26},
                                   {"ip": "198.51.100.7", "n": 14}]},
        }
