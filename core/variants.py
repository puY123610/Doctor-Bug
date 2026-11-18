"""VAR1：指令段变体——按失败类别给派单的任务说明加一段对症提醒。

只加在任务说明里：派给谁（kb/lookup）、判卷怎么判都不动；变体段不进
考卷、不进冻结清单。未分类和「其他异常」不拼段、不编。

可追溯：同一节点前面某轮已经用过某段，后面各轮沿用同一段（即使这轮
抽到的类别变了），不悄悄换段；运行记录里每轮每个节点记一行
{node, kind, variant}，沿用时多带 carried_from（首次用段的轮次）。
"""

from __future__ import annotations

from acceptor.failure_kinds import ASSERTION, IMPORT, TIMEOUT, UNCLASSIFIED

# 类别 → (变体名, 段落正文)。只挂三类，理由见 ai/reports/VAR1.md。
VARIANTS: dict[str, tuple[str, str]] = {
    ASSERTION: (
        "断言对照",
        "【对症提醒：断言没对上】\n"
        "下面这些失败是断言错：测试期望的值和代码算出来的值不一样。请按顺序做：\n"
        "1. 先把失败输出里的期望值和实际值并排写出来，标出到底差在哪一项、哪一位。\n"
        "2. 顺着差的那一项往回找：是哪一行代码算出了这个值，它用的输入是什么。\n"
        "3. 只改算错的那一行；不许去改测试里的期望值。",
    ),
    IMPORT: (
        "导入排查",
        "【对症提醒：导入失败】\n"
        "下面这些失败是导入失败：测试连要用的模块或名字都没拿到。请按顺序做：\n"
        "1. 先看报错里缺的是哪个模块、哪个名字。\n"
        "2. 查这个文件在不在、路径和模块名拼写对不对、包目录下有没有 __init__.py、"
        "这个名字是不是被改名或删掉了。\n"
        "3. 确认是代码里写错了再改代码；缺的是第三方包就在回复里说明，"
        "不要自己装包，也不要改测试里的 import。",
    ),
    TIMEOUT: (
        "超时排查",
        "【对症提醒：超时】\n"
        "下面这些失败是超时：代码在规定时间内没跑完。请按顺序做：\n"
        "1. 先确认是不是死循环：循环的退出条件会不会永远不满足，递归有没有停下来的时候。\n"
        "2. 再看是不是数据量太大或重复干活：有没有在循环里一遍遍做同一件重活。\n"
        "3. 找到卡住的那一段再改；不许靠调大超时、跳过测试来过关。",
    ),
}

_NAME_TO_KIND = {name: kind for kind, (name, _text) in VARIANTS.items()}


def variant_text(name: str) -> str:
    kind = _NAME_TO_KIND.get(name)
    return VARIANTS[kind][1] if kind is not None else ""


def _first_use(node: str, prior_rounds: list[dict]) -> tuple[str, int] | None:
    for record in prior_rounds:
        for entry in record.get("variants") or []:
            if entry.get("node") == node and entry.get("variant"):
                return entry["variant"], int(record.get("round", 0))
    return None


def plan_variants(
    nodes: list[str],
    failure_kinds: dict[str, str],
    prior_rounds: list[dict],
) -> list[dict]:
    """给本轮分到的节点逐条选段。返回运行记录行（每节点一行）。"""
    entries: list[dict] = []
    for node in nodes:
        kind = failure_kinds.get(node, UNCLASSIFIED)
        earlier = _first_use(node, prior_rounds)
        if earlier is not None:
            entries.append(
                {
                    "node": node,
                    "kind": kind,
                    "variant": earlier[0],
                    "carried_from": earlier[1],
                }
            )
            continue
        name = VARIANTS[kind][0] if kind in VARIANTS else ""
        entries.append({"node": node, "kind": kind, "variant": name})
    return entries


def render_variant_note(entries: list[dict]) -> str:
    """把用到的段拼成一段任务说明附加文字；一段都没用到时返回空串。"""
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        name = entry.get("variant") or ""
        if not name or not variant_text(name):
            continue
        grouped.setdefault(name, []).append(entry["node"])
    blocks: list[str] = []
    for name, nodes in grouped.items():
        lines = [variant_text(name), "适用的失败 node id："]
        lines.extend(f"- {node}" for node in nodes)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def with_variant_note(prompt: str, note: str) -> str:
    """任务说明拼接点：有段才拼，没有就原样返回。"""
    return prompt + "\n\n" + note if note else prompt
