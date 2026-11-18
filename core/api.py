"""双 UI 门面层：Web UI 与 TUI 共用的唯一接口面（API1）。

规范全文见 ``../research/2026-11-14-ui-interface-reservation.md``。本模块
只做「收拢」：把已经存在的结构化后端对象与事件通道映射成普通 dict /
list / str，不新造机制、不改 runner / acceptor / kb / dispatcher / repro 的
任何语义。展示层只依赖本模块，不再直接 import 内核实现。

两条通道（事件模式表见 :data:`EVENT_FIELD_SCHEMA`）：

* **runner 进度事件**——``run_repair`` 的 ``on_event`` 回调收到五类 dict：
  ``run_started`` / ``verdict`` / ``dispatch`` / ``actor_done`` /
  ``run_finished``；
* **dispatcher 工具事件**——``ChatSession`` 的 ``event_sink`` 收到
  ``{"event": "tool_call", "tool": ..., "summary": ...}``（``summary`` 可选）。

两条通道共用同一个回调签名 ``callable(dict)``，靠 ``event`` 字段区分；
新增事件类型必须向后兼容（旧监听者忽略未知类型即可）。

安全约束（写死在代码里）：

* :func:`get_model_config` / :func:`get_status` 只报告密钥环境变量是否已
  设置，永不返回密钥值，也永不读取 ``.env.local``（由入口点用
  :func:`core.model_config.load_local_env` 载入环境）；
* :func:`run_repair` 的付费闸与 dispatcher 版逐字一致：非 ``scripted``
  且 ``confirm_paid is not True`` 时直接拒绝、不运行、不写运行记录；
* :func:`freeze_repro` 的 confirm 硬闸语义不变：只认字面量 ``True``。

跨事件循环约束（TUI1-fix2 教训）：:class:`ChatSession` 在第一次
``reply`` 时绑定调用方正在运行的事件循环，之后复用同一个 dispatcher
Agent；绝不逐条消息新建/关闭事件循环。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from acceptor.acceptor import FrozenAcceptor
from acceptor.evidence import evidence_from_failed
from acceptor.frozen import MANIFEST_NAME
from kb.lookup import DispatchKB

from .autoprofile import build_profile as _build_profile_impl
from .autoprofile import default_profile_name, state_root_for
from .dispatcher import (
    PAID_REJECTION,
    _ensure_auto_profile,
    _resolve_record_path,
    _resolve_workspace,
    build_dispatcher,
    set_event_sink,
)
from .model_config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_PROVIDER,
    load_config,
    load_profile_catalog,
)
from .repro import REPRO_READY, FreezeResult
from .repro import freeze_repro as _freeze_repro_impl
from .repro import propose_repro as _propose_repro_impl
from .results import build_result_bundle
from .runner import run_arm

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
PROFILES_ROOT = PLATFORM_ROOT / "profiles"
RUNS_DIR = PLATFORM_ROOT / "runs"

ARMS = ("A", "B", "C")
ACTOR_BACKENDS = ("scripted", "llm", "agentscope")

CHANNEL_RUNNER = "runner"
CHANNEL_DISPATCHER = "dispatcher"

RUNNER_EVENT_NAMES: tuple[str, ...] = (
    "run_started",
    "verdict",
    "dispatch",
    "actor_done",
    "run_finished",
)
DISPATCHER_EVENT_NAMES: tuple[str, ...] = ("tool_call",)

# 事件名 -> 通道名。两种 UI 按此表把回调流分派到各自的渲染路径。
EVENT_CHANNELS: dict[str, str] = {
    **{name: CHANNEL_RUNNER for name in RUNNER_EVENT_NAMES},
    **{name: CHANNEL_DISPATCHER for name in DISPATCHER_EVENT_NAMES},
}

# 事件名 -> 字段全集（含必需的 ``event`` 判别字段）。监听者必须容忍
# 未知 *事件类型*（忽略即可）；已知类型里列出的字段则是稳定的。
EVENT_FIELD_SCHEMA: dict[str, tuple[str, ...]] = {
    "run_started": ("event", "arm", "actors", "kb_enabled", "workspace"),
    "verdict": ("event", "round", "status", "failed_count"),
    "dispatch": ("event", "round", "assignments"),
    "actor_done": ("event", "round", "actor_id", "applied", "note"),
    "run_finished": ("event", "outcome", "rounds", "final_failed"),
    "tool_call": ("event", "tool", "summary"),
}

# 可能缺席的字段（dispatcher 的 tool_call 只有在有摘要时才带 summary）。
EVENT_OPTIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "tool_call": ("summary",),
}

# 失败用例 node id 的分隔符，与 dispatcher.kb_explain 完全一致。
_NODE_ID_SPLIT = re.compile(r"[,;，；\s]+")

_FREEZE_REJECTION = (
    "冻结复现测试会写入仓库 tests/ 并生成验收金库，必须先向用户呈现"
    "「症状复现对照」（你遇到的问题 → 原始代码上的实际表现 → 期望结果），"
    "并取得用户对症状的明确确认，确认后再以 confirm=True 重试。"
    "（当前请求未冻结任何文件）"
)


# ---------------------------------------------------------------------------
# 体检与配置
# ---------------------------------------------------------------------------
def _key_is_set(env_name: str) -> bool:
    """只判断环境变量是否非空，绝不读取/返回其值。"""
    return bool(os.environ.get(env_name, "").strip())


def _model_summary() -> dict:
    """模型配置摘要：永不含密钥值。"""
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 - 配置缺失是可报告状态
        env_name = (
            os.environ.get("PLATFORM_MODEL_API_KEY_ENV", "").strip()
            or DEFAULT_API_KEY_ENV
        )
        return {
            "configured": False,
            "provider": DEFAULT_PROVIDER,
            "model": "",
            "api_key_env": env_name,
            "api_key_set": _key_is_set(env_name),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "configured": True,
        "provider": cfg.provider,
        "model": cfg.model,
        "api_key_env": cfg.api_key_env,
        "api_key_set": _key_is_set(cfg.api_key_env),
    }


def _profiles_summary() -> list[dict]:
    """已配置 profile 的摘要（无密钥值；只报 api_key_set 布尔）。"""
    try:
        catalog = load_profile_catalog()
    except Exception:  # noqa: BLE001 - 配置缺失/损坏是可报告状态
        return []
    return [
        {
            "name": profile.name,
            "provider": profile.provider,
            "model": profile.model,
            "api_key_set": _key_is_set(profile.api_key_env),
            "active": profile.name == catalog.active,
            "auto_generated": profile.auto_generated,
        }
        for profile in catalog.profiles
    ]


def _roles_summary() -> dict:
    """生效角色摘要（只读，绝不触发工人档自动生成）。"""
    try:
        from .model_config import effective_role_profiles

        return effective_role_profiles()
    except Exception:  # noqa: BLE001 - 配置损坏是可报告状态
        return {"mapped": False, "secretary": None, "worker": None}


def get_model_config() -> dict:
    """当前模型配置；既有三键语义不变，另附 ``profiles`` 与 ``roles`` 摘要。

    返回 ``{provider, model, api_key_set, profiles, roles}``。``profiles`` 每项为
    ``{name, provider, model, api_key_set, active, auto_generated}``；任何地方都不含密钥值。
    """
    summary = _model_summary()
    return {
        "provider": summary["provider"],
        "model": summary["model"],
        "api_key_set": summary["api_key_set"],
        "profiles": _profiles_summary(),
        "roles": _roles_summary(),
    }


def get_status() -> dict:
    """平台体检的结构化版：环境基线 / 验收金库 / 模型配置摘要。"""
    # 与 dispatcher.platform_check 走同一个 in-process 检查器，避免逻辑分叉。
    from .cli import _run_tool

    env_code, env_output = _run_tool("_cifix_tool_env_check", "env_check.py")
    env_lines = [line for line in env_output.splitlines() if line.strip()]
    env_ok = env_code == 0 and "ENV_CHECK_PASS" in env_output
    python_line = next(
        (line for line in env_lines if line.startswith("Python:")), ""
    )
    agentscope_line = next(
        (line for line in env_lines if line.startswith("agentscope:")), ""
    )

    from acceptor.parsers import PARSERS

    engine_ok = bool(PARSERS)

    return {
        "ok": env_ok and engine_ok,
        "environment": {
            "ok": env_ok,
            "exit_code": env_code,
            "python": python_line.removeprefix("Python:").strip(),
            "agentscope": agentscope_line.removeprefix("agentscope:").strip(),
            "lines": env_lines,
        },
        "acceptor": {
            "ok": engine_ok,
            "parsers": sorted(PARSERS),
        },
        "model": _model_summary(),
    }


# ---------------------------------------------------------------------------
# 修复运行（runner 门面）
# ---------------------------------------------------------------------------
def _next_record_path() -> Path:
    """runs/api_run_<时间戳>.json；同一秒内重复运行自动加序号。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"api_run_{stamp}.json"
    index = 1
    while path.exists():
        path = RUNS_DIR / f"api_run_{stamp}-{index}.json"
        index += 1
    return path


def _refusal(
    reason: str,
    *,
    arm: Any,
    actors: Any,
    workspace: Any,
) -> dict:
    """付费闸拒绝：与 RunRecord 同键位，``outcome`` 用门面哨兵 ``REFUSED``。"""
    return {
        "outcome": "REFUSED",
        "reason": reason,
        "arm": str(arm).upper() if isinstance(arm, str) else arm,
        "actors": actors,
        "workspace": str(workspace),
        "kb_enabled": True,
        "rounds": [],
        "final_failed": [],
        "scope_denials": [],
        "artifacts": {
            "record_path": None,
            "bundle_path": None,
            "bundle_error": "未执行任何运行（付费闸拒绝）",
        },
    }


def run_repair(
    arm: str = "C",
    actors: str = "scripted",
    workspace: str | None = None,
    max_rounds: int = 3,
    confirm_paid: bool = False,
    on_event: Callable[[dict], None] | None = None,
    parallel: int = 1,
) -> dict:
    """运行一轮修复，返回 RunRecord 的 dict 形（另附 ``artifacts``）。

    付费闸与 :func:`core.dispatcher.run_repair` 逐字一致：``actors`` 不是
    ``scripted`` 且 ``confirm_paid is not True`` 时直接返回结构化拒绝
    （``outcome == "REFUSED"``，带 ``reason``），不解析工作区、不运行、
    不写任何运行记录。其余参数错误按普通 Python 异常抛出。

    ``on_event`` 收到 runner 的五类进度事件（见 :data:`EVENT_FIELD_SCHEMA`）。
    返回成功时额外带 ``artifacts``：``record_path``（落盘记录）、
    ``bundle_path``（结果包目录，失败时为 None）与 ``bundle_error``。

    相对 ``workspace`` 与 dispatcher 同款解析：先按平台目录，再按当前目录；
    任何仓库都先走 :func:`core.autoprofile.build_profile` 自动建档
    （零模型、零费用），没有内置工作区与固定金库。``workspace``
    必填：缺省直接抛 ValueError。
    """
    # 付费闸必须最先执行：不得触碰模型、工作区或运行记录。
    if actors != "scripted" and confirm_paid is not True:
        return _refusal(
            PAID_REJECTION, arm=arm, actors=actors, workspace=workspace
        )

    arm = str(arm).upper()
    if arm not in ARMS:
        raise ValueError(f"未知策略臂 {arm!r}，可选 A/B/C")
    if actors not in ACTOR_BACKENDS:
        raise ValueError(
            f"未知执行后端 {actors!r}，可选 scripted/llm/agentscope"
        )
    try:
        rounds = int(max_rounds)
    except (TypeError, ValueError):
        raise ValueError(
            f"max_rounds 必须是整数，收到 {max_rounds!r}"
        ) from None
    if rounds < 0:
        raise ValueError("max_rounds 必须 >= 0")
    if not isinstance(parallel, int) or parallel < 1:
        raise ValueError(f"parallel 必须是 >= 1 的整数，收到 {parallel!r}")

    if workspace is None:
        raise ValueError("run_repair 需要显式 workspace（无默认工作区）")
    workspace_path = _resolve_workspace(workspace)
    if not workspace_path.is_dir():
        raise FileNotFoundError(f"工作区不存在: {workspace_path}")

    profile_dir = _ensure_auto_profile(workspace_path)

    run_workspaces: list[str] = []

    def _relay(event: dict) -> None:
        if event.get("event") == "run_started":
            run_workspaces.append(str(event.get("workspace", "")))
        if on_event is not None:
            on_event(event)

    if parallel > 1:
        from core.parallel import run_parallel

        record = run_parallel(
            arm=arm,
            workspace_src=workspace_path,
            attempts=parallel,
            kb_enabled=True,
            max_rounds=rounds,
            work_root=RUNS_DIR / "work",
            actor_backend=actors,
            on_event=_relay,
            profile_dir=profile_dir,
        ).record
    else:
        record = run_arm(
            arm=arm,
            workspace_src=workspace_path,
            kb_enabled=True,
            max_rounds=rounds,
            work_root=RUNS_DIR / "work",
            actor_backend=actors,
            on_event=_relay,
            profile_dir=profile_dir,
        )

    out_path = _next_record_path()
    record_path: str | None = str(out_path)
    bundle_path: Path | None = None
    bundle_error = ""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        # 记录写盘失败不吞掉运行结果：原样返回 RunRecord，artifacts 标明原因。
        record_path = None
        bundle_error = f"运行记录写入失败: {type(exc).__name__}: {exc}"

    if record_path is not None:
        if run_workspaces:
            try:
                bundle_path = build_result_bundle(
                    record,
                    workspace_path,
                    run_workspaces[-1],
                    actor_backend=actors,
                    bundle_root=RUNS_DIR / "bundles",
                    record_path=out_path,
                )
            except Exception as exc:  # noqa: BLE001 - 运行结果仍然有效
                bundle_error = f"{type(exc).__name__}: {exc}"
        else:
            bundle_error = "未收到最终工作副本路径"

    payload = record.to_dict()
    payload["artifacts"] = {
        "record_path": record_path,
        "bundle_path": str(bundle_path) if bundle_path is not None else None,
        "bundle_error": bundle_error,
    }
    return payload


# ---------------------------------------------------------------------------
# 冻结验收与派单解释
# ---------------------------------------------------------------------------
def _vault_for_workspace(workspace_path: Path) -> Path:
    """工作区对应的验收金库：只认该工作区自己的档案金库。

    尚无档案时抛 FileNotFoundError，不回落到任何内置金库。
    """
    candidate = (
        state_root_for(workspace_path)
        / "profiles"
        / default_profile_name(workspace_path)
        / "vault"
    )
    if (candidate / MANIFEST_NAME).is_file():
        return candidate
    raise FileNotFoundError(
        f"该工作区尚无冻结档案: {workspace_path}（请先建档）"
    )


def accept_check(workspace: str) -> dict:
    """冻结验收的结构化结果（``Verdict.to_dict()``，含 TAMPERED 分支）。"""
    workspace_path = _resolve_workspace(workspace)
    if not workspace_path.is_dir():
        raise FileNotFoundError(f"工作区不存在: {workspace_path}")
    verdict = FrozenAcceptor(_vault_for_workspace(workspace_path)).check(
        workspace_path
    )
    return verdict.to_dict()


def _node_ids(failed_tests: str | Iterable[str]) -> list[str]:
    if isinstance(failed_tests, str):
        parts = _NODE_ID_SPLIT.split(failed_tests.strip())
    else:
        parts = [str(item).strip() for item in failed_tests]
    return [item for item in dict.fromkeys(parts) if item]


def explain_dispatch(
    failed_tests: str | Iterable[str], workspace: str
) -> dict:
    """知识库改派的结构化版（``DispatchPlan`` 的 dict 形）。

    派单表取 ``workspace`` 自己档案里的表；尚无档案时抛
    FileNotFoundError。``failed_tests`` 可传逗号/分号/空白分隔的
    字符串（与 dispatch 工具一致），也可传 node id 序列。返回
    ``assignments`` / ``unmapped``。
    """
    node_ids = _node_ids(failed_tests)
    workspace_path = _resolve_workspace(workspace)
    table_path = (
        state_root_for(workspace_path)
        / "profiles"
        / default_profile_name(workspace_path)
        / "dispatch_table.json"
    )
    if not table_path.is_file():
        raise FileNotFoundError(
            f"该工作区尚无派单表: {workspace_path}（请先建档）"
        )
    kb = DispatchKB.from_file(table_path, enabled=True)
    plan = kb.lookup(evidence_from_failed(node_ids))
    return {
        "assignments": {
            actor_id: list(nodes) for actor_id, nodes in plan.assignments.items()
        },
        "unmapped": list(plan.unmapped),
    }


# ---------------------------------------------------------------------------
# 自动建档与复现测试
# ---------------------------------------------------------------------------
def build_profile(workspace: str) -> dict:
    """为仓库自动建档（零模型、零费用），返回 ``ProfileResult`` 的 dict 形。

    注意：这会重写 ``profiles/<仓库名>/``（内核既有语义，防止旧档案残留）。
    """
    workspace_path = _resolve_workspace(workspace)
    result = _build_profile_impl(
        workspace_path,
        out_root=state_root_for(workspace_path) / "profiles",
    )
    return result.to_dict()


def propose_repro(
    workspace: str,
    symptom: str = "",
    *,
    model: Any = None,
    max_attempts: int = 2,
    name: str | None = None,
) -> dict:
    """复现测试分诊的结构化版；永不冻结、永不写目标仓库。

    返回 ``ReproResult.to_dict()``；``status == "REPRO_READY"`` 时另附
    ``comparison``（症状复现对照的三要素），供 UI 原样呈现并向用户确认
    「这复现的是不是你遇到的问题」；其余状态 ``comparison`` 为 ``None``。
    ``model=None`` 且症状非空时由内核经 :func:`core.model_config.build_model`
    构建真实模型（这是内核既有语义，调用方负责事先征得用户同意）。
    """
    workspace_path = _resolve_workspace(workspace)
    result = _propose_repro_impl(
        workspace_path,
        symptom,
        model=model,
        max_attempts=max_attempts,
        name=name,
        profiles_root=state_root_for(workspace_path) / "profiles",
    )
    payload = result.to_dict()
    comparison = result.comparison if result.status == REPRO_READY else None
    payload["comparison"] = (
        {
            "symptom": comparison.symptom,
            "observed": comparison.observed,
            "expected": comparison.expected,
            "assertion_line": comparison.assertion_line,
            "input_expr": comparison.input_expr,
        }
        if comparison is not None
        else None
    )
    return payload


def freeze_repro(
    workspace: str,
    confirm: bool = False,
    *,
    name: str | None = None,
) -> dict:
    """冻结复现测试的结构化版；``confirm`` 只认字面量 ``True``。

    与 dispatcher 版同款硬闸：``confirm is not True`` 时立即返回
    ``status == "REFUSED"`` 的 ``FreezeResult`` dict，不读取暂存候选、
    不写任何文件。确认后委托 :func:`core.repro.freeze_repro`。
    """
    workspace_path = _resolve_workspace(workspace)
    if confirm is not True:
        return FreezeResult(
            status="REFUSED",
            repo_path=workspace_path,
            name=str(name or default_profile_name(workspace_path)),
            reason=_FREEZE_REJECTION,
        ).to_dict()
    result = _freeze_repro_impl(
        workspace_path,
        name=name,
        confirm=True,
        profiles_root=state_root_for(workspace_path) / "profiles",
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------
def get_run_record(path: str | Path) -> dict:
    """读取运行记录 JSON（也接受结果包目录，自动取其 ``record.json``）。"""
    record_path = _resolve_record_path(path)
    if record_path.is_dir():
        record_path = record_path / "record.json"
    data = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"运行记录不是 JSON 对象: {record_path}")
    return data


_RUN_RECORD_KEYS = ("arm", "outcome", "rounds")


def list_runs() -> list[dict]:
    """运行记录摘要列表（按修改时间倒序，最新在前）。

    只扫描 ``runs/*.json`` 顶层的运行记录；结果包目录（``runs/bundles``）、
    机制自检摘要等非运行记录文件自动跳过。单条记录摘要字段：
    ``path / name / modified_at / arm / kb_enabled / outcome / rounds /
    final_failed_count``。
    """
    if not RUNS_DIR.is_dir():
        return []
    entries: list[dict] = []
    for path in RUNS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if any(key not in data for key in _RUN_RECORD_KEYS):
            continue
        rounds = data.get("rounds")
        final_failed = data.get("final_failed")
        entries.append(
            {
                "path": str(path),
                "name": path.name,
                "modified_at": datetime.fromtimestamp(
                    path.stat().st_mtime
                )
                .astimezone()
                .isoformat(timespec="seconds"),
                "arm": data.get("arm"),
                "kb_enabled": bool(data.get("kb_enabled")),
                "outcome": data.get("outcome"),
                "rounds": len(rounds) if isinstance(rounds, list) else 0,
                "final_failed_count": (
                    len(final_failed) if isinstance(final_failed, list) else 0
                ),
                "_mtime": path.stat().st_mtime,
            }
        )
    entries.sort(key=lambda item: (item["_mtime"], item["name"]), reverse=True)
    for entry in entries:
        entry.pop("_mtime")
    return entries


# ---------------------------------------------------------------------------
# 员工档案（只读映射 IDENTITY.md / MEMORY.md，不新造存储）
# ---------------------------------------------------------------------------
def _archive_profile_dir(workspace: str) -> Path:
    """Profile directory that holds ``agents/<name>/`` for this workspace.

    Follows the same root as :func:`build_profile` on this facade: a patched
    ``PROFILES_ROOT`` wins, otherwise :func:`core.autoprofile.state_root_for`.
    """
    workspace_path = _resolve_workspace(workspace)
    if not workspace_path.is_dir():
        raise FileNotFoundError(f"工作区不存在: {workspace_path}")
    from .autoprofile import default_profile_name, state_root_for

    default_root = (PLATFORM_ROOT / "profiles").resolve()
    try:
        configured = Path(PROFILES_ROOT).expanduser().resolve()
    except OSError:
        configured = Path(PROFILES_ROOT)
    if configured != default_root:
        root = Path(PROFILES_ROOT)
    else:
        root = state_root_for(workspace_path) / "profiles"
    return root / default_profile_name(workspace_path)


def list_agent_archives(workspace: str) -> list[dict]:
    """List employee archives for ``workspace``.

    Each item is ``{name, identity_path, memory_path}``. An unknown workspace
    raises ``FileNotFoundError``. A workspace with no profile yet returns
    an empty list. Nothing is created.
    """
    from .agent_archive import list_archives

    return list_archives(_archive_profile_dir(workspace))


def get_agent_archive(workspace: str, name: str) -> dict:
    """Read one employee's IDENTITY.md and MEMORY.md.

    Returns ``{name, found, identity, memory, message}``. ``found`` is false
    and ``message`` says 没有这个员工 when the archive is absent. Does not
    create files.
    """
    from .agent_archive import read_archive

    profile_dir = _archive_profile_dir(workspace)
    archive = read_archive(profile_dir, name)
    label = str(name).strip()
    if archive is None:
        return {
            "name": label,
            "found": False,
            "identity": "",
            "memory": "",
            "message": f"没有这个员工: {label or '（未提供名字）'}",
        }
    return {
        "name": archive.name,
        "found": True,
        "identity": archive.identity,
        "memory": archive.memory,
        "message": "",
        "identity_path": str(archive.identity_path),
        "memory_path": str(archive.memory_path),
    }


def list_roster(workspace: str) -> list[dict]:
    """当前项目名册。只映射 :func:`core.roster.list_roster`。"""
    from .roster import list_roster as _list_roster

    return _list_roster(workspace)


def add_worker(workspace: str, name: str, module: str) -> dict:
    """手动添加一名员工。只映射 :func:`core.roster.add_worker`。"""
    from .roster import add_worker as _add_worker

    return _add_worker(workspace, name, module, channel="boss")


def remove_worker(workspace: str, name: str) -> dict:
    """从名册移除一名员工并归档档案。只映射 :func:`core.roster.remove_worker`。"""
    from .roster import remove_worker as _remove_worker

    return _remove_worker(workspace, name)


# ---------------------------------------------------------------------------
# 调度员会话（ChatSession）
# ---------------------------------------------------------------------------
class ChatSession:
    """一个绑定调用方事件循环的调度员会话。

    首次 :meth:`reply` 时经 :func:`core.dispatcher.build_dispatcher` 构建
    dispatcher Agent，并绑定当前正在运行的事件循环；后续消息复用同一个
    Agent 与同一个循环（TUI1-fix2 硬约束）。工具事件经 ``event_sink``
    透出，schema 见 :data:`EVENT_FIELD_SCHEMA` 的 ``tool_call``。
    ``set_event_sink`` 与 dispatcher 模块一样是**进程级**绑定：同一进程
    只应有一个活跃调度员会话（与现有 TUI 的假设一致）。

    ``model=None`` 时由 dispatcher 惰性构建真实模型（需要 ``model_config.json``
    与密钥环境变量）；测试与离线演示应注入 ``FakeChatModel``。
    ``workspace`` 是会话绑定工作区：工具调用未显式指明仓库时默认使用它；
    ``None`` 时会话处于待命态，缺省工作区的工具会被结构化拒绝。
    """

    def __init__(
        self,
        *,
        model: Any = None,
        actors: str = "scripted",
        event_sink: Callable[[dict], None] | None = None,
        workspace: str | Path | None = None,
        confirm_responder: Callable[[str], str] | None = None,
        confirm_timeout: float | None = None,
        mode: str = "ask",
    ) -> None:
        actors = str(actors).strip().lower()
        if actors not in ACTOR_BACKENDS:
            raise ValueError(
                f"actors 只能是 scripted/llm/agentscope，收到 {actors!r}"
            )
        self.actors = actors
        self.workspace = workspace
        self.confirm_responder = confirm_responder
        # None：一直等应答者（交互场景既有语义）。正数：到点按拒绝。
        self.confirm_timeout = confirm_timeout
        self.mode = str(mode or "ask")
        self._model = model
        self._event_sink = event_sink
        self._agent: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None

    @property
    def agent(self) -> Any:
        """当前复用的 dispatcher Agent（首条消息之前为 ``None``）。"""
        return self._agent

    def _ensure_agent(self) -> Any:
        if self._agent is None:
            self._agent = build_dispatcher(
                model=self._model,
                event_sink=self._event_sink,
                default_actors=self.actors,
                workspace=self.workspace,
                secretary_files=True,
                confirm_responder=self.confirm_responder,
                mode=self.mode,
            )
        return self._agent

    async def _ensure_mcp(self, agent: Any) -> None:
        """首条消息时把已登记的 MCP 工具挂到秘书身上（只挂一次）。

        挂不上（配置损坏/连接问题）不挡对话：把原因留在
        ``self._mcp_notice`` 里，并经事件口转达一次。
        """
        if getattr(self, "_mcp_tried", False):
            return
        self._mcp_tried = True
        from core.dispatcher import attach_secretary_mcp

        workspace = (
            Path(self.workspace)
            if self.workspace is not None
            else PLATFORM_ROOT
        )
        try:
            toolset = await attach_secretary_mcp(agent, workspace)
        except Exception as exc:  # noqa: BLE001 - 对话优先，MCP 降级
            self._mcp_notice = (
                f"MCP 工具未挂上：{type(exc).__name__}: {exc}"
            )
        else:
            if toolset is not None and getattr(toolset, "notices", None):
                self._mcp_notice = "；".join(toolset.notices)
        notice = getattr(self, "_mcp_notice", None)
        if notice and self._event_sink is not None:
            self._event_sink({"event": "mcp_notice", "message": notice})

    async def reply(
        self,
        text: str,
        images: Sequence[str] | None = None,
    ) -> str:
        """把一条用户消息交给主治医生，返回它的文本回复。

        ``images`` 默认 ``None``，与只传文本的旧调用兼容。给出路径时走和
        TUI 相同的附图通道：当前模型能力表 ``vision`` 为真才附上第一张真实
        图片，否则只发文字。
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("消息不能为空")

        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif loop is not self._loop:
            raise RuntimeError(
                "ChatSession 已绑定另一个事件循环：请在创建会话的同一循环里"
                "复用本会话（禁止逐条消息新建事件循环）"
            )

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            agent = self._ensure_agent()
            await self._ensure_mcp(agent)
            from .dispatcher import build_user_message
            from .model_config import model_capabilities

            vision = model_capabilities(
                str(getattr(getattr(agent, "model", None), "model", "") or "")
            ).vision
            message = build_user_message(text, images, vision=vision)
            reply = await (agent.reply(message) if getattr(self, "confirm_responder", None) is None else self._drive_confirming_reply(agent, message))
            return reply.get_text_content() or "（主治医生没有返回文本）"


def create_chat_session(
    actors: str = "scripted",
    *,
    model: Any = None,
    event_sink: Callable[[dict], None] | None = None,
    workspace: str | Path | None = None,
) -> ChatSession:
    """构造 :class:`ChatSession`；``actors`` 默认 scripted（离线、零费用）。

    ``workspace`` 透传给 :func:`core.dispatcher.build_dispatcher` 作为会话
    绑定工作区；``None`` 时会话处于待命态。
    """
    return ChatSession(
        actors=actors,
        model=model,
        event_sink=event_sink,
        workspace=workspace,
    )


__all__ = [
    "ACTOR_BACKENDS",
    "ARMS",
    "CHANNEL_DISPATCHER",
    "CHANNEL_RUNNER",
    "ChatSession",
    "DISPATCHER_EVENT_NAMES",
    "EVENT_CHANNELS",
    "EVENT_FIELD_SCHEMA",
    "EVENT_OPTIONAL_FIELDS",
    "PAID_REJECTION",
    "PROFILES_ROOT",
    "RUNNER_EVENT_NAMES",
    "RUNS_DIR",
    "accept_check",
    "add_worker",
    "build_profile",
    "create_chat_session",
    "explain_dispatch",
    "freeze_repro",
    "get_model_config",
    "get_run_record",
    "get_status",
    "list_roster",
    "list_runs",
    "propose_repro",
    "remove_worker",
    "run_repair",
    "set_event_sink",
]


async def _drive_confirming_reply(self, agent, message):
    """消费回复流，只把最后一个 Msg 当回复；确认最多三轮。

    全程走 reply_stream：停驻事件在流里现身，恢复也走
    流，恢复后再次停驻不会被吞掉。每轮的审计等恢复流
    走完才写——写的是已发生的事实，不是预告。
    """
    from agentscope.event import (
        ConfirmResult,
        RequireUserConfirmEvent,
        UserConfirmResultEvent,
    )
    from agentscope.message import Msg

    import json

    from .policy import Decision, PolicyEngine, confirm_line

    secretary_tool = getattr(agent, "_drbug_secretary_read", None)
    if secretary_tool is not None:
        secretary_tool._denied_targets = set()
    pending = message
    answered: list[tuple[str, str]] = []
    rounds = 0
    while True:
        last_msg = None
        confirm_event = None
        async for item in agent.reply_stream(
            pending, yield_final_msg=True
        ):
            if isinstance(item, RequireUserConfirmEvent):
                confirm_event = item
                break
            if isinstance(item, Msg):
                last_msg = item
        if answered:
            # 走到这里说明上一轮的结果事件已被框架处理
            # 完毕：答 Y 的已经执行，答 N 的没有执行。
            engine = PolicyEngine(workspace=self.workspace)
            for target, answer in answered:
                if answer == "TIMEOUT":
                    verdict = "deny"
                    reason = "答复 超时未答复；未执行"
                else:
                    verdict = "allow" if answer == "Y" else "deny"
                    reason = (
                        f"答复 {answer}；"
                        f"{'已执行' if answer == 'Y' else '未执行'}"
                    )
                engine.audit(
                    "secretary",
                    "cross_dir_read",
                    target,
                    Decision(
                        verdict=verdict,
                        tier=1,
                        reason=reason,
                    ),
                )
            answered = []
        if confirm_event is None:
            return last_msg
        rounds += 1
        if rounds > 3:
            raise RuntimeError(
                "BLOCKER: user confirm exceeded three rounds"
            )
        # 停驻事件只带第一个待确认调用。同轮其余的还在
        # asking，不补上的话恢复后流会停在「还在等人」而不再问。
        pending_calls = list(confirm_event.tool_calls or [])
        seen_ids = {getattr(call, "id", None) for call in pending_calls}
        state = getattr(agent, "state", None)
        getter = getattr(state, "get_awaiting_tool_calls", None)
        if callable(getter):
            for call in getter(getattr(agent, "name", "")) or []:
                call_id = getattr(call, "id", None)
                if call_id in seen_ids:
                    continue
                pending_calls.append(call)
                seen_ids.add(call_id)
        results = []
        for tool_call in pending_calls:
            raw = tool_call.input
            if isinstance(raw, str):
                raw = json.loads(raw)
            target = ""
            if isinstance(raw, dict):
                target = str(raw.get("file_path", "")).strip()
            question = confirm_line(
                "secretary", "cross_dir_read", target
            )
            if self._event_sink is not None:
                self._event_sink(
                    {
                        "type": "confirm",
                        "confirm_line": question,
                        "text": question,
                    }
                )
            if self.confirm_timeout is None:
                answer = self.confirm_responder(question)
            else:
                try:
                    answer = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.confirm_responder, question
                        ),
                        timeout=self.confirm_timeout,
                    )
                except asyncio.TimeoutError:
                    answer = "TIMEOUT"
            if answer == "Y":
                tool = getattr(
                    agent, "_drbug_secretary_read", None
                )
                if tool is not None:
                    tool._ask_release = target
            elif secretary_tool is not None:
                secretary_tool._denied_targets.add(target)
            results.append(
                ConfirmResult(
                    confirmed=answer == "Y",
                    tool_call=tool_call,
                    rules=None,
                )
            )
            answered.append((target, answer))
        pending = UserConfirmResultEvent(
            reply_id=confirm_event.reply_id,
            confirm_results=results,
        )


ChatSession._drive_confirming_reply = _drive_confirming_reply
