#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
n1bus —— 服务器管理面板的入口。

本文件只做三件事：解析参数、把各个域装配起来、起 HTTP 服务。
其余代码按域拆在 n1bus_*.py 里：

    n1bus_util      通用工具（子进程、文本、网段、密钥比较）
    n1bus_cache     单元属性缓存（systemd 单元 → 路径 / 状态）
    n1bus_audit     审计日志
    n1bus_files     文件管理器（白名单根 + 路径判定）
    n1bus_actions   白名单动作表 + 参数校验 + 执行
    n1bus_system    systemd / docker / 端口 / 进程
    n1bus_security  账户、会话、限流、封禁、执法
    n1bus_probe     真机与演示两套数据源
    n1bus_tasks     计划任务与告警
    n1bus_kms       KMS 授权服务（只读 + 白名单写转发）
    n1bus_modules   模块域（商店清单、模块包校验与解包）
    n1bus_web       HTTP 层（Handler + 页面拼装）

两条硬性约束（内容原样保留，见 docs/adr/）：

* **不提供任意命令执行**。只有 n1bus_actions 里写死的白名单动作，参数以列表
  形式交给 subprocess，不经过 shell；危险动作界面二次确认 + 无论成败都写审计。
* 观测数据只活在内存里（环形缓冲够画曲线就行，不落库），落盘的只有审计日志。
"""

import argparse
import base64
import collections
import copy
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from n1bus_audit import Audit
from n1bus_kms import KMSService, NIMBUS_URL, _DemoKMS
import n1bus_modules
from n1bus_modules import Modules
from n1bus_probe import DemoProbe, HostProbe, Sampler
from n1bus_security import (AUTOBAN_INTERVAL, AUTOBAN_MIN_THRESHOLD, AUTOBAN_THRESHOLD,
                            Accounts, Defender, Enforcer, LoginGuard, PRIVATE_NETS)
from n1bus_tasks import Alerts, Tasks
from n1bus_util import VERSION, dedup_nets, expand_binds, now_iso, parse_networks
from n1bus_web import Handler


HERE = Path(__file__).resolve().parent


def main():
    # 控制台编码可能是 GBK / ASCII（Windows 与 LANG=C 的 Linux 均如此），一行中文横幅
    # 就会让面板无法启动。因此先把输出通道统一设为 UTF-8，解码失败时替换字符而不中断进程。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    def _env_int(name, fallback):
        """读取环境变量中的整数；解析失败时返回回退值。

        这些参数由 systemd 单元的 Environment= 配置。若取值非法，int() 会抛出异常，
        而该调用发生在 argparse 计算默认值的阶段，会直接导致面板无法启动。
        """
        try:
            return int(os.environ.get(name) or fallback)
        except (TypeError, ValueError):
            return fallback

    ap = argparse.ArgumentParser(description="N1bus —— Linux 服务器管理面板")
    ap.add_argument("--mode", choices=["demo", "real"], default="real")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="监听地址，逗号分隔可绑多个；写 docker 表示再绑一份宿主在 docker 网桥上的地址")
    ap.add_argument("--allow-ip", default=os.environ.get("N1BUS_ALLOW_IP", ""),
                    help="来源白名单（逗号分隔的网段）；留空 = 不限制，靠绑定地址与 token 守门")
    ap.add_argument("--port", type=int, default=8402)
    ap.add_argument("--token", default="", help="直接给 token（默认从 token 文件读或生成）")
    ap.add_argument("--token-file", default="", help="默认 <data-dir>/n1bus.token")
    ap.add_argument("--data-dir", default="/var/lib/n1bus", help="token 与审计日志的目录")
    ap.add_argument("--interval", type=float, default=2.0, help="采样间隔（秒）")
    ap.add_argument("--enforce-url", default=os.environ.get("N1BUS_ENFORCE_URL", ""),
                    help="从哪个面板拉「该封谁」，例如 http://127.0.0.1:8099（v1m-dash）")
    ap.add_argument("--enforce-token", default=os.environ.get("N1BUS_ENFORCE_TOKEN", ""),
                    help="上面那个面板的访问 token")
    ap.add_argument("--enforce-interval", type=int, default=60,
                    help="拉取间隔（秒），最小 10；0 表示关闭自动封禁")
    ap.add_argument("--auto-ban", choices=["on", "off"],
                    default=os.environ.get("N1BUS_AUTO_BAN") or "on",
                    help="重复失败来源永久封禁：累计失败次数超过 --auto-ban-after 的来源"
                         "执行 ufw deny，且不自动解封（on/off，默认 on）")
    ap.add_argument("--auto-ban-after", type=int,
                    default=_env_int("N1BUS_AUTO_BAN_AFTER", AUTOBAN_THRESHOLD),
                    help="累计失败次数超过该值即永久封禁（默认 %d，最小 %d）"
                         % (AUTOBAN_THRESHOLD, AUTOBAN_MIN_THRESHOLD))
    ap.add_argument("--auto-ban-interval", type=int,
                    default=_env_int("N1BUS_AUTO_BAN_INTERVAL", AUTOBAN_INTERVAL),
                    help="巡检间隔（秒），最小 30（默认 %d）" % AUTOBAN_INTERVAL)
    ap.add_argument("--nimbus-url", default=os.environ.get("NIMBUS_URL") or NIMBUS_URL,
                    help="KMS 授权服务（v1m-dash）的地址，默认 %s" % NIMBUS_URL)
    ap.add_argument("--nimbus-token", default=os.environ.get("NIMBUS_TOKEN", ""),
                    help="上面那个面板的 token；默认自动从它的 docker 卷里读")
    ap.add_argument("--store-url", default=os.environ.get("N1BUS_STORE_URL")
                    or n1bus_modules.STORE_URL,
                    help="模块商店（registry 仓库的 GitHub Pages）地址，默认 %s"
                         % n1bus_modules.STORE_URL)
    args = ap.parse_args()

    if args.mode == "demo":
        data_dir = Path(args.data_dir if args.data_dir != "/var/lib/n1bus"
                        else HERE / ".n1bus-demo-data")
    else:
        data_dir = Path(args.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit("无法创建数据目录：%s" % e)
    if args.mode == "demo":
        # 演示模式把模块根指到数据目录下：/opt 在开发机上不可写，而安装流程必须真落盘
        # 才能演练到底（否则 module.install 的「该模块没有安装」会误报）。
        # 必须在构造 Modules 之前设置 —— 动作层读的是这个模块级变量。
        n1bus_modules.MODULES_DIR = str(data_dir / "modules")

    token = args.token or os.environ.get("N1BUS_TOKEN") or ""
    token_file = Path(args.token_file) if args.token_file else data_dir / "n1bus.token"
    if not token:
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            token = secrets.token_urlsafe(24)
            try:
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text(token, encoding="utf-8")
                os.chmod(token_file, 0o600)
            except OSError as e:
                sys.exit("无法写入 token 文件：%s" % e)

    sampler = Sampler(interval=args.interval, fake=(args.mode == "demo"))
    audit = Audit(data_dir / "audit.log")
    accounts = Accounts(data_dir / "accounts.json")
    enforcer = Enforcer(args.enforce_url, args.enforce_token, args.enforce_interval, audit)
    if args.mode == "demo" or not args.enforce_interval:
        enforcer.enabled = False
    # 排除网段 = 私有网段 + 面板访问白名单。两者都不应被自动封禁，尤其是 --allow-ip：
    # 它是管理员访问面板所使用的来源。
    defender = Defender(data_dir / "defender.json", threshold=args.auto_ban_after,
                        enabled=(args.auto_ban == "on"),
                        skip=dedup_nets(parse_networks(PRIVATE_NETS)
                                        + parse_networks(args.allow_ip)),
                        audit=audit, interval=args.auto_ban_interval,
                        dry=(args.mode == "demo"))
    probe = DemoProbe(sampler, audit) if args.mode == "demo" else HostProbe(sampler, audit)
    tasks = Tasks(data_dir, audit, probe)
    alerts = Alerts(data_dir, audit, probe)
    app = {"probe": probe, "sampler": sampler, "token": token, "mode": args.mode,
           "started": now_iso(), "enforcer": enforcer, "defender": defender,
           "kms": _DemoKMS() if args.mode == "demo"
                  else KMSService(args.nimbus_url, args.nimbus_token),
           "accounts": accounts, "audit": audit,
           "guard": LoginGuard(data_dir / "loginguard.json"),
           "tasks": tasks, "alerts": alerts,
           # 演示模式用一个可写的临时模块目录：安装流程要真的落盘才能演练到底。
           # 同时改写模块根 —— 动作层（n1bus_actions）通过 modules_dir() 读它，
           # 两边必须是同一个值，否则「该模块没有安装」会误报。
           "modules": Modules(root=(data_dir / "modules") if args.mode == "demo"
                                   else n1bus_modules.MODULES_DIR,
                              store_url=args.store_url,
                              dry=(args.mode == "demo")),
           "allow_nets": parse_networks(args.allow_ip)}
    Handler.app = app

    # 先把端口占住，再起采样线程：反过来的话 systemd 已经报 active、端口却还没监听，
    # 健康检查会取到未监听的端口而误判（install.sh 的健康检查曾出现一次该误判）。
    servers = []
    for addr in expand_binds(args.bind):
        try:
            srv = ThreadingHTTPServer((addr, args.port), Handler)
        except OSError as e:
            print("  !! 无法绑定 %s:%d（%s）" % (addr, args.port, e))
            continue
        srv.daemon_threads = True
        servers.append((addr, srv))
    if not servers:
        sys.exit("没有任何地址绑定成功，退出")
    for _, srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    sampler.start()
    if enforcer.enabled:
        threading.Thread(target=enforcer.loop, daemon=True).start()
    if defender.state["enabled"]:
        defender.start(probe.security)
    tasks.start()
    alerts.start()
    print("=" * 72)
    print("  N1bus %s  [%s 模式]" % (VERSION, args.mode))
    for addr, _ in servers:
        print("  监听 %s:%d" % (addr, args.port))
    print("  打开这个地址（已带 token）：http://%s:%d/?token=%s"
          % (servers[0][0] if servers[0][0] != "0.0.0.0" else "127.0.0.1", args.port, token))
    print("  纯 token：%s（机器凭证，浏览器改用账户登录）" % token)
    print("  如需查看 token：cat %s" % token_file)
    print("  数据目录：%s（审计日志 %s）" % (data_dir, data_dir / "audit.log"))
    if accounts.need_setup():
        print("  注意：还没有账户，打开页面先设置账户名与密码")
    else:
        print("  账户：%s（修改密码见面板「账户」页）" % "、".join(accounts.names()))
    if app["allow_nets"]:
        print("  来源白名单：%s" % "、".join(str(n) for n in app["allow_nets"]))
    if args.mode == "real":
        print("  systemd：%s   docker：%s" % (
            "可用" if probe.services.available else "不可用",
            "可用" if probe.docker.available else "不可用"))
        print("  自动封禁：%s" % ("%s（每 %d 秒拉一次）" % (enforcer.url, enforcer.interval)
                                 if enforcer.enabled else "关闭"))
        print("  重复失败来源永久封禁：%s" % (
            "开启 —— 累计失败 > %d 次即执行 ufw deny，每 %d 秒巡检一次%s"
            % (defender.state["threshold"], defender.interval,
               "（演示模式：仅记录，不实际执行）" if defender.dry else "")
            if defender.state["enabled"] else "关闭"))
    print("=" * 72)
    sys.stdout.flush()
    try:
        servers[0][1].serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
