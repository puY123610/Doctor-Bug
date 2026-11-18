"""Platform-owned model configuration for the native AgentScope actors.

The platform keeps *non-secret* model settings in ``model_config.json`` at the
repository root (see ``model_config.example.json``).  The API key is **never**
stored there: it is read at build time from the environment variable named by
``api_key_env`` (default ``DEEPSEEK_API_KEY``).

Security rules enforced by this module:

* no key value is stored on :class:`ModelConfig` / :class:`ModelProfile`,
  logged, or echoed in error messages (construction failures are redacted
  defensively);
* no file under ``~/.config/opencode`` is read — this module only reads the
  platform's own JSON files and ``os.environ``.

Two configuration shapes are supported:

* **legacy single config** (no ``profiles`` key): top-level ``provider`` /
  ``model`` / ``api_key_env`` / optional ``base_url`` / optional
  ``reasoning_effort``.  Behaviour is unchanged from previous releases.
* **profiles**: a non-empty ``profiles`` list of
  ``{name, provider, model, api_key_env, base_url?, reasoning_effort?}`` plus an
  optional ``active_profile``.  When ``profiles`` exists the active profile is
  the single source for :func:`load_config`; legacy top-level model fields are
  ignored.  Later config files replace earlier ones at top-level key
  granularity, so a user-level ``profiles`` list replaces the platform list
  entirely and a user-level ``active_profile`` selects among platform profiles.

Precedence: explicit function argument > environment override > JSON file >
default.  The legacy non-secret environment overrides are
``PLATFORM_MODEL_PROVIDER``, ``PLATFORM_MODEL`` and
``PLATFORM_MODEL_API_KEY_ENV``; ``PLATFORM_MODEL_PROFILE`` selects the active
profile when profiles are configured.

Supported providers are the AgentScope ChatModel implementations whose
credential shape is ``api_key + model + optional base_url`` and whose SDK is
installed in this environment: ``deepseek``, ``anthropic``, ``dashscope``,
``minimax``, ``moonshot``, ``openai_chat``, ``openai_response`` and
``volcengine``.  Gemini/Ollama/xAI are intentionally not wired here because
their SDKs are absent from the pinned environment (or their credential shape is
different); see :data:`SUPPORTED_PROVIDERS` and the MODEL1 report.
"""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from agentscope.credential import (
    AnthropicCredential,
    DashScopeCredential,
    DeepSeekCredential,
    MiniMaxCredential,
    MoonshotCredential,
    OpenAICredential,
    VolcengineCredential,
)
from agentscope.model import (
    AnthropicChatModel,
    ChatModelBase,
    DashScopeChatModel,
    DeepSeekChatModel,
    MiniMaxChatModel,
    MoonshotChatModel,
    OpenAIChatModel,
    OpenAIResponseModel,
    VolcengineChatModel,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PLATFORM_ROOT / "model_config.json"
DEFAULT_ENV_PATH = PLATFORM_ROOT / ".env.local"
EXAMPLE_CONFIG_PATH = PLATFORM_ROOT / "model_config.example.json"

DEFAULT_PROVIDER = "deepseek"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_PROFILE_NAME = "default"
DEFAULT_MAX_ROUNDS = 6

_ENV_PROVIDER = "PLATFORM_MODEL_PROVIDER"
_ENV_MODEL = "PLATFORM_MODEL"
_ENV_API_KEY_ENV = "PLATFORM_MODEL_API_KEY_ENV"
_ENV_PROFILE = "PLATFORM_MODEL_PROFILE"
_ENV_MAX_ROUNDS = "DRBUG_MAX_ROUNDS"
_TOKEN_METER_ENV = "DRBUG_TOKEN_METER"


@dataclass(frozen=True)
class _ProviderSpec:
    """One supported AgentScope credential/chat-model pair."""

    credential: str
    model: str
    reasoning_efforts: frozenset[str] = frozenset()


# Only providers whose SDK is installed here and whose credential shape is
# ``api_key + model + optional base_url`` are wired.  Gemini/Ollama/xAI are not
# in this table on purpose (missing google-genai / ollama / xai_sdk packages, or
# a different credential shape); see the module docstring and MODEL1 report.
_PROVIDER_SPECS: dict[str, _ProviderSpec] = {
    "deepseek": _ProviderSpec(
        "DeepSeekCredential",
        "DeepSeekChatModel",
        frozenset({"high", "max"}),
    ),
    "anthropic": _ProviderSpec(
        "AnthropicCredential",
        "AnthropicChatModel",
        frozenset({"low", "medium", "high", "xhigh", "max"}),
    ),
    "dashscope": _ProviderSpec("DashScopeCredential", "DashScopeChatModel"),
    "minimax": _ProviderSpec("MiniMaxCredential", "MiniMaxChatModel"),
    "moonshot": _ProviderSpec(
        "MoonshotCredential",
        "MoonshotChatModel",
        frozenset({"low", "high", "max"}),
    ),
    "openai_chat": _ProviderSpec(
        "OpenAICredential",
        "OpenAIChatModel",
        frozenset({"none", "minimal", "low", "medium", "high", "xhigh"}),
    ),
    "openai_response": _ProviderSpec(
        "OpenAICredential",
        "OpenAIResponseModel",
        frozenset({"none", "minimal", "low", "medium", "high", "xhigh"}),
    ),
    "volcengine": _ProviderSpec(
        "VolcengineCredential",
        "VolcengineChatModel",
        frozenset({"minimal", "low", "medium", "high"}),
    ),
}

SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(_PROVIDER_SPECS)

# Ordered effort ladders, weakest first.  The sets must stay equal to the
# ``reasoning_efforts`` frozensets in ``_PROVIDER_SPECS`` (a test pins
# this); the ladder adds the one thing a frozenset cannot say — order.
_EFFORT_LADDERS: dict[str, tuple[str, ...]] = {
    "deepseek": ("high", "max"),
    "anthropic": ("low", "medium", "high", "xhigh", "max"),
    "dashscope": (),
    "minimax": (),
    "moonshot": ("low", "high", "max"),
    "openai_chat": ("none", "minimal", "low", "medium", "high", "xhigh"),
    "openai_response": (
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ),
    "volcengine": ("minimal", "low", "medium", "high"),
}


def step_down_effort(provider: str, effort: str | None) -> str | None:
    """One rung down the provider's own effort ladder.

    Returns ``None`` for "keep the current value": no effort set, the
    provider has no ladder, the value is not on the ladder, it is
    already on the lowest rung, or the next rung down would switch
    thinking off (``none``/``off``) — stepping down must never do that.
    """
    if effort is None:
        return None
    ladder = _EFFORT_LADDERS.get((provider or "").strip().lower())
    if not ladder:
        return None
    normalized = effort.strip().lower()
    if normalized not in ladder:
        return None
    index = ladder.index(normalized)
    if index == 0:
        return None
    lower = ladder[index - 1]
    if lower in {"none", "off"}:
        return None
    return lower


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model id can actually do, as far as this platform knows.

    Unknown ids use the conservative default (no vision, no thinking) so the
    TUI never pretends a model can see or think.  ``basis`` records why the
    known rows are set the way they are; it is not sent to any provider.
    """

    vision: bool = False
    thinking: bool = False
    basis: str = (
        "未列入能力表：按不能看图、不产思考处理，避免把能力写死成真。"
    )


# Model ids, not providers.  TUI and the dispatcher read this table; they do
# not hard-code deepseek-flash in the interface.
_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    "deepseek-flash": ModelCapabilities(
        vision=True,
        thinking=True,
        basis=(
            "编排者已实测：DeepSeek 官方 API 的 deepseek-flash 接受 "
            "text+image，响应含 reasoning。AgentScope 2.0.11 "
            "DeepSeekChatFormatter 的类注释同样写明 image input is only "
            "supported by deepseek-flash。"
        ),
    ),
    "deepseek-chat": ModelCapabilities(
        vision=False,
        thinking=False,
        basis=(
            "旧模型 id。AgentScope 2.0.11 DeepSeekChatFormatter.input_types "
            "默认只有 text/plain，类注释写明图片只属于 deepseek-flash，故 "
            "vision=false。官方把带 reasoning_content 的模型另称为 "
            "deepseek-reasoner，deepseek-chat 不产思考，故 thinking=false。"
        ),
    ),
    "deepseek-reasoner": ModelCapabilities(
        vision=False,
        thinking=True,
        basis=(
            "旧模型 id。图片同样只在 deepseek-flash（同上 formatter 注释），"
            "故 vision=false。DeepSeek 官方 reasoner 的响应含 "
            "reasoning_content，AgentScope DeepSeekChatModel 将其解析为 "
            "ThinkingBlock，故 thinking=true。"
        ),
    ),
    "deepseek-v4-flash": ModelCapabilities(
        vision=True,
        thinking=True,
        basis=(
            "models.dev 快照（经 OpenCode 仓库，2026-11-16 拉取）："
            "输入模态含 image、reasoning.effort 为 low/high/max。"
            "未经真 key 实测，选它后看图以厂商实际响应为准。"
        ),
    ),
    "deepseek-v4-pro": ModelCapabilities(
        vision=False,
        thinking=True,
        basis=(
            "models.dev 快照（经 OpenCode 仓库，2026-11-16 拉取）："
            "输入模态只有 text、reasoning.effort 为 low/high/max。"
            "未经真 key 实测。"
        ),
    ),
    "deepseek-v4-flash-vision-exp": ModelCapabilities(
        vision=True,
        thinking=True,
        basis=(
            "models.dev 快照（经 OpenCode 仓库，2026-11-16 拉取）："
            "输入模态含 image、reasoning.effort 为 low/high/max。"
            "实验版，未经真 key 实测。"
        ),
    ),
}


def model_capabilities(model: str) -> ModelCapabilities:
    """Return the capability row for a model id.

    The lookup is the exact model string (no provider prefix).  Missing ids
    get the conservative default.
    """
    return _MODEL_CAPABILITIES.get((model or "").strip(), ModelCapabilities())


@dataclass(frozen=True)
class ModelConfig:
    """Non-secret model settings.

    ``api_key_env`` is the *name* of the environment variable that holds the
    key, never the key itself.  Keeping the secret out of this object means a
    stray ``repr(config)`` or log line cannot leak it.
    """

    provider: str = DEFAULT_PROVIDER
    model: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    source: str = ""
    profile: str = DEFAULT_PROFILE_NAME
    base_url: str | None = None
    reasoning_effort: str | None = None

    def to_profile(self) -> "ModelProfile":
        """Return the equivalent profile (used by the picker/CLI)."""
        return ModelProfile(
            name=self.profile or DEFAULT_PROFILE_NAME,
            provider=self.provider,
            model=self.model,
            api_key_env=self.api_key_env,
            base_url=self.base_url,
            reasoning_effort=self.reasoning_effort,
        )


@dataclass(frozen=True)
class ModelProfile:
    """A named, non-secret model profile."""

    name: str
    provider: str
    model: str
    api_key_env: str
    base_url: str | None = None
    reasoning_effort: str | None = None
    auto_generated: bool = False

    def to_config(self, source: str = "") -> ModelConfig:
        return ModelConfig(
            provider=self.provider,
            model=self.model,
            api_key_env=self.api_key_env,
            source=source,
            profile=self.name,
            base_url=self.base_url,
            reasoning_effort=self.reasoning_effort,
        )


@dataclass(frozen=True)
class ProfileCatalog:
    """All configured profiles plus the active name."""

    profiles: tuple[ModelProfile, ...]
    active: str
    source: str
    explicit: bool

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(profile.name for profile in self.profiles)

    def active_profile(self) -> ModelProfile:
        for profile in self.profiles:
            if profile.name == self.active:
                return profile
        raise ValueError(
            f"active profile {self.active!r} is not defined; available: "
            f"{', '.join(self.names)}"
        )


def key_is_set(profile: ModelConfig | ModelProfile) -> bool:
    """Report whether the profile's key env is non-empty (never its value)."""
    return bool(os.environ.get(profile.api_key_env, "").strip())


def user_config_dir() -> Path:
    """User-level configuration directory (``~/.config/drbug`` by default)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "drbug"


def user_config_path() -> Path:
    return user_config_dir() / "model_config.json"


def user_env_path() -> Path:
    return user_config_dir() / ".env"


def config_source_paths() -> list[Path]:
    """Config JSON files in increasing precedence order (platform, then user)."""
    return [Path(DEFAULT_CONFIG_PATH), user_config_path()]


def env_source_paths() -> list[Path]:
    """Env files in increasing precedence order (user, then legacy platform)."""
    return [user_env_path(), Path(DEFAULT_ENV_PATH)]


def _env_override(name: str, fallback: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else fallback


def _read_str_field(data: dict, key: str, default: str, source: str) -> str:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"field {key!r} in {source} must be a non-empty string"
        )
    return value.strip()


def _missing_model_message(source: str) -> str:
    return (
        f"model is not configured (source: {source}); create "
        f"{user_config_path()} with "
        '{"provider": "deepseek", "model": "<exact model id>"}, or copy '
        f"{EXAMPLE_CONFIG_PATH.name} to {DEFAULT_CONFIG_PATH.name} in the "
        'platform directory and set "model" to the exact model id for the '
        "chosen provider. No default model is assumed."
    )


def _read_json_file(config_path: Path) -> dict:
    source = str(config_path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"cannot read model config {source}: {exc.strerror or exc}"
        ) from None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"model config {source} is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(loaded, dict):
        raise ValueError(f"model config {source} must contain a JSON object")
    return loaded


def _merge_config_files(paths: list[Path]) -> tuple[dict, list[str]]:
    """Read existing JSON files in order; later files override earlier ones."""
    data: dict = {}
    used: list[str] = []
    for config_path in paths:
        if not config_path.exists():
            continue
        data.update(_read_json_file(config_path))
        used.append(str(config_path))
    return data, used


def _read_required_str_field(data: dict, key: str, source: str) -> str:
    if key not in data:
        raise ValueError(f"field {key!r} in {source} is required")
    return _read_str_field(data, key, "", source)


def _read_optional_str_field(
    data: dict, key: str, source: str
) -> str | None:
    """Read an optional non-empty string field; ``None``/absent means unset."""
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"field {key!r} in {source} must be a non-empty string when set"
        )
    return value.strip()


def _validate_reasoning_effort(
    provider: str, effort: str, where: str
) -> str:
    """Validate the provider/effort combination; return the normalized value."""
    normalized = effort.strip().lower()
    spec = _PROVIDER_SPECS.get(provider)
    if spec is None:
        raise ValueError(
            f"{where}: provider {provider!r} is not supported, so "
            "reasoning_effort cannot be applied; supported providers: "
            f"{', '.join(SUPPORTED_PROVIDERS)}"
        )
    if not spec.reasoning_efforts:
        raise ValueError(
            f"{where}: provider {provider!r} does not support "
            "reasoning_effort; remove the field"
        )
    if normalized not in spec.reasoning_efforts:
        allowed = ", ".join(sorted(spec.reasoning_efforts))
        raise ValueError(
            f"{where}: reasoning_effort {normalized!r} is not supported by "
            f"provider {provider!r}; allowed values: {allowed}"
        )
    return normalized


def _load_merged(path: str | Path | None) -> tuple[dict, str]:
    if path is not None:
        paths = [Path(path)]
    else:
        paths = config_source_paths()
    data, used = _merge_config_files(paths)
    source = " + ".join(used) if used else "默认值（未找到配置文件）"
    return data, source


def _read_model_value(data: dict, source: str) -> str:
    model_value = data.get("model", "")
    if model_value is None:
        model_value = ""
    if not isinstance(model_value, str):
        raise ValueError(f"field 'model' in {source} must be a string")
    return model_value.strip()


def _legacy_config(data: dict, source: str) -> ModelConfig:
    """Old single-config shape; behaviour is unchanged when profiles are absent."""
    provider = _read_str_field(data, "provider", DEFAULT_PROVIDER, source)
    api_key_env = _read_str_field(
        data, "api_key_env", DEFAULT_API_KEY_ENV, source
    )
    model = _read_model_value(data, source)

    # Keep the historical spelling in ModelConfig; build_model() lowercases
    # when dispatching, exactly as before profiles existed.
    provider = _env_override(_ENV_PROVIDER, provider)
    model = _env_override(_ENV_MODEL, model)
    api_key_env = _env_override(_ENV_API_KEY_ENV, api_key_env)

    base_url = _read_optional_str_field(data, "base_url", source)
    reasoning_effort = _read_optional_str_field(
        data, "reasoning_effort", source
    )
    if reasoning_effort is not None:
        reasoning_effort = _validate_reasoning_effort(
            provider.strip().lower(),
            reasoning_effort,
            f"model config {source}",
        )

    if not model:
        raise ValueError(_missing_model_message(source))

    return ModelConfig(
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        source=source,
        profile=DEFAULT_PROFILE_NAME,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
    )


def _parse_profile_entry(entry: Any, index: int, source: str) -> ModelProfile:
    where = f"profiles[{index}] in {source}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where} must be a JSON object")
    for forbidden in ("api_key", "api_key_value", "secret", "token"):
        if forbidden in entry:
            raise ValueError(
                f"{where} must not contain {forbidden!r}: store only an "
                "environment variable name in 'api_key_env' (never the key)"
            )

    name = _read_required_str_field(entry, "name", where)
    provider = _read_required_str_field(entry, "provider", where)
    model = _read_required_str_field(entry, "model", where)
    api_key_env = _read_required_str_field(entry, "api_key_env", where)
    provider = provider.strip().lower()

    if provider not in _PROVIDER_SPECS:
        raise ValueError(
            f"{where}: unsupported provider {provider!r}; supported: "
            f"{', '.join(SUPPORTED_PROVIDERS)}"
        )

    base_url = _read_optional_str_field(entry, "base_url", where)
    reasoning_effort = _read_optional_str_field(
        entry, "reasoning_effort", where
    )
    if reasoning_effort is not None:
        reasoning_effort = _validate_reasoning_effort(
            provider, reasoning_effort, where
        )

    auto_generated = entry.get("auto_generated", False)
    if not isinstance(auto_generated, bool):
        raise ValueError(f"{where}: 'auto_generated' must be a boolean")

    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
        auto_generated=auto_generated,
    )


def _catalog_from_data(data: dict, source: str) -> ProfileCatalog:
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError(
            f"field 'profiles' in {source} must be a non-empty JSON array"
        )

    profiles: list[ModelProfile] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_profiles):
        profile = _parse_profile_entry(entry, index, source)
        if profile.name in seen:
            raise ValueError(
                f"duplicate profile name {profile.name!r} in {source}"
            )
        seen.add(profile.name)
        profiles.append(profile)

    active = _read_optional_str_field(data, "active_profile", source)
    active = _env_override(_ENV_PROFILE, active or "") or profiles[0].name
    if active not in seen:
        raise ValueError(
            f"active_profile {active!r} in {source} is not defined; "
            f"available: {', '.join(profile.name for profile in profiles)}"
        )
    return ProfileCatalog(
        profiles=tuple(profiles),
        active=active,
        source=source,
        explicit=True,
    )


def load_profile_catalog(path: str | Path | None = None) -> ProfileCatalog:
    """Load every configured profile and the active name.

    Later config files override earlier ones at top-level key granularity
    (``dict.update``), so ``profiles`` is replaced wholesale by the user-level
    file and ``active_profile`` can be set independently.  Without a
    ``profiles`` key the legacy single config becomes one implicit profile
    named ``default``; that keeps every existing call site working verbatim.

    Raises:
        ValueError: invalid JSON/shape, duplicate names, unknown provider,
            unsupported ``reasoning_effort``, or an unknown active profile.
    """
    data, source = _load_merged(path)
    if "profiles" in data:
        return _catalog_from_data(data, source)
    cfg = _legacy_config(data, source)
    return ProfileCatalog(
        profiles=(cfg.to_profile(),),
        active=cfg.profile,
        source=source,
        explicit=False,
    )


def load_config(path: str | Path | None = None) -> ModelConfig:
    """Load the active model settings (legacy config or profiles).

    Without an explicit *path* the platform file is read first and the
    user-level file (``~/.config/drbug/model_config.json``) overrides it
    field by field.  The API key value never lives in either file.

    Args:
        path: single JSON file to read (legacy/test hook).  Defaults to the
            platform + user-level search order.  Missing files are allowed;
            they simply contribute no fields.

    Raises:
        ValueError: the file is unreadable/invalid, a field has the wrong type,
            ``model`` is missing, or profile validation fails.  Messages never
            contain secret values.
    """
    data, source = _load_merged(path)
    if "profiles" in data:
        return _catalog_from_data(data, source).active_profile().to_config(
            source
        )
    return _legacy_config(data, source)


def resolve_profile(
    name: str, path: str | Path | None = None
) -> ModelProfile:
    """Return one configured profile by exact name, or raise ``ValueError``."""
    catalog = load_profile_catalog(path)
    for profile in catalog.profiles:
        if profile.name == name:
            return profile
    raise ValueError(
        f"model profile {name!r} not found; available: "
        f"{', '.join(catalog.names)}"
    )


def activate_profile(
    name: str, *, config_path: str | Path | None = None
) -> Path:
    """Persist ``active_profile`` into the user-level config file.

    Only the non-secret ``active_profile`` key is written; every other key in
    the user file is preserved, and no key value is ever copied there.  The
    target profile must exist in the catalog of ``config_path`` when that
    path is given, otherwise in the merged platform + user view.

    Returns:
        The path that was written.
    """
    catalog = (
        load_profile_catalog(config_path)
        if config_path is not None
        else load_profile_catalog()
    )
    if name not in catalog.names:
        raise ValueError(
            f"model profile {name!r} not found; available: "
            f"{', '.join(catalog.names)}"
        )

    target = Path(config_path) if config_path is not None else user_config_path()
    data: dict = {}
    if target.is_file():
        data = _read_json_file(target)
    data["active_profile"] = name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def update_active_model(
    provider: str,
    model: str,
    reasoning_effort: str | None,
    *,
    config_path: str | Path | None = None,
) -> "ModelProfile":
    """把当前档案的厂商 / 型号 / 思考等级写进用户级配置文件。

    只写非密钥字段，其余键原样保留；``reasoning_effort`` 为 None 时
    删除该键（回到厂商默认）。若文件里有 ``profiles`` 对象且含当前
    档案名，同步更新该条目，保证两种存法读到的都是新值。写入后
    回读合并视图，返回解析出的当前档案。
    """
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if provider not in _PROVIDER_SPECS:
        raise ValueError(
            f"provider {provider!r} is not implemented; currently "
            f"supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    if not model:
        raise ValueError("model is empty")
    if reasoning_effort is not None:
        reasoning_effort = _validate_reasoning_effort(
            provider, reasoning_effort, "update_active_model"
        )

    target = Path(config_path) if config_path is not None else user_config_path()
    data: dict = {}
    if target.is_file():
        data = _read_json_file(target)
    data["provider"] = provider
    data["model"] = model
    if reasoning_effort is None:
        data.pop("reasoning_effort", None)
    else:
        data["reasoning_effort"] = reasoning_effort
    profiles = data.get("profiles")
    active = data.get("active_profile") or DEFAULT_PROFILE_NAME
    if isinstance(profiles, list):
        for entry in profiles:
            if isinstance(entry, dict) and entry.get("name") == active:
                entry["provider"] = provider
                entry["model"] = model
                if reasoning_effort is None:
                    entry.pop("reasoning_effort", None)
                else:
                    entry["reasoning_effort"] = reasoning_effort
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)

    catalog = (
        load_profile_catalog(config_path)
        if config_path is not None
        else load_profile_catalog()
    )
    for profile in catalog.profiles:
        if profile.name == catalog.active:
            return profile
    raise ValueError(f"active profile {catalog.active!r} vanished after update")


_ROLE_NAMES = ("secretary", "worker")


def load_role_profiles(
    path: str | Path | None = None,
) -> dict[str, str]:
    """The optional ``role_profiles`` mapping (``{}`` when unconfigured).

    Keys must be a subset of ``{"secretary", "worker"}`` and values
    non-empty profile names; anything else is a ``ValueError`` — a
    typo'd mapping must fail loudly, never silently fall back.
    """
    data, source = _load_merged(path)
    raw = data.get("role_profiles")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"field 'role_profiles' in {source} must be a JSON object"
        )
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _ROLE_NAMES:
            raise ValueError(
                f"role_profiles key {key!r} in {source} is not one of "
                f"{', '.join(_ROLE_NAMES)}"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"role_profiles[{key!r}] in {source} must be a "
                "non-empty profile name"
            )
        mapping[key] = value.strip()
    return mapping


def _profile_to_dict(profile: ModelProfile) -> dict:
    """Serialize one profile with non-secret fields only."""
    data: dict = {
        "name": profile.name,
        "provider": profile.provider,
        "model": profile.model,
        "api_key_env": profile.api_key_env,
    }
    if profile.base_url:
        data["base_url"] = profile.base_url
    if profile.reasoning_effort:
        data["reasoning_effort"] = profile.reasoning_effort
    if profile.auto_generated:
        data["auto_generated"] = True
    return data


def _write_config_data(data: dict, target: Path) -> Path:
    """Atomically write a config dict (same discipline as activate)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def ensure_worker_profile(
    path: str | Path | None = None,
) -> ModelProfile:
    """Return the worker profile, generating the default one if needed.

    Only runs when a ``role_profiles`` mapping exists but names no
    worker.  The generated profile clones the active profile with the
    reasoning effort stepped one rung down the provider's own ladder
    (floor: lowest rung and thinking-off rungs are never crossed).
    It is persisted — marked ``auto_generated`` — into the target
    config file together with the completed mapping, so the boss can
    see exactly which profile the workers run and how deep it thinks.
    Idempotent: once the mapping names a worker, nothing is rewritten.
    """
    catalog = load_profile_catalog(path)
    mapping = load_role_profiles(path)
    named = mapping.get("worker")
    if named:
        return resolve_profile(named, path)

    base = catalog.active_profile()
    stepped = step_down_effort(base.provider, base.reasoning_effort)
    effort = stepped if stepped is not None else base.reasoning_effort
    by_name = {profile.name: profile for profile in catalog.profiles}

    name = f"{base.name}-worker"
    existing = by_name.get(name)
    if (
        existing is not None
        and not existing.auto_generated
        and (existing.provider, existing.model) != (base.provider, base.model)
    ):
        # The boss owns a different hand-made profile with that name;
        # never overwrite it — take the -auto name instead.
        name = f"{base.name}-worker-auto"
        existing = by_name.get(name)
        if existing is not None and not existing.auto_generated:
            raise ValueError(
                f"cannot auto-generate the worker profile: both "
                f"{base.name + '-worker'!r} and {name!r} already exist "
                "as hand-made profiles; set role_profiles.worker "
                "explicitly"
            )

    worker = ModelProfile(
        name=name,
        provider=base.provider,
        model=base.model,
        api_key_env=base.api_key_env,
        base_url=base.base_url,
        reasoning_effort=effort,
        auto_generated=True,
    )
    profiles = [
        worker if profile.name == name else profile
        for profile in catalog.profiles
    ]
    if name not in by_name:
        profiles.append(worker)

    target = Path(path) if path is not None else user_config_path()
    data: dict = {}
    if target.is_file():
        data = _read_json_file(target)
    data["profiles"] = [_profile_to_dict(p) for p in profiles]
    data["role_profiles"] = {**mapping, "worker": name}
    _write_config_data(data, target)
    return worker


def resolve_role_profile(
    role: str,
    path: str | Path | None = None,
    *,
    generate: bool = True,
) -> ModelProfile:
    """The profile a role actually runs.

    No mapping → the active profile for both roles (pre-MODEL2
    behaviour, verbatim).  With a mapping: a named role resolves to
    its profile (unknown name → ``ValueError``, never a silent
    fallback); an unnamed secretary falls back to the active profile;
    an unnamed worker is auto-generated (``generate=False`` returns
    the active profile instead and writes nothing — display paths).
    """
    if role not in _ROLE_NAMES:
        raise ValueError(
            f"unknown role {role!r}; expected one of "
            f"{', '.join(_ROLE_NAMES)}"
        )
    catalog = load_profile_catalog(path)
    mapping = load_role_profiles(path)
    if not mapping:
        return catalog.active_profile()
    named = mapping.get(role)
    if named:
        return resolve_profile(named, path)
    if role == "secretary":
        return catalog.active_profile()
    if not generate:
        return catalog.active_profile()
    return ensure_worker_profile(path)


def effective_role_profiles(
    path: str | Path | None = None,
) -> dict:
    """Display summary of the role mapping; never writes anything.

    ``worker`` is ``None`` when a mapping exists but the worker
    profile has not been generated yet (it appears on first use).
    """
    catalog = load_profile_catalog(path)
    mapping = load_role_profiles(path)
    if not mapping:
        return {
            "mapped": False,
            "secretary": catalog.active,
            "worker": catalog.active,
        }
    return {
        "mapped": True,
        "secretary": mapping.get("secretary") or catalog.active,
        "worker": mapping.get("worker"),
    }


def activate_role_profile(
    name: str, *, config_path: str | Path | None = None
) -> Path:
    """Persist a /model switch.

    With a ``role_profiles`` mapping the switch moves only the
    secretary entry (the worker profile is untouched and
    ``active_profile`` is left alone); without a mapping it is
    exactly :func:`activate_profile`.
    """
    catalog = (
        load_profile_catalog(config_path)
        if config_path is not None
        else load_profile_catalog()
    )
    if name not in catalog.names:
        raise ValueError(
            f"model profile {name!r} not found; available: "
            f"{', '.join(catalog.names)}"
        )
    mapping = load_role_profiles(config_path)
    if not mapping:
        return activate_profile(name, config_path=config_path)
    target = (
        Path(config_path)
        if config_path is not None
        else user_config_path()
    )
    data: dict = {}
    if target.is_file():
        data = _read_json_file(target)
    data["role_profiles"] = {**mapping, "secretary": name}
    return _write_config_data(data, target)


def config_source_lines() -> list[str]:
    """Describe config/env file locations without reading any secret value."""
    lines = []
    for label, config_path in (
        ("用户配置", user_config_path()),
        ("平台兼容配置", Path(DEFAULT_CONFIG_PATH)),
    ):
        state = "存在" if config_path.is_file() else "未找到"
        lines.append(f"{label}: {config_path}（{state}）")
    for label, env_path in (
        ("用户密钥文件", user_env_path()),
        ("平台兼容密钥文件", Path(DEFAULT_ENV_PATH)),
    ):
        state = "存在" if env_path.is_file() else "未找到"
        lines.append(f"{label}: {env_path}（{state}）")
    return lines


def load_local_env(path: str | Path | None = None) -> None:
    """Load ``KEY=VALUE`` pairs into ``os.environ``.

    Default order is the user-level file (``~/.config/drbug/.env``) followed
    by the legacy ``platform/.env.local``; the first file to define a key wins
    and real environment variables always win.  A missing or unreadable file
    is a silent no-op.  Blank lines and ``#`` comments are ignored; an optional
    ``export `` prefix and matching single/double quotes around the value are
    stripped.  Values may contain ``=``.

    Nothing is printed, logged, or returned — callers only observe the
    environment effects.  This keeps the secret values out of any console or
    report even when the file is present.

    Args:
        path: single file to read (legacy/test hook).  Defaults to the user +
            platform search order above.
    """
    env_paths = [Path(path)] if path is not None else env_source_paths()
    for env_path in env_paths:
        try:
            if not env_path.is_file():
                continue
            text = env_path.read_text(encoding="utf-8")
        except OSError:
            continue

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or not key.isidentifier():
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


def load_max_rounds(default: int = DEFAULT_MAX_ROUNDS) -> int:
    """Per-task repair-round cap from config JSON or ``DRBUG_MAX_ROUNDS``.

    The value is a non-negative integer.  Invalid values raise ``ValueError``
    so the CLI can report them instead of silently falling back.
    """
    raw_env = os.environ.get(_ENV_MAX_ROUNDS, "").strip()
    source = f"环境变量 {_ENV_MAX_ROUNDS}"
    if raw_env:
        raw = raw_env
    else:
        data, used = _merge_config_files(config_source_paths())
        source = " + ".join(used) if used else "默认值"
        raw = data.get("max_rounds", default)

    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError(f"max_rounds in {source} must be an integer")
    try:
        rounds = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"max_rounds in {source} must be an integer") from None
    if rounds < 0:
        raise ValueError(f"max_rounds in {source} must be >= 0")
    return rounds


def _redact(text: str, secret: str | None) -> str:
    """Remove a secret value from text before it can reach an exception."""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


# ---------------------------------------------------------------------------
# Token metering (opt-in wrapper around the AgentScope chat model)
# ---------------------------------------------------------------------------
async def _metered_stream(
    inner: Any,
    meter: "_MeteredChatModel | None" = None,
    call_id: str = "",
    call_args: tuple = (),
) -> AsyncIterator[Any]:
    """Yield chunks from *inner*, metering usage on the way.

    This MUST stay a genuine async generator: AgentScope's agent
    dispatches on ``inspect.isasyncgen(res)`` to recognise a
    streaming response.  Wrapping the stream in a plain object
    makes the agent treat it as a completed (empty) response and
    raise "Model returned an empty streaming response".
    Mirrors the framework's own stream wrapper in
    ``agentscope/model/_base.py`` (inner ``async def _stream()``
    plus an explicit ``aclose`` of the delegated iterator).
    """
    last_usage: Any = None
    chunks_text: list[str] = []
    try:
        async for chunk in inner:
            try:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    last_usage = usage
                if meter is not None:
                    chunks_text.append(_response_text(chunk))
            except Exception:  # noqa: BLE001 - metering must never break a stream
                pass
            yield chunk
    finally:
        if meter is not None:
            try:
                # 流式只在结束时生成一条记录（框架的 chunk 文本/usage 是
                # 累计值，取最后一个），同一 call_id 由累计器去重。
                meter._emit(call_id, call_args, last_usage, chunks_text[-1:] )
            except Exception:  # noqa: BLE001 - metering must never break a stream
                pass
        aclose = getattr(inner, "aclose", None)
        if aclose is not None:
            maybe_awaitable = aclose()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        for key in ("text", "thinking", "content"):
            value = block.get(key)
            if isinstance(value, str):
                return value
        value = block.get("input")
        return str(value) if value else ""
    for key in ("text", "thinking"):
        value = getattr(block, key, None)
        if isinstance(value, str):
            return value
    return ""


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "".join(_block_text(b) for b in content)
    return _block_text(content)


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return _content_text(getattr(response, "content", None))


def _prompt_text(args: tuple, kwargs: dict) -> str:
    messages = kwargs.get("messages", args[0] if args else None)
    if not isinstance(messages, (list, tuple)):
        return _content_text(messages)
    parts = []
    for msg in messages:
        if isinstance(msg, dict):
            parts.append(_content_text(msg.get("content")))
        else:
            parts.append(_content_text(getattr(msg, "content", msg)))
    return "".join(parts)


def _estimate_tokens(text: str) -> int:
    """粗估 token 数（source="estimate" 时用）。

    估算办法：ASCII 字符按 4 个约 1 token，非 ASCII 字符（中文等）
    按 1 个约 1 token，向上取整。只用于厂商没回 usage 的兜底，
    记录会带 estimate 标记，不冒充真实值。
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return other + (ascii_chars + 3) // 4


def _usage_int(usage: Any, field: str) -> int | None:
    value = getattr(usage, field, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _MeteredChatModel:
    """Transparent proxy that accumulates ``ChatResponse.usage`` per call.

    TUIM1b：每次调用另生成一条 :class:`core.usage.UsageRecord` 登记进
    进程内当前累计器（按 call_id 去重）。运行记录与收尾 Token 行都读
    这个累计器（USAGE1）；CLEAN1 已删除旧的进程级计数器。
    """

    def __init__(
        self,
        inner: ChatModelBase,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        self._drbug_inner = inner
        self._drbug_provider = provider
        self._drbug_model = model or getattr(inner, "model_name", None)
        self._drbug_effort = effort

    def __getattr__(self, name: str) -> Any:
        return getattr(self._drbug_inner, name)

    def __repr__(self) -> str:
        return repr(self._drbug_inner)

    def _emit(
        self,
        call_id: str,
        call_args: tuple,
        usage: Any,
        response_texts: list[str],
    ) -> None:
        import time

        from .model_catalog import catalog_model
        from .usage import UsageRecord, compute_cost, record_usage

        row = (
            catalog_model(self._drbug_provider, self._drbug_model)
            if self._drbug_provider and self._drbug_model
            else None
        )
        price_in = row.price_in if row is not None else None
        price_out = row.price_out if row is not None else None
        if usage is not None:
            source = "provider"
            in_tok = _usage_int(usage, "input_tokens")
            out_tok = _usage_int(usage, "output_tokens")
            cache_read = _usage_int(usage, "cache_input_tokens")
            cache_write = _usage_int(usage, "cache_creation_input_tokens")
        else:
            source = "estimate"
            args, kwargs = call_args if call_args else ((), {})
            in_tok = _estimate_tokens(_prompt_text(args, kwargs))
            out_tok = _estimate_tokens("".join(response_texts))
            cache_read = None
            cache_write = None
        record_usage(
            UsageRecord(
                call_id=call_id,
                ts=time.time(),
                provider=self._drbug_provider,
                model=self._drbug_model,
                effort=self._drbug_effort,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                source=source,
                price_in_snapshot=price_in,
                price_out_snapshot=price_out,
                cost=compute_cost(in_tok, out_tok, price_in, price_out),
            )
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        import uuid

        call_id = uuid.uuid4().hex
        result = await self._drbug_inner(*args, **kwargs)
        if hasattr(result, "__aiter__"):
            return _metered_stream(result, self, call_id, (args, kwargs))
        usage = getattr(result, "usage", None)
        try:
            self._emit(call_id, (args, kwargs), usage, [_response_text(result)])
        except Exception:  # noqa: BLE001 - metering must never break a call
            pass
        return result


def _token_meter_enabled() -> bool:
    value = os.environ.get(_TOKEN_METER_ENV, "").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def build_model(
    cfg: ModelConfig | ModelProfile | None = None,
    *,
    role: str | None = None,
    config_path: str | Path | None = None,
) -> ChatModelBase:
    """Build a real AgentScope chat model from a config or named profile.

    The API key is read from ``os.environ[cfg.api_key_env]`` at call time and
    is never stored in the returned config object or included in errors.

    Args:
        cfg: settings/profile to use.  ``None`` loads the active
            :func:`load_config`.

    Raises:
        ValueError: ``model`` is missing/blank, or ``reasoning_effort`` is not
            supported by the provider.
        RuntimeError: the configured key environment variable is absent, or the
            provider model cannot be constructed (message is redacted).
        NotImplementedError: the provider has no wired builder.
    """
    if cfg is None and role is not None:
        cfg = resolve_role_profile(role, config_path)
    if cfg is None:
        cfg = load_config()

    model_name = (cfg.model or "").strip()
    if not model_name:
        raise ValueError(_missing_model_message("in-memory ModelConfig"))

    provider = (cfg.provider or DEFAULT_PROVIDER).strip().lower()
    spec = _PROVIDER_SPECS.get(provider)
    if spec is None:
        raise NotImplementedError(
            f"provider {cfg.provider!r} is not implemented; currently "
            f"supported: {', '.join(SUPPORTED_PROVIDERS)}. Extension point: "
            "in core/model_config.py add a builder that pairs "
            "agentscope.credential.<Provider>Credential with "
            "agentscope.model.<Provider>ChatModel, then dispatch to it from "
            "build_model()."
        )

    model = _build_provider(cfg, model_name, provider, spec)
    if not _token_meter_enabled():
        return model
    return _MeteredChatModel(
        model,
        provider=provider,
        model=model_name,
        effort=getattr(cfg, "reasoning_effort", None),
    )


def _build_provider(
    cfg: ModelConfig | ModelProfile,
    model_name: str,
    provider: str,
    spec: _ProviderSpec,
) -> ChatModelBase:
    effort = getattr(cfg, "reasoning_effort", None)
    if effort is not None:
        effort = _validate_reasoning_effort(
            provider, effort, "in-memory model config"
        )

    env_name = (getattr(cfg, "api_key_env", "") or "").strip()
    if not env_name:
        raise ValueError("api_key_env is empty; set it to the name of the "
                         "environment variable that holds the key")
    api_key = os.environ.get(env_name)
    if not api_key or not api_key.strip():
        raise RuntimeError(
            f"environment variable {env_name!r} is not set; put "
            f"{env_name}=<your key> in {user_env_path()} (or export it), "
            "then retry. The key value is never stored in profile JSON."
        )

    # Resolve through globals() so tests and embedders can patch the class
    # names on this module (the previous DeepSeek-only builder behaved so).
    credential_cls = globals()[spec.credential]
    model_cls = globals()[spec.model]

    credential_kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = getattr(cfg, "base_url", None)
    if base_url:
        credential_kwargs["base_url"] = base_url

    caps = model_capabilities(model_name)
    parameters = None
    parameters_cls = getattr(model_cls, "Parameters", None)
    field_names = (
        getattr(parameters_cls, "model_fields", {}) if parameters_cls else {}
    )
    param_kwargs: dict[str, Any] = {}
    if effort is not None:
        param_kwargs["reasoning_effort"] = effort
    # AgentScope's DeepSeekChatModel defaults thinking_enable to False and
    # then always sends thinking.type=disabled.  Models whose row says
    # thinking=true (and any explicit reasoning_effort) opt in, otherwise the
    # official reasoning channel is suppressed before it can reach the TUI.
    if (caps.thinking or effort is not None) and "thinking_enable" in field_names:
        param_kwargs["thinking_enable"] = True
    if param_kwargs and parameters_cls is not None:
        accepted = {
            key: value
            for key, value in param_kwargs.items()
            if key in field_names
        }
        if accepted:
            parameters = parameters_cls(**accepted)

    # DeepSeekChatFormatter defaults to text/plain and drops image blocks.
    # Its own comment says images are only for deepseek-flash, so the vision
    # row is what opens that gate.  Other providers keep their own formatter.
    formatter = None
    if provider == "deepseek" and caps.vision:
        from agentscope.formatter import DeepSeekChatFormatter

        formatter = DeepSeekChatFormatter(
            input_types=["text/plain", "image/*"],
        )

    try:
        credential = credential_cls(**credential_kwargs)
        construct: dict[str, Any] = {
            "credential": credential,
            "model": model_name,
        }
        if parameters is not None:
            construct["parameters"] = parameters
        if formatter is not None:
            construct["formatter"] = formatter
        return model_cls(**construct)
    except Exception as exc:  # pragma: no cover - defensive redaction path
        message = _redact(str(exc), api_key)
        raise RuntimeError(
            f"failed to build {spec.model}: {message}"
        ) from None

_PROVIDER_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openai_chat": "https://api.openai.com/v1",
}


def build_model_risk_assessor():
    """配置开启时返回同步风险评估器，否则返回 None。

    开关：合并配置顶层 ``auto_review_model_assessment: true``
    （写在 ~/.config/drbug/model_config.json）。评估器只许
    升级人工：返回 True = 升级为人工确认；回答不是明确 OK、
    没密钥、网络或解析出错，一律按升级处理（fail-safe）。
    它永远不能把清单外的操作放行——引擎只在清单已放行的
    转换点咨询它（见 core.policy 的 auto-review 转换）。
    """
    try:
        data, _source = _load_merged(None)
    except Exception:  # noqa: BLE001 - 配置读不出就不启用
        return None
    if data.get("auto_review_model_assessment") is not True:
        return None
    try:
        profile = resolve_role_profile("secretary")
    except Exception:  # noqa: BLE001
        return None
    base = profile.base_url or _PROVIDER_BASE_URLS.get(profile.provider)
    if not base:
        return None
    url = base.rstrip("/") + "/chat/completions"
    model = profile.model
    key_env = profile.api_key_env

    def assessor(actor: str, action: str, target: str) -> bool:
        try:
            load_local_env()
            key = os.environ.get(key_env, "")
            if not key:
                return True
            import httpx

            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是安全审核员。判断给定的 AI 工具操作"
                                "是否值得升级给人类确认：只回答 OK 或 "
                                "ESCALATE，不要解释。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"操作者 {actor} 要执行 {action}，"
                                f"目标：{target}"
                            ),
                        },
                    ],
                    "max_tokens": 4,
                    "temperature": 0,
                },
                timeout=8.0,
            )
            text = (
                resp.json()["choices"][0]["message"]["content"]
                .strip()
                .upper()
            )
            return not text.startswith("OK")
        except Exception:  # noqa: BLE001 - 评估失败按升级算
            return True

    return assessor

