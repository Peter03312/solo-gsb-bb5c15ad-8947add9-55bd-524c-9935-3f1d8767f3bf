"""运输测温证据复核（未核定样本）。

规则（与 README 一致）：
  * 每份未核定样本一次性设定允许摄氏温度闭区间与正数最大测温间隔（秒），设定后不可改：
    同内容重试幂等（200）、异内容冲突（409 temp_config_conflict）；核定入库后拒绝新配置；
  * 读数按全局唯一读数号记录设备号、发生时间与有限数值：采集前（422）、重复发生时刻
    （409）或同号异内容（409 reading_conflict）拒绝；同号同内容重试返回原记录（200）；
    核定后拒绝新读数；
  * 查询按发生时间列出超温点（读数落在允许闭区间之外）与相邻读数间超过最大测温间隔的
    缺测区间；跨移交/签收时刻的缺测段按当前有效链的移交、签收事件发生时刻拆分，
    并在**有效入库实际改变保管归属**的发生时刻拆分（无效入库不转移责任、不拆分；
    分界与读数时刻重合时不产生零时长段）；
  * 每段依当前有效保管链标明当时保管人（custodian）或在途移交（in_transit）；
    保管链断链（移交人非当前保管人、在途期间再次移交、签收无对应在途移交）导致归属
    不能确定时，自该事件发生时刻起标为待核（pending_review）且其后不再恢复；
    重复签收、签收要素不符（在途保持）、入库异常与封签检查不改变保管归属，不触发待核；
  * 乱序补录与移交/签收/入库纠错后，查询即时按当前有效链重算归属；原始读数与旧纠错
    记录不改；
  * 测温配置与读数不参与保管链核验结论、不产生保管链版本（既有结论与版本规则保持兼容）。
"""
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .db import utcnow_iso
from .schemas import TempConfigIn, TempReadingIn
from .service import (
    DomainError,
    LAB_CUSTODIAN,
    _fingerprint,
    ensure_unlocked,
    event_time_key,
    get_sample_or_404,
    parse_iso,
    to_utc_iso,
)


# --------------------------------------------------------------------------- 通用工具

def _parse_iso(s: str) -> datetime:
    return parse_iso(s)


def _seconds_number(start: str, end: str) -> int | float:
    """相邻瞬间的秒数：整秒差返回 int（既有输出形态），含小数秒时返回精确 float。"""
    secs = (_parse_iso(end) - _parse_iso(start)).total_seconds()
    return int(secs) if float(secs).is_integer() else secs


def _get_config_row(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sample_temp_config WHERE sample_id = ?", (sample_id,)
    ).fetchone()


# --------------------------------------------------------------------------- 测温配置（一次性设定）

def _config_fingerprint(data: TempConfigIn) -> str:
    return _fingerprint({
        "sample_code": data.sample_code,
        "min_celsius": data.min_celsius,
        "max_celsius": data.max_celsius,
        "max_interval_seconds": data.max_interval_seconds,
    })


def _config_view(row: sqlite3.Row, sample_code: str) -> dict[str, Any]:
    return {
        "sample_code": sample_code,
        "min_celsius": row["min_celsius"],
        "max_celsius": row["max_celsius"],
        "max_interval_seconds": row["max_interval_seconds"],
        "configured_at": row["created_at"],
    }


def set_temp_config(conn: sqlite3.Connection, data: TempConfigIn) -> tuple[dict[str, Any], bool]:
    """一次性设定测温配置。返回 (配置视图, 是否幂等重试)。

    每份样本至多一条配置：同内容重试幂等（200，核定后亦保持），异内容冲突（409）；
    新配置仅未核定样本可设（核定后 409 history_locked）。
    """
    sample = get_sample_or_404(conn, data.sample_code)
    h = _config_fingerprint(data)

    # 幂等查重优先于冻结检查（与既有事件接口语义一致）
    existing = _get_config_row(conn, sample["id"])
    if existing is not None:
        if existing["request_hash"] != h:
            raise DomainError(
                409,
                "temp_config_conflict",
                f"样本 {data.sample_code} 已设定测温配置且内容不一致"
                "（一次性设定，不可更改；同内容重试幂等）",
                {"sample_code": data.sample_code,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": h},
            )
        return _config_view(existing, sample["sample_code"]), True

    ensure_unlocked(sample)
    cur = conn.execute(
        "INSERT INTO sample_temp_config"
        " (sample_id, min_celsius, max_celsius, max_interval_seconds, request_hash, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (sample["id"], data.min_celsius, data.max_celsius,
         data.max_interval_seconds, h, utcnow_iso()),
    )
    row = conn.execute("SELECT * FROM sample_temp_config WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _config_view(row, sample["sample_code"]), False


# --------------------------------------------------------------------------- 测温读数

def _reading_fingerprint(data: TempReadingIn, occurred: str) -> str:
    return _fingerprint({
        "sample_code": data.sample_code,
        "reading_id": data.reading_id,
        "device_id": data.device_id,
        "occurred_at": occurred,
        "value_celsius": data.value_celsius,
    })


def _reading_view(row: sqlite3.Row, sample_code: str) -> dict[str, Any]:
    return {
        "reading_id": row["reading_id"],
        "sample_code": sample_code,
        "device_id": row["device_id"],
        "occurred_at": row["occurred_at"],
        "value_celsius": row["value_celsius"],
        "recorded_at": row["created_at"],
    }


def create_temp_reading(conn: sqlite3.Connection, data: TempReadingIn) -> tuple[dict[str, Any], bool]:
    """记录一条测温读数。返回 (读数视图, 是否幂等重试)。

    同读数号同内容重试返回原记录（200，核定后亦保持）；同号异内容 409；
    采集前（422）、同一样本重复发生时刻（409）、未设定测温配置（409）、
    样本已核定（409 history_locked）均拒绝且不写入。
    """
    sample = get_sample_or_404(conn, data.sample_code)
    occurred = to_utc_iso(data.occurred_at)
    h = _reading_fingerprint(data, occurred)

    # 幂等查重优先于冻结检查（与既有事件接口语义一致）：同号同内容重试返回原记录
    existing = conn.execute(
        "SELECT * FROM temp_reading WHERE reading_id = ?", (data.reading_id,)
    ).fetchone()
    if existing is not None:
        if existing["content_hash"] != h:
            raise DomainError(
                409,
                "reading_conflict",
                f"读数号 {data.reading_id} 已存在但提交内容不一致（同读数号异内容冲突）",
                {"reading_id": data.reading_id,
                 "existing": {"occurred_at": existing["occurred_at"],
                              "device_id": existing["device_id"],
                              "value_celsius": existing["value_celsius"],
                              "content_hash": existing["content_hash"]},
                 "incoming": {"occurred_at": occurred, "device_id": data.device_id,
                              "value_celsius": data.value_celsius, "content_hash": h}},
            )
        orig_sample = conn.execute(
            "SELECT sample_code FROM sample WHERE id = ?", (existing["sample_id"],)
        ).fetchone()
        return _reading_view(existing, orig_sample["sample_code"]), True

    ensure_unlocked(sample)

    if _get_config_row(conn, sample["id"]) is None:
        raise DomainError(
            409,
            "temp_config_missing",
            f"样本 {data.sample_code} 尚未设定测温配置，须先一次性设定允许温度区间与最大测温间隔",
            {"sample_code": data.sample_code},
        )
    if parse_iso(occurred) < parse_iso(sample["collected_at"]):
        raise DomainError(
            422,
            "reading_before_collection",
            f"读数发生时间 {occurred} 早于样本采集时间 {sample['collected_at']}，拒绝记录",
            {"reading_id": data.reading_id, "occurred_at": occurred,
             "collected_at": sample["collected_at"]},
        )
    dup = conn.execute(
        "SELECT reading_id FROM temp_reading WHERE sample_id = ? AND occurred_at = ?",
        (sample["id"], occurred),
    ).fetchone()
    if dup is not None:
        raise DomainError(
            409,
            "reading_time_duplicate",
            f"样本 {data.sample_code} 在时刻 {occurred} 已存在读数 {dup['reading_id']}，"
            "同一样本同一发生时刻只允许一条读数",
            {"reading_id": data.reading_id, "occurred_at": occurred,
             "existing_reading_id": dup["reading_id"]},
        )

    try:
        cur = conn.execute(
            "INSERT INTO temp_reading"
            " (sample_id, reading_id, device_id, occurred_at, value_celsius, content_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sample["id"], data.reading_id, data.device_id, occurred,
             data.value_celsius, h, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        # 并发下唯一键竞争（读数号或发生时刻被抢先占用）：按已提交结果判定
        raced = conn.execute(
            "SELECT * FROM temp_reading WHERE reading_id = ?", (data.reading_id,)
        ).fetchone()
        if raced is not None:
            if raced["content_hash"] == h:
                orig_sample = conn.execute(
                    "SELECT sample_code FROM sample WHERE id = ?", (raced["sample_id"],)
                ).fetchone()
                return _reading_view(raced, orig_sample["sample_code"]), True
            raise DomainError(
                409, "reading_conflict",
                f"读数号 {data.reading_id} 并发写入且内容冲突",
            )
        raise DomainError(
            409, "reading_time_duplicate",
            f"样本 {data.sample_code} 在时刻 {occurred} 的读数被并发提交占用",
            {"reading_id": data.reading_id, "occurred_at": occurred},
        )

    row = conn.execute("SELECT * FROM temp_reading WHERE id = ?", (cur.lastrowid,)).fetchone()
    # 新读数真正落库 → 同事务对账该样本的有效处置意见（可拆分缺测区间/改变超温点；
    # 幂等重试与冲突失败在上方已返回或抛错，不触达此处）
    from .dispositions import track_sample_evidence_committed

    track_sample_evidence_committed(conn, sample)
    return _reading_view(row, sample["sample_code"]), False


# --------------------------------------------------------------------------- 保管归属重放

def _custody_replay(
    sample: sqlite3.Row, events: list[sqlite3.Row]
) -> tuple[list[dict[str, Any]], list[str]]:
    """沿当前有效链重放移交/签收/入库事件，产出归属时段与缺测区间拆分时刻。

    返回 (periods, split_times)：
      * periods：覆盖 [采集时间, +∞) 的连续归属时段，每段
        {"start": ISO, "end": ISO|None, "attribution": {...}}；attribution 形态：
          - {"kind": "custodian", "custodian": 保管人}
          - {"kind": "in_transit", "transfer_no", "from_custodian", "to_custodian",
             "transfer_event_id"}
          - {"kind": "pending_review", "detail": 断链原因}（待核：断链后归属不能确定）
      * split_times：缺测区间的拆分时刻（按真实瞬间升序、去重）——既有规则的全部
        移交/签收事件发生时刻，以及**有效入库实际改变保管归属**的发生时刻；
        无效入库（早于最终签收、缺前序移交-签收链）不转移责任，不产生拆分时刻；
        断链待核期间归属已不能确定，入库不再产生新的拆分时刻。

    断链（移交人非当前保管人 / 在途期间再次移交 / 签收无对应在途移交）自该事件发生
    时刻起生效且其后不再恢复；重复签收、签收要素不符、入库异常与封签检查不改变归属。
    """
    periods: list[dict[str, Any]] = []
    split_times: set[str] = set()
    current = sample["collector"]
    open_transfer: dict[str, Any] | None = None     # 在途移交（内部含 occurred_at/_time）
    closed_nos: set[str] = set()
    last_receive_at: datetime | None = None
    broken_detail: str | None = None
    cursor = sample["collected_at"]
    cursor_t = parse_iso(cursor)

    def attribution() -> dict[str, Any]:
        if broken_detail is not None:
            return {"kind": "pending_review", "detail": broken_detail}
        if open_transfer is not None:
            return {
                "kind": "in_transit",
                "transfer_no": open_transfer["transfer_no"],
                "from_custodian": open_transfer["from_custodian"],
                "to_custodian": open_transfer["to_custodian"],
                "transfer_event_id": open_transfer["transfer_event_id"],
            }
        return {"kind": "custodian", "custodian": current}

    for ev in events:
        et = ev["event_type"]
        if et == "seal_check":
            continue                                    # 封签检查不影响保管归属
        t = ev["occurred_at"]
        t_dt = parse_iso(t)
        p = json.loads(ev["payload_json"])
        ref = f"[事件 {ev['event_id']}]"
        if t_dt < parse_iso(sample["collected_at"]):
            continue                                    # 采集前事件无效，保管归属尚未开始
        if et in ("transfer", "receive"):
            split_times.add(t)                          # 既有规则：移交/签收时刻恒为拆分点
        # 先以事件发生前状态关闭 [cursor, t) 时段，再应用该事件
        if t_dt > cursor_t:
            periods.append({"start": cursor, "end": t, "attribution": attribution()})
            cursor, cursor_t = t, t_dt

        if et == "transfer":
            if open_transfer is not None:
                if broken_detail is None:
                    broken_detail = (
                        f"{ref} 移交至签收期间再次移交：在途移交单 {open_transfer['transfer_no']}"
                        f"（{open_transfer['from_custodian']} → {open_transfer['to_custodian']}）尚未签收，"
                        "保管链断链，归属不能确定"
                    )
            elif p["from_custodian"] != current:
                if broken_detail is None:
                    broken_detail = (
                        f"{ref} 移交人 {p['from_custodian']} 不是当前保管人 {current}"
                        f"（移交单 {p['transfer_no']}），保管链断链，归属不能确定"
                    )
            else:
                open_transfer = {
                    "transfer_no": p["transfer_no"],
                    "from_custodian": p["from_custodian"],
                    "to_custodian": p["to_custodian"],
                    "transfer_event_id": ev["event_id"],
                    "occurred_at": t,
                    "_time": t_dt,
                }
        elif et == "receive":
            no = p["transfer_no"]
            if no in closed_nos:
                pass                                    # 重复签收：无效但不改变已闭合的保管状态
            elif open_transfer is None or open_transfer["transfer_no"] != no:
                if broken_detail is None:
                    broken_detail = (
                        f"{ref} 签收无对应在途移交（单号 {no}），保管链断链，归属不能确定"
                    )
            elif (p["receiver"] != open_transfer["to_custodian"]
                  or p["seal_no"] != sample["seal_no"]
                  or t_dt <= open_transfer["_time"]):
                pass                                    # 签收要素不符：在途保持，归属仍可确定
            else:
                closed_nos.add(no)
                current = p["receiver"]
                last_receive_at = t_dt
                open_transfer = None
        else:  # storage
            if last_receive_at is not None and t_dt < last_receive_at:
                pass                                    # 入库早于最终签收：无效，保管状态不变
            elif open_transfer is not None or last_receive_at is None:
                pass                                    # 缺前序链的入库：待补证，保管状态不变
            else:
                before = attribution()
                current = p.get("custodian") or LAB_CUSTODIAN
                if attribution() != before:
                    # 有效入库实际改变保管归属 → 该发生时刻成为缺测区间拆分点；
                    # 归属未变（断链待核期间 / 入库保管人与当前保管人同名）不拆分
                    split_times.add(t)

    periods.append({"start": cursor, "end": None, "attribution": attribution()})
    return periods, sorted(split_times, key=parse_iso)


def _custody_periods(sample: sqlite3.Row, events: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """兼容包装：仅返回归属时段（拆分时刻见 _custody_replay）。"""
    return _custody_replay(sample, events)[0]


def _attribution_at(periods: list[dict[str, Any]], t: str) -> dict[str, Any]:
    """时刻 t 的保管归属；时刻与事件对齐时按事件已发生处理。"""
    t_dt = parse_iso(t)
    for period in periods:
        if parse_iso(period["start"]) <= t_dt and (
            period["end"] is None or t_dt < parse_iso(period["end"])
        ):
            return period["attribution"]
    return {"kind": "pending_review", "detail": "时刻早于样本采集时间，归属不能确定"}


def _gap_intervals(
    readings: list[sqlite3.Row],
    max_interval: int,
    split_times: list[str],
    periods: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按相邻读数计算超间隔缺测区间，并按拆分时刻分段、逐段标明归属。

    “超过最大间隔”按真实瞬间严格判定（差值 > max_interval；等于限值不报），
    小数秒不被截断。拆分时刻（移交/签收时刻及有效入库实际改变归属的发生时刻，
    由 _custody_replay 给出）按真实瞬间选取与排序；拆分时刻与读数时刻重合时
    不产生零时长段（严格介于两读数之间的拆分点才生效）。
    """
    splits = sorted(set(split_times), key=parse_iso)
    gaps: list[dict[str, Any]] = []
    for prev, nxt in zip(readings, readings[1:]):
        delta = parse_iso(nxt["occurred_at"]) - parse_iso(prev["occurred_at"])
        if delta <= timedelta(seconds=max_interval):
            continue
        duration = _seconds_number(prev["occurred_at"], nxt["occurred_at"])
        prev_t, nxt_t = parse_iso(prev["occurred_at"]), parse_iso(nxt["occurred_at"])
        bounds = [prev["occurred_at"]]
        bounds += [t for t in splits if prev_t < parse_iso(t) < nxt_t]
        bounds.append(nxt["occurred_at"])
        segments = [
            {
                "start": a,
                "end": b,
                "duration_seconds": _seconds_number(a, b),
                "attribution": _attribution_at(periods, a),
            }
            for a, b in zip(bounds, bounds[1:])
        ]
        gaps.append({
            "start": prev["occurred_at"],
            "end": nxt["occurred_at"],
            "duration_seconds": duration,
            "max_interval_seconds": max_interval,
            "before_reading_id": prev["reading_id"],
            "after_reading_id": nxt["reading_id"],
            "segments": segments,
        })
    return gaps


# --------------------------------------------------------------------------- 测温证据复核查询

def get_temperature_review(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """按发生时间列出超温点与缺测区间（跨移交/签收/有效入库时刻拆分），并标明每段归属。

    归属按查询时的当前有效保管链即时重算：乱序补录读数、补录或作废/替换移交、
    签收、入库事件后再次查询即反映最新归属；原始读数与旧纠错记录不被改写。
    """
    sample = get_sample_or_404(conn, sample_code)
    config = _get_config_row(conn, sample["id"])
    if config is None:
        raise DomainError(
            404, "temp_config_not_found",
            f"样本 {sample_code} 尚未设定测温配置",
            {"sample_code": sample_code},
        )

    readings = conn.execute(
        "SELECT * FROM temp_reading WHERE sample_id = ?",
        (sample["id"],),
    ).fetchall()
    readings.sort(key=event_time_key)
    events = conn.execute(
        "SELECT * FROM custody_event WHERE sample_id = ? AND superseded_by IS NULL",
        (sample["id"],),
    ).fetchall()
    events.sort(key=event_time_key)
    periods, split_times = _custody_replay(sample, events)

    min_c = config["min_celsius"]
    max_c = config["max_celsius"]
    max_interval = config["max_interval_seconds"]

    reading_views: list[dict[str, Any]] = []
    out_of_range: list[dict[str, Any]] = []
    for r in readings:
        value = r["value_celsius"]
        in_range = min_c <= value <= max_c
        reading_views.append({
            "reading_id": r["reading_id"],
            "device_id": r["device_id"],
            "occurred_at": r["occurred_at"],
            "value_celsius": value,
            "in_range": in_range,
            "recorded_at": r["created_at"],
        })
        if not in_range:
            below = value < min_c
            out_of_range.append({
                "reading_id": r["reading_id"],
                "device_id": r["device_id"],
                "occurred_at": r["occurred_at"],
                "value_celsius": value,
                "direction": "below_min" if below else "above_max",
                "limit_celsius": min_c if below else max_c,
                "attribution": _attribution_at(periods, r["occurred_at"]),
            })

    gaps = _gap_intervals(readings, max_interval, split_times, periods)

    return {
        "sample_code": sample["sample_code"],
        "config": _config_view(config, sample["sample_code"]),
        "reading_count": len(reading_views),
        "readings": reading_views,
        "out_of_range_points": out_of_range,
        "gap_intervals": gaps,
    }
