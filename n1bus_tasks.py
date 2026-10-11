"""n1bus 的计划任务与告警：面板内的任务表 Tasks 与阈值告警 Alerts。"""

import json
import os
import threading
import time
import urllib.request

from n1bus_actions import ACTIONS, run_action


# --------------------------------------------------------------------------
# 计划任务与告警：由面板自身启动后台线程，不修改系统 crontab
# --------------------------------------------------------------------------
class Tasks:
    """面板内的计划任务表。

    任务只能调用 ACTIONS 中已登记的动作 —— 不写入系统 crontab、不在磁盘上生成任何脚本文件。
    代价是：面板未运行时任务同样不会执行（页面上已说明）。
    """

    MIN_EVERY = 60
    TICK = 30

    def __init__(self, data_dir, audit, probe):
        self.path = os.path.join(str(data_dir), "tasks.json")
        self.audit = audit
        self.probe = probe
        self.lock = threading.Lock()
        self.items = self._load()
        self.recent = []
        self.stop = threading.Event()
        self._seq = max([int(t.get("id") or 0) for t in self.items] or [0])

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        return [t for t in (data if isinstance(data, list) else [])
                if isinstance(t, dict) and t.get("action") in ACTIONS]

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def listing(self):
        return {"tasks": self.items,
                "actions": [{"name": n, "label": s.get("label", n),
                             "params": sorted((s.get("params") or {}).keys()),
                             "danger": bool(s.get("danger"))}
                            for n, s in sorted(ACTIONS.items())],
                "recent": self.recent[-20:][::-1], "min_every": self.MIN_EVERY,
                "now": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "仅执行白名单动作；不写入系统 crontab"}

    def add(self, action, params, every, ip, enabled=True):
        spec = ACTIONS.get(action or "")
        if not spec:
            return False, "该动作不存在"
        try:
            every = int(every)
        except (TypeError, ValueError):
            return False, "周期必须是整数秒"
        if not (self.MIN_EVERY <= every <= 30 * 86400):
            return False, "周期需在 %d 秒至 30 天之间" % self.MIN_EVERY
        clean = {}
        for key, validator in (spec.get("params") or {}).items():
            try:
                clean[key] = validator((params or {}).get(key))
            except (ValueError, TypeError, AttributeError) as e:
                return False, "参数 %s 不合法：%s" % (key, e)
        with self.lock:
            self._seq += 1
            self.items.append({"id": self._seq, "action": action, "params": clean,
                               "every": every, "enabled": bool(enabled),
                               "next_at": time.time() + every, "last_run": "",
                               "last_ok": None, "last_result": ""})
            self._save()
        self.audit.write("task.add", "%s 每 %d 秒" % (action, every), True, "", ip)
        return True, ""

    def remove(self, tid, ip):
        with self.lock:
            before = len(self.items)
            self.items = [t for t in self.items if str(t.get("id")) != str(tid)]
            if len(self.items) == before:
                return False, "该任务不存在"
            self._save()
        self.audit.write("task.del", str(tid), True, "", ip)
        return True, ""

    def toggle(self, tid, ip):
        with self.lock:
            for t in self.items:
                if str(t.get("id")) == str(tid):
                    t["enabled"] = not t.get("enabled")
                    t["next_at"] = time.time() + int(t.get("every") or 300)
                    self._save()
                    self.audit.write("task.toggle", str(tid), True,
                                     "现在 %s" % ("启用" if t["enabled"] else "停用"), ip)
                    return True, ""
        return False, "该任务不存在"

    def run_now(self, tid, ip):
        target = next((t for t in self.items if str(t.get("id")) == str(tid)), None)
        if not target:
            return False, {"error": "该任务不存在"}
        return True, self._run(target, ip)

    def _run(self, t, ip):
        res = run_action(t.get("action"), t.get("params") or {}, ip, self.audit,
                         dry=not self.probe.real)
        ok = bool(res.get("ok"))
        t["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        t["last_ok"] = ok
        t["last_result"] = (res.get("error") or res.get("detail") or "")[:200]
        self.recent.append({"ts": t["last_run"], "action": t.get("action"), "ok": ok,
                            "detail": t["last_result"], "demo": bool(res.get("demo"))})
        with self.lock:
            self._save()
        return {"ok": ok, "detail": t["last_result"], "demo": bool(res.get("demo"))}

    def tick(self):
        now = time.time()
        for t in list(self.items):
            if not t.get("enabled") or now < float(t.get("next_at") or 0):
                continue
            t["next_at"] = now + int(t.get("every") or 300)
            try:
                self._run(t, "task")
            except Exception:
                continue

    def start(self):
        def loop():
            while not self.stop.wait(self.TICK):
                try:
                    self.tick()
                except Exception:
                    continue
        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self):
        self.stop.set()


class Alerts:
    """阈值告警：超阈值发 Webhook，同指标冷却期内只发一次。"""

    TICK = 60
    DEFAULTS = {"enabled": False, "webhook": "", "cooldown_min": 30,
                "rules": {"disk_percent": 90, "mem_percent": 92, "load_per_core": 2.0,
                          "failed_units": 1}}

    def __init__(self, data_dir, audit, probe):
        self.path = os.path.join(str(data_dir), "alerts.json")
        self.audit = audit
        self.probe = probe
        self.lock = threading.Lock()
        self.cfg = self._load()
        self.fired = {}
        self.recent = []
        self.stop = threading.Event()

    def _load(self):
        cfg = json.loads(json.dumps(self.DEFAULTS))
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return cfg
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in cfg})
            if isinstance(saved.get("rules"), dict):
                cfg["rules"].update(saved["rules"])
        return cfg

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def save(self, body, ip):
        body = body or {}
        webhook = str(body.get("webhook") or "").strip()
        if webhook and not webhook.startswith(("http://", "https://")):
            return False, "Webhook 必须以 http:// 或 https:// 开头"
        if len(webhook) > 300:
            return False, "Webhook 地址长度超过 300 字符上限"
        try:
            cooldown = int(body.get("cooldown_min") or 30)
        except (TypeError, ValueError):
            return False, "冷却时间必须是整数分钟"
        rules = {}
        for key, default in self.DEFAULTS["rules"].items():
            try:
                rules[key] = float((body.get("rules") or {}).get(key, default) or 0)
            except (TypeError, ValueError):
                return False, "阈值 %s 不是有效数字" % key
        if body.get("enabled") and not webhook:
            return False, "启用告警需先配置 Webhook"
        with self.lock:
            self.cfg = {"enabled": bool(body.get("enabled")), "webhook": webhook,
                        "cooldown_min": max(1, min(1440, cooldown)), "rules": rules}
            self._save()
        self.audit.write("alert.save", webhook or "(未设置)", True,
                         "冷却 %d 分钟" % self.cfg["cooldown_min"], ip)
        return True, ""

    def status(self):
        return {"config": self.cfg, "recent": self.recent[-20:][::-1],
                "note": "同一指标冷却期内只发一次"}

    def _post(self, payload):
        import urllib.request
        url = self.cfg.get("webhook") or ""
        if not url:
            return False, "未配置 Webhook"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "n1bus-alert/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300, "HTTP %s" % resp.status
        except Exception as e:  # noqa: BLE001
            return False, "%s: %s" % (type(e).__name__, str(e)[:120])

    def test(self, ip):
        host = os.uname().nodename if hasattr(os, "uname") else "n1bus"
        ok, detail = self._post({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "test",
                                 "metric": "test", "value": 0, "threshold": 0, "host": host,
                                 "text": "N1bus 测试告警：通道正常"})
        self.recent.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "metric": "test",
                            "ok": ok, "detail": detail})
        self.audit.write("alert.test", "test", ok, detail, ip)
        return ok, detail

    def check(self):
        if not self.cfg.get("enabled"):
            return
        try:
            snap = self.probe.overview()
        except Exception:  # noqa: BLE001
            return
        rules = self.cfg.get("rules") or {}
        cpu = snap.get("cpu") or {}
        mem = snap.get("mem") or {}
        svc = snap.get("services") or {}
        disk = snap.get("disk") or []
        root = next((d for d in disk if d.get("mount") == "/"), (disk or [{}])[0])
        cores = max(1, int(cpu.get("count") or 1))
        checks = [("disk_percent", float(root.get("percent") or 0), "根分区使用率"),
                  ("mem_percent", float(mem.get("percent") or 0), "内存使用率"),
                  ("load_per_core", float((cpu.get("load") or [0])[0] or 0) / cores, "每核负载"),
                  ("failed_units", float(svc.get("failed") or 0), "失败的服务单元")]
        cooldown = max(1, int(self.cfg.get("cooldown_min") or 30)) * 60
        now = time.time()
        for metric, value, label in checks:
            threshold = float(rules.get(metric) or 0)
            if threshold <= 0 or value < threshold:
                continue
            if now - float(self.fired.get(metric) or 0) < cooldown:
                continue
            self.fired[metric] = now
            host = (snap.get("info") or {}).get("hostname") or "n1bus"
            ok, detail = self._post({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "level": "warn",
                                     "metric": metric, "value": round(value, 2),
                                     "threshold": threshold, "host": host,
                                     "text": "%s %s 超过阈值 %s" % (label, round(value, 2),
                                                                   threshold)})
            self.recent.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "metric": metric,
                                "value": round(value, 2), "threshold": threshold, "ok": ok,
                                "detail": detail})

    def start(self):
        def loop():
            while not self.stop.wait(self.TICK):
                try:
                    self.check()
                except Exception:
                    continue
        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self):
        self.stop.set()
