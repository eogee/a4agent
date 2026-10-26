"""循环任务触发计划的纯计算测试：解析校验、下次触发时间、计划描述。"""
from datetime import datetime

import pytest

from backend.app import loop_schedule as ls


# ---------------- 解析与校验 ----------------

def test_parse_time():
    assert ls.parse_time("09:05").strftime("%H:%M") == "09:05"
    assert ls.parse_time(" 23:59 ") == ls.parse_time("23:59")
    for bad in ("9", "24:00", "09:60", "ab:cd", ""):
        with pytest.raises(ValueError):
            ls.parse_time(bad)


def test_parse_config_once_interval_daily():
    assert ls.parse_config("once", {"run_at": "2026-10-26 09:00"}) == {
        "run_at": "2026-10-26 09:00"}
    # 斜杠与 ISO 分隔符都吃，入库统一成 YYYY-MM-DD HH:MM
    assert ls.parse_config("once", {"run_at": "2026/10/26T09:00"}) == {
        "run_at": "2026-10-26 09:00"}
    assert ls.parse_config("interval", {"minutes": "30"}) == {"minutes": 30}
    assert ls.parse_config("daily", {"time": "7:05"}) == {"time": "07:05"}


def test_parse_config_weekly_sorts_and_dedupes():
    assert ls.parse_config("weekly", {"weekdays": [5, 1, 5], "time": "09:00"}) == {
        "weekdays": [1, 5], "time": "09:00"}


def test_parse_config_rejects_bad_input():
    cases = [
        ("hourly", {"minutes": 5}),          # 未支持的类型
        ("interval", {"minutes": 0}),         # 下界
        ("interval", {"minutes": 7 * 24 * 60 + 1}),  # 上界
        ("interval", {"minutes": "abc"}),
        ("daily", {"time": "9"}),
        ("weekly", {"weekdays": [], "time": "09:00"}),
        ("weekly", {"weekdays": [8], "time": "09:00"}),
        ("once", {"run_at": "2026-13-40 09:00"}),
        ("daily", "not-json"),
    ]
    for schedule_type, raw in cases:
        with pytest.raises(ValueError):
            ls.parse_config(schedule_type, raw)


def test_parse_config_accepts_json_string():
    """库里存的是 JSON 字符串，编辑器回传的也是字符串。"""
    assert ls.parse_config("interval", '{"minutes": 15}') == {"minutes": 15}


# ---------------- 下次触发时间 ----------------

def test_next_run_interval_counts_from_last_run():
    now = datetime(2026, 10, 25, 12, 0)
    last = datetime(2026, 10, 25, 11, 50)
    assert ls.next_run("interval", {"minutes": 30}, now, last_run_at=last) == \
        datetime(2026, 10, 25, 12, 20)


def test_next_run_interval_uses_created_at_when_never_run():
    created = datetime(2026, 10, 25, 12, 0)
    assert ls.next_run("interval", {"minutes": 10}, created, created_at=created) == \
        datetime(2026, 10, 25, 12, 10)


def test_next_run_interval_does_not_stack_after_downtime():
    """停机 3 小时后重启：从当前时刻重新起算，不按错过次数连补四下。"""
    now = datetime(2026, 10, 25, 12, 0)
    stale = datetime(2026, 10, 25, 9, 0)
    assert ls.next_run("interval", {"minutes": 30}, now, last_run_at=stale) == \
        datetime(2026, 10, 25, 12, 30)


def test_next_run_daily_rolls_to_tomorrow():
    cfg = {"time": "09:00"}
    assert ls.next_run("daily", cfg, datetime(2026, 10, 25, 8, 0)) == \
        datetime(2026, 10, 25, 9, 0)
    # 恰好等于触发时刻也算已过，顺延到明天
    assert ls.next_run("daily", cfg, datetime(2026, 10, 25, 9, 0)) == \
        datetime(2026, 10, 26, 9, 0)
    # 跨月
    assert ls.next_run("daily", cfg, datetime(2026, 10, 31, 10, 0)) == \
        datetime(2026, 11, 1, 9, 0)


def test_next_run_weekly_picks_next_matching_day():
    cfg = {"weekdays": [1, 5], "time": "09:00"}  # 周一、周五
    # 2026-10-25 是周日 → 下一个是周一（10-26）
    assert ls.next_run("weekly", cfg, datetime(2026, 10, 25, 12, 0)) == \
        datetime(2026, 10, 26, 9, 0)
    # 当天（周五 10-30）但时刻已过 → 顺延到下周一 11-02
    assert ls.next_run("weekly", cfg, datetime(2026, 10, 30, 10, 0)) == \
        datetime(2026, 11, 2, 9, 0)


def test_next_run_once_returns_its_own_moment():
    cfg = {"run_at": "2026-10-26 09:00"}
    after = datetime(2026, 10, 25, 12, 0)
    assert ls.next_run("once", cfg, after) == datetime(2026, 10, 26, 9, 0)


# ---------------- 计划描述 ----------------

def test_describe_all_types():
    assert ls.describe("once", {"run_at": "2026-10-26 09:00"}) == "2026-10-26 09:00 执行一次"
    assert ls.describe("interval", {"minutes": 30}) == "每 30 分钟"
    assert ls.describe("interval", {"minutes": 60}) == "每 1 小时"
    assert ls.describe("interval", {"minutes": 1440}) == "每 1 天"
    assert ls.describe("daily", {"time": "09:00"}) == "每天 09:00"
    assert ls.describe("weekly", {"weekdays": [1, 3], "time": "09:00"}) == "每周一、三 09:00"
