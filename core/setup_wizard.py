# -*- coding: utf-8 -*-
"""First-run setup: pick a provider, name a model, store the API key.

This is the product's front door (重制计划阶段 1).  Before this module,
a fresh install had the provider wiring (``core.model_config``) but no
way for a new user to *reach* it: the config file had to be hand-written
and the key hand-placed in an env file nobody told them about, so the
first chat message died with a raw ``RuntimeError``.

The split follows the same rule the rest of the product uses:

* non-secret choices (provider / model / the *name* of the key variable)
  go to ``~/.config/drbug/model_config.json`` — the key value itself is
  never written there;
* the key value goes only to ``~/.config/drbug/.env`` (mode 0600),
  which ``core.model_config.load_local_env`` already reads first.

Only DeepSeek is end-to-end verified with a real key; the other wired
providers are listed too but flagged ``verified=False`` so the UI can
say so honestly instead of implying equal confidence.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from core.model_config import (
    SUPPORTED_PROVIDERS,
    load_config,
    load_local_env,
    user_config_path,
    user_env_path,
)

#: Providers in wizard order: the verified one first, the rest in the
#: registry's own order.  ``default_model`` is only filled where a
#: default has actually been exercised end-to-end; everywhere else the
#: user types the model id (we do not invent ids for unverified
#: providers).  ``key_env`` is the conventional variable name for that
#: provider's API key.
_PROVIDER_ROWS: tuple[tuple[str, str, str | None, bool], ...] = (
    ("deepseek", "DEEPSEEK_API_KEY", "deepseek-flash", True),
    ("anthropic", "ANTHROPIC_API_KEY", None, False),
    ("dashscope", "DASHSCOPE_API_KEY", None, False),
    ("minimax", "MINIMAX_API_KEY", None, False),
    ("moonshot", "MOONSHOT_API_KEY", None, False),
    ("openai_chat", "OPENAI_API_KEY", None, False),
    ("openai_response", "OPENAI_API_KEY", None, False),
    ("volcengine", "VOLCENGINE_API_KEY", None, False),
)


@dataclass(frozen=True)
class ProviderChoice:
    provider: str
    key_env: str
    default_model: str | None
    verified: bool


def provider_choices() -> list[ProviderChoice]:
    """The wizard's provider list, verified provider first."""
    rows = {row[0]: row for row in _PROVIDER_ROWS}
    choices: list[ProviderChoice] = []
    for provider in [r[0] for r in _PROVIDER_ROWS]:
        _, key_env, default_model, verified = rows[provider]
        if provider in SUPPORTED_PROVIDERS:
            choices.append(
                ProviderChoice(provider, key_env, default_model, verified)
            )
    return choices


def choice_for(provider: str) -> ProviderChoice:
    for choice in provider_choices():
        if choice.provider == provider:
            return choice
    raise ValueError(
        f"未知厂商：{provider}（可选："
        + "、".join(c.provider for c in provider_choices())
        + "）"
    )


@dataclass(frozen=True)
class SetupStatus:
    configured: bool
    provider: str
    model: str
    key_env: str
    key_set: bool
    problems: tuple[str, ...]


def setup_status() -> SetupStatus:
    """Whether this machine can actually call a model right now.

    ``configured`` is the wizard's trigger: it is only true when a
    provider/model resolve *and* the key variable they name is present
    in the environment (after loading the user's env files).
    """
    load_local_env()
    problems: list[str] = []
    try:
        config = load_config()
    except Exception as exc:  # unreadable config -> treat as unconfigured
        return SetupStatus(False, "", "", "", False, (f"配置读不出来：{exc}",))
    provider = (config.provider or "").strip()
    model = (config.model or "").strip()
    key_env = (config.api_key_env or "").strip()
    if not provider or not model:
        problems.append("还没选厂商和模型")
    if provider and provider not in SUPPORTED_PROVIDERS:
        problems.append(f"厂商 {provider} 不在已接列表里")
    key_set = bool(key_env and os.environ.get(key_env, "").strip())
    if key_env and not key_set:
        problems.append(f"密钥环境变量 {key_env} 还没设置")
    if not key_env:
        problems.append("配置里没写密钥变量名")
    return SetupStatus(
        configured=not problems,
        provider=provider,
        model=model,
        key_env=key_env,
        key_set=key_set,
        problems=tuple(problems),
    )


def needs_setup() -> bool:
    return not setup_status().configured


@dataclass(frozen=True)
class SetupResult:
    provider: str
    model: str
    key_env: str
    config_path: Path
    env_path: Path


def run_setup(
    provider: str,
    model: str,
    api_key: str,
    *,
    key_env: str | None = None,
) -> SetupResult:
    """Persist one wizard submission.  The key value is only ever
    written to the user env file — never to the JSON config, never
    returned, never logged."""
    choice = choice_for(provider)
    model = (model or "").strip()
    if not model:
        raise ValueError("模型名不能为空")
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("API key 不能为空")
    env_name = (key_env or choice.key_env).strip()
    if not env_name or not env_name.isidentifier():
        raise ValueError(f"密钥变量名不合法：{env_name!r}")

    # 1) Non-secret config: merge into the user config, preserving any
    #    keys already there (active_profile, profiles, role_profiles…),
    #    written atomically like activate_profile does.
    config_path = user_config_path()
    data: dict = {}
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
    data["provider"] = provider
    data["model"] = model
    data["api_key_env"] = env_name
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(config_path)

    # 2) The key itself: upsert one line in the user env file, then
    #    tighten permissions — the file holds a live credential.
    env_path = user_env_path()
    lines: list[str] = []
    if env_path.is_file():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    new_line = f"{env_name}={api_key}"
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{env_name}=") or stripped.startswith(
            f"export {env_name}="
        ):
            if not replaced:
                out.append(new_line)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass

    # 3) Take effect in this process so callers can verify immediately.
    os.environ[env_name] = api_key
    return SetupResult(provider, model, env_name, config_path, env_path)
