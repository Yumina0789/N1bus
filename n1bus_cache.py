"""n1bus 模块级只读缓存：systemd 单元属性。

这里只放**没有 TTL、只在写操作后整批作废**的进程内缓存：
FragmentPath（判断「发行版自带」还是「用户 / 第三方装的」）与 UnitFileState
（enabled / disabled / static …）。逐个 `systemctl show` 约 8 ms，210 个约 1.5 s；
这份缓存让每个进程只付一次这个代价。

放在独立文件里是为了断开循环依赖：作废动作在 HTTP 层（n1bus_web），
而这些表由 system 域（n1bus_system）填充，两边都不该互相 import。

2026-10 删除了原先的 **TTL 数据缓存**（`cached()` / `_cache_methods()` / `_CACHE` /
`cache_meta()` / `clear_data_cache()` / `CACHE_KEYS_FOR_ACTION`）：它们在 `bc2f9ab`
把单文件拆成 12 个模块时**整段漏搬了注册**，全仓库零调用、`_CACHE` 恒为空，
`?fresh=1` 与响应里的 `_cache` 元数据因此一直是空转。经确认「不要缓存了」，一并删除，
连同前端的 `forceFresh()` 计数器。
"""

UNIT_PATH_CACHE = {}
UNIT_STATE_CACHE = {}
UNIT_PROBED = set()          # 已向 systemctl 查询过的单元（查询失败也计入，避免重复查询）


def clear_unit_cache():
    """作废单元属性缓存。

    这三张表**没有过期时间**（模块级、进程内），而任何写操作都可能改 UnitFileState /
    ActiveState。留下旧值就会出现：systemctl 实际已 disabled，面板仍显示 enabled ——
    表现为「点击开机自启无反应」（该故障出现过）。重新查询一批单元仅约 10 ms，所以直接清空。
    """
    UNIT_STATE_CACHE.clear()
    UNIT_PATH_CACHE.clear()
    UNIT_PROBED.clear()
