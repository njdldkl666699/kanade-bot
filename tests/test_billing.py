"""utils/billing 峰谷判定与Token计费计算的单元测试"""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic_ai.usage import RunUsage

from kanade_bot.utils.billing import (
    TokenBillingConfig,
    compute_token_cost,
    has_billable_usage,
    is_peak_hours,
)

CN = ZoneInfo("Asia/Shanghai")


def dt(*args: int) -> datetime:
    """构造上海时区的时间"""
    return datetime(*args, tzinfo=CN)


# 2026-10-12 为普通周一（非节假日）；2026-10-10 周六；2026-10-01 国庆（周四）
MONDAY = dt(2026, 10, 12, 10, 0)
SATURDAY = dt(2026, 10, 10, 10, 0)
NATIONAL_DAY = dt(2026, 10, 1, 10, 0)


class TestIsPeakHours:
    def test_weekday_morning_peak(self):
        assert is_peak_hours(MONDAY) is True

    def test_lunch_break_is_off_peak(self):
        assert is_peak_hours(dt(2026, 10, 12, 13, 0)) is False

    def test_boundary_start_inclusive(self):
        assert is_peak_hours(dt(2026, 10, 12, 9, 0)) is True

    def test_boundary_end_exclusive(self):
        assert is_peak_hours(dt(2026, 10, 12, 12, 0)) is False

    def test_afternoon_peak(self):
        assert is_peak_hours(dt(2026, 10, 12, 17, 59)) is True

    def test_weekend_is_off_peak(self):
        assert is_peak_hours(SATURDAY) is False

    def test_holiday_weekday_is_off_peak(self):
        assert is_peak_hours(NATIONAL_DAY) is False


class TestHasBillableUsage:
    """是否存在可计费内容：全轮被豁免抵扣为空时免单的判定"""

    def test_default_empty(self):
        assert has_billable_usage(RunUsage()) is False

    def test_zeroed_after_waiver(self):
        # 首请求免费全额抵扣后的形态：input/output 均为 0
        assert has_billable_usage(RunUsage(input_tokens=0, output_tokens=0)) is False

    def test_input_only(self):
        assert has_billable_usage(RunUsage(input_tokens=10)) is True

    def test_output_only(self):
        assert has_billable_usage(RunUsage(output_tokens=1)) is True

    def test_fully_cached_input_still_billable(self):
        # 全缓存命中的输入仍视为有可计费内容（维持单轮最低消耗语义）
        assert has_billable_usage(RunUsage(input_tokens=100, cache_read_tokens=100)) is True


class TestComputeTokenCost:
    config = TokenBillingConfig()

    def test_average_turn_peak(self):
        # 一问一答均值口径：输入481（未命中）、输出321 → 高峰约21.2，向上取整22
        usage = RunUsage(input_tokens=481, cache_read_tokens=0, output_tokens=321)
        assert compute_token_cost(usage, self.config, peak=True) == 22

    def test_average_turn_off_peak(self):
        usage = RunUsage(input_tokens=481, cache_read_tokens=0, output_tokens=321)
        assert compute_token_cost(usage, self.config, peak=False) == 11

    def test_cache_read_tokens_not_billed(self):
        # 输入1000其中800命中缓存：仅200计费 → ceil(0.2*12)=3
        usage = RunUsage(input_tokens=1000, cache_read_tokens=800, output_tokens=0)
        assert compute_token_cost(usage, self.config, peak=True) == 3

    def test_fully_cached_input_min_cost(self):
        usage = RunUsage(input_tokens=5000, cache_read_tokens=5000, output_tokens=0)
        assert compute_token_cost(usage, self.config, peak=True) == 1

    def test_min_cost_override(self):
        config = TokenBillingConfig(min_cost=2)
        usage = RunUsage(input_tokens=5000, cache_read_tokens=5000, output_tokens=0)
        assert compute_token_cost(usage, config, peak=False) == 2

    def test_exact_integer_not_over_ceiled(self):
        # 1000未命中输入、高峰12水晶/千 → 恰为12，浮点噪声不得使其进位为13
        usage = RunUsage(input_tokens=1000, cache_read_tokens=0, output_tokens=0)
        assert compute_token_cost(usage, self.config, peak=True) == 12

    def test_output_only(self):
        # 仅输出1000 token，空闲24水晶/千
        usage = RunUsage(input_tokens=0, cache_read_tokens=0, output_tokens=1000)
        assert compute_token_cost(usage, self.config, peak=False) == 24
