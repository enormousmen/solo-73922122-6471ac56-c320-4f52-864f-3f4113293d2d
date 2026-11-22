"""FastAPI 预演服务：导入告警/静默规则，输出加静默前后的首呼对照。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine
from .sample import SAMPLE_PAYLOAD


class AlertIn(BaseModel):
    id: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    fired_at: str
    recovered_at: Optional[str] = None


class SilenceRuleIn(BaseModel):
    id: str = Field(..., min_length=1)
    labels: dict[str, str]
    start_at: str
    end_at: str


class SimulationRequest(BaseModel):
    alerts: list[AlertIn]
    current_silences: list[SilenceRuleIn] = Field(default_factory=list)
    proposed_silences: list[SilenceRuleIn] = Field(default_factory=list)
    wait_minutes: float = Field(..., gt=0)
    cutoff_at: str
    # 只取首屏结论（不含未静默区间）；默认 false => 完整结果，保持旧行为
    summary_only: bool = False


class DetailsRequest(BaseModel):
    """展开某一轮时按 simulation_id 取回服务端留存的输入，浏览器只传选中轮次。"""

    simulation_id: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)
    round_index: int = Field(..., ge=1)


@dataclass(frozen=True)
class SimulationInputs:
    """生成某张结果表时的输入快照；展开明细时原样复用。"""

    alerts: list[engine.Alert]
    current: list[engine.SilenceRule]
    proposed: list[engine.SilenceRule]
    wait: timedelta
    cutoff: datetime
    wait_minutes: float


# 进程内保留最近若干次预演输入，供展开时复用；表只在预演成功后登记。
_STORE: "OrderedDict[str, SimulationInputs]" = OrderedDict()
_STORE_LIMIT = 32
_STORE_LOCK = Lock()


def _remember(inputs: SimulationInputs) -> str:
    simulation_id = uuid4().hex
    with _STORE_LOCK:
        _STORE[simulation_id] = inputs
        while len(_STORE) > _STORE_LIMIT:
            _STORE.popitem(last=False)
    return simulation_id


def _recall(simulation_id: str) -> SimulationInputs:
    with _STORE_LOCK:
        inputs = _STORE.get(simulation_id)
    if inputs is None:
        raise HTTPException(
            404, "预演结果已失效（服务重启或结果被淘汰），请重新运行预演"
        )
    return inputs


app = FastAPI(title="静默规则首呼预演", version="1.1.0")


def _build_alert(a: AlertIn) -> engine.Alert:
    try:
        fired = engine.parse_dt(a.fired_at)
        recovered = engine.parse_dt(a.recovered_at) if a.recovered_at else None
    except ValueError as exc:
        raise HTTPException(422, f"告警 {a.id} 时间格式无法解析: {exc}")
    if recovered is not None and recovered < fired:
        raise HTTPException(422, f"告警 {a.id} 的恢复时刻早于触发时刻")
    return engine.Alert(
        id=a.id,
        service=a.service,
        labels=dict(a.labels),
        fired_at=fired,
        recovered_at=recovered,
    )


def _build_rule(r: SilenceRuleIn) -> engine.SilenceRule:
    try:
        start = engine.parse_dt(r.start_at)
        end = engine.parse_dt(r.end_at)
    except ValueError as exc:
        raise HTTPException(422, f"静默规则 {r.id} 时间格式无法解析: {exc}")
    if end <= start:
        raise HTTPException(422, f"静默规则 {r.id} 的终点必须晚于起点")
    if not r.labels:
        raise HTTPException(422, f"静默规则 {r.id} 至少需要一个标签条件")
    return engine.SilenceRule(
        id=r.id, labels=dict(r.labels), start_at=start, end_at=end
    )


def _validate(req: SimulationRequest):
    if not req.alerts:
        raise HTTPException(422, "至少导入一条告警")

    ids = [a.id for a in req.alerts]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "告警 ID 存在重复")
    rule_ids = [r.id for r in req.current_silences + req.proposed_silences]
    if len(set(rule_ids)) != len(rule_ids):
        raise HTTPException(422, "静默规则 ID 存在重复")

    try:
        cutoff = engine.parse_dt(req.cutoff_at)
    except ValueError as exc:
        raise HTTPException(422, f"观察截止时间无法解析: {exc}")

    alerts = [_build_alert(a) for a in req.alerts]
    dropped = [a.id for a in alerts if a.fired_at >= cutoff]
    alerts = [a for a in alerts if a.fired_at < cutoff]
    if not alerts:
        raise HTTPException(422, "观察截止之前没有任何告警触发，无法预演")

    current = [_build_rule(r) for r in req.current_silences]
    proposed = [_build_rule(r) for r in req.proposed_silences]
    wait = timedelta(minutes=req.wait_minutes)
    return alerts, current, proposed, wait, cutoff, dropped


@app.post("/api/simulate")
def simulate(req: SimulationRequest):
    alerts, current, proposed, wait, cutoff, dropped = _validate(req)

    if req.summary_only:
        # 首屏：逐轮边扫描边定首呼，确定即停；不带未静默区间 → 不返回明细
        rows = engine.scan_rounds(
            engine.build_rounds(alerts), current, proposed, wait, cutoff
        )
    else:
        # 默认：两套规则各自完整模拟（含每段未静默区间）
        before = engine.simulate(alerts, current, wait, cutoff)
        after = engine.simulate(alerts, current + proposed, wait, cutoff)
        rows = engine.compare(before, after)

    simulation_id = _remember(
        SimulationInputs(
            alerts=alerts,
            current=current,
            proposed=proposed,
            wait=wait,
            cutoff=cutoff,
            wait_minutes=req.wait_minutes,
        )
    )
    return {
        "simulation_id": simulation_id,
        "summary_only": req.summary_only,
        "cutoff_at": engine.dt_iso(cutoff),
        "wait_minutes": req.wait_minutes,
        "ignored_alerts_after_cutoff": dropped,
        "summary": engine.summarize(rows),
        "rounds": rows,
    }


@app.post("/api/round-details")
def round_details(req: DetailsRequest):
    """展开某轮时补算两套规则下的完整未静默区间（沿用原预演输入）。"""
    inputs = _recall(req.simulation_id)
    rounds = engine.build_rounds(inputs.alerts)
    match = [
        r
        for r in rounds
        if r.service == req.service and r.round_index == req.round_index
    ]
    if not match:
        raise HTTPException(404, "该轮次不属于这次预演结果")

    round_ = match[0]
    before = engine.simulate_round(round_, inputs.current, inputs.wait, inputs.cutoff)
    after = engine.simulate_round(
        round_, inputs.current + inputs.proposed, inputs.wait, inputs.cutoff
    )
    return {
        "simulation_id": req.simulation_id,
        "service": round_.service,
        "round_index": round_.round_index,
        "before": _detail_payload(before),
        "after": _detail_payload(after),
    }


def _detail_payload(r: engine.RoundResult) -> dict:
    """展开明细只补未静默区间（首屏已给出首呼结论）。"""
    return {
        "unsilenced_stretches": [
            {
                "start": engine.dt_iso(s.start),
                "end": engine.dt_iso(s.end),
                "open_ended": s.open_ended,
            }
            for s in r.stretches
        ]
    }


@app.get("/api/sample")
def sample():
    return SAMPLE_PAYLOAD


@app.get("/")
def index():
    return FileResponse("app/static/index.html")


app.mount("/static", StaticFiles(directory="app/static"), name="static")
