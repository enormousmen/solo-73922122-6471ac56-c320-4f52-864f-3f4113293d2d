"""静默规则预演引擎。

建模口径（与需求逐条对应）：

* 告警 ``fired_at`` 触发、``recovered_at`` 为空表示截至观察截止仍未恢复，
  活动区间按右开 ``[fired_at, recovered_at)`` 处理。
* 静默规则在 ``start_at`` 生效、在 ``end_at`` 解除；规则为“全标签匹配”，
  即告警标签包含规则列出的全部标签条件才被屏蔽。
* 同一服务：只要后一条告警在前一条仍活动时触发，就并入同一故障轮次；
  轮次直到该服务参与轮次的告警全部恢复才结束（静默不结束轮次）。
* 轮次内，只要该服务一直存在“未被静默的活动告警”，首呼等待就接着计时；
  未静默告警断档（被静默或全部恢复）后再次出现，则重新等待一个完整时长。
* 同一时刻的变化（触发/恢复/静默生效/解除）先一并应用，再判定计时与首呼。
* 每轮故障至多首呼一次；观察截止（含恰好落在截止点）仍未满足等待的轮次
  标记为“截至观察结束未首呼”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# 用带时区的极大时间表示“无限远/未恢复”，避免 naive/aware 混用比较。
INF = datetime.max.replace(tzinfo=timezone.utc)


def parse_dt(value: str | datetime) -> datetime:
    """解析 ISO-8601 时间；不带时区的时间按 UTC 处理并统一为 aware。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dt_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None or dt == INF:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Alert:
    id: str
    service: str
    labels: dict[str, str]
    fired_at: datetime
    recovered_at: Optional[datetime]  # None => 截至观察截止未恢复

    @property
    def end(self) -> datetime:
        return self.recovered_at if self.recovered_at is not None else INF

    def active_at(self, t: datetime) -> bool:
        return self.fired_at <= t < self.end

    def silenced_at(self, t: datetime, rules: Iterable["SilenceRule"]) -> bool:
        return any(rule.covers(self, t) for rule in rules)


@dataclass(frozen=True)
class SilenceRule:
    id: str
    labels: dict[str, str]
    start_at: datetime
    end_at: datetime  # 右开：[start_at, end_at)

    def matches(self, alert: Alert) -> bool:
        """全标签匹配：规则中的每个标签条件都必须出现在告警标签上。"""
        return all(alert.labels.get(k) == v for k, v in self.labels.items())

    def covers(self, alert: Alert, t: datetime) -> bool:
        return self.start_at <= t < self.end_at and self.matches(alert)


@dataclass
class Round:
    """同一服务的一轮故障（静默不切分轮次）。"""

    service: str
    round_index: int
    alerts: list[Alert] = field(default_factory=list)
    started_at: datetime = INF
    ended_at: Optional[datetime] = None  # None => 有告警未恢复，轮次未结束


@dataclass
class Stretch:
    """轮次内一段“连续存在未静默活动告警”的区间（右开）。"""

    start: datetime
    end: datetime
    open_ended: bool  # True => 因观察截止截断，真实终点未知


@dataclass
class RoundResult:
    service: str
    round_index: int
    started_at: datetime
    ended_at: Optional[datetime]
    alert_ids: list[str]
    stretches: list[Stretch]
    first_call_at: Optional[datetime]
    called_alert_ids: list[str]  # 随首呼发出的未静默活动告警
    still_waiting_at_cutoff: bool  # 截止时仍在等待（未首呼的原因之一）


@dataclass
class CallScan:
    """只有首屏结论的扫描结果（不保留未静默区间）。

    ``events_scanned`` 记录实际检查过状态的时点数；一旦首呼确定就停止，
    因此可能小于 ``events_total``（该轮全部告警/规则事件时点数）。
    """

    first_call_at: Optional[datetime]
    called_alert_ids: list[str]
    still_waiting_at_cutoff: bool
    events_total: int
    events_scanned: int


# --------------------------------------------------------------------------- #
# 轮次构建：纯告警驱动，与静默规则无关（两轮模拟共用同一套轮次）
# --------------------------------------------------------------------------- #


def build_rounds(alerts: list[Alert]) -> list[Round]:
    by_service: dict[str, list[Alert]] = {}
    for alert in alerts:
        by_service.setdefault(alert.service, []).append(alert)

    rounds: list[Round] = []
    for service in sorted(by_service):
        svc_alerts = sorted(by_service[service], key=lambda a: (a.fired_at, a.id))
        current: Optional[Round] = None
        for alert in svc_alerts:
            if current is None or alert.fired_at > current_horizon(current):
                if current is not None:
                    rounds.append(current)
                current = Round(
                    service=service,
                    round_index=(current.round_index + 1) if current else 1,
                    alerts=[alert],
                    started_at=alert.fired_at,
                )
            else:
                current.alerts.append(alert)
        if current is not None:
            rounds.append(current)

        # 轮次结束时间 = 全部告警恢复时间的最大值；任一未恢复 => 未结束
        for r in [r for r in rounds if r.service == service]:
            ends = [a.recovered_at for a in r.alerts if a.recovered_at is not None]
            if len(ends) < len(r.alerts):
                r.ended_at = None
            else:
                r.ended_at = max(ends)
    return rounds


def current_horizon(round_: Round) -> datetime:
    """当前轮次已纳入告警的最远活动边界（未恢复 => INF）。"""
    horizon = round_.started_at
    for a in round_.alerts:
        if a.end == INF:
            return INF
        horizon = max(horizon, a.end)
    return horizon


# --------------------------------------------------------------------------- #
# 未静默连续区间
# --------------------------------------------------------------------------- #


def rules_for_round(round_: Round, rules: list[SilenceRule]) -> list[SilenceRule]:
    """只保留能命中本轮回告警的规则。

    规则是否命中只看标签，与时间无关；命中不了任何一条告警的规则永远不会改变
    本轮“存在未静默活动告警”的状态，它的起止时刻也就不必作为事件时点参与切分。
    这是首屏提前结束扫描之外的又一处纯等价剪枝。
    """
    return [rule for rule in rules if any(rule.matches(a) for a in round_.alerts)]


def _active_unsilenced(
    round_: Round, rules: list[SilenceRule], t: datetime
) -> list[Alert]:
    return [
        a for a in round_.alerts if a.active_at(t) and not a.silenced_at(t, rules)
    ]


def _event_times(
    round_: Round, rules: list[SilenceRule], cutoff: datetime
) -> list[datetime]:
    """轮次内可能改变“未静默活动状态”的全部时点（严格早于截止）。"""
    times: set[datetime] = {round_.started_at}
    for a in round_.alerts:
        if a.fired_at < cutoff:
            times.add(a.fired_at)
        if a.recovered_at is not None and a.recovered_at < cutoff:
            times.add(a.recovered_at)
    for rule in rules:
        if rule.start_at < cutoff:
            times.add(rule.start_at)
        if rule.end_at < cutoff:
            times.add(rule.end_at)
    return sorted(t for t in times if t < cutoff)


def unsilenced_stretches(
    round_: Round, rules: list[SilenceRule], cutoff: datetime
) -> list[Stretch]:
    """把轮次投影到时间轴，切出“存在未静默活动告警”的连续区间。

    同刻事件先全部生效再判定：以事件时刻 t 的状态描述区间 [t, next_t)，
    因此触发、恢复、静默生效/解除落在同一时刻时自动合并处理。
    """
    rules = rules_for_round(round_, rules)
    event_times = _event_times(round_, rules, cutoff)
    if not event_times:
        return []

    stretches: list[Stretch] = []
    seg_start: Optional[datetime] = None

    def active_unsilenced(t: datetime) -> list[Alert]:
        return _active_unsilenced(round_, rules, t)

    for i, t in enumerate(event_times):
        present = active_unsilenced(t)
        next_t = event_times[i + 1] if i + 1 < len(event_times) else cutoff
        if present:
            if seg_start is None:
                seg_start = t
            if next_t == cutoff:
                # 截止点状态决定末段是否为开口段
                end_present = active_unsilenced(cutoff)
                if end_present:
                    stretches.append(Stretch(seg_start, cutoff, True))
                else:
                    stretches.append(Stretch(seg_start, cutoff, False))
                seg_start = None
        else:
            if seg_start is not None:
                stretches.append(Stretch(seg_start, t, False))
                seg_start = None
    if seg_start is not None:  # 理论兜底（正常在末段分支闭合）
        stretches.append(Stretch(seg_start, cutoff, True))
    return stretches


# --------------------------------------------------------------------------- #
# 单套规则下的轮次首呼判定
# --------------------------------------------------------------------------- #


def scan_round(
    round_: Round,
    rules: list[SilenceRule],
    wait: timedelta,
    cutoff: datetime,
) -> CallScan:
    """只为首屏求首呼结论，边扫描边判定，首呼一旦确定立即停止。

    与 :func:`simulate_round` 的结论完全一致，但不构建 ``Stretch`` 列表：
    按事件时点推进“连续未静默”段，段内凑满等待时长即给出首呼；确定后
    直接返回，不再检查该轮此后的任何事件时点（后面的告警触发/恢复、静默
    生效/解除都无法再改变“每轮只首呼一次”的结论）。
    """
    rules = rules_for_round(round_, rules)
    event_times = _event_times(round_, rules, cutoff)
    events_total = len(event_times)

    def hit(candidate: datetime, scanned: int) -> CallScan:
        return CallScan(
            first_call_at=candidate,
            called_alert_ids=[
                a.id for a in _active_unsilenced(round_, rules, candidate)
            ],
            still_waiting_at_cutoff=False,
            events_total=events_total,
            events_scanned=scanned,
        )

    seg_start: Optional[datetime] = None
    for i, t in enumerate(event_times):
        if seg_start is not None:
            candidate = seg_start + wait
            # 凑满等待的时刻早于下一个事件时点：无需再看 t 的状态，直接首呼。
            if candidate < cutoff and candidate < t:
                return hit(candidate, scanned=i)
        # 到此处必须检查 t 时刻的状态：第 i+1 个时点已扫描
        present = bool(_active_unsilenced(round_, rules, t))
        if seg_start is not None:
            candidate = seg_start + wait
            # 恰好在 t 凑满：仅当 t 时刻仍未静默才成立（同刻静默生效不算）
            if candidate < cutoff and candidate == t and present:
                return hit(candidate, scanned=i + 1)
            if not present:
                seg_start = None
        if present and seg_start is None:
            seg_start = t

    if seg_start is not None:
        candidate = seg_start + wait
        # 最后一段一直延续到观察截止：截止前凑满则首呼（开口段）
        if candidate < cutoff:
            return hit(candidate, scanned=events_total)

    return CallScan(
        first_call_at=None,
        called_alert_ids=[],
        # 只有“最后一个时点起一直未静默到截止”才算仍在等待；静默恰好在截止
        # 点解除时，未静默状态是截止点才出现的，尚未开始等待。
        still_waiting_at_cutoff=bool(
            seg_start is not None and _active_unsilenced(round_, rules, cutoff)
        ),
        events_total=events_total,
        events_scanned=events_total,
    )


def simulate_round(
    round_: Round,
    rules: list[SilenceRule],
    wait: timedelta,
    cutoff: datetime,
) -> RoundResult:
    """完整结果：先切出全部未静默区间，再由区间推导首呼结论。"""
    stretches = unsilenced_stretches(round_, rules, cutoff)
    first_call_at = _first_call_from(stretches, wait, cutoff)
    called_alerts = (
        _active_unsilenced(round_, rules_for_round(round_, rules), first_call_at)
        if first_call_at is not None
        else []
    )
    waiting = (
        first_call_at is None
        and bool(stretches)
        and stretches[-1].open_ended
    )
    return RoundResult(
        service=round_.service,
        round_index=round_.round_index,
        started_at=round_.started_at,
        ended_at=round_.ended_at,
        alert_ids=[a.id for a in round_.alerts],
        stretches=stretches,
        first_call_at=first_call_at,
        called_alert_ids=[a.id for a in called_alerts],
        still_waiting_at_cutoff=waiting,
    )


def _first_call_from(
    stretches: list[Stretch], wait: timedelta, cutoff: datetime
) -> Optional[datetime]:
    for stretch in stretches:
        candidate = stretch.start + wait
        # 必须在本段内满足等待；观察截止时刻本身不再判定首呼
        if candidate < cutoff and (stretch.open_ended or candidate < stretch.end):
            return candidate
    return None


def simulate(
    alerts: list[Alert],
    rules: list[SilenceRule],
    wait: timedelta,
    cutoff: datetime,
) -> list[RoundResult]:
    rounds = build_rounds(alerts)
    return [simulate_round(r, rules, wait, cutoff) for r in rounds]


# --------------------------------------------------------------------------- #
# 两套规则对照
# --------------------------------------------------------------------------- #


def compare(
    before: list[RoundResult], after: list[RoundResult]
) -> list[dict]:
    rows: list[dict] = []
    for b, a in zip(before, after):
        assert (b.service, b.round_index) == (a.service, a.round_index)
        rows.append(
            {
                "service": b.service,
                "round_index": b.round_index,
                "started_at": dt_iso(b.started_at),
                "ended_at": dt_iso(b.ended_at),
                "alert_ids": b.alert_ids,
                "verdict": _verdict(b.first_call_at, a.first_call_at),
                "delay_seconds": _delay(b.first_call_at, a.first_call_at),
                "before": _result_payload(b),
                "after": _result_payload(a),
            }
        )
    return rows


def _verdict(
    before_at: Optional[datetime], after_at: Optional[datetime]
) -> str:
    if before_at is None and after_at is None:
        return "still_not_called"
    if before_at is not None and after_at is None:
        return "suppressed"
    if before_at is None and after_at is not None:
        return "newly_called"
    assert before_at is not None and after_at is not None
    if after_at > before_at:
        return "delayed"
    if after_at < before_at:
        return "earlier"
    return "unchanged"


def _delay(
    before_at: Optional[datetime], after_at: Optional[datetime]
) -> Optional[int]:
    if before_at is None or after_at is None:
        return None
    return int((after_at - before_at).total_seconds())


# --------------------------------------------------------------------------- #
# 首屏扫描：只求结论，不保留区间
# --------------------------------------------------------------------------- #


def scan_rounds(
    rounds: list[Round],
    current_rules: list[SilenceRule],
    proposed_rules: list[SilenceRule],
    wait: timedelta,
    cutoff: datetime,
) -> list[dict]:
    """逐轮分别扫描两套规则，输出首屏所需结论（无未静默区间）。"""
    rows: list[dict] = []
    for round_ in rounds:
        before = scan_round(round_, current_rules, wait, cutoff)
        after = scan_round(
            round_, current_rules + proposed_rules, wait, cutoff
        )
        rows.append(
            {
                "service": round_.service,
                "round_index": round_.round_index,
                "started_at": dt_iso(round_.started_at),
                "ended_at": dt_iso(round_.ended_at),
                "alert_ids": [a.id for a in round_.alerts],
                "verdict": _verdict(
                    before.first_call_at, after.first_call_at
                ),
                "delay_seconds": _delay(
                    before.first_call_at, after.first_call_at
                ),
                "before": _scan_payload(before),
                "after": _scan_payload(after),
            }
        )
    return rows


def _scan_payload(scan: CallScan) -> dict:
    return {
        "first_call_at": dt_iso(scan.first_call_at),
        "called_alert_ids": scan.called_alert_ids,
        "still_waiting_at_cutoff": scan.still_waiting_at_cutoff,
        "scan": {
            "events_total": scan.events_total,
            "events_scanned": scan.events_scanned,
            "stopped_early": scan.events_scanned < scan.events_total,
        },
    }


def _result_payload(r: RoundResult) -> dict:
    return {
        "first_call_at": dt_iso(r.first_call_at),
        "called_alert_ids": r.called_alert_ids,
        "still_waiting_at_cutoff": r.still_waiting_at_cutoff,
        "unsilenced_stretches": [
            {
                "start": dt_iso(s.start),
                "end": dt_iso(s.end),
                "open_ended": s.open_ended,
            }
            for s in r.stretches
        ],
    }


def summarize(rows: list[dict]) -> dict:
    def count(key: str) -> int:
        return sum(1 for r in rows if r["verdict"] == key)

    before_called = sum(1 for r in rows if r["before"]["first_call_at"])
    after_called = sum(1 for r in rows if r["after"]["first_call_at"])
    return {
        "total_rounds": len(rows),
        "before_called": before_called,
        "after_called": after_called,
        "delayed": count("delayed"),
        "suppressed": count("suppressed"),
        "unchanged": count("unchanged"),
        "earlier": count("earlier"),
        "newly_called": count("newly_called"),
        "still_not_called": count("still_not_called"),
        "affected": sum(
            1
            for r in rows
            if r["verdict"] in ("delayed", "suppressed", "earlier", "newly_called")
        ),
        "before_waiting_at_cutoff": sum(
            1 for r in rows if r["before"]["still_waiting_at_cutoff"]
        ),
        "after_waiting_at_cutoff": sum(
            1 for r in rows if r["after"]["still_waiting_at_cutoff"]
        ),
        "not_called_after": sum(
            1 for r in rows if r["after"]["first_call_at"] is None
        ),
    }
