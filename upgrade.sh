#!/usr/bin/env bash
# N1bus 升级：从 git 仓库取最新代码，再重装。
#
#   sudo bash upgrade.sh          # git pull + 重装（沿用现有部署参数）
#   sudo bash upgrade.sh --check  # 只看有没有新提交，什么都不改
#
# 前提：本脚本所在目录是一个 git 工作区（即当初用 git clone 部署的）。
# 如果服务器上当初是 `curl … | bash install.sh` 装的，先做一次性迁移：
#
#   sudo git clone https://github.com/Yumina0789/N1bus.git /opt/n1bus-src
#   cd /opt/n1bus-src && sudo bash install.sh
#
# 之后升级都走：cd /opt/n1bus-src && sudo bash upgrade.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
UNIT="/etc/systemd/system/n1bus.service"
CHECK=0
if [ "${1:-}" = "--check" ]; then CHECK=1; fi

if ! git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  cat >&2 <<EOF
这个目录不是 git 工作区：$SRC

一次性迁移到 git 部署（当前文件会被仓库里的版本覆盖，旧文件自动留 .bak）：
  sudo git clone https://github.com/Yumina0789/N1bus.git /opt/n1bus-src
  cd /opt/n1bus-src && sudo bash install.sh

之后升级：
  cd /opt/n1bus-src && sudo bash upgrade.sh
EOF
  exit 1
fi

[ "$(id -u)" = "0" ] || { echo "需要 root：sudo bash $0" >&2; exit 1; }

before="$(git -C "$SRC" rev-parse --short HEAD)"
echo "==> 当前提交：$before"
# --ff-only：仓库里有本地改动或分叉时直接失败，不擅自合并
git -C "$SRC" pull --ff-only
after="$(git -C "$SRC" rev-parse --short HEAD)"
echo "==> 最新提交：$after"

if [ "$CHECK" = "1" ]; then
  if [ "$before" = "$after" ]; then echo "已是最新。"; else echo "有新版本：$before → $after"; fi
  exit 0
fi

# 从现有 systemd 单元里把部署参数捞出来，重装时原样带上。
# 不这么做的话，install.sh 会按**默认值**重写单元 —— --bind / --allow-ip / --enforce-*
# 会被静默丢掉，表现为「升级之后反代连不上面板」或「自动封禁莫名关闭」。
ARGS=()
if [ -f "$UNIT" ]; then
  line="$(grep -m1 '^ExecStart=' "$UNIT" || true)"
  # 去掉 "ExecStart=" 以及前面的解释器与脚本路径，只留参数
  rest="$(printf '%s' "$line" | sed -E 's|^ExecStart=[^ ]+ +[^ ]*/n1bus\.py *||')"
  # 这里刻意让 shell 按空格拆词（参数值本身不含空格：地址、端口、网段、URL、token）
  # shellcheck disable=SC2086
  set -- $rest
  while [ $# -gt 0 ]; do
    case "$1" in
      --port|--bind|--allow-ip|--data-dir|--enforce-url|--enforce-token)
        ARGS+=("$1" "$2"); shift 2 ;;
      *) shift ;;
    esac
  done
  if [ ${#ARGS[@]} -gt 0 ]; then
    echo "==> 沿用现有部署参数：${ARGS[*]}"
  else
    echo "==> 现有单元里没有需要保留的参数（用默认值重装）"
  fi
fi

echo "==> 重装（$before → $after）"
exec bash "$SRC/install.sh" ${ARGS[@]+"${ARGS[@]}"}
