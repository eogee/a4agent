"""循环任务的触发计划：解析、下次触发时间计算、计划描述。

四种计划（`schedule_type` + `schedule_config` JSON）：

  once     {"run_at": "2026-10-26 09:00"}              一次性
  interval {"minutes": 30}                             每 N 分钟（自上次触发/创建起算）
  daily    {"time": "09:00"}                           每天固定时刻
  weekly   {"weekdays": [1, 3, 5], "time": "09:00"}    每周选定星期（1=周一 … 7=周日）

全部按本地时间（与项目其它模块一致，用 naive datetime）。这里只做纯计算，
不碰数据库不碰线程，便于把跨天/跨周/停机补算的边界用单测钉死。
"""
import json
from datetime import datetime, time, timedelta

SCHEDULE_TYPES = ("once", "interval", "daily", "weekly")
WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")

INTERVAL_MAX_MINUTES = 7 * 24 * 60  # 间隔上限一周，再长就该用 daily/weekly
_DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def parse_time(text) -> time:
    """"09:30" → time；格式或范围不对抛 ValueError（消息直接给用户看）。"""
    raw = str(text or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError("时刻格式应为 HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("时刻格式应为 HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时刻超出范围（00:00–23:59）")
    return time(hour, minute)


def parse_datetime(text) -> datetime:
    """"2026-10-26 09:00" / ISO 形态 → datetime。"""
    raw = str(text or "").strip().replace("/", "-")
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("时间格式应为 YYYY-MM-DD HH:MM")


def parse_config(schedule_type: str, raw) -> dict:
    """校验并规范化触发计划，返回可直接入库的 dict。

    非法输入抛 ValueError（中文消息，API 层原样回给前端弹窗）。
    """
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError(f"不支持的触发类型：{schedule_type or '（空）'}")
    if isinstance(raw, dict):
        cfg = raw
    else:
        try:
            cfg = json.loads(raw or "{}")
        except ValueError:
            raise ValueError("触发计划格式错误") from None
    if not isinstance(cfg, dict):
        raise ValueError("触发计划格式错误")

    if schedule_type == "once":
        return {"run_at": parse_datetime(cfg.get("run_at")).strftime("%Y-%m-%d %H:%M")}

    if schedule_type == "interval":
        try:
            minutes = int(cfg.get("minutes"))
        except (TypeError, ValueError):
            raise ValueError("间隔分钟数必须是整数") from None
        if not 1 <= minutes <= INTERVAL_MAX_MINUTES:
            raise ValueError(
                f"间隔需在 1 分钟到 {INTERVAL_MAX_MINUTES // 1440} 天之间")
        return {"minutes": minutes}

    if schedule_type == "daily":
        return {"time": parse_time(cfg.get("time")).strftime("%H:%M")}

    days: list[int] = []
    for value in cfg.get("weekdays") or []:
        try:
            day = int(value)
        except (TypeError, ValueError):
            raise ValueError("星期取值应为 1–7") from None
        if not 1 <= day <= 7:
            raise ValueError("星期取值应为 1–7")
        if day not in days:
            days.append(day)
    if not days:
        raise ValueError("每周计划至少要选一天")
    return {"weekdays": sorted(days), "time": parse_time(cfg.get("time")).strftime("%H:%M")}


def next_run(schedule_type: str, config: dict, after: datetime,
             last_run_at: datetime | None = None,
             created_at: datetime | None = None) -> datetime | None:
    """`after` 之后的下一次触发时间；一次性任务返回它自己的时刻（可能已过去）。

    interval 的基准取「上次触发」优先、其次「创建时间」——应用停机很久后
    重新计算时不会按错过次数叠加，只从基准往后推一次（FR-2.2 的语义）。
    """
    if schedule_type == "once":
        return parse_datetime(config.get("run_at"))
    if schedule_type == "interval":
        minutes = int(config["minutes"])
        base = last_run_at or created_at or after
        nxt = base + timedelta(minutes=minutes)
        if nxt <= after:  # 基准太久远（改了间隔/停机过久）→ 从当前时刻重新起算
            nxt = after + timedelta(minutes=minutes)
        return nxt
    if schedule_type == "daily":
        moment = parse_time(config.get("time"))
        candidate = datetime.combine(after.date(), moment)
        if candidate <= after:
            candidate = datetime.combine(after.date() + timedelta(days=1), moment)
        return candidate
    # weekly
    moment = parse_time(config.get("time"))
    wanted = {int(d) for d in config.get("weekdays") or []}
    for offset in range(8):  # 今天到下周同日，最多 7 天必命中
        day = after.date() + timedelta(days=offset)
        if day.isoweekday() in wanted:
            candidate = datetime.combine(day, moment)
            if candidate > after:
                return candidate
    return None


def describe(schedule_type: str, config: dict) -> str:
    """卡片上的计划描述（如「每天 09:00」「每 30 分钟」「每周一、三 09:00」）。"""
    if schedule_type == "once":
        return f"{config.get('run_at')} 执行一次"
    if schedule_type == "interval":
        minutes = int(config.get("minutes") or 0)
        if minutes and minutes % 1440 == 0:
            return f"每 {minutes // 1440} 天"
        if minutes and minutes % 60 == 0:
            return f"每 {minutes // 60} 小时"
        return f"每 {minutes} 分钟"
    if schedule_type == "daily":
        return f"每天 {config.get('time')}"
    names = "、".join(WEEKDAY_NAMES[int(d) - 1] for d in config.get("weekdays") or []
                     if 1 <= int(d) <= 7)
    return f"每周{names} {config.get('time')}"
