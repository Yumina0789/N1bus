#!/usr/bin/env bash
# N1bus 安装脚本：把面板装成宿主机上的 systemd 服务。
#
#   curl -fsSL https://raw.githubusercontent.com/Yumina0789/N1bus/main/install.sh | sudo bash
#   sudo bash install.sh --port 8402 --bind 127.0.0.1
#   sudo bash install.sh --uninstall          # 停掉并删服务，数据与 token 留着
#   sudo bash install.sh --purge              # 连数据一起删
#
# 为什么装在宿主机而不是容器：面板要管 systemd、进程、/proc、/var/log、docker，
# 容器化就得 privileged + 挂 docker.sock，隔离反而没了。见 docs/adr/0001。
set -euo pipefail

PREFIX="/opt/n1bus"
DATA_DIR="/var/lib/n1bus"
UNIT="/etc/systemd/system/n1bus.service"
PORT="8402"
BIND="127.0.0.1"
ALLOW_IP=""
TOKEN=""
REPO_RAW="${N1BUS_REPO_RAW:-https://raw.githubusercontent.com/Yumina0789/N1bus/main}"
UNINSTALL=0
PURGE=0
PY="$(command -v python3 || true)"
# 管道执行时 BASH_SOURCE 可能不存在，退回 $0；两者都不可用时以当前目录为准
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo ".")"

usage() {
  cat <<'EOF'
N1bus 安装脚本：把面板装成宿主机上的 systemd 服务。

选项：
  --port N        监听端口（默认 8402）
  --bind IP       监听地址（默认 127.0.0.1）。逗号分隔可绑多个，写 docker 表示
                  再绑一份宿主在 docker 网桥上的地址（反代在容器里时需要）
  --allow-ip 网段  来源白名单（逗号分隔）；留空 = 不限制
  --prefix DIR    安装目录（默认 /opt/n1bus）
  --data-dir DIR  数据目录（默认 /var/lib/n1bus，放 token 与审计日志）
  --token STR     指定 token（默认随机生成）
  --uninstall     停止并移除服务，保留数据与 token
  --purge         连数据目录一起删
  -h, --help      看这段
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?}"; shift 2 ;;
    --bind) BIND="${2:?}"; shift 2 ;;
    --allow-ip) ALLOW_IP="${2:?}"; shift 2 ;;
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --data-dir) DATA_DIR="${2:?}"; shift 2 ;;
    --token) TOKEN="${2:?}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) UNINSTALL=1; PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" != "0" ]; then
  echo "需要 root：面板要调 systemctl / docker / 读 /proc 与 /var/log。" >&2
  exit 1
fi

if [ "$UNINSTALL" = "1" ]; then
  echo "==> 停止并移除 n1bus.service"
  systemctl disable --now n1bus 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload || true
  if [ "$PURGE" = "1" ]; then
    echo "==> 删除 $DATA_DIR 与 $PREFIX"
    rm -rf "$DATA_DIR" "$PREFIX"
  else
    echo "==> 数据保留在 $DATA_DIR（如需删除请加 --purge）"
  fi
  echo "完成。"
  exit 0
fi

if [ -z "$PY" ]; then
  echo "找不到 python3。N1bus 只用标准库，安装 python3 即可（apt install -y python3）。" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 源码清单。必须保持显式清单，不要替换为 glob：本脚本常在 `curl … | bash` 管道中或从任意目录执行，
# 此时 $SCRIPT_DIR 中没有这些文件，glob 匹配不到就不会展开，会把字面量
# "n1bus*.py" 当作文件名去请求，导致 404（2026-10 迁移时曾发生，并回滚过一次）。
# 代价是新增模块需要同步修改此处 —— 由 tools/check.py 的「清单一致性」检查覆盖。
SRC_FILES="n1bus.py n1bus_util.py n1bus_cache.py n1bus_audit.py n1bus_files.py n1bus_actions.py n1bus_system.py n1bus_security.py n1bus_probe.py n1bus_tasks.py n1bus_modules.py n1bus_web.py ui.html ui.css ui.js"

echo "==> 取源码"
for f in $SRC_FILES; do
  if [ -f "$SCRIPT_DIR/$f" ]; then
    cp -f "$SCRIPT_DIR/$f" "$WORK/$f"
  else
    curl -fsSL --retry 3 --connect-timeout 15 -H 'Cache-Control: no-cache' \
      "$REPO_RAW/$f" -o "$WORK/$f"
  fi
  [ -s "$WORK/$f" ] || { echo "无法获取 $f" >&2; exit 1; }
done

echo "==> 安装到 $PREFIX"
mkdir -p "$PREFIX" "$DATA_DIR"
chmod 700 "$DATA_DIR"
for f in $SRC_FILES; do
  # 留上一份：升级出问题能立刻回滚
  [ -f "$PREFIX/$f" ] && cp -f "$PREFIX/$f" "$PREFIX/$f.bak"
  install -m 0644 "$WORK/$f" "$PREFIX/$f"
done

if [ ! -s "$DATA_DIR/n1bus.token" ]; then
  if [ -n "$TOKEN" ]; then
    printf '%s' "$TOKEN" > "$DATA_DIR/n1bus.token"
  else
    "$PY" -c 'import secrets;print(secrets.token_urlsafe(24),end="")' > "$DATA_DIR/n1bus.token"
  fi
fi
chmod 600 "$DATA_DIR/n1bus.token"
TOKEN="$(cat "$DATA_DIR/n1bus.token")"

echo "==> 写 systemd 单元"
ALLOW_ARGS=""
if [ -n "$ALLOW_IP" ]; then
  ALLOW_ARGS=" --allow-ip $ALLOW_IP"
  echo "    来源白名单：$ALLOW_IP"
fi
cat > "$UNIT" <<EOF
[Unit]
Description=N1bus 服务器管理面板
Documentation=https://github.com/Yumina0789/N1bus
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY $PREFIX/n1bus.py --mode real --bind $BIND --port $PORT --data-dir $DATA_DIR --token-file $DATA_DIR/n1bus.token$ALLOW_ARGS
Restart=always
RestartSec=3
User=root
# 面板本身需要 systemctl / docker / 进程信号等 root 能力；以下加固仅用于限制其可写范围。
# 注意 ProtectSystem 的档位：yes = /usr /boot 只读；**full = 连 /etc 也只读**；
# strict = 仅 /dev /proc /sys 可写。因此使用 full 时必须逐个放行**需要写入**的目录，
# 否则对应的白名单动作会以「... is not writable」失败（该故障已出现过两次）：
#   /etc/ufw            —— ufw.allow / ufw.deny 要写 user.rules
#   /etc/systemd/system —— boot.enable / boot.disable 要建 *.wants/ 符号链接
#                          （漏了它，面板上「设为开机自启」永远失败）
ProtectSystem=full
ReadWritePaths=/etc/ufw /etc/systemd/system
ProtectHome=read-only
PrivateTmp=true
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable n1bus
# 必须使用 restart 而非 `enable --now`：对已在运行的服务，`--now` 不会重启它，
# 导致升级后仍运行旧代码（此情况曾发生过一次：新接口持续返回 404）。
systemctl restart n1bus
sleep 1
if ! systemctl is-active --quiet n1bus; then
  echo "!! 服务未启动，查看日志：journalctl -u n1bus -n 50 --no-pager" >&2
  exit 1
fi

# 服务处于 active 不等于端口已监听：面板需先建好采样器才会 bind。确认真正可连接后再报告成功。
PROBE_IP="${BIND%%,*}"                 # --bind 可以是逗号列表，拿第一个探活
[ "$PROBE_IP" = "0.0.0.0" ] && PROBE_IP="127.0.0.1"
[ "$PROBE_IP" = "docker" ] && PROBE_IP="127.0.0.1"
printf '==> 等端口 %s:%s 就绪' "$PROBE_IP" "$PORT"
ready=0
for _ in $(seq 1 30); do
  if (exec 3<>"/dev/tcp/$PROBE_IP/$PORT") 2>/dev/null; then
    exec 3<&- 2>/dev/null || true
    ready=1
    break
  fi
  printf '.'
  sleep 0.5
done
printf '\n'
if [ "$ready" != "1" ]; then
  echo "!! 端口未监听，查看日志：journalctl -u n1bus -n 50 --no-pager" >&2
  exit 1
fi
echo "==> 服务运行中，端口已就绪"

cat <<EOF

N1bus 安装完成。

  本机打开：http://$BIND:$PORT/?token=$TOKEN
  纯 token ：$TOKEN（存在 $DATA_DIR/n1bus.token，权限 600）

  从本机访问时建议使用 SSH 隧道：
      ssh -L $PORT:127.0.0.1:$PORT root@<这台机器的 IP>
      浏览器打开 http://127.0.0.1:$PORT/?token=$TOKEN

  如需使用域名，请交由反向代理处理，不要将 $BIND 改为 0.0.0.0：
  这是一个可启停服务、终止进程、封禁 IP 的 root 面板，公网侧唯一的防线就是 token。

  看日志：journalctl -u n1bus -f
  升级  ：重跑这个脚本（会自动留 .bak）
  卸载  ：bash install.sh --uninstall [--purge]
EOF
