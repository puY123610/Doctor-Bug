"""可选模型目录：每个已接线厂商开放给 API key 的聊天模型清单。

数据来源：OpenCode 仓库内置的 models.dev 快照
（packages/core/src/models-dev/snapshot.txt，2026-11-16 拉取），
按本产品已接线的 8 个厂商精选聊天模型（图像/音频/审核等专用模型
不列）。每档 ``efforts`` 取 models.dev 的
capabilities.reasoning.effort 与本框架各厂商 reasoning_effort 阶梯
（core.model_config 的 _EFFORT_LADDERS，镜像 AgentScope 各
ChatModel Parameters 的 Literal 取值）的交集：目录里列出的每一档
都保证能真正传进请求；交集为空就是当前接线传不到（例如
DashScope 的 Parameters 没有 reasoning_effort 字段），不假装能选。

厂商键与 core.model_config 的 provider 键一致；models.dev 的
dashscope 对应其 alibaba 条目，openai_chat / openai_response
共用 openai 条目。

上下文 / 输出上限（token 数）与基础价（美元 / 每百万 token，不含
时段与区域乘数）同样取自 models.dev 快照 2026-11-16（TUIM1b）。
目录外的自定义模型没有这四列数据，展示层不许显示 0 或编造。
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CatalogModel:
    """目录里的一行：模型 id、展示名、看图/思考、 可选思考档位，
    以及上下文/输出上限（token）与基础入/出价（美元/每百万 token）。"""

    id: str
    name: str
    vision: bool
    thinking: bool
    efforts: tuple[str, ...] = ()
    context_limit: int | None = None
    output_limit: int | None = None
    price_in: float | None = None
    price_out: float | None = None


PROVIDER_MODELS: dict[str, tuple[CatalogModel, ...]] = {
    "deepseek": (
        CatalogModel("deepseek-flash", "DeepSeek V4.1 Flash 1017", True, True, ("high", "max")),
        CatalogModel("deepseek-v4-flash", "DeepSeek V4 Flash", True, True, ("high", "max")),
        CatalogModel("deepseek-v4-pro", "DeepSeek V4 Pro", False, True, ("high", "max")),
        CatalogModel("deepseek-v4-flash-vision-exp", "DeepSeek V4 Flash Vision Exp", True, True, ("high", "max")),
    ),
    "anthropic": (
        CatalogModel("claude-sonnet-5", "Claude Sonnet 5", True, True, ("low", "medium", "high", "xhigh", "max")),
        CatalogModel("claude-sonnet-4-6", "Claude Sonnet 4.6", True, True, ("low", "medium", "high", "max")),
        CatalogModel("claude-opus-4-8", "Claude Opus 4.8", True, True, ("low", "medium", "high", "xhigh", "max")),
        CatalogModel("claude-opus-4-7", "Claude Opus 4.7", True, True, ("low", "medium", "high", "xhigh", "max")),
        CatalogModel("claude-opus-4-5-20251101", "Claude Opus 4.5", True, True, ("low", "medium", "high")),
        CatalogModel("claude-haiku-4-5-20251001", "Claude Haiku 4.5", True, False, ()),
    ),
    "dashscope": (
        CatalogModel("qwen3.8-max", "Qwen3.8 Max", True, True, ()),
        CatalogModel("qwen3.7-plus", "Qwen3.7 Plus", True, False, ()),
        CatalogModel("qwen3.5-397b-a17b", "Qwen3.5 397B-A17B", True, False, ()),
        CatalogModel("qwen-vl-max", "Qwen-VL Max", True, False, ()),
        CatalogModel("qwen-plus", "Qwen Plus", False, False, ()),
    ),
    "minimax": (
        CatalogModel("MiniMax-M3.1-Flash", "MiniMax-M3.1-Flash", True, True, ()),
        CatalogModel("MiniMax-M3", "MiniMax-M3", True, False, ()),
        CatalogModel("MiniMax-M2.7", "MiniMax-M2.7", False, False, ()),
        CatalogModel("MiniMax-M2.5", "MiniMax-M2.5", False, False, ()),
    ),
    "moonshot": (
        CatalogModel("kimi-k3", "Kimi K3", True, True, ("low", "high", "max")),
        CatalogModel("kimi-k3.1-pro", "Kimi K3.1 Pro", True, True, ("low", "high", "max")),
        CatalogModel("kimi-k2.7-code", "Kimi K2.7 Code", True, False, ()),
        CatalogModel("kimi-k2.6", "Kimi K2.6", True, False, ()),
    ),
    "openai_chat": (
        CatalogModel("gpt-5.6", "GPT-5.6", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5.5", "GPT-5.5", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5.4", "GPT-5.4", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5.4-mini", "GPT-5.4 mini", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5-mini", "GPT-5 Mini", True, True, ("minimal", "low", "medium", "high")),
        CatalogModel("o3", "o3", True, True, ("low", "medium", "high")),
        CatalogModel("o4-mini", "o4-mini", True, True, ("low", "medium", "high")),
        CatalogModel("gpt-4.1", "GPT-4.1", True, False, ()),
        CatalogModel("gpt-4o", "GPT-4o", True, False, ()),
    ),
    "openai_response": (
        CatalogModel("gpt-5.6", "GPT-5.6", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5.5", "GPT-5.5", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5.4", "GPT-5.4", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5.4-mini", "GPT-5.4 mini", True, True, ("none", "low", "medium", "high", "xhigh")),
        CatalogModel("gpt-5-mini", "GPT-5 Mini", True, True, ("minimal", "low", "medium", "high")),
        CatalogModel("o3", "o3", True, True, ("low", "medium", "high")),
        CatalogModel("o4-mini", "o4-mini", True, True, ("low", "medium", "high")),
        CatalogModel("gpt-4.1", "GPT-4.1", True, False, ()),
        CatalogModel("gpt-4o", "GPT-4o", True, False, ()),
    ),
    "volcengine": (
        CatalogModel("doubao-seed-2-1-pro-260628", "Seed 2.1 Pro", True, True, ("minimal", "low", "medium", "high")),
        CatalogModel("doubao-seed-2-0-pro-260215", "Seed 2.0 Pro", True, True, ("minimal", "low", "medium", "high")),
        CatalogModel("doubao-seed-2-0-lite-260428", "Seed 2.0 Lite", True, True, ("minimal", "low", "medium", "high")),
        CatalogModel("doubao-seed-1-8-251228", "Seed 1.8", True, True, ("minimal", "low", "medium", "high")),
        CatalogModel("glm-5-2-260617", "GLM-5.2", False, True, ("minimal", "low", "medium", "high")),
        CatalogModel("doubao-seed-1-6-flash-250828", "Seed 1.6 Flash", True, False, ()),
    ),
}

# id: (上下文, 输出, 入价, 出价)；models.dev 快照 2026-11-16。
_LIMITS_AND_PRICES: dict[str, tuple[int, int, float, float]] = {
    "deepseek-flash": (1048576, 393216, 0.15, 0.6),
    "deepseek-v4-flash": (1048576, 393216, 0.15, 0.6),
    "deepseek-v4-pro": (1048576, 393216, 0.66, 1.98),
    "deepseek-v4-flash-vision-exp": (1048576, 393216, 0.15, 0.6),
    "claude-sonnet-5": (1000000, 128000, 2, 10),
    "claude-sonnet-4-6": (1000000, 128000, 3, 15),
    "claude-opus-4-8": (1000000, 128000, 5, 25),
    "claude-opus-4-7": (1000000, 128000, 5, 25),
    "claude-opus-4-5-20251101": (200000, 64000, 5, 25),
    "claude-haiku-4-5-20251001": (200000, 64000, 1, 5),
    "qwen3.8-max": (1000000, 131072, 2, 6),
    "qwen3.7-plus": (1000000, 131072, 0.4, 1.6),
    "qwen3.5-397b-a17b": (262144, 65536, 0.6, 3.6),
    "qwen-vl-max": (131072, 8192, 0.8, 3.2),
    "qwen-plus": (1000000, 32768, 0.4, 1.2),
    "MiniMax-M3.1-Flash": (1000000, 524288, 0.15, 0.6),
    "MiniMax-M3": (1000000, 524288, 0.3, 1.2),
    "MiniMax-M2.7": (204800, 131072, 0.3, 1.2),
    "MiniMax-M2.5": (204800, 131072, 0.3, 1.2),
    "kimi-k3": (1048576, 1048576, 3, 15),
    "kimi-k3.1-pro": (1048576, 1048576, 2, 10),
    "kimi-k2.7-code": (262144, 262144, 0.95, 4),
    "kimi-k2.6": (262144, 262144, 0.95, 4),
    "gpt-5.6": (1050000, 128000, 4, 20),
    "gpt-5.5": (1050000, 128000, 5, 30),
    "gpt-5.4": (1050000, 128000, 2.5, 15),
    "gpt-5.4-mini": (400000, 128000, 0.75, 4.5),
    "gpt-5-mini": (400000, 128000, 0.25, 2),
    "o3": (200000, 100000, 2, 8),
    "o4-mini": (200000, 100000, 1.1, 4.4),
    "gpt-4.1": (1047576, 32768, 2, 8),
    "gpt-4o": (128000, 16384, 2.5, 10),
    "doubao-seed-2-1-pro-260628": (256000, 256000, 0.8906, 4.45301),
    "doubao-seed-2-0-pro-260215": (256000, 128000, 0.47499, 2.37494),
    "doubao-seed-2-0-lite-260428": (256000, 131072, 0.08906, 0.53436),
    "doubao-seed-1-8-251228": (256000, 64000, 0.11875, 1.18747),
    "glm-5-2-260617": (1000000, 131072, 1.18747, 4.15615),
    "doubao-seed-1-6-flash-250828": (256000, 32000, 0.02227, 0.22265),
}


def _with_limits(row: CatalogModel) -> CatalogModel:
    data = _LIMITS_AND_PRICES.get(row.id)
    if data is None:
        return row
    ctx, out, p_in, p_out = data
    return replace(
        row,
        context_limit=ctx,
        output_limit=out,
        price_in=float(p_in),
        price_out=float(p_out),
    )


PROVIDER_MODELS = {
    provider: tuple(_with_limits(row) for row in rows)
    for provider, rows in PROVIDER_MODELS.items()
}


def format_price(value: float | None) -> str | None:
    """价格展示：最多 3 位小数、去尾零（0.8906 → 0.891，2 → 2）；未知返回 None。"""
    if value is None:
        return None
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def format_context(tokens: int | None) -> str | None:
    """上下文展示：≥100 万显示 1M，其余按千取整显示 NNNK；未知返回 None。"""
    if tokens is None:
        return None
    if tokens >= 1_000_000:
        return "1M"
    return f"{round(tokens / 1000)}K"


def format_price_pair(row: CatalogModel) -> str | None:
    """「$入/$出 每百万token」；任一价格未知返回 None（不许显示 0 或编造）。"""
    p_in = format_price(row.price_in)
    p_out = format_price(row.price_out)
    if p_in is None or p_out is None:
        return None
    return f"${p_in}/${p_out} 每百万token"


def models_for(provider: str) -> tuple[CatalogModel, ...]:
    """该厂商的目录模型（未知厂商返回空元组）。"""
    return PROVIDER_MODELS.get((provider or "").strip().lower(), ())


def catalog_model(provider: str, model_id: str) -> CatalogModel | None:
    """按厂商 + 模型 id 查目录行。"""
    for row in models_for(provider):
        if row.id == model_id:
            return row
    return None
