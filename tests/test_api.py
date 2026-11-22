"""FastAPI 接口层自测。"""

from fastapi.testclient import TestClient

from app.main import app
from app.sample import SAMPLE_PAYLOAD

client = TestClient(app)


def test_sample_endpoint():
    r = client.get("/api/sample")
    assert r.status_code == 200
    assert len(r.json()["alerts"]) >= 5


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "静默规则首呼预演".encode() in r.content


def test_full_simulation_sample():
    r = client.post("/api/simulate", json=SAMPLE_PAYLOAD)
    assert r.status_code == 200, r.text
    data = r.json()
    s = data["summary"]

    # 7 个故障轮次：auth/cache(2)/gateway/order-db/payment-api/test-svc
    assert s["total_rounds"] == 7
    # payment-api 推迟、cache#1 推迟、auth 被压掉、order-db 被压掉
    assert s["delayed"] == 2
    assert s["suppressed"] == 2
    # gateway 无变化；cache#2 与 test-svc 前后均未首呼
    assert s["unchanged"] == 1
    assert s["still_not_called"] == 2
    assert s["before_called"] == 5
    assert s["after_called"] == 3

    by_key = {(x["service"], x["round_index"]): x for x in data["rounds"]}

    pay = by_key[("payment-api", 1)]
    assert pay["before"]["first_call_at"].endswith("10:10:00Z")
    assert pay["after"]["first_call_at"].endswith("10:20:00Z")
    # 随呼快照：10:20 时 A1 仍在静默窗口内，只剩 A2
    assert pay["after"]["called_alert_ids"] == ["A2-payment-latency"]
    assert pay["delay_seconds"] == 600

    order = by_key[("order-db", 1)]
    assert order["verdict"] == "suppressed"
    assert order["after"]["first_call_at"] is None
    assert order["ended_at"] is None  # A3 未恢复 => 轮次未结束

    auth = by_key[("auth", 1)]
    assert auth["verdict"] == "suppressed"
    assert auth["after"]["still_waiting_at_cutoff"] is True
    assert auth["before"]["first_call_at"].endswith("11:25:00Z")

    gw = by_key[("gateway", 1)]
    assert gw["verdict"] == "unchanged"
    assert gw["before"]["first_call_at"] == gw["after"]["first_call_at"]

    cache2 = by_key[("cache", 2)]
    assert cache2["verdict"] == "still_not_called"
    assert cache2["before"]["first_call_at"] is None


def test_validation_duplicate_alert_ids():
    payload = {
        **SAMPLE_PAYLOAD,
        "alerts": [dict(SAMPLE_PAYLOAD["alerts"][0]), dict(SAMPLE_PAYLOAD["alerts"][0])],
    }
    r = client.post("/api/simulate", json=payload)
    assert r.status_code == 422
    assert "重复" in r.json()["detail"]


def test_validation_recovered_before_fired():
    payload = {
        "alerts": [
            {
                "id": "x",
                "service": "svc",
                "labels": {},
                "fired_at": "2026-11-21T11:00:00Z",
                "recovered_at": "2026-11-21T10:00:00Z",
            }
        ],
        "current_silences": [],
        "proposed_silences": [],
        "wait_minutes": 10,
        "cutoff_at": "2026-11-21T12:00:00Z",
    }
    r = client.post("/api/simulate", json=payload)
    assert r.status_code == 422
    assert "恢复时刻早于触发时刻" in r.json()["detail"]


def test_alert_after_cutoff_ignored():
    payload = {
        "alerts": [
            {
                "id": "late",
                "service": "svc",
                "labels": {"x": "y"},
                "fired_at": "2026-11-21T13:00:00Z",
                "recovered_at": None,
            },
            {
                "id": "ok",
                "service": "svc",
                "labels": {"x": "y"},
                "fired_at": "2026-11-21T11:00:00Z",
                "recovered_at": None,
            },
        ],
        "current_silences": [],
        "proposed_silences": [],
        "wait_minutes": 10,
        "cutoff_at": "2026-11-21T12:00:00Z",
    }
    r = client.post("/api/simulate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["ignored_alerts_after_cutoff"] == ["late"]
    assert data["rounds"][0]["alert_ids"] == ["ok"]


def test_bad_rule_interval_rejected():
    payload = {
        "alerts": [
            {
                "id": "a",
                "service": "svc",
                "labels": {"x": "y"},
                "fired_at": "2026-11-21T11:00:00Z",
                "recovered_at": None,
            }
        ],
        "current_silences": [],
        "proposed_silences": [
            {
                "id": "bad",
                "labels": {"x": "y"},
                "start_at": "2026-11-21T11:30:00Z",
                "end_at": "2026-11-21T11:00:00Z",
            }
        ],
        "wait_minutes": 10,
        "cutoff_at": "2026-11-21T12:00:00Z",
    }
    r = client.post("/api/simulate", json=payload)
    assert r.status_code == 422
    assert "终点必须晚于起点" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# 首屏 summary_only / 展开明细
# --------------------------------------------------------------------------- #


def test_default_simulate_still_returns_full_result():
    r = client.post("/api/simulate", json=SAMPLE_PAYLOAD)
    data = r.json()
    assert data["summary_only"] is False
    assert data["simulation_id"]
    for row in data["rounds"]:
        for side in ("before", "after"):
            assert "unsilenced_stretches" in row[side]
            assert "scan" not in row[side]
    # 完整结果与旧行为一致
    assert data["summary"]["total_rounds"] == 7


def test_summary_only_omits_stretches_but_keeps_conclusions():
    full = client.post("/api/simulate", json=SAMPLE_PAYLOAD).json()
    brief = client.post(
        "/api/simulate", json={**SAMPLE_PAYLOAD, "summary_only": True}
    ).json()

    assert brief["summary_only"] is True
    assert brief["summary"] == full["summary"]
    assert [(r["service"], r["round_index"], r["verdict"], r["delay_seconds"])
            for r in brief["rounds"]] == [
        (r["service"], r["round_index"], r["verdict"], r["delay_seconds"])
        for r in full["rounds"]
    ]
    for b, f in zip(brief["rounds"], full["rounds"]):
        for side in ("before", "after"):
            assert b[side]["first_call_at"] == f[side]["first_call_at"]
            assert b[side]["called_alert_ids"] == f[side]["called_alert_ids"]
            assert b[side]["still_waiting_at_cutoff"] == f[side]["still_waiting_at_cutoff"]
            assert "unsilenced_stretches" not in b[side]
            assert "scan" in b[side]


def test_summary_only_reports_early_stop():
    brief = client.post(
        "/api/simulate", json={**SAMPLE_PAYLOAD, "summary_only": True}
    ).json()
    by_key = {(r["service"], r["round_index"]): r for r in brief["rounds"]}
    # payment-api 两套规则都在首呼确定后停止
    pay = by_key[("payment-api", 1)]
    assert pay["before"]["scan"]["stopped_early"] is True
    assert pay["after"]["scan"]["stopped_early"] is True
    assert pay["after"]["scan"]["events_scanned"] < pay["after"]["scan"]["events_total"]
    # 截止时仍在等待的 auth#1，加静默后必须扫满才能确认
    auth = by_key[("auth", 1)]
    assert auth["after"]["still_waiting_at_cutoff"] is True
    assert auth["after"]["scan"]["stopped_early"] is False


def test_round_details_uses_stored_inputs():
    brief = client.post(
        "/api/simulate", json={**SAMPLE_PAYLOAD, "summary_only": True}
    ).json()
    sim_id = brief["simulation_id"]
    r = client.post(
        "/api/round-details",
        json={"simulation_id": sim_id, "service": "payment-api", "round_index": 1},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["before"]["unsilenced_stretches"] == [
        {"start": "2026-11-21T10:00:00Z", "end": "2026-11-21T10:40:00Z", "open_ended": False}
    ]
    assert d["after"]["unsilenced_stretches"] == [
        {"start": "2026-11-21T10:00:00Z", "end": "2026-11-21T10:05:00Z", "open_ended": False},
        {"start": "2026-11-21T10:10:00Z", "end": "2026-11-21T10:40:00Z", "open_ended": False},
    ]
    # 展开明细与完整模拟的区间完全一致（同一套输入）
    full = client.post("/api/simulate", json=SAMPLE_PAYLOAD).json()
    full_row = next(
        x for x in full["rounds"]
        if (x["service"], x["round_index"]) == ("payment-api", 1)
    )
    assert d["before"]["unsilenced_stretches"] == full_row["before"]["unsilenced_stretches"]
    assert d["after"]["unsilenced_stretches"] == full_row["after"]["unsilenced_stretches"]


def test_round_details_rejects_unknown_simulation_and_round():
    brief = client.post(
        "/api/simulate", json={**SAMPLE_PAYLOAD, "summary_only": True}
    ).json()
    r = client.post(
        "/api/round-details",
        json={"simulation_id": "nope", "service": "payment-api", "round_index": 1},
    )
    assert r.status_code == 404
    assert "重新运行预演" in r.json()["detail"]

    r = client.post(
        "/api/round-details",
        json={
            "simulation_id": brief["simulation_id"],
            "service": "payment-api",
            "round_index": 9,
        },
    )
    assert r.status_code == 404
    assert "不属于这次预演" in r.json()["detail"]


def test_each_simulation_gets_its_own_id_and_inputs():
    a = client.post("/api/simulate", json=SAMPLE_PAYLOAD).json()
    payload_b = {
        **SAMPLE_PAYLOAD,
        "wait_minutes": 30,
    }
    b = client.post("/api/simulate", json={**payload_b, "summary_only": True}).json()
    assert a["simulation_id"] != b["simulation_id"]

    # 用 B 的 id 展开，必须按 B 的等待时长（30 分钟）计算，而不是 A 的 10 分钟
    d = client.post(
        "/api/round-details",
        json={"simulation_id": b["simulation_id"], "service": "gateway", "round_index": 1},
    ).json()
    # gateway#1 未静默区间只有 11:00-11:15，30 分钟等待在区间内无法满足
    assert d["before"]["unsilenced_stretches"] == [
        {"start": "2026-11-21T11:00:00Z", "end": "2026-11-21T11:15:00Z", "open_ended": False}
    ]
    a_row = next(x for x in a["rounds"] if x["service"] == "gateway")
    assert a_row["before"]["first_call_at"].endswith("11:10:00Z")
