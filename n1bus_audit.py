"""n1bus 审计日志：所有改变系统状态的尝试都追加一行 JSON（Audit）。"""

import json
import shutil
import threading
import time
from pathlib import Path

from n1bus_util import now_iso, read_text


class Audit:
    """审计日志：所有改变系统状态的尝试都追加一行 JSON。落盘的只有这个。"""

    def __init__(self, path, max_bytes=2 << 20):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()

    def write(self, action, target, ok, detail, ip):
        entry = {"ts": now_iso(), "t": int(time.time()), "ip": ip, "action": action,
                 "target": target, "ok": bool(ok), "detail": (detail or "")[:600]}
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    shutil.copy2(str(self.path), str(self.path) + ".1")
                    open(self.path, "w", encoding="utf-8").close()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return entry

    def tail(self, n=200):
        out = []
        for line in read_text(str(self.path)).splitlines()[-n:][::-1]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
