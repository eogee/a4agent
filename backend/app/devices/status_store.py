"""A 侧状态存储：sqlite 表 device_status，按 (device, session_id) 唯一。"""
import time
from datetime import datetime

from ..database import SessionLocal
from ..models import DeviceStatus
from . import status_schema


def _apply(row: DeviceStatus, payload: dict) -> None:
    row.seq = payload["seq"]
    row.event = payload["event"]
    row.state = payload["state"]
    row.agent = payload["agent"]
    row.workspace = payload["workspace"]
    row.summary = payload["summary"]
    row.ts = payload["ts"]
    row.updated_at = datetime.now()


def upsert(device: str, payload: dict, db=None):
    """写入或更新一条状态，返回 (changed, should_notify)。

    - 同一 (device, session_id) 只保留 seq 最大的一条，旧值直接丢弃；
    - should_notify 只在关键事件且序号超过 notified_seq 时为 True。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        row = (db.query(DeviceStatus)
               .filter(DeviceStatus.device == device,
                       DeviceStatus.session_id == payload["session_id"])
               .one_or_none())
        if row is None:
            row = DeviceStatus(device=device,
                               session_id=payload["session_id"],
                               notified_seq=0)
            _apply(row, payload)
            db.add(row)
            notify = payload["event"] in status_schema.KEY_EVENTS
        else:
            if payload["seq"] <= (row.seq or 0):
                return False, False
            notify = (payload["event"] in status_schema.KEY_EVENTS
                      and payload["seq"] > (row.notified_seq or 0))
            _apply(row, payload)
        db.commit()
        return True, notify
    except Exception:
        db.rollback()
        raise
    finally:
        if own:
            db.close()


def mark_notified(device: str, session_id: str, seq: int, db=None) -> None:
    """记录已发过人读提醒的序号；提醒失败也调用，避免每拍重复打扰。"""
    own = db is None
    db = db or SessionLocal()
    try:
        row = (db.query(DeviceStatus)
               .filter(DeviceStatus.device == device,
                       DeviceStatus.session_id == session_id)
               .one_or_none())
        if row is not None and seq > (row.notified_seq or 0):
            row.notified_seq = seq
            db.commit()
    finally:
        if own:
            db.close()


def list_sessions(device: str, limit: int = 50, db=None) -> list:
    """返回每个会话的最新一条，按时间倒序；附带展示文案与陈旧标记。"""
    own = db is None
    db = db or SessionLocal()
    try:
        rows = (db.query(DeviceStatus)
                .filter(DeviceStatus.device == device)
                .order_by(DeviceStatus.ts.desc())
                .limit(status_schema.MAX_ROWS_PER_DEVICE)
                .all())
        now = time.time()
        out = []
        seen = set()
        for row in rows:
            if row.session_id in seen:
                continue
            seen.add(row.session_id)
            ts = float(row.ts or 0)
            item = {
                "session_id": row.session_id,
                "event": row.event,
                "state": row.state,
                "agent": row.agent,
                "workspace": row.workspace,
                "summary": row.summary,
                "ts": ts,
                "age_seconds": max(0, int(now - ts)),
                "stale": (now - ts) > status_schema.STALE_AFTER_SECONDS,
            }
            item.update(status_schema.labels(item))
            out.append(item)
            if len(out) >= limit:
                break
        return out
    finally:
        if own:
            db.close()


def cleanup(now=None, db=None) -> None:
    """按 7 天 + 每设备 1000 条清理；先到先清。"""
    own = db is None
    db = db or SessionLocal()
    try:
        now = time.time() if now is None else now
        cutoff = now - status_schema.RETENTION_DAYS * 86400
        db.query(DeviceStatus).filter(
            DeviceStatus.ts < cutoff).delete(synchronize_session=False)
        devices = [row[0] for row in
                   db.query(DeviceStatus.device).distinct().all()]
        for device in devices:
            ids = [row[0] for row in
                   (db.query(DeviceStatus.id)
                    .filter(DeviceStatus.device == device)
                    .order_by(DeviceStatus.ts.desc())
                    .offset(status_schema.MAX_ROWS_PER_DEVICE)
                    .all())]
            if ids:
                db.query(DeviceStatus).filter(
                    DeviceStatus.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if own:
            db.close()
