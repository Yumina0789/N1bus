#!/bin/bash
# change-key.sh —— 换掉 DSH 容器的 DeepSeek API key
#
# 为什么要有这个脚本：/opt/dsh/.env 是 600 的，改它得先登服务器；改完还得重启容器、
# 还得确认真能用。手工做这三步容易漏最后一步 —— 然后错误要等到某次对话才暴露。
#
# 做四件事，任何一步失败都回到原样：
#   1. 备份 .env（带时间戳，不回滚也留着）
#   2. 只替换 DEEPSEEK_API_KEY 一行，其余注释与内容原样不动
#   3. 重建并重启 dsh 容器
#   4. 真跑一次模型调用验证；不通就自动回滚并重启回旧 key
#
# 用法：
#   sudo /opt/dsh/change-key.sh              # 交互式，粘贴新 key（不回显）
#   sudo /opt/dsh/change-key.sh sk-xxxxx     # 也可直接给参数（会进 shell 历史，慎用）
#   sudo /opt/dsh/change-key.sh --check      # 只验证当前 key 能不能用，不改任何东西
#   sudo /opt/dsh/change-key.sh --rollback   # 回退到最近一次备份

set -uo pipefail

ENV_FILE="${DSH_ENV_FILE:-/opt/dsh/.env}"
COMPOSE_DIR="${DSH_COMPOSE_DIR:-/opt/dsh}"
CONTAINER="${DSH_CONTAINER:-dsh}"
KEY_NAME="DEEPSEEK_API_KEY"
VERIFY_PROMPT="${DSH_VERIFY_PROMPT:-只回复两个字：你好}"
VERIFY_TIMEOUT="${DSH_VERIFY_TIMEOUT:-120}"

c_r=$'\033[31m'; c_g=$'\033[32m'; c_y=$'\033[33m'; c_b=$'\033[1m'; c_0=$'\033[0m'
say()  { printf '%s\n' "$*"; }
good() { printf '%s✔%s %s\n' "$c_g" "$c_0" "$*"; }
bad()  { printf '%s✘%s %s\n' "$c_r" "$c_0" "$*" >&2; }
warn() { printf '%s!%s %s\n' "$c_y" "$c_0" "$*"; }
die()  { bad "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "要以 root 跑：sudo $0"

have() { command -v "$1" >/dev/null 2>&1; }
have docker || die "找不到 docker"
have python3 || die "找不到 python3"

# ---------------------------------------------------------------- 读取当前 key
current_key() {
    [ -f "$ENV_FILE" ] || return 1
    sed -n "s/^[[:space:]]*${KEY_NAME}[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -1
}

# ---------------------------------------------------------------- 验证 key 真能用
# 真发一次最小请求。成功返回 0，并把容器输出打到 stdout。
verify_key() {
    local out rc
    # headless 跑一次；--no-open 类参数不需要。输出里含模型回复。
    out=$(timeout "$VERIFY_TIMEOUT" docker exec "$CONTAINER" \
              dsh headless "$VERIFY_PROMPT" 2>&1)
    rc=$?
    if [ $rc -ne 0 ]; then
        printf '%s\n' "$out" | tail -5 >&2
        return 1
    fi
    printf '%s\n' "$out"
    # 明显的鉴权失败特征
    if printf '%s' "$out" | grep -qiE "invalid[_ ]api[_ ]key|authentication|401|unauthorized|incorrect api key"; then
        return 1
    fi
    return 0
}

latest_backup() {
    ls -1t "${ENV_FILE}.bak."* 2>/dev/null | head -1
}

restart_dsh() {
    ( cd "$COMPOSE_DIR" && docker compose up -d >/dev/null 2>&1 ) \
        || die "docker compose up -d 失败，去看 $COMPOSE_DIR 下的 compose 输出"
    # 等容器真的 Up（compose 返回时可能还在起）
    local i
    for i in $(seq 1 30); do
        if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ---------------------------------------------------------------- 模式：只检查
if [ "${1:-}" = "--check" ]; then
    say "${c_b}只验证当前 key${c_0}（不改任何文件）"
    k=$(current_key) || die "读不到 $ENV_FILE"
    [ -n "$k" ] || die "$ENV_FILE 里没有 $KEY_NAME"
    say "  当前 key：${k:0:7}…（${#k} 字符）"
    if out=$(verify_key); then
        good "key 可用。模型回复：$(printf '%s' "$out" | tail -1)"
        exit 0
    fi
    bad "key 不可用（模型调用失败）"
    exit 1
fi

# ---------------------------------------------------------------- 模式：回滚
if [ "${1:-}" = "--rollback" ]; then
    b=$(latest_backup) || true
    [ -n "${b:-}" ] || die "没有找到任何备份（${ENV_FILE}.bak.*）"
    say "${c_b}回滚到${c_0} $b"
    cp -p "$b" "$ENV_FILE"
    k=$(current_key)
    say "  恢复成：${k:0:7}…（${#k} 字符）"
    restart_dsh || die "容器没起来"
    if out=$(verify_key); then
        good "回滚后 key 可用"
        exit 0
    fi
    bad "回滚后仍然不可用 —— 那个备份里的 key 本身可能也失效了"
    exit 1
fi

# ---------------------------------------------------------------- 取新 key
if [ -n "${1:-}" ]; then
    NEWKEY="$1"
    warn "从命令行读到 key（会留在 shell 历史里；下次建议直接跑脚本、交互粘贴）"
else
    printf '%s' "${c_b}粘贴新的 DeepSeek API key${c_0}（输入不回显，粘贴后回车）："
    read -rs NEWKEY
    printf '\n'
fi

NEWKEY="$(printf '%s' "$NEWKEY" | tr -d '[:space:]')"
[ -n "$NEWKEY" ] || die "没输入任何内容"
case "$NEWKEY" in
    sk-*) ;;
    *) die "格式不对：DeepSeek 的 key 以 sk- 开头" ;;
esac
[ "${#NEWKEY}" -ge 20 ] || die "长度不对：只有 ${#NEWKEY} 个字符，看着不像完整 key"

OLDKEY="$(current_key || true)"
if [ -n "$OLDKEY" ] && [ "$OLDKEY" = "$NEWKEY" ]; then
    warn "新旧 key 一模一样，仍然继续（也许你只是想重新验证一次）"
fi

say ""
say "${c_b}即将修改${c_0} $ENV_FILE"
say "  旧 key：${OLDKEY:0:7}…（${#OLDKEY} 字符）"
say "  新 key：${NEWKEY:0:7}…（${#NEWKEY} 字符）"
printf '继续？（输入 yes 确认）：'
read -r ans
[ "$ans" = "yes" ] || die "已取消，什么都没改"

# ---------------------------------------------------------------- 备份
STAMP="$(date +%Y%m%d-%H%M%S)"
BAK="${ENV_FILE}.bak.${STAMP}"
cp -p "$ENV_FILE" "$BAK" || die "备份失败，未做任何修改"
chmod 600 "$BAK"
good "已备份到 $BAK"

# ---------------------------------------------------------------- 写入
# 用 python 精确改一行：保留注释、保留其它变量、保留顺序，末尾补换行。
python3 - "$ENV_FILE" "$KEY_NAME" "$NEWKEY" <<'PY' || die "写入失败"
import sys, os, tempfile

path, name, value = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "r", encoding="utf-8") as fh:
    lines = fh.read().splitlines()

out, hit = [], False
for ln in lines:
    stripped = ln.lstrip()
    if stripped.startswith(name) and "=" in stripped:
        # 保留前面的缩进，其余整行换掉
        indent = ln[:len(ln) - len(stripped)]
        out.append("%s%s=%s" % (indent, name, value))
        hit = True
    else:
        out.append(ln)
if not hit:
    if out and out[-1].strip():
        out.append("")
    out.append("%s=%s" % (name, value))

data = "\n".join(out) + "\n"
# 原子替换，避免写一半被中断留下坏文件
d = os.path.dirname(os.path.abspath(path)) or "."
fd, tmp = tempfile.mkstemp(dir=d)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
chmod 600 "$ENV_FILE"
good "已写入新 key（权限保持 600）"

# ---------------------------------------------------------------- 重启 + 验证
say ""
say "${c_b}重建并重启容器${c_0}"
if ! restart_dsh; then
    warn "容器没起来 —— 立刻回滚"
    cp -p "$BAK" "$ENV_FILE"
    restart_dsh || bad "回滚后容器依然没起来，需要手工看 docker logs $CONTAINER"
    die "已回滚到旧 key"
fi
good "容器已重启"

say ""
say "${c_b}验证新 key${c_0}（真跑一次模型调用，最多等 ${VERIFY_TIMEOUT}s）"
if out=$(verify_key); then
    good "新 key 可用！模型回复：$(printf '%s' "$out" | tail -1)"
    say ""
    say "旧 key 现在可以到 https://platform.deepseek.com/ 吊销了。"
    say "备份留在 $BAK（确认无误后可以删）"
    exit 0
fi

# ---------------------------------------------------------------- 失败回滚
bad "新 key 验证失败 —— 自动回滚"
cp -p "$BAK" "$ENV_FILE"
if restart_dsh; then
    if out=$(verify_key); then
        good "已回滚，旧 key 仍然可用"
    else
        warn "已回滚，但旧 key 现在也验证不过 —— 可能是网络或平台侧问题，不一定是 key"
    fi
else
    bad "回滚后容器没起来，请手工排查：docker logs --tail 50 $CONTAINER"
fi
die "新 key 没通过验证，已恢复原状"
