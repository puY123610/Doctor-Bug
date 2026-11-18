"""用量数据契约（TUIM1b / G 口径）：每次模型调用一条 UsageRecord。

后续侧边栏、状态弹窗都只读这里，不各自重算。

铁律：未知就是 None，绝不把未知记成 0。
- token 未知 → 对应字段 None，totals 里该字段不计入、missing 计数 +1；
- 价格或 token 未知 → cost 为 None，totals 里 unknown 计数 +1。
- 缓存 token（读/写）本单只记录、不计价。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

Source = Literal["provider", "estimate"]

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def compute_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    price_in: float | None,
    price_out: float | None,
) -> float | None:
    """in*price_in/1e6 + out*price_out/1e6；任一项未知返回 None（不当 0）。"""
    if None in (input_tokens, output_tokens, price_in, price_out):
        return None
    return input_tokens * price_in / 1e6 + output_tokens * price_out / 1e6


@dataclass(frozen=True)
class UsageRecord:
    """一次模型调用的用量；call_id 全局唯一，用于去重。"""

    call_id: str
    ts: float
    provider: str | None
    model: str | None
    effort: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    source: Source
    price_in_snapshot: float | None
    price_out_snapshot: float | None
    cost: float | None


class UsageAccumulator:
    """按 call_id 去重的累计器（同一调用的重复事件只记一次）。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, UsageRecord] = {}

    def add(self, record: UsageRecord) -> bool:
        """记录一条；该 call_id 已记过则忽略并返回 False。"""
        with self._lock:
            if record.call_id in self._records:
                return False
            self._records[record.call_id] = record
            return True

    def records(self) -> list[UsageRecord]:
        with self._lock:
            return list(self._records.values())

    def totals(self) -> dict:
        """累计：各 token 字段为已知值之和 + missing 计数；cost 为已知之和 + unknown 计数。"""
        records = self.records()
        out: dict = {"calls": len(records)}
        for field in _TOKEN_FIELDS:
            known = [getattr(r, field) for r in records if getattr(r, field) is not None]
            out[field] = {"sum": sum(known), "missing": len(records) - len(known)}
        costs = [r.cost for r in records if r.cost is not None]
        out["cost"] = {"sum": sum(costs), "unknown": len(records) - len(costs)}
        out["estimated_calls"] = sum(1 for r in records if r.source == "estimate")
        return out


# 进程内当前累计器（模块级登记/读取，供后续 TUI 读取）。
_CURRENT_LOCK = threading.Lock()
_CURRENT = UsageAccumulator()


def current_accumulator() -> UsageAccumulator:
    with _CURRENT_LOCK:
        return _CURRENT


def reset_current() -> UsageAccumulator:
    """换一个全新的当前累计器并返回它。"""
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = UsageAccumulator()
        return _CURRENT


def record_usage(record: UsageRecord) -> bool:
    """登记一条到当前累计器（按 call_id 去重）。"""
    return current_accumulator().add(record)


# ---------------------------------------------------------------------------
# G：侧边栏与 /status 共用的唯一读取口。两处都只调这里，数字不各自重算。
# ---------------------------------------------------------------------------
UNKNOWN_TEXT = "未知"
NO_CALLS_TEXT = "暂无调用"


@dataclass(frozen=True)
class UsageSummary:
    """本会话用量的展示口径（同一累计器同一时刻读出）。"""

    calls: int
    tokens_text: str
    cost_text: str
    estimated_calls: int

    def lines(self) -> list[str]:
        out = [
            f"调用 {self.calls} 次",
            f"token 合计 {self.tokens_text}",
            f"成本 {self.cost_text}",
        ]
        if self.estimated_calls:
            out.append(f"其中 {self.estimated_calls} 次为估算")
        return out

    def one_line(self) -> str:
        return "；".join(self.lines())


def summarize(acc: UsageAccumulator | None = None) -> UsageSummary:
    """厂商值与估算都按记录原样累计；未知不当 0；成本只用调用时快照算出的 cost。"""
    totals = (acc or current_accumulator()).totals()
    calls = int(totals.get("calls", 0))
    estimated = int(totals.get("estimated_calls", 0))
    if not calls:
        # 没有任何调用：不编数字，也不显示假的 0。
        return UsageSummary(0, NO_CALLS_TEXT, NO_CALLS_TEXT, 0)
    tin = totals["input_tokens"]
    tout = totals["output_tokens"]
    missing = max(tin["missing"], tout["missing"])
    if missing >= calls:
        tokens = UNKNOWN_TEXT
    else:
        tokens = f"{tin['sum'] + tout['sum']:,}"
        if missing:
            tokens += f"（{missing} 次{UNKNOWN_TEXT}）"
    cost = totals["cost"]
    if cost["unknown"] >= calls:
        cost_text = UNKNOWN_TEXT
    else:
        cost_text = f"${cost['sum']:.4f}"
        if cost["unknown"]:
            cost_text += f"（{cost['unknown']} 次{UNKNOWN_TEXT}）"
    if estimated:
        tokens += "（含估算）"
    return UsageSummary(calls, tokens, cost_text, estimated)


# ---------------------------------------------------------------------------
# USAGE1：运行记录（RunRecord.usage）与 CLI「Token 用量」行的读取口。
# 只读累计器、不改记录侧；数字与 summarize() 同一口径（未知不当 0、
# 估算照记并标出、按 call_id 去重、成本只用调用时快照）。
# ---------------------------------------------------------------------------
# record.usage 沿用的旧键名 → UsageRecord 字段。
RUN_USAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_write_tokens"),
)


def call_ids(acc: UsageAccumulator | None = None) -> frozenset[str]:
    """当前累计器里已记录的调用 ID（运行前记基线用）。"""
    return frozenset(r.call_id for r in (acc or current_accumulator()).records())


def accumulator_since(
    before: frozenset[str] | set[str],
    acc: UsageAccumulator | None = None,
) -> UsageAccumulator:
    """取基线之后新增的调用，装进一个独立累计器（仍按 call_id 去重）。"""
    out = UsageAccumulator()
    for record in (acc or current_accumulator()).records():
        if record.call_id not in before:
            out.add(record)
    return out


def run_usage_dict(acc: UsageAccumulator) -> dict:
    """生成 RunRecord.usage：旧键名不变，值改为统一口径。

    - 没有调用 → 空 dict（与旧行为一致：不写 usage）；
    - 某字段所有调用都未知 → None（不记 0）；部分未知 → 已知之和，
      未知次数记在 ``missing``；
    - 估算次数记在 ``estimated_calls``；成本为调用时快照之和，全未知为 None；
    - ``summary`` 即 summarize() 对同一批调用的一行文案。
    """
    totals = acc.totals()
    calls = int(totals.get("calls", 0))
    if not calls:
        return {}
    out: dict = {"calls": calls}
    missing: dict[str, int] = {}
    for key, field in RUN_USAGE_FIELDS:
        item = totals[field]
        out[key] = None if item["missing"] >= calls else item["sum"]
        if item["missing"]:
            missing[key] = item["missing"]
    if missing:
        out["missing"] = missing
    cost = totals["cost"]
    out["cost"] = None if cost["unknown"] >= calls else cost["sum"]
    if cost["unknown"]:
        out["cost_unknown"] = cost["unknown"]
    estimated = int(totals.get("estimated_calls", 0))
    if estimated:
        out["estimated_calls"] = estimated
    out["summary"] = summarize(acc).one_line()
    return out


NOT_CAPTURED_LINE = "Token 用量: 未捕获（模型未返回 usage）"


def _usage_value(usage: dict, key: str) -> str:
    value = usage.get(key)
    text = UNKNOWN_TEXT if value is None else str(value)
    gap = (usage.get("missing") or {}).get(key)
    if value is not None and gap:
        text += f"（{gap} 次{UNKNOWN_TEXT}）"
    return text


def format_run_usage_line(usage: dict | None) -> str:
    """CLI / 调度员收尾「Token 用量」行；全部已知时与旧格式逐字相同。"""
    if not isinstance(usage, dict) or not usage.get("calls"):
        return NOT_CAPTURED_LINE
    line = (
        "Token 用量: "
        f"输入 {_usage_value(usage, 'input_tokens')} / "
        f"输出 {_usage_value(usage, 'output_tokens')} / "
        f"缓存读 {_usage_value(usage, 'cache_input_tokens')} / "
        f"缓存写 {_usage_value(usage, 'cache_creation_input_tokens')} / "
        f"模型调用 {usage['calls']} 次"
    )
    if usage.get("estimated_calls"):
        line += f"（其中 {usage['estimated_calls']} 次为估算）"
    return line
