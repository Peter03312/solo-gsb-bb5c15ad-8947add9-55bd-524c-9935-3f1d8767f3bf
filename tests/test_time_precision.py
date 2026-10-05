"""时间精度（小数秒）端到端自测：真实瞬间归一 UTC 后的全部判定。

运行：python -m tests.test_time_precision
覆盖：保管链排序/核验（同秒不同瞬间、真实同瞬间冲突）、时区偏移归一与无时区按 UTC、
整秒记录重试/查询兼容、批量移交与整批对账签收、读数同一瞬间去重与缺测严格超间隔、
校准证明左闭右开覆盖/端点相邻/重叠、可信异常摘要随真实精度证据变化、无效时间 422、
失败不留部分写入。
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_time_precision.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan（启动钩子）
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def post(path, body):
    return client.post(f"{API}{path}", json=body)


def get(path):
    return client.get(f"{API}{path}")


def reg(code, when, collector="王采集", seal=None):
    return post("/samples", {
        "sample_code": code, "collected_at": when,
        "collector": collector, "seal_no": seal or f"SEAL-{code}",
    })


def chain(code):
    return get(f"/samples/{code}/chain").json()


# ---------------------------------------------------------------- 1) 保管链同秒不同瞬间
print("1) 采集 .100 / 移交 .200 / 签收 .300 / 入库 .500 → valid，时间线按真实瞬间排序")
r = reg("TP-1", "2026-10-04T10:00:00.100Z")
check("登记 201 且保留 .100",
      r.status_code == 201 and r.json()["sample"]["collected_at"] == "2026-10-04T10:00:00.100000Z", r.text)
post("/events", {"event_id": "TP-T", "event_type": "transfer", "sample_code": "TP-1",
                 "occurred_at": "2026-10-04T10:00:00.200Z", "transfer_no": "TR-TP1",
                 "from_custodian": "王采集", "to_custodian": "李运输"})
post("/events", {"event_id": "TP-R", "event_type": "receive", "sample_code": "TP-1",
                 "occurred_at": "2026-10-04T10:00:00.300Z", "transfer_no": "TR-TP1",
                 "receiver": "李运输", "seal_no": "SEAL-TP-1"})
post("/events", {"event_id": "TP-S", "event_type": "storage", "sample_code": "TP-1",
                 "occurred_at": "2026-10-04T10:00:00.500Z", "custodian": "实验室"})
c = chain("TP-1")
check("结论 valid（签收不再被误判为不晚于移交）", c["status"] == "valid", str(c["reasons"]))
check("时间线真实瞬间排序", [t["event_id"] for t in c["timeline"]] == ["TP-T", "TP-R", "TP-S"])

# ---------------------------------------------------------------- 2) 真实同瞬间仍冲突
print("2) 签收瞬间等于移交瞬间（同为 .500，含不同表示）→ 无效“不晚于移交”")
reg("TP-2", "2026-10-04T10:00:00Z", collector="甲", seal="S2")
post("/events", {"event_id": "TP2-T", "event_type": "transfer", "sample_code": "TP-2",
                 "occurred_at": "2026-10-04T10:00:01.500Z", "transfer_no": "TR-TP2",
                 "from_custodian": "甲", "to_custodian": "乙"})
post("/events", {"event_id": "TP2-R", "event_type": "receive", "sample_code": "TP-2",
                 "occurred_at": "2026-10-04T18:00:01.500+08:00", "transfer_no": "TR-TP2",
                 "receiver": "乙", "seal_no": "S2"})
reasons = chain("TP-2")["reasons"]
check("等价偏移的同瞬间签收判不晚于移交", any("不晚于移交" in x for x in reasons), str(reasons))

# ---------------------------------------------------------------- 3) 时区归一 / 幂等 / 截断防护
print("3) 时区偏移按真实瞬间归一 UTC；等价表示同号幂等；不同瞬间同号 409")
r = reg("TP-3", "2026-10-04T18:00:00.300+08:00", collector="甲", seal="S3")
check("+08:00 .300 → UTC .300000Z", r.json()["sample"]["collected_at"] == "2026-10-04T10:00:00.300000Z", r.text)
r2 = reg("TP-3", "2026-10-04T18:00:00.300+08:00", collector="甲", seal="S3")
check("等价表示同号重试 200", r2.status_code == 200 and r2.json()["idempotent"] is True, r2.text)
r3 = reg("TP-3", "2026-10-04T18:00:00.301+08:00", collector="甲", seal="S3")
check(".300→.301 同号异内容 409", r3.status_code == 409 and r3.json()["error"]["code"] == "sample_conflict", r3.text)
# 无时区小数秒按 UTC
r = reg("TP-NAIVE", "2026-10-04T10:00:00.123456", collector="甲", seal="SN")
check("无时区输入按 UTC 且保留微秒",
      r.json()["sample"]["collected_at"] == "2026-10-04T10:00:00.123456Z", r.text)

# ---------------------------------------------------------------- 4) 整秒历史格式与行为保持
print("4) 整秒输入仍落为秒精度文本；同号重试 200（既有整秒记录兼容）")
r = reg("TP-4", "2026-10-04T10:00:00Z", collector="甲", seal="S4")
check("整秒无小数后缀", r.json()["sample"]["collected_at"] == "2026-10-04T10:00:00Z", r.text)
r = reg("TP-4", "2026-10-04T10:00:00Z", collector="甲", seal="S4")
check("整秒同号重试 200", r.status_code == 200 and r.json()["idempotent"] is True, r.text)

# ---------------------------------------------------------------- 5) 测温读数：去重与严格超间隔
print("5) 读数同秒不同瞬间不判重；真实同瞬间（等价表示）判重；缺测严格 > 限值；等于限值不报")
post("/temperature-configs",
     {"sample_code": "TP-1", "min_celsius": 2, "max_celsius": 8, "max_interval_seconds": 60})


def reading(rid, when, value, device="D1"):
    return post("/temperature-readings",
                {"sample_code": "TP-1", "reading_id": rid, "device_id": device,
                 "occurred_at": when, "value_celsius": value})


check("读数 .100 201", reading("T-R1", "2026-10-04T10:01:00.100Z", 5.0).status_code == 201)
check("同秒 .200 另一瞬间 201", reading("T-R2", "2026-10-04T10:01:00.200Z", 5.0).status_code == 201)
r = reading("T-R3", "2026-10-04T18:01:00.200+08:00", 5.0)
check("等价偏移同瞬间 409 reading_time_duplicate",
      r.status_code == 409 and r.json()["error"]["code"] == "reading_time_duplicate", r.text)
check("60.6 秒后读数 201", reading("T-R4", "2026-10-04T10:02:00.800Z", 5.0).status_code == 201)
check("恰好 +60.000 秒（边界）201", reading("T-R5", "2026-10-04T10:03:00.800Z", 5.0).status_code == 201)
v = get("/samples/TP-1/temperature").json()
gaps = v["gap_intervals"]
check("恰一个缺测：.2→.8 共 60.6 秒（精确小数）",
      len(gaps) == 1 and gaps[0]["before_reading_id"] == "T-R2"
      and gaps[0]["after_reading_id"] == "T-R4" and gaps[0]["duration_seconds"] == 60.6, str(gaps))
check("同秒 .1→.2（0.1 秒）不报缺测",
      all(g["before_reading_id"] != "T-R1" for g in gaps), str(gaps))
check("各段时长求和等于区间时长",
      all(g["duration_seconds"] == sum(s["duration_seconds"] for s in g["segments"]) for g in gaps))
# 采集前读数仍拒绝（按真实瞬间）
r = reading("T-R6", "2026-10-04T09:59:59.900Z", 5.0)
check("早于采集瞬间 422 reading_before_collection",
      r.status_code == 422 and r.json()["error"]["code"] == "reading_before_collection", r.text)

# ---------------------------------------------------------------- 6) 校准证明左闭右开 / 相邻 / 重叠
print("6) 校准证明左闭右开覆盖、端点相邻允许、真实重叠（含小数秒）409")
r = post("/temperature-calibrations", {
    "certificate_id": "TC-1", "device_id": "D1",
    "valid_from": "2026-10-04T10:01:00.100Z", "valid_to": "2026-10-04T10:01:00.200Z",
    "issued_by": "计量员"})
check("半秒区间证明 201", r.status_code == 201, r.text)
r = post("/temperature-calibrations", {
    "certificate_id": "TC-2", "device_id": "D1",
    "valid_from": "2026-10-04T10:01:00.200Z", "valid_to": "2026-10-04T10:02:00.900Z",
    "issued_by": "计量员"})
check("端点相邻（.200 相接）201", r.status_code == 201, r.text)
r = post("/temperature-calibrations", {
    "certificate_id": "TC-3", "device_id": "D1",
    "valid_from": "2026-10-04T10:02:00.85Z", "valid_to": "2026-10-04T10:02:00.95Z",
    "issued_by": "计量员"})
check("小数秒区间重叠 409 calibration_overlap",
      r.status_code == 409 and r.json()["error"]["code"] == "calibration_overlap", r.text)
r = post("/temperature-calibrations", {
    "certificate_id": "TC-4", "device_id": "D1",
    "valid_from": "2026-10-04T10:01:00.100Z", "valid_to": "2026-10-04T10:01:00.100Z",
    "issued_by": "计量员"})
check("空时段（valid_from==valid_to）422", r.status_code == 422, r.text)
r = post("/temperature-calibrations", {
    "certificate_id": "TC-5", "device_id": "D1",
    "valid_from": "2026-10-04T10:01:00.200Z", "valid_to": "2026-10-04T10:01:00.100Z",
    "issued_by": "计量员"})
check("反序时段 422", r.status_code == 422, r.text)
# 同号同内容等价时区表示重试 200
r = post("/temperature-calibrations", {
    "certificate_id": "TC-1", "device_id": "D1",
    "valid_from": "2026-10-04T18:01:00.100+08:00", "valid_to": "2026-10-04T18:01:00.200+08:00",
    "issued_by": "计量员"})
check("证明等价时区表示重试 200", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
tv = get("/samples/TP-1/temperature/trusted").json()
m = {x["reading_id"]: x for x in tv["reading_matches"]}
check("R1 被 TC-1 覆盖（含起点）", m["T-R1"]["certificate_id"] == "TC-1", str(m["T-R1"]))
check("R2 不被 TC-1 覆盖（终点不含）而被 TC-2 覆盖", m["T-R2"]["certificate_id"] == "TC-2", str(m["T-R2"]))

# ---------------------------------------------------------------- 7) 批量移交 + 整批签收
print("7) 批量移交 .100 / 整批签收 .200：小数秒严格晚于；同秒更早瞬间整单 422 不留记录")
for code, seal in (("TP-B1", "SB1"), ("TP-B2", "SB2"), ("TP-B3", "SB3")):
    reg(code, "2026-10-04T09:00:00Z", seal=seal)
r = post("/transfer-batches", {
    "batch_id": "TPBAT1", "occurred_at": "2026-10-04T11:00:00.100Z",
    "from_custodian": "王采集", "to_custodian": "李运输", "items": [
        {"sample_code": "TP-B1", "transfer_no": "TB1", "event_id": "TB-E1"},
        {"sample_code": "TP-B2", "transfer_no": "TB2", "event_id": "TB-E2"}]})
check("批量移交 201", r.status_code == 201, r.text)
r = post("/transfer-batches/TPBAT1/reconciliation", {
    "reconciliation_no": "TPREC1", "receiver": "李运输",
    "occurred_at": "2026-10-04T11:00:00.200Z", "items": [
        {"sample_code": "TP-B1", "event_id": "RC-E1", "verified_seal_no": "SB1"},
        {"sample_code": "TP-B2", "event_id": "RC-E2", "verified_seal_no": "SB2"}]})
check("整批签收 .200（晚于 .100）201", r.status_code == 201, r.text)
post("/transfer-batches", {
    "batch_id": "TPBAT3", "occurred_at": "2026-10-04T12:00:00.100Z",
    "from_custodian": "王采集", "to_custodian": "李运输", "items": [
        {"sample_code": "TP-B3", "transfer_no": "TB9", "event_id": "TB-E9"}]})
r = post("/transfer-batches/TPBAT3/reconciliation", {
    "reconciliation_no": "TPREC3", "receiver": "李运输",
    "occurred_at": "2026-10-04T12:00:00.050Z", "items": [
        {"sample_code": "TP-B3", "event_id": "RC-E9", "verified_seal_no": "SB3"}]})
check("签收早于同秒移交瞬间 422 receive_time_invalid",
      r.status_code == 422
      and r.json()["error"]["details"]["items"][0]["code"] == "receive_time_invalid", r.text)
check("整单失败不留对账（仍 404）",
      get("/transfer-batches/TPBAT3/reconciliation").status_code == 404)
# 失败单号修正为更晚瞬间后可复用成功
r = post("/transfer-batches/TPBAT3/reconciliation", {
    "reconciliation_no": "TPREC3", "receiver": "李运输",
    "occurred_at": "2026-10-04T12:00:00.200Z", "items": [
        {"sample_code": "TP-B3", "event_id": "RC-E9", "verified_seal_no": "SB3"}]})
check("失败后修正时间复用单号成功 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 8) 可信异常证据摘要随真实精度
print("8) 可信异常：超温点证据摘要随读数真实精度/归属变化；过期摘要 422 不留记录")
reading("T-ROT", "2026-10-04T10:05:00.100Z", 99.0)
post("/temperature-calibrations", {
    "certificate_id": "TC-9", "device_id": "D1",
    "valid_from": "2026-10-04T10:05:00Z", "valid_to": "2026-10-04T10:06:00Z",
    "issued_by": "计量员"})
a = get("/samples/TP-1/temperature/anomalies").json()
oor = [i for i in a["items"] if i["anomaly_type"] == "out_of_range_point"]
check("带证明超温点入异常", len(oor) == 1, str(a)[:400])
if oor:
    it = oor[0]
    r = post("/samples/TP-1/temperature/anomalies/dispositions", {
        "disposition_no": "TD-1", "sample_code": "TP-1", "anomaly_id": it["anomaly_id"],
        "expected_evidence_digest": it["evidence_digest"], "decision": "acknowledged",
        "reason": "认可", "handler": "质控"})
    check("处置 201", r.status_code == 201, r.text)
    r = post("/samples/TP-1/temperature/anomalies/dispositions", {
        "disposition_no": "TD-2", "sample_code": "TP-1", "anomaly_id": it["anomaly_id"],
        "expected_evidence_digest": "0" * 64, "decision": "acknowledged",
        "reason": "过期摘要", "handler": "质控"})
    check("过期摘要 422 anomaly_stale",
          r.status_code == 422 and r.json()["error"]["code"] == "anomaly_stale", r.text)
    # 同摘要重复处置 409
    r = post("/samples/TP-1/temperature/anomalies/dispositions", {
        "disposition_no": "TD-3", "sample_code": "TP-1", "anomaly_id": it["anomaly_id"],
        "expected_evidence_digest": it["evidence_digest"], "decision": "excluded",
        "reason": "重复", "handler": "质控乙"})
    check("同异常同摘要重复 409 anomaly_already_disposed",
          r.status_code == 409 and r.json()["error"]["code"] == "anomaly_already_disposed", r.text)

# ---------------------------------------------------------------- 9) 无效时间文本
print("9) 无效时间文本仍 422（既有错误语义）")
r = reg("TP-BAD", "not-a-time")
check("非法时间 422", r.status_code == 422, r.text)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
