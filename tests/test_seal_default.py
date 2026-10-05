"""端到端自测：封签检查省略 seal_intact 的默认完好规则与历史兼容。

覆盖本轮修复：
  * 新提交 seal_check 只填 checked_seal_no、省略 seal_intact → 按完好（true）落库与核验，
    完整保管链判 valid 可核定入库；明确提交 false 仍表示封签破损 → invalid、核定拒绝；
  * 单事件上报与纠错替换（replace）走同一套归一：核验、事件内容与新版本快照一致；
  * 已落库事件/纠错记录/历史版本/封存案卷保持原样（含旧版把省略误落为 false 的记录，
    升级只补 hash_gen 列、不回填、不重算指纹）；
  * 幂等：既有编号同请求重试返回原结果（旧行允许 legacy 指纹命中），异内容仍 409，
    不因新默认值重算历史指纹；失败提交不留下新事件或版本。

运行：python -m tests.test_seal_default
"""
import json
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_seal_default.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db_session, init_db  # noqa: E402
from app.main import app  # noqa: E402
import app.service as svc  # noqa: E402

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


def reg(code, seal=None, collector="张三"):
    return client.post(f"{API}/samples", json={
        "sample_code": code,
        "collected_at": "2026-09-30T01:00:00Z",
        "collector": collector,
        "seal_no": seal or f"SEAL-{code}",
    })


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


def correction(body):
    return client.post(f"{API}/corrections", json=body)


def full_transfer_receive(code):
    """移交→签收（时间线 02:00/03:00），调用方再接封签检查与入库。"""
    reg(code)
    ev({"event_id": f"{code}-T", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": f"TR-{code}",
        "from_custodian": "张三", "to_custodian": "李四"})
    ev({"event_id": f"{code}-R", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": f"TR-{code}",
        "receiver": "李四", "seal_no": f"SEAL-{code}"})


def version_count(code):
    return client.get(f"{API}/samples/{code}/versions").json()["total"]


# ---------------------------------------------------------------- 1. 省略字段按完好，明确 false 仍破损
print("1) 新提交 seal_check 省略 seal_intact → 完好；明确 false → 破损")
full_transfer_receive("SD-1")
r = ev({"event_id": "SD1-C", "event_type": "seal_check", "sample_code": "SD-1",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-1"})
check("省略字段 201", r.status_code == 201, r.text)
check("落库 payload seal_intact=true",
      r.json()["event"]["payload"] == {"checked_seal_no": "SEAL-SD-1", "seal_intact": True},
      r.text)
ev({"event_id": "SD1-S", "event_type": "storage", "sample_code": "SD-1",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
c = chain("SD-1")
check("完整链 valid", c["status"] == "valid" and not any("破损" in x for x in c["reasons"]),
      str(c["reasons"]))
r = client.post(f"{API}/samples/SD-1/approve", json={"approver": "质量主管"})
check("有效链核定入库 200", r.status_code == 200, r.text)

full_transfer_receive("SD-2")
ev({"event_id": "SD2-C", "event_type": "seal_check", "sample_code": "SD-2",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-2", "seal_intact": False})
ev({"event_id": "SD2-S", "event_type": "storage", "sample_code": "SD-2",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
c = chain("SD-2")
check("明确 false 判 invalid 且原因含破损",
      c["status"] == "invalid" and any("破损" in x for x in c["reasons"]), str(c["reasons"]))
r = client.post(f"{API}/samples/SD-2/approve", json={"approver": "质量主管"})
check("破损链核定被拒 422", r.status_code == 422, r.text)

# ---------------------------------------------------------------- 2. 幂等：省略体/显式 true 互通，与 false 冲突
print("2) 新记录的幂等与冲突：省略=显式 true，同号异内容（false）冲突")
full_transfer_receive("SD-3")
ev({"event_id": "SD3-C", "event_type": "seal_check", "sample_code": "SD-3",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-3"})
v_before = version_count("SD-3")
r = ev({"event_id": "SD3-C", "event_type": "seal_check", "sample_code": "SD-3",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-3"})
check("省略体同号重试 200 idempotent",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = ev({"event_id": "SD3-C", "event_type": "seal_check", "sample_code": "SD-3",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-3", "seal_intact": True})
check("显式 true 与省略记录同内容 200",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = ev({"event_id": "SD3-C", "event_type": "seal_check", "sample_code": "SD-3",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-3", "seal_intact": False})
check("显式 false 与省略记录异内容 409",
      r.status_code == 409 and r.json()["error"]["code"] == "event_conflict", r.text)
check("冲突/重试均不新增版本", version_count("SD-3") == v_before,
      f"{version_count('SD-3')} vs {v_before}")

# 反向：明确 false 的记录，省略体重试也属异内容
r = ev({"event_id": "SD2-C", "event_type": "seal_check", "sample_code": "SD-2",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-2"})
check("false 记录 vs 省略体 409 event_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "event_conflict", r.text)
r = ev({"event_id": "SD2-C", "event_type": "seal_check", "sample_code": "SD-2",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-2", "seal_intact": False})
check("false 记录显式 false 重试 200",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)

# ---------------------------------------------------------------- 3. 替换纠错：核验/事件内容/版本快照一致
print("3) replace 的新事件省略 seal_intact → true，核验、内容、快照一致")
full_transfer_receive("SD-4")
ev({"event_id": "SD4-C", "event_type": "seal_check", "sample_code": "SD-4",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "WRONG-NO", "seal_intact": True})
check("替换前封签号不符 invalid", chain("SD-4")["status"] == "invalid")
r = correction({"correction_id": "FIX-SD4", "sample_code": "SD-4",
                "target_event_id": "SD4-C", "action": "replace",
                "operator": "纠错员", "reason": "扫码错误，整体替换",
                "new_event": {"event_id": "SD4-C2", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-SD-4"}})
check("替换 201", r.status_code == 201, r.text)
new_in_eval = [e for e in r.json()["evaluation"]["timeline"] if e["event_id"] == "SD4-C2"][0]
check("响应核验中的新事件 payload=true",
      new_in_eval["payload"] == {"checked_seal_no": "SEAL-SD-4", "seal_intact": True},
      str(new_in_eval["payload"]))
ev({"event_id": "SD4-S", "event_type": "storage", "sample_code": "SD-4",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
c = chain("SD-4")
new_in_chain = [e for e in c["timeline"] if e["event_id"] == "SD4-C2"][0]
check("替换后链 valid", c["status"] == "valid", str(c["reasons"]))
check("链查询新事件 payload=true",
      new_in_chain["payload"] == {"checked_seal_no": "SEAL-SD-4", "seal_intact": True},
      str(new_in_chain["payload"]))
# 最新版本快照内的新事件内容须与核验/链查询一致
vers = client.get(f"{API}/samples/SD-4/versions").json()
snap = client.get(f"{API}/samples/SD-4/versions/{vers['current_version']}").json()["version_snapshot"]
new_in_snap = [e for e in snap["timeline"] if e["event_id"] == "SD4-C2"][0]
check("新版本快照 payload 一致",
      new_in_snap["payload"] == {"checked_seal_no": "SEAL-SD-4", "seal_intact": True}
      and snap["status"] == "valid", str(new_in_snap["payload"]))
# 纠错同请求（省略体）重试幂等，不新增版本
v_after_storage = vers["total"]
r = correction({"correction_id": "FIX-SD4", "sample_code": "SD-4",
                "target_event_id": "SD4-C", "action": "replace",
                "operator": "纠错员", "reason": "扫码错误，整体替换",
                "new_event": {"event_id": "SD4-C2", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-SD-4"}})
check("替换纠错省略体重试 200", r.status_code == 200, r.text)
check("重试不新增版本", version_count("SD-4") == v_after_storage)
# 同纠错号异内容（新事件显式 false）→ 409
r = correction({"correction_id": "FIX-SD4", "sample_code": "SD-4",
                "target_event_id": "SD4-C", "action": "replace",
                "operator": "纠错员", "reason": "扫码错误，整体替换",
                "new_event": {"event_id": "SD4-C2", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-SD-4", "seal_intact": False}})
check("替换纠错异内容 409 correction_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "correction_conflict", r.text)

# 新写入的行带现行指纹代际标记
with db_session() as conn:
    gen_event = conn.execute(
        "SELECT hash_gen FROM custody_event WHERE event_id='SD4-C2'").fetchone()[0]
    gen_corr = conn.execute(
        "SELECT hash_gen FROM event_correction WHERE correction_id='FIX-SD4'").fetchone()[0]
check("新事件/新纠错 hash_gen=1", gen_event == 1 and gen_corr == 1,
      f"{gen_event}/{gen_corr}")

# ---------------------------------------------------------------- 4. 失败提交不留事件或版本
print("4) 失败提交不留下新事件或版本")
v_before = version_count("SD-4")
r = correction({"correction_id": "FIX-SD4-NOPE", "sample_code": "SD-4",
                "target_event_id": "NOT-EXIST", "action": "void",
                "operator": "纠错员", "reason": "目标不存在"})
check("目标不存在 404", r.status_code == 404, r.text)
with db_session() as conn:
    n_corr = conn.execute(
        "SELECT COUNT(*) FROM event_correction WHERE correction_id='FIX-SD4-NOPE'").fetchone()[0]
check("失败纠错未留记录", n_corr == 0)
check("失败纠错未新增版本", version_count("SD-4") == v_before)

# replace 新事件号已占用 → 整单失败，目标事件仍有效、纠错不留痕、版本不增
r = correction({"correction_id": "FIX-SD4-DUP", "sample_code": "SD-4",
                "target_event_id": "SD4-C2", "action": "replace",
                "operator": "纠错员", "reason": "新号撞车",
                "new_event": {"event_id": "SD3-C", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-SD-4"}})
check("新事件号占用 409", r.status_code == 409 and r.json()["error"]["code"] == "new_event_id_occupied",
      r.text)
with db_session() as conn:
    still_effective = conn.execute(
        "SELECT superseded_by IS NULL FROM custody_event WHERE event_id='SD4-C2'").fetchone()[0]
    n_corr = conn.execute(
        "SELECT COUNT(*) FROM event_correction WHERE correction_id='FIX-SD4-DUP'").fetchone()[0]
check("失败替换：目标仍有效且无纠错留痕", bool(still_effective) and n_corr == 0)
check("失败替换未新增版本", version_count("SD-4") == v_before)


# ---------------------------------------------------------------- 5. 旧版误落 false 的历史记录：原样保留 + 同请求重试幂等 + 异内容冲突
print("5) 旧版误落 false 的历史记录（hash_gen=NULL）保持原样，重试兼容、异内容冲突")
def _fp(obj) -> str:
    return svc._fingerprint(obj)


def insert_legacy_seal_event(code, event_id, seal_no, occurred="2026-09-30T03:30:00Z"):
    """按旧版规则直接落一条 seal_check：省略被 bool(None) 误落为 false，hash_gen 留 NULL。"""
    payload = {"checked_seal_no": seal_no, "seal_intact": False}
    canon = {"event_id": event_id, "event_type": "seal_check", "sample_code": code,
             "occurred_at": occurred, **payload}
    with db_session(immediate=True) as conn:
        sid = conn.execute("SELECT id FROM sample WHERE sample_code=?", (code,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO custody_event (sample_id,event_id,event_type,occurred_at,payload_json,"
            "content_hash,created_at) VALUES (?,?,?,?,?,?,?)",
            (sid, event_id, "seal_check", occurred,
             json.dumps(payload, ensure_ascii=False), _fp(canon), "2026-09-30T03:31:00Z"),
        )
    return _fp(canon)


full_transfer_receive("SD-5")
legacy_hash = insert_legacy_seal_event("SD-5", "SD5-C", "SEAL-SD-5")
# 历史记录原样：仍按破损判 invalid
c = chain("SD-5")
check("旧记录仍判 invalid（破损）",
      c["status"] == "invalid" and any("破损" in x for x in c["reasons"]), str(c["reasons"]))
# 省略体同请求重试 → 返回原结果（200 幂等），不重写历史指纹
r = ev({"event_id": "SD5-C", "event_type": "seal_check", "sample_code": "SD-5",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-5"})
check("旧记录省略体重试 200 idempotent",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
# 显式 false 同样是同内容（旧记录确为 false）
r = ev({"event_id": "SD5-C", "event_type": "seal_check", "sample_code": "SD-5",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-5", "seal_intact": False})
check("旧记录显式 false 重试 200",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
# 显式 true 与旧记录是异内容 → 409
r = ev({"event_id": "SD5-C", "event_type": "seal_check", "sample_code": "SD-5",
        "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-SD-5", "seal_intact": True})
check("旧记录显式 true 409 event_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "event_conflict", r.text)
with db_session() as conn:
    row = conn.execute(
        "SELECT payload_json, content_hash, hash_gen FROM custody_event WHERE event_id='SD5-C'"
    ).fetchone()
check("旧记录 payload/指纹/代际均未被重写",
      json.loads(row["payload_json"]) == {"checked_seal_no": "SEAL-SD-5", "seal_intact": False}
      and row["content_hash"] == legacy_hash and row["hash_gen"] is None,
      str(tuple(row)))

# ---------------------------------------------------------------- 6. 旧版替换纠错（省略误落 false）的请求指纹兼容
print("6) 旧版替换纠错记录（hash_gen=NULL）：同请求重试幂等、异内容冲突")
full_transfer_receive("SD-6")
# 旧版状态：SD6-C 被 FIX-SD6 替换为 SD6-C2（new_event 省略误落 false），两条事件 hash_gen 均 NULL
with db_session(immediate=True) as conn:
    sid = conn.execute("SELECT id FROM sample WHERE sample_code='SD-6'").fetchone()["id"]
    legacy_new_canon = {"event_id": "SD6-C2", "event_type": "seal_check", "sample_code": "SD-6",
                        "occurred_at": "2026-09-30T03:30:00Z",
                        "checked_seal_no": "SEAL-SD-6", "seal_intact": False}
    payload = {"checked_seal_no": "SEAL-SD-6", "seal_intact": False}
    conn.execute(
        "INSERT INTO custody_event (sample_id,event_id,event_type,occurred_at,payload_json,"
        "content_hash,created_at,superseded_by,replaces_event_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, "SD6-C", "seal_check", "2026-09-30T03:20:00Z",
         json.dumps({"checked_seal_no": "SEAL-SD-6", "seal_intact": False}),
         _fp({**{"event_id": "SD6-C", "event_type": "seal_check", "sample_code": "SD-6",
                 "occurred_at": "2026-09-30T03:20:00Z"}, **payload}),
         "2026-09-30T03:21:00Z", "FIX-SD6", None))
    conn.execute(
        "INSERT INTO custody_event (sample_id,event_id,event_type,occurred_at,payload_json,"
        "content_hash,created_at,superseded_by,replaces_event_id) VALUES (?,?,?,?,?,?,?,NULL,?)",
        (sid, "SD6-C2", "seal_check", "2026-09-30T03:30:00Z",
         json.dumps(payload), _fp(legacy_new_canon), "2026-09-30T03:31:00Z", "SD6-C"))
    corr_obj = {"correction_id": "FIX-SD6", "sample_code": "SD-6", "target_event_id": "SD6-C",
                "action": "replace", "operator": "纠错员", "reason": "旧版纠错",
                "new_event": legacy_new_canon}
    corr_hash = _fp(corr_obj)
    conn.execute(
        "INSERT INTO event_correction (correction_id,sample_id,target_event_id,action,operator,"
        "reason,new_event_id,new_event_row_id,request_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("FIX-SD6", sid, "SD6-C", "replace", "纠错员", "旧版纠错", "SD6-C2", None,
         corr_hash, "2026-09-30T03:31:00Z"))
# 同请求（new_event 省略 seal_intact）重试 → 200
r = correction({"correction_id": "FIX-SD6", "sample_code": "SD-6", "target_event_id": "SD6-C",
                "action": "replace", "operator": "纠错员", "reason": "旧版纠错",
                "new_event": {"event_id": "SD6-C2", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-SD-6"}})
check("旧纠错省略体重试 200", r.status_code == 200, r.text)
# 异内容（new_event 显式 true）→ 409
r = correction({"correction_id": "FIX-SD6", "sample_code": "SD-6", "target_event_id": "SD6-C",
                "action": "replace", "operator": "纠错员", "reason": "旧版纠错",
                "new_event": {"event_id": "SD6-C2", "event_type": "seal_check",
                              "occurred_at": "2026-09-30T03:30:00Z",
                              "checked_seal_no": "SEAL-SD-6", "seal_intact": True}})
check("旧纠错显式 true 409 correction_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "correction_conflict", r.text)
with db_session() as conn:
    kept = conn.execute(
        "SELECT request_hash, hash_gen FROM event_correction WHERE correction_id='FIX-SD6'"
    ).fetchone()
check("旧纠错指纹未重算、代际仍 NULL", kept["request_hash"] == corr_hash and kept["hash_gen"] is None,
      str(tuple(kept)))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
