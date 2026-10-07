import math
from collections.abc import Callable
from datetime import datetime, time

from pydantic_ai.usage import RunUsage

from kanade_bot.utils.common import asia_shanghai_now
from kanade_bot.utils.schema import AttrDocModel

try:
    from chinese_calendar import is_holiday as _is_holiday
except ImportError:  # pragma: no cover - 依赖缺失时退化为仅按周末判定
    _is_holiday = None


PEAK_TIME_RANGES: tuple[tuple[time, time], ...] = ((time(9), time(12)), (time(14), time(18)))
"""高峰时段（左闭右开）：9:00-12:00、14:00-18:00（北京时间）"""

type UsageCallback = Callable[[RunUsage, bool], None]
"""轮次结束回调：`(累计usage, 是否有文本产出)`，供按Token计费的调用方扣费"""


def is_peak_hours(at: datetime | None = None) -> bool:
    """判断北京时间 `at` 是否处于高峰时段

    规则：周一至周五（不含法定节假日）的 9:00-12:00、14:00-18:00 为高峰；
    周末（含调休上班的周六日）与法定节假日全天均为空闲时段。

    :param at: 上海时区的aware时间或视为上海时间的naive时间；`None` 取当前时间
    """
    if at is None:
        at = asia_shanghai_now()

    if at.weekday() >= 5:
        return False

    if _is_holiday is not None:
        try:
            if _is_holiday(at.date()):
                return False
        except NotImplementedError:
            # 日期超出 chinese-calendar 支持的年份范围：退化为仅按周末判定
            pass

    current = at.time()
    return any(start <= current < end for start, end in PEAK_TIME_RANGES)


class TokenBillingConfig(AttrDocModel):
    """按Token计费配置（单位：水晶/千token）"""

    peak_input_per_1k: float = 12
    """高峰时段 缓存未命中输入"""

    peak_output_per_1k: float = 48
    """高峰时段 输出"""

    off_peak_input_per_1k: float = 6
    """空闲时段 缓存未命中输入"""

    off_peak_output_per_1k: float = 24
    """空闲时段 输出"""

    min_cost: int = 1
    """单轮最低消耗水晶数"""


def compute_token_cost(
    usage: RunUsage,
    config: TokenBillingConfig,
    *,
    peak: bool | None = None,
) -> int:
    """按一轮补全的usage计算水晶消耗，向上取整

    输入按缓存未命中部分计费，缓存命中不收费；输出为全部输出token（含reasoning）。

    :param usage: 本轮所有补全请求累计的usage
    :param config: 费率配置
    :param peak: 是否按高峰费率计费；`None` 时按当前时间判定
    :returns: 向上取整后的水晶消耗，不低于 `min_cost`
    """
    if peak is None:
        peak = is_peak_hours()

    input_per_1k = config.peak_input_per_1k if peak else config.off_peak_input_per_1k
    output_per_1k = config.peak_output_per_1k if peak else config.off_peak_output_per_1k

    billed_input = max(0, usage.input_tokens - usage.cache_read_tokens)
    # round 先截断浮点噪声，防止恰好整数值因1e-15误差被ceil多进一位
    cost = math.ceil(
        round(billed_input / 1000 * input_per_1k + usage.output_tokens / 1000 * output_per_1k, 9)
    )
    return max(cost, config.min_cost)
