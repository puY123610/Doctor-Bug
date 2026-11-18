"""三层模型状态：用户默认、会话覆盖、生效解析。

会话覆盖只活在调用方持有的 :class:`SessionModelState` 里，不写任何文件。
丢掉这个对象（进程退出、新开应用）覆盖就没了。落盘仍走
``core.model_config.update_active_model`` / ``activate_role_profile``，
本模块不碰磁盘。

解析规则：``effective = 会话覆盖 ?? 用户默认``。覆盖是整组替换，
不是按字段合并。目录外的 model id 是自定义模型：照常可用，但思考
档位未知，``cycle_effort`` 拒绝假装能切。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import model_config
from .model_catalog import catalog_model

# 状态行覆盖标记、ctrl+t 落行、max 警告。文案是规格原文，测试钉这三句。
SESSION_MARKER = "· 本次会话"
CUSTOM_EFFORT_NOTICE = "该模型未在目录中，思考等级不可调"
MAX_EFFORT_WARNING = "max 档更费 token、响应更慢、可能想过头，难任务再用。"


class CustomModelError(Exception):
    """模型 id 不在目录里，思考等级控件必须停用。"""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__(
            f"{provider}/{model} is not in the catalog; effort is not adjustable"
        )


@dataclass(frozen=True)
class ModelTriple:
    """一组 (provider, model, effort)。effort 为 None 表示厂商默认。"""

    provider: str
    model: str
    effort: str | None = None

    def as_tuple(self) -> tuple[str, str, str | None]:
        return (self.provider, self.model, self.effort)


def user_default(path: str | None = None) -> ModelTriple:
    """配置文件里当前档案。新会话以它为准。不写文件。

    经模块属性取 ``load_config``，调用时才解析。模块级 ``from … import
    load_config`` 会在导入时绑死函数对象，既有测试对
    ``core.model_config.load_config`` 的补丁就打不中，状态行会读到真配置。
    """
    cfg = model_config.load_config(path)
    return ModelTriple(
        provider=(cfg.provider or "").strip().lower(),
        model=(cfg.model or "").strip(),
        effort=cfg.reasoning_effort,
    )


def resolve_effective(
    default: ModelTriple,
    override: ModelTriple | None,
) -> ModelTriple:
    """effective = 会话覆盖 ?? 用户默认。覆盖整组优先，没有就用默认。"""
    return override if override is not None else default


class SessionModelState:
    """一次应用进程里的会话覆盖。不落盘。"""

    def __init__(self) -> None:
        self.override: ModelTriple | None = None

    def clear(self) -> None:
        self.override = None

    def set_override(self, triple: ModelTriple) -> None:
        self.override = triple

    @property
    def active(self) -> bool:
        return self.override is not None

    def effective(self, path: str | None = None) -> ModelTriple:
        return resolve_effective(user_default(path), self.override)


def is_custom_model(provider: str, model: str) -> bool:
    """目录里没有这个 model id。可用，但思考等级不可调。"""
    return catalog_model(provider, model) is None


def effort_rungs(provider: str, model: str) -> tuple[str | None, ...] | None:
    """思考档位循环，含「默认」（None）。

    目录外返回 None（控件禁用）。目录内即使 efforts 为空也返回
    ``(None,)``：只有默认一档，不是自定义模型。
    """
    row = catalog_model(provider, model)
    if row is None:
        return None
    return (None, *row.efforts)


def cycle_effort(
    provider: str,
    model: str,
    current: str | None,
    *,
    step: int = 1,
) -> str | None:
    """沿目录档位走一格，含默认，到头回绕。

    当前值不在档位上时，从「默认」那一格再走。目录外抛
    :class:`CustomModelError`，不改任何状态。
    """
    rungs = effort_rungs(provider, model)
    if rungs is None:
        raise CustomModelError(provider, model)
    if current in rungs:
        index = rungs.index(current)
    else:
        index = 0
    return rungs[(index + step) % len(rungs)]


def effort_label(effort: str | None) -> str:
    """界面上的思考等级。None 与空串都显示「默认」，不编造档位名。"""
    if effort is None or not str(effort).strip():
        return "默认"
    return str(effort).strip()


def session_effort_line(effort: str | None) -> str:
    """ctrl+t 落在历史区的那一行。"""
    return f"思考等级：{effort_label(effort)}（仅本次会话）"
