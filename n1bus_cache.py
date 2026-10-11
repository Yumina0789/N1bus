"""n1bus 只读 TTL 缓存；UNIT_*_CACHE 也放在这里是为了断开循环依赖 —— clear_data_cache() 要清它们，而它们原本在 system 域，n1bus_system 会反向 import 这三个。"""

import copy
import threading
import time

# 单元属性缓存：FragmentPath（判断"发行版自带"还是"用户/第三方装的"）与
# UnitFileState（enabled / disabled / static …）。
# 逐个 systemctl show 约 8ms，210 个约 1.5s；这份缓存让它每个进程只付出一次代价。
UNIT_PATH_CACHE = {}
UNIT_STATE_CACHE = {}
UNIT_PROBED = set()          # 问过 systemctl 的单元（问不到也算问过，别反复问）


# --------------------------------------------------------------------------
# 只读 TTL 缓存：给"昂贵但幂等"的观测结果去重
# --------------------------------------------------------------------------
# 为什么需要它（真机实测，2 vCPU 的托管机）：
#   systemctl list-unit-files --type=service --state=enabled   ≈ 1.8 s   ← /api/autostart
#   apt-get -s autoremove                                      ≈ 0.9 s   ← /api/cleanup
#   fail2ban-client status[+jail] + ufw status verbose         ≈ 0.3 s   ← /api/security
# 这些命令的输出在几秒内不会变，但前端每次轮询/切页都重跑一次。机器只有 2 vCPU，
# 代价不只是慢，还有每次 fork 的 CPU 抖动。
#
# 规矩（与"不假装"那条设计原则对齐）：
#   * **只缓存只读观测**。任何写操作、任何随参数变化的结果都不进这里。
#   * TTL 一律短（默认 15 s，最多 30 s）。数据新鲜度有界，且响应里会带 cache 元数据，
#     界面能如实说明"这批数据可能是 N 秒前的"。
#   * 任何缓存命中都可以被 `?fresh=1` 强制绕过 —— 用户点"刷新"就该拿到当下的真值。
#   * 缓存只在内存里，进程重启即空（和采样曲线一样，不落库）。
DATA_CACHE_TTL = 15.0   # 常规观测
SLOW_CACHE_TTL = 30.0   # 要等子进程的那几条（systemctl / docker ps / ip route）
CLEANUP_CACHE_TTL = 300.0   # 清理账本：apt 模拟 + journald/镜像体积，几分钟内不会变

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LOCAL = threading.local()   # 每个请求线程各自的命中记录，用来给响应附元数据


def cache_meta():
    """当前线程最近一次缓存决策。没命中过就返回 None。"""
    hit, ttl, ts = getattr(_CACHE_LOCAL, "last", (False, 0.0, 0.0))
    if not hit:
        return None
    return {"stale": True, "ttl": ttl, "age": max(0, int(time.time() - ts))}


def _cache_key_name(k):
    """取出缓存键里用于前缀匹配的那部分。

    无参方法的键就是方法名本身（字符串）；**带参数方法的键是 `(方法名, 参数元组)`**
    —— 见 cached()。所以作废缓存时不能假定键一定是字符串，否则一旦缓存里存在带参数的
    条目，这里就会抛 AttributeError。

    这个错误的代价不小：它发生在 `do_POST` 里、动作**已经执行完并写过审计之后**，
    于是现象是「动作明明成功了，面板却报 JSON 解析失败」—— 因为异常把响应体也带走了。
    """
    if isinstance(k, tuple) and k and isinstance(k[0], str):
        return k[0]
    return k if isinstance(k, str) else ""


def clear_data_cache(prefixes=None):
    """作废只读缓存。

    给 prefixes 就只删以这些前缀开头的缓存键（写操作按影响面精确作废）；
    不给（None）就全清 —— 老调用与"拿不准影响面"的情况都走这条。
    返回被删掉的键，方便自检与排查。
    """
    with _CACHE_LOCK:
        if prefixes is None:
            gone = list(_CACHE)
        elif prefixes:
            pre = tuple(prefixes)
            gone = [k for k in _CACHE if _cache_key_name(k).startswith(pre)]
        else:
            gone = []
        for k in gone:
            _CACHE.pop(k, None)
    # 单元属性缓存**无条件作废**，不看 prefixes。
    # 这些是模块级的、**没有过期时间**的进程内缓存（为了省 systemctl show 的 8ms/个）。
    # 任何写操作都可能改 UnitFileState / ActiveState，留下旧值就会出现：
    #   systemctl 明明已经 disabled，面板还坚持显示 enabled —— 于是「点自启没反应」。
    # 重查一批量只要 ~10ms，宁可清掉。
    UNIT_STATE_CACHE.clear()
    UNIT_PATH_CACHE.clear()
    UNIT_PROBED.clear()
    return gone


# 写操作 → 要作废哪些只读缓存。
# 以前是"任何动作成功后清空全部"：点一下「取消自启」，docker / 安全 / 网络那些
# 毫不相干的观测也一起被打掉，下一个 tick 全部重查 —— 2 vCPU 上就是白烧一秒多。
# 现在按下表精确作废；**没登记的动作一律全清**（宁可多清，不可留陈旧值）。
# 注意字典顺序即匹配顺序：更具体的前缀写在前面（docker.autostart vs docker.*）。
CACHE_KEYS_FOR_ACTION = {
    "docker.autostart": ("HostProbe.autostart",),
    "docker.prune_images": ("Docker.", "HostProbe.cleanup"),
    "docker.": ("Docker.", "HostProbe.cleanup", "HostProbe.netinfo", "HostProbe.autostart"),
    "service.": ("Services.", "HostProbe.autostart"),
    "systemd.": ("Services.", "HostProbe.autostart"),
    "boot.": ("HostProbe.autostart", "Services."),
    "process.": ("Ports.",),
    "ufw.": ("Security.",),
    "fail2ban.": ("Security.",),
    "clean.": ("HostProbe.cleanup",),
    "journal.": ("HostProbe.cleanup",),
    "apt.": ("HostProbe.cleanup",),
    # 下面这些要么只动面板自己的文件、要么页面随后就断了，没有只读观测需要作废
    "backup.": (),
    "time.": (),
    "panel.": (),
    "system.": (),
    "account.": (),
}


def _cache_prefixes_for(action):
    """这个动作影响到哪些只读缓存？没登记就返回 None（调用方按全清处理）。"""
    for prefix, keys in CACHE_KEYS_FOR_ACTION.items():
        if action.startswith(prefix):
            return keys
    return None


def cached(ttl, key=None):
    """把一个只读方法的结果缓存 ttl 秒。

    要求被装饰的方法：只读、幂等、**参数可枚举**。
    * 无参方法：不传 key，整份结果一个缓存槽。
    * 带参方法：必须传 key=lambda self, *a, **kw: "..."，把参数折进缓存键。
      参数空间不可控的方法（比如按关键字搜日志）**不要**用它 —— 会撑爆缓存。

    返回的是拷贝：调用方随便改，不会污染缓存里那份。
    """
    def deco(fn):
        name = getattr(fn, "__qualname__", fn.__name__)

        def slot(k):
            with _CACHE_LOCK:
                _CACHE.setdefault(k, (0.0, None))

        if key is None:
            slot(name)

        def wrapper(self, *args, **kwargs):
            # 带参数时把**方法名也并入缓存键**：key_fn 通常返回一个纯参数元组
            # （例如 (0, 0, 10)），若两个不同类的方法参数形式相同，会命中同一缓存槽
            # 并相互读到对方的结果 —— 这类问题比「参数被忽略」更难定位。
            k = (name, key(self, *args, **kwargs)) if key else name
            if key is not None:
                slot(k)
            if getattr(_CACHE_LOCAL, "fresh", False):   # ?fresh=1：这一趟必须取真值
                val = fn(self, *args, **kwargs)
                with _CACHE_LOCK:
                    _CACHE[k] = (time.time(), copy.deepcopy(val))
                _CACHE_LOCAL.last = (False, ttl, time.time())
                return val
            now = time.time()
            with _CACHE_LOCK:
                ts, val = _CACHE.get(k, (0.0, None))
                hit = val is not None and (now - ts) < ttl
            if hit:
                _CACHE_LOCAL.last = (True, ttl, ts)
                return copy.deepcopy(val)
            val = fn(self, *args, **kwargs)
            with _CACHE_LOCK:
                _CACHE[k] = (time.time(), copy.deepcopy(val))
            _CACHE_LOCAL.last = (False, ttl, time.time())
            return val

        wrapper.__name__ = getattr(fn, "__name__", "cached")
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return deco


def _cache_methods(cls, mapping):
    """类定义完再打补丁：免得到处改类体，也让"哪些被缓存了"一眼看得全。

    mapping 的值：ttl 秒数，或 (ttl, key_fn) —— 带参数的方法必须走后者。
    """
    for meth, spec in mapping.items():
        ttl, key = spec if isinstance(spec, tuple) else (spec, None)
        fn = getattr(cls, meth)
        setattr(cls, meth, cached(ttl, key)(fn))


def _trim_services(res, q):
    """服务列表按 `?limit=` 裁剪（P9：真机上 210 个单元 ≈ 19.5 KB）。

    默认**不裁** —— 界面的搜索框要在全部单元里找，悄悄截断等于骗人；
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
