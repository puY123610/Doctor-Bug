"""drbug — 命令行门面：体检、单臂运行与冻结验收。

This module is a thin presentation layer over existing platform code.  It
imports (or, when import is impossible, subprocesses) ``tools/env_check.py``
instead of duplicating its logic, and it drives
``core.runner.run_arm`` exclusively through its public ``on_event`` callback.

Output policy: when stdout is a TTY and ``rich`` is importable, lines are
styled with rich; otherwise the exact same information is emitted with plain
``print`` so tests and redirections stay readable.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

RUNS_DIR = PLATFORM_ROOT / "runs"
TOOLS_DIR = PLATFORM_ROOT / "tools"
PROFILES_ROOT = PLATFORM_ROOT / "profiles"

#: Patch points used by tests/embedders: when these module constants differ
#: from the installation defaults, they win over per-project state paths.
_DEFAULT_RUNS_DIR = PLATFORM_ROOT / "runs"
_DEFAULT_PROFILES_ROOT = PLATFORM_ROOT / "profiles"

NOTE_LIMIT = 120
BANNER_WIDTH = 62


def _profiles_root_for(workspace: str | Path) -> Path:
    """Profiles root: platform default for built-ins, else ``<project>/.drbug``."""
    if PROFILES_ROOT != _DEFAULT_PROFILES_ROOT:
        return PROFILES_ROOT
    from core.autoprofile import state_root_for

    return state_root_for(workspace) / "profiles"


def _runs_root_for(workspace: str | Path) -> Path:
    """Runs/bundles/work root with the same built-in vs external split."""
    if RUNS_DIR != _DEFAULT_RUNS_DIR:
        return RUNS_DIR
    from core.autoprofile import state_root_for

    return state_root_for(workspace) / "runs"


def _onboarding_lines() -> list[str]:
    """One-step BYOK setup guide.  Never contains a key value."""
    from core.model_config import (
        DEFAULT_API_KEY_ENV,
        load_config,
        user_config_dir,
        user_config_path,
        user_env_path,
    )

    try:
        cfg = load_config()
        missing_model = False
        key_env = cfg.api_key_env
    except Exception:  # noqa: BLE001 - only absence matters for the guide
        missing_model = True
        key_env = (
            os.environ.get("PLATFORM_MODEL_API_KEY_ENV", "").strip()
            or DEFAULT_API_KEY_ENV
        )
    key_missing = not bool(os.environ.get(key_env, "").strip())
    if not missing_model and not key_missing:
        return []

    lines = [
        "首次使用需要配置模型（BYOK，一次填好即可；本提示不会显示密钥值）：",
        f"  1. 创建目录：{user_config_dir()}",
    ]
    if missing_model:
        lines.extend(
            [
                f"  2. 写入 {user_config_path()}：",
                '     {"provider": "deepseek", "model": "deepseek-chat"}',
                "     （model 换成你实际可用的精确模型名）",
            ]
        )
    if key_missing:
        lines.extend(
            [
                f"  {'3' if missing_model else '2'}. 在 {user_env_path()} "
                f"写入一行：{key_env}=<你的密钥>",
            ]
        )
    lines.extend(
        [
            "  最后运行 drbug check 验证。",
            "  兼容说明：平台目录的 model_config.json 与 .env.local 仍会被读取。",
        ]
    )
    return lines


class Printer:
    """Minimal output abstraction: rich when it helps, plain print otherwise."""

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self._console = None
        try:
            if self.stream.isatty():
                from rich.console import Console

                self._console = Console(file=self.stream)
        except Exception:  # noqa: BLE001 - rich is optional decoration only
            self._console = None

    def out(self, text: str = "", style: str | None = None) -> None:
        if self._console is not None:
            self._console.print(text, style=style, markup=False, highlight=False)
        else:
            print(text, file=self.stream)


def _display_path(path: Path) -> str:
    """Prefer a cwd-relative path for human output; fall back to absolute."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _load_tool_module(module_name: str, path: Path):
    """Import a standalone tool file without requiring a package __init__."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load tool module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _run_tool_file(path: Path) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, str(path)],
        cwd=PLATFORM_ROOT,
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    return completed.returncode, output


def _run_tool(module_name: str, filename: str) -> tuple[int, str]:
    """Run a tools/*.py checker in-process when possible, else by subprocess."""
    path = TOOLS_DIR / filename
    try:
        module = _load_tool_module(module_name, path)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.main()
        return (0 if code is None else int(code)), buffer.getvalue()
    except Exception:  # noqa: BLE001 - import/in-process path failed; use CLI
        return _run_tool_file(path)


def _key_status_line(env_name: str) -> str:
    """Report presence only — the value itself must never be printed."""
    configured = bool(os.environ.get(env_name, "").strip())
    return f"密钥状态: {env_name} {'已设置' if configured else '未设置'}（不显示值）"


def _profile_line(profile, active: bool) -> str:
    """One profile row for check/model: name, provider/model, key presence."""
    from core.model_config import key_is_set, model_capabilities

    marker = "*" if active else " "
    effort = (
        f" · reasoning_effort={profile.reasoning_effort}"
        if profile.reasoning_effort
        else ""
    )
    key_state = "已设置" if key_is_set(profile) else "未设置"
    caps = model_capabilities(profile.model)
    abilities = (
        f" · 看图 {'有' if caps.vision else '无'}"
        f" · 思考 {'有' if caps.thinking else '无'}"
    )
    auto = " · 自动生成" if getattr(profile, "auto_generated", False) else ""
    return (
        f"  {marker} {profile.name} · {profile.provider}/{profile.model}"
        f" · 密钥 {profile.api_key_env} {key_state}{effort}{abilities}{auto}"
    )


def _role_lines() -> list[str]:
    """Effective secretary/worker line for model displays."""
    from core.model_config import (
        effective_role_profiles,
        load_profile_catalog,
        model_capabilities,
    )

    try:
        catalog = load_profile_catalog()
        roles = effective_role_profiles()
    except Exception:  # noqa: BLE001 - display only
        return []
    by_name = {p.name: p for p in catalog.profiles}

    def describe(name: str | None) -> str:
        if name is None:
            return "自动生成（首次派活时生成）"
        profile = by_name.get(name)
        if profile is None:
            return name
        effort = profile.reasoning_effort or "未设置"
        return f"{name}（effort={effort}）"

    lines = [
        "  生效角色: "
        f"秘书: {describe(roles['secretary'])} · "
        f"工人: {describe(roles['worker'])}"
        + ("" if roles["mapped"] else "（未配映射，全员统一）")
    ]
    secretary = by_name.get(roles["secretary"])
    if secretary is not None and not model_capabilities(
        secretary.model
    ).vision:
        lines.append(
            "  注意: 秘书当前档案不能看图，附图时会被提示降级。"
        )
    return lines


def _profile_lines(catalog) -> list[str]:
    return [
        f"  当前 profile: {catalog.active}",
        "  profiles:",
        *(
            _profile_line(profile, profile.name == catalog.active)
            for profile in catalog.profiles
        ),
        *_role_lines(),
    ]


def _model_config_lines() -> list[str]:
    """Describe provider/model/key-presence without ever revealing a secret."""
    from core.model_config import config_source_lines, load_profile_catalog

    sources = [f"  {line}" for line in config_source_lines()]
    try:
        from core.model_config import load_config

        cfg = load_config()
        catalog = load_profile_catalog()
    except Exception as exc:  # noqa: BLE001 - report the reason, not a secret
        key_env = (
            os.environ.get("PLATFORM_MODEL_API_KEY_ENV", "").strip()
            or "DEEPSEEK_API_KEY"
        )
        return [
            "模型配置: 未配置",
            "  配置来源: 未找到可用配置（将使用默认值）",
            f"  原因: {type(exc).__name__}: {exc}",
            *sources,
            _key_status_line(key_env),
            "  提示: 按下方「首次配置」填写用户级配置即可，"
            "平台目录旧配置仍兼容。",
        ]
    return [
        f"模型配置: provider={cfg.provider} model={cfg.model}",
        *_profile_lines(catalog),
        f"  配置来源: {cfg.source}",
        *sources,
        _key_status_line(cfg.api_key_env),
    ]


def _model_label() -> str:
    try:
        from core.model_config import load_config

        cfg = load_config()
    except Exception:  # noqa: BLE001 - banner only; check subcommand explains
        return "未配置（见 drbug check）"
    return f"{cfg.model}（provider={cfg.provider}）"


def _cmd_model(args, printer: Printer) -> int:
    """List configured profiles or persist ``active_profile`` (never a key)."""
    from core.model_config import (
        activate_role_profile,
        load_profile_catalog,
        load_role_profiles,
        resolve_profile,
    )

    action = getattr(args, "action", None)
    if action in (None, "list"):
        try:
            catalog = load_profile_catalog()
        except Exception as exc:  # noqa: BLE001 - config errors are reportable
            printer.out(
                f"模型配置读取失败: {type(exc).__name__}: {exc}",
                style="bold red",
            )
            return 1
        printer.out("== drbug model — 已配置 profiles ==")
        for line in _profile_lines(catalog):
            printer.out(line)
        printer.out(
            "  提示: 用 drbug model use <name> 切换 active_profile；"
            "密钥只从环境变量/用户 .env 读取，不显示值。",
            style="dim",
        )
        return 0

    if action != "use":
        printer.out(
            "用法: drbug model [use <name>]（省略 use 时列出 profiles）",
            style="bold red",
        )
        return 2
    name = getattr(args, "name", None)
    if not name:
        printer.out("用法: drbug model use <name>", style="bold red")
        return 2
    try:
        target = activate_role_profile(name)
        profile = resolve_profile(name)
    except ValueError as exc:
        printer.out(f"切换失败: {exc}", style="bold red")
        return 1
    except OSError as exc:
        printer.out(
            f"切换失败: 无法写入用户配置: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 1

    if load_role_profiles():
        printer.out(
            f"已切换秘书档: {profile.name}"
            f"（{profile.provider}/{profile.model}；工人档不变）",
            style="bold green",
        )
    else:
        printer.out(
            f"已切换 active_profile: {profile.name}"
            f"（{profile.provider}/{profile.model}）",
            style="bold green",
        )
    printer.out(f"已写入: {target}（只写 profile 名，不含密钥）")
    return 0


def _cmd_setup(args, printer: Printer) -> int:
    """First-run wizard: provider -> model -> API key (masked input).

    The interactive path asks three questions; ``--scripted`` reads
    DRBUG_SETUP_PROVIDER / DRBUG_SETUP_MODEL / DRBUG_SETUP_API_KEY
    from the environment instead (for the cleanroom acceptance script).
    The key value is only ever handed to core.setup_wizard.run_setup,
    which stores it in the user env file — it is never printed here.
    """
    import getpass
    import os

    from core.setup_wizard import (
        choice_for,
        provider_choices,
        run_setup,
        setup_status,
    )

    if getattr(args, "scripted", False):
        provider = os.environ.get("DRBUG_SETUP_PROVIDER", "").strip()
        model = os.environ.get("DRBUG_SETUP_MODEL", "").strip()
        api_key = os.environ.get("DRBUG_SETUP_API_KEY", "")
        if not provider or not api_key.strip():
            printer.out(
                "脚本模式需要 DRBUG_SETUP_PROVIDER 与 DRBUG_SETUP_API_KEY",
                style="bold red",
            )
            return 2
        if not model:
            try:
                model = choice_for(provider).default_model or ""
            except ValueError as exc:
                printer.out(str(exc), style="bold red")
                return 2
        if not model:
            printer.out(
                "脚本模式需要 DRBUG_SETUP_MODEL（该厂商没有预填默认模型）",
                style="bold red",
            )
            return 2
        try:
            run_setup(provider, model, api_key)
        except ValueError as exc:
            printer.out(str(exc), style="bold red")
            return 2
    else:
        choices = provider_choices()
        printer.out("== drbug setup — 先配好模型，产品才能干活 ==")
        printer.out("选一家模型厂商，再填模型名和你的 API key。")
        printer.out("密钥只存进用户密钥文件（权限 600），不写进配置、不回显。")
        for index, choice in enumerate(choices, 1):
            mark = "已真测" if choice.verified else "未端到端验证"
            printer.out(f"  {index}. {choice.provider}（{mark}）")
        try:
            raw = input("选厂商编号 [1]：").strip()
            picked = choices[int(raw) - 1] if raw else choices[0]
            default_model = picked.default_model or ""
            prompt = f"模型名 [{default_model}]：" if default_model else "模型名："
            model = input(prompt).strip() or default_model
            if not model:
                printer.out("模型名不能为空", style="bold red")
                return 2
            api_key = getpass.getpass(
                f"粘贴 {picked.provider} 的 API key（输入不回显）："
            )
        except (EOFError, KeyboardInterrupt):
            printer.out("已取消，还没写入任何配置。")
            return 2
        except (ValueError, IndexError):
            printer.out("厂商编号不对", style="bold red")
            return 2
        try:
            run_setup(picked.provider, model, api_key)
        except ValueError as exc:
            printer.out(str(exc), style="bold red")
            return 2

    status = setup_status()
    if status.configured:
        printer.out(
            f"配置完成：{status.provider}/{status.model}，密钥已设置。"
            "现在可以运行 drbug 了。",
            style="bold green",
        )
        return 0
    printer.out("配置写进去了，但体检还没过：", style="bold red")
    for problem in status.problems:
        printer.out(f"  - {problem}")
    return 2



def _cmd_check(args, printer: Printer) -> int:
    printer.out("== drbug check — 环境体检 ==")

    env_code, env_output = _run_tool("_cifix_tool_env_check", "env_check.py")
    printer.out("-- 环境基线 --")
    for line in env_output.splitlines():
        printer.out(f"  {line}")
    env_ok = env_code == 0 and "ENV_CHECK_PASS" in env_output

    printer.out("-- 模型配置 --")
    for line in _model_config_lines():
        printer.out(f"  {line}")
    onboarding = _onboarding_lines()
    if onboarding:
        printer.out("")
        for line in onboarding:
            printer.out(f"  {line}")

    printer.out("-- 验收引擎 --")
    from acceptor.parsers import PARSERS

    engine_ok = bool(PARSERS)
    printer.out(
        f"  验收引擎: {'可用' if engine_ok else '不可用'}"
        f"（解析器: {'/'.join(sorted(PARSERS))}）"
    )

    ok = env_ok and engine_ok
    printer.out("")
    printer.out(
        f"体检结论: {'通过' if ok else '未通过'}（环境={'OK' if env_ok else 'FAIL'}, "
        f"引擎={'OK' if engine_ok else 'FAIL'}）",
        style="bold green" if ok else "bold red",
    )
    return 0 if ok else 1


def _cmd_agents(args, printer: Printer) -> int:
    """List employee archives for a workspace. Read-only."""
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        printer.out(f"工作区不存在: {workspace}", style="bold red")
        return 2
    from core.agent_archive import list_archives
    from core.autoprofile import default_profile_name

    profile_dir = _profiles_root_for(workspace) / default_profile_name(workspace)
    try:
        rows = list_archives(profile_dir)
    except OSError as exc:
        printer.out(
            f"读取员工档案失败: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 2
    printer.out("== drbug agents — 员工档案 ==")
    printer.out(f"工作区: {_display_path(workspace)}")
    printer.out(f"档位: {_display_path(profile_dir)}")
    if not rows:
        printer.out("没有员工档案。")
        return 0
    for row in rows:
        printer.out(f"- {row['name']}")
    printer.out(f"共 {len(rows)} 名")
    return 0


def _cmd_team(args, printer: Printer) -> int:
    """列名册，或把增删落盘到 profile 派单表。"""
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        printer.out(f"工作区不存在: {workspace}", style="bold red")
        return 2
    from core.roster import (
        RosterError,
        add_worker,
        format_add_result,
        format_remove_result,
        format_roster_listing,
        list_roster,
        remove_worker,
    )

    profiles_root = _profiles_root_for(workspace)
    action = args.action
    try:
        if action is None:
            if args.name:
                printer.out(
                    "用法: drbug team <workspace> "
                    "[add <name> --module <path> | remove <name>]",
                    style="bold red",
                )
                return 2
            rows = list_roster(workspace, profiles_root=profiles_root)
            printer.out("== drbug team — 名册 ==")
            printer.out(f"工作区: {_display_path(workspace)}")
            printer.out(format_roster_listing(rows))
            return 0
        if action == "add":
            if not args.name or not args.module:
                printer.out(
                    "用法: drbug team <workspace> add <name> --module <path>",
                    style="bold red",
                )
                return 2
            result = add_worker(
                workspace,
                args.name,
                args.module,
                profiles_root=profiles_root,
                channel="boss",
            )
            printer.out(format_add_result(result))
            return 0
        if not args.name:
            printer.out(
                "用法: drbug team <workspace> remove <name>",
                style="bold red",
            )
            return 2
        result = remove_worker(
            workspace, args.name, profiles_root=profiles_root
        )
        printer.out(format_remove_result(result))
        return 0
    except (RosterError, OSError, ValueError) as exc:
        printer.out(f"拒绝: {exc}", style="bold red")
        return 2


def _cmd_brief(args, printer: Printer) -> int:
    """查看项目简报；--generate 时通读项目生成（调用模型）。"""
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        printer.out(f"工作区不存在: {workspace}", style="bold red")
        return 2
    from core import brief as brief_mod

    profile_dir = brief_mod.profile_dir_for(workspace)
    if not args.generate:
        printer.out(
            brief_mod.format_brief_for_display(workspace, profile_dir)
        )
        return 0
    if not profile_dir.is_dir():
        from core.autoprofile import build_profile

        try:
            build_profile(
                workspace,
                out_root=profile_dir.parent,
                name=profile_dir.name,
            )
        except Exception as exc:  # noqa: BLE001 - reportable
            printer.out(
                f"建档失败: {type(exc).__name__}: {exc}",
                style="bold red",
            )
            return 2
    try:
        result = brief_mod.generate_brief(
            workspace, profile_dir, confirm=args.confirm
        )
    except Exception as exc:  # noqa: BLE001 - reportable
        printer.out(
            f"简报生成失败: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 2
    if result["status"] == "refused_hand_edited":
        printer.out(
            "已有简报与上次生成不一致（可能被手动改过），拒绝覆盖。"
            "确认要重新生成请加 --confirm。",
            style="bold red",
        )
        return 2
    printer.out(f"简报已生成: {_display_path(Path(result['brief_path']))}")
    printer.out(
        f"已核实规则 {result['rules']} 条；"
        f"未核实 {result['unverified']} 条；"
        f"无出处丢弃 {result['dropped_no_source']} 条"
    )
    if result["skipped_secret_names"]:
        printer.out(
            f"跳过凭据类文件 {len(result['skipped_secret_names'])} 个"
            f"（仅记名）: {', '.join(result['skipped_secret_names'])}"
        )
    return 0


def _cmd_profile(args, printer: Printer) -> int:
    """Programmatic profile build: no model call, no API spend."""
    from core.autoprofile import build_profile

    repo = Path(args.repo).expanduser().resolve()
    printer.out("== drbug profile — 自动建档 ==")
    try:
        result = build_profile(
            repo, out_root=_profiles_root_for(repo), name=args.name
        )
    except Exception as exc:  # noqa: BLE001 - reportable setup error
        printer.out(
            f"建档失败: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 2

    printer.out(f"源仓库: {_display_path(result.repo_path)}")
    printer.out(f"档位目录: {_display_path(result.profile_dir)}")
    printer.out(
        f"检测包: {'、'.join(result.packages) or '无'} "
        f"（vault package={result.metadata.get('vault_package')}）"
    )
    printer.out(
        f"测试: {result.test_total} 条（通过 {result.passed_count}，"
        f"当前失败 {result.failed_count}）"
    )
    printer.out(
        "专家: "
        + "、".join(
            item["id"] for item in result.dispatch_table["specialists"]
        )
    )
    printer.out(
        f"歧义: {len(result.ambiguities)} 条"
        + (
            "（详见 PROFILE.md 的「歧义与未把握处」）"
            if result.ambiguities
            else ""
        )
    )
    printer.out(f"派单表: {_display_path(result.dispatch_table_path)}")
    printer.out(f"验收金库: {_display_path(result.vault_dir)}")
    printer.out(f"建档报告: {_display_path(result.markdown_path)}")
    return 0


def _shell_quote(text: str) -> str:
    """Readable shell quoting: prefer double quotes, fall back to shlex."""
    if '"' not in text:
        return f'"{text}"'
    return shlex.quote(text)


def _print_repro_result(printer: Printer, result) -> None:
    """Print a triage result: symptom comparison first, test code as detail."""
    from core.repro import comparison_lines

    printer.out(f"分诊状态: {result.status}")
    if result.reason:
        printer.out(f"原因: {result.reason}")
    if result.test_path is not None:
        printer.out(f"暂存候选: {_display_path(result.test_path)}")
    printer.out(
        f"尝试次数: {len(result.attempts)}"
        + (
            "（判定：" + "、".join(
                str(item.get("verdict", "?")) for item in result.attempts
            ) + "）"
            if result.attempts
            else ""
        )
    )
    if result.status == "REPRO_READY":
        printer.out("")
        for line in comparison_lines(result.comparison):
            printer.out(line)
        printer.out("")
        printer.out("请确认：这复现的是不是你遇到的那个问题？")
        printer.out(
            "  只有你本人确认「是」才会冻结；说「不是」则候选作废、不冻结。"
        )
    if result.test_source:
        printer.out("")
        printer.out("-- 技术细节：候选测试全文（可展开，不作为确认依据） --")
        for line in result.test_source.splitlines():
            printer.out(f"  {line}")
    if result.failing_output:
        printer.out("-- 技术细节：程序验真输出（末尾 40 行） --")
        for line in result.failing_output.splitlines()[-40:]:
            printer.out(f"  {line}")
    if result.status == "REPRO_READY":
        printer.out(
            "验真结论: REPRO_READY（候选测试在原始代码副本上以 "
            "AssertionError 失败）",
            style="bold green",
        )
    else:
        printer.out("验真结论: CANNOT_REPRODUCE（候选未通过程序门）", style="red")


def _stdin_is_interactive() -> bool:
    """Terminal interaction check; redirected stdin/stdout never blocks a script."""
    try:
        if not (sys.stdin is not None and sys.stdin.isatty()):
            return False
        return bool(sys.stdout is not None and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _ask_symptom_confirmation(printer: Printer) -> bool:
    """Ask about the symptom (not the test); default and EOF refuse."""
    printer.out("")
    printer.out("这复现的是不是你遇到的那个问题？")
    printer.out("  [y] 是，就是这个问题 → 冻结这条复现测试")
    printer.out("  [N] 不是 / 直接回车 → 候选作废，不冻结（默认）")
    printer.out("请输入 y 或 N 后回车: ")
    try:
        answer = sys.stdin.readline()
    except (AttributeError, OSError, ValueError):
        return False
    if not answer:
        return False
    return answer.strip().lower() in {
        "y",
        "yes",
        "是",
        "对",
        "确认",
        "是，就是这个问题",
    }


def _print_freeze_receipt(printer: Printer, frozen) -> None:
    printer.out("-- 冻结回执 --")
    printer.out(f"冻结文件: {_display_path(frozen.repo_test_path)}")
    printer.out(f"sha256: {frozen.sha256}")
    printer.out(
        f"profile: {_display_path(frozen.profile_dir)}"
        f"（origin={frozen.origin or 'repro'}）"
    )
    if frozen.receipt_path is not None:
        printer.out(f"回执留档: {_display_path(frozen.receipt_path)}")
    printer.out(
        "说明: 此后改动该文件即判 TAMPERED（冻结验收会直接判失败）。"
    )


def _freeze_and_report(
    printer: Printer, repro_mod, repo: Path, profiles_root: Path
) -> int:
    """Freeze an existing verified candidate and print the receipt."""
    try:
        frozen = repro_mod.freeze_repro(
            repo, confirm=True, profiles_root=profiles_root
        )
    except Exception as exc:  # noqa: BLE001 - reportable setup failure
        printer.out(
            f"冻结失败: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 2

    if frozen.status != repro_mod.FROZEN:
        printer.out(
            f"冻结未完成（{frozen.status}）: {frozen.reason}",
            style="bold red",
        )
        return 2

    printer.out("")
    printer.out("冻结完成", style="bold green")
    printer.out(f"仓库测试: {_display_path(frozen.repo_test_path)}")
    printer.out(f"profile 路径: {_display_path(frozen.profile_dir)}")
    printer.out(f"派单表: {_display_path(frozen.dispatch_table_path)}")
    printer.out(f"验收金库: {_display_path(frozen.vault_dir)}")
    _print_freeze_receipt(printer, frozen)
    printer.out(
        "后续修复（既有 --profile auto 路径）: "
        f"drbug run --arm C --workspace {_shell_quote(str(repo))} "
        "--profile auto"
    )
    return 0


def _cmd_repro(args, printer: Printer) -> int:
    """Repro-first path: propose a failing test, then freeze only on request."""
    from core import repro as repro_mod

    repo = Path(args.repo).expanduser().resolve()
    profiles_root = _profiles_root_for(repo)
    symptom = str(args.symptom)
    printer.out("== drbug repro — 无测试仓库的复现测试分诊 ==")
    printer.out(f"仓库: {_display_path(repo)}")
    printer.out(f"症状: {symptom}")

    if not args.confirm_freeze:
        try:
            result = repro_mod.propose_repro(
                repo, symptom, profiles_root=profiles_root
            )
        except Exception as exc:  # noqa: BLE001 - reportable setup failure
            printer.out(
                f"分诊失败: {type(exc).__name__}: {exc}",
                style="bold red",
            )
            return 2
        _print_repro_result(printer, result)
        if result.status != repro_mod.REPRO_READY:
            printer.out(
                "候选未通过程序验真，未写入目标仓库，也不会冻结。",
                style="red",
            )
            return 1

        if _stdin_is_interactive():
            if _ask_symptom_confirmation(printer):
                return _freeze_and_report(
                    printer, repro_mod, repo, profiles_root
                )
            rejected = repro_mod.reject_repro(
                repo,
                reason="用户在终端回答这不是其遇到的问题。",
                profiles_root=profiles_root,
            )
            printer.out("")
            printer.out(rejected.reason, style="yellow")
            printer.out(
                "结论: 未冻结，目标仓库未写入任何文件。"
                "如果症状描述有偏差，请补充后重新运行："
            )
            printer.out(
                f"  drbug repro {_shell_quote(str(repo))} "
                '--symptom "更准确的症状描述"'
            )
            return 1

        command = (
            f"drbug repro {_shell_quote(str(repo))} "
            f"--symptom {_shell_quote(symptom)} --confirm-freeze"
        )
        printer.out("")
        printer.out(
            "候选只在暂存区，尚未写入目标仓库。请确认上面的症状对照确实"
            "是你的问题；确认后执行："
        )
        printer.out(f"  {command}", style="bold")
        return 0

    # --confirm-freeze: reuse an existing REPRO_READY candidate when present,
    # otherwise run triage once, then freeze behind the confirm gate.
    try:
        candidate = repro_mod.load_candidate(
            repo, profiles_root=profiles_root
        )
        if (
            isinstance(candidate, dict)
            and candidate.get("status") == repro_mod.REPRO_READY
            and candidate.get("test_source")
        ):
            printer.out("")
            comparison = repro_mod.build_comparison(
                str(candidate.get("symptom") or symptom),
                str(candidate.get("test_source") or ""),
                str(candidate.get("failing_output") or ""),
            )
            for line in repro_mod.comparison_lines(comparison):
                printer.out(line)
            printer.out(
                "（已按 --confirm-freeze 视为你本人确认了上面的症状。）"
            )

        frozen = repro_mod.freeze_repro(
            repo, confirm=True, profiles_root=profiles_root
        )
        if frozen.status == repro_mod.NO_CANDIDATE:
            printer.out(
                "未发现可冻结的 REPRO_READY 候选，先运行分诊..."
            )
            proposed = repro_mod.propose_repro(
                repo, symptom, profiles_root=profiles_root
            )
            _print_repro_result(printer, proposed)
            if proposed.status != repro_mod.REPRO_READY:
                printer.out(
                    "候选未通过程序验真，不能冻结。", style="bold red"
                )
                return 1
            frozen = repro_mod.freeze_repro(
                repo, confirm=True, profiles_root=profiles_root
            )
    except Exception as exc:  # noqa: BLE001 - reportable setup failure
        printer.out(
            f"冻结失败: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 2

    if frozen.status != repro_mod.FROZEN:
        printer.out(
            f"冻结未完成（{frozen.status}）: {frozen.reason}",
            style="bold red",
        )
        return 2

    printer.out("冻结完成", style="bold green")
    printer.out(f"仓库测试: {_display_path(frozen.repo_test_path)}")
    printer.out(f"profile 路径: {_display_path(frozen.profile_dir)}")
    printer.out(f"派单表: {_display_path(frozen.dispatch_table_path)}")
    printer.out(f"验收金库: {_display_path(frozen.vault_dir)}")
    _print_freeze_receipt(printer, frozen)
    printer.out(
        "后续修复（既有 --profile auto 路径）: "
        f"drbug run --arm C --workspace {_shell_quote(str(repo))} "
        "--profile auto"
    )
    return 0


def _event_printer(printer: Printer):
    """Build the live progress callback passed to ``run_arm``."""

    def on_event(event: dict) -> None:
        kind = event.get("event")
        if kind == "run_started":
            kb_state = "开启" if event.get("kb_enabled") else "关闭"
            printer.out(
                f"工作副本: {event.get('workspace')}"
                f"（arm={event.get('arm')}, actors={event.get('actors')}, "
                f"kb={kb_state}）",
                style="dim",
            )
        elif kind == "verdict":
            round_no = event.get("round", 0)
            label = "第 0 轮（首验）" if round_no == 0 else f"第 {round_no} 轮"
            status = str(event.get("status", ""))
            failed_count = event.get("failed_count", 0)
            style = "green" if status == "PASS" else "red"
            printer.out(
                f"{label} 验收 {status} — {failed_count} 条失败",
                style=style,
            )
        elif kind == "dispatch":
            assignments = event.get("assignments", {})
            bits = "、".join(
                f"{actor_id}×{len(nodes)}"
                for actor_id, nodes in assignments.items()
            )
            printer.out(
                f"派单（第 {event.get('round')} 轮）: {bits or '无'}",
                style="cyan",
            )
        elif kind == "actor_done":
            mark = "✓" if event.get("applied") else "✗"
            note = str(event.get("note", ""))
            if len(note) > NOTE_LIMIT:
                note = note[:NOTE_LIMIT] + "…"
            printer.out(
                f"  {mark} {event.get('actor_id')} 完成 — {note}",
                style="green" if event.get("applied") else "yellow",
            )
        elif kind == "run_finished":
            printer.out(
                f"收尾信号: outcome={event.get('outcome')} "
                f"轮次={event.get('rounds')} "
                f"遗留失败={len(event.get('final_failed', []))}",
                style="bold",
            )

    return on_event


def _run_command(args, printer: Printer) -> int:
    arm = str(args.arm).upper()
    actors = "agentscope"  # 产品定版：只用真实后端
    workspace = (
        Path(args.workspace).expanduser().resolve()
        if getattr(args, "workspace", None) is not None
        else Path.cwd().resolve()
    )
    runs_root = _runs_root_for(workspace)
    profiles_root = _profiles_root_for(workspace)
    profile_arg = getattr(args, "profile", None)
    profile = str(profile_arg) if profile_arg is not None else "auto"
    vault_dir = getattr(args, "vault", None)
    kb_path = getattr(args, "kb", None)
    if args.max_rounds is None:
        from core.model_config import load_max_rounds

        try:
            max_rounds = load_max_rounds()
        except ValueError as exc:
            printer.out(f"轮次上限配置无效: {exc}", style="bold red")
            return 2
    else:
        max_rounds = int(args.max_rounds)
    profile_dir = None
    if profile == "auto":
        from core.autoprofile import build_profile, default_profile_name

        profile_name = default_profile_name(workspace)
        profile_dir = profiles_root / profile_name
        if not profile_dir.is_dir():
            printer.out(
                f"自动建档: {workspace} -> {_display_path(profile_dir)}"
            )
            try:
                result = build_profile(
                    workspace, out_root=profiles_root, name=profile_name
                )
            except Exception as exc:  # noqa: BLE001 - reportable setup error
                printer.out(
                    f"自动建档失败: {type(exc).__name__}: {exc}",
                    style="bold red",
                )
                return 2
            printer.out(
                f"建档完成: 包={'、'.join(result.packages) or '无'} "
                f"测试={result.test_total} 失败={result.failed_count} "
                f"报告={_display_path(result.markdown_path)}"
            )
        else:
            printer.out(f"使用已有档位: {_display_path(profile_dir)}")
        vault_dir = vault_dir or profile_dir / "vault"
        kb_path = kb_path or profile_dir / "dispatch_table.json"
    else:
        profile_dir = profiles_root / profile
        if not profile_dir.is_dir():
            printer.out(
                f"档案不存在: {_display_path(profile_dir)}"
                "（请先用 drbug profile 建档）",
                style="bold red",
            )
            return 2
        vault_dir = vault_dir or profile_dir / "vault"
        kb_path = kb_path or profile_dir / "dispatch_table.json"
    if args.out is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = runs_root / f"cli_run_{stamp}.json"
    else:
        out = Path(args.out)

    printer.out("=" * BANNER_WIDTH, style="bold")
    printer.out("drbug run — 冻结验收 + 按失败证据改派", style="bold cyan")
    printer.out(f"策略: 臂 {arm}")
    printer.out(f"后端: {actors}")
    printer.out(
        f"配置: profile={profile} vault={vault_dir} kb={kb_path}"
    )
    printer.out(f"模型: {_model_label()}")
    printer.out(f"工作区: {workspace}")
    printer.out(f"轮次上限: {max_rounds}")
    if actors == "agentscope":
        os.environ.setdefault("DRBUG_TOKEN_METER", "1")
    if actors != "scripted":
        printer.out(
            "!! 将调用真实模型、消耗 API 额度（仅提示，不拦截）",
            style="bold red",
        )
    printer.out("=" * BANNER_WIDTH, style="bold")

    parallel_n = int(getattr(args, "parallel", 1) or 1)
    if parallel_n < 1 or parallel_n > 8:
        printer.out(
            f"parallel 须在 1-8 之间，收到 {parallel_n}", style="bold red"
        )
        return 2
    if parallel_n > 1:
        printer.out(f"并行尝试: {parallel_n} 份（隔离副本，判卷择优）")

    from core.runner import run_arm

    base_events = _event_printer(printer)
    captured: dict[str, str] = {}

    def on_event(event: dict) -> None:
        if event.get("event") == "run_started":
            captured["workspace"] = str(event.get("workspace", ""))
        base_events(event)

    from core.autoprofile import work_root_for

    if parallel_n > 1:
        from core.parallel import run_parallel

        record = run_parallel(
            arm=arm,
            workspace_src=workspace,
            attempts=parallel_n,
            kb_enabled=True,
            max_rounds=max_rounds,
            work_root=work_root_for(workspace, runs_root),
            actor_backend=actors,
            on_event=on_event,
            vault_dir=vault_dir,
            kb_path=kb_path,
            profile_dir=profile_dir,
        ).record
    else:
        record = run_arm(
            arm=arm,
            workspace_src=workspace,
            kb_enabled=True,
            max_rounds=max_rounds,
            work_root=work_root_for(workspace, runs_root),
            actor_backend=actors,
            on_event=on_event,
            vault_dir=vault_dir,
            kb_path=kb_path,
            profile_dir=profile_dir,
        )

    if actors == "agentscope":
        from core.usage import format_run_usage_line

        # USAGE1：与运行记录同读 record.usage（统一口径），不再读老计数器。
        printer.out(
            format_run_usage_line(getattr(record, "usage", None)), style="dim"
        )
    elif actors == "llm":
        printer.out(
            "Token 用量: 不可用（llm 后端是外部 OpenCode 子进程，"
            "无逐 token 回传）",
            style="dim",
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    repair_lines: list[str] = []
    if profile_dir is not None and captured.get("workspace"):
        from core.repro import repair_comparison_lines

        repair_lines = repair_comparison_lines(
            workspace,
            captured["workspace"],
            name=profile_dir.name,
            profiles_root=profiles_root,
        )

    bundle_path = None
    bundle_error = ""
    if captured.get("workspace"):
        from core.results import build_result_bundle

        try:
            bundle_path = build_result_bundle(
                record,
                workspace,
                captured["workspace"],
                actor_backend=actors,
                bundle_root=runs_root / "bundles",
                record_path=out,
            )
        except Exception as exc:  # noqa: BLE001 - run result stays valid
            bundle_error = f"{type(exc).__name__}: {exc}"
    else:
        bundle_error = "未收到最终工作副本路径"

    printer.out("")
    printer.out(
        f"终局汇总: 结果={record.outcome} 轮次={len(record.rounds)} "
        f"记录路径={_display_path(out)}",
        style="bold green" if record.outcome == "PASS" else "bold red",
    )
    if bundle_path is not None:
        from core.results import describe_adoption

        printer.out(
            f"结果包: {_display_path(bundle_path)}"
            f"（采纳: {describe_adoption(bundle_path)}）",
            style="bold",
        )
    else:
        printer.out(f"结果包: 生成失败 — {bundle_error}", style="red")
    for line in repair_lines:
        printer.out(line)
    return 0 if record.outcome == "PASS" else 1


def _cmd_accept(args, printer: Printer) -> int:
    workspace = (
        Path(args.workspace).expanduser().resolve()
        if getattr(args, "workspace", None) is not None
        else Path.cwd().resolve()
    )
    profile = getattr(args, "profile", None)
    vault = getattr(args, "vault", None)
    if vault is None:
        # Only the workspace's own profile vault may judge it.
        from core.autoprofile import default_profile_name

        profile_name = (
            str(profile) if profile else default_profile_name(workspace)
        )
        candidate = (
            _profiles_root_for(workspace) / profile_name / "vault"
        )
        if candidate.is_dir():
            vault = candidate
        else:
            printer.out(
                "验收无法执行: 该工作区尚无冻结档案；"
                "请先建档（drbug profile）",
                style="bold red",
            )
            return 2
    try:
        from acceptor.acceptor import FrozenAcceptor

        verdict = FrozenAcceptor(vault).check(workspace)
    except Exception as exc:  # noqa: BLE001 - bad workspace/vault is reportable
        printer.out(
            f"验收无法执行: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 2

    style = "green" if verdict.status == "PASS" else "red"
    printer.out(f"验收结论: {verdict.status}", style=f"bold {style}")
    printer.out(
        f"通过/失败计数: {verdict.passed_count} / {verdict.failed_count}"
    )
    if verdict.tampered_files:
        printer.out("篡改文件清单: " + "、".join(verdict.tampered_files))
    else:
        printer.out("篡改文件清单: 无")
    return 0 if verdict.status == "PASS" else 1


def _cmd_chat(args, printer: Printer) -> int:
    """启动 Textual 全屏交互页（惰性 import，避免无 TTY 时拖慢其它子命令）。"""
    from core.tui import run as tui_run

    project_root = (
        Path(args.workspace_root).expanduser().resolve()
        if getattr(args, "workspace_root", None) is not None
        else Path.cwd().resolve()
    )
    onboarding = _onboarding_lines()
    if onboarding:
        for line in onboarding:
            printer.out(f"  {line}")
        printer.out("")
    previous = os.environ.get("DRBUG_CHAT_WORKSPACE")
    os.environ["DRBUG_CHAT_WORKSPACE"] = str(project_root)
    try:
        return int(
            tui_run(workspace_root=project_root, perm_mode=args.mode)
        )
    finally:
        if previous is None:
            os.environ.pop("DRBUG_CHAT_WORKSPACE", None)
        else:
            os.environ["DRBUG_CHAT_WORKSPACE"] = previous


def _cmd_apply(args, printer: Printer) -> int:
    """Apply a bundle's patch into its original workspace (guarded)."""
    from core.results import apply_bundle

    try:
        result = apply_bundle(
            args.bundle_dir,
            args.workspace,
            dry_run=args.dry_run,
        )
    except (ValueError, FileNotFoundError) as exc:
        printer.out(f"未应用: {exc}", style="bold red")
        return 1
    if result["dry_run"]:
        printer.out("== drbug apply — 试运行（只校验，不写文件）==")
    else:
        printer.out("== drbug apply — 已应用 ==")
    for item in result["files"]:
        printer.out(f"  {item['status']}: {item['path']}")
    if result["dry_run"]:
        printer.out("校验通过；去掉 --dry-run 即正式应用。")
    else:
        printer.out(f"已应用到: {result['workspace']}（采纳记录已更新）")
    return 0


def _cmd_mcp_list(args, printer: Printer) -> int:
    import asyncio as _asyncio

    from core.mcp_tools import MCPToolSet, load_mcp_servers

    try:
        servers = load_mcp_servers()
    except ValueError as exc:
        printer.out(f"未执行：{exc}")
        return 1
    if not servers:
        printer.out(
            "未登记任何 MCP 服务器。在 ~/.config/drbug/mcp.json "
            '写 {"servers": [{"name": "...", "command": "...", '
            '"args": [...]}]} 后再来。'
        )
        return 0

    async def _probe() -> MCPToolSet:
        toolset = MCPToolSet(workspace=Path.cwd())
        tools = await toolset.open()
        for tool in tools:
            printer.out(f"工具：{tool.name} — {tool.description}")
        await toolset.close()
        return toolset

    toolset = _asyncio.run(_probe())
    for spec in servers:
        printer.out(f"服务器：{spec['name']}（{spec['command']}）")
    for notice in toolset.notices:
        printer.out(f"提示：{notice}")
    return 0


def _cmd_js_inventory(args, printer: Printer) -> int:
    import json as _json

    from core.js_inventory import scan_js_ts_inventory

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        printer.out(f"未执行：仓库不存在：{repo}")
        return 1
    result = scan_js_ts_inventory(repo)
    printer.out(_json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("notice"):
        printer.out(f"提示：{result['notice']}")
    return 0


def _cmd_market_list(args, printer: Printer) -> int:
    from core.market import format_entry_line, installed_ids, load_catalog

    try:
        entries = load_catalog()
    except ValueError as exc:
        printer.out(f"未执行：{exc}")
        return 1
    if not entries:
        printer.out("扩展目录是空的。")
        return 0
    installed = installed_ids()
    for entry in entries:
        printer.out(format_entry_line(entry, entry["id"] in installed))
    printer.out(
        "安装：drbug market install <id>；卸载：drbug market uninstall <id>",
        style="dim",
    )
    return 0


def _cmd_market_install(args, printer: Printer) -> int:
    from core.market import install

    try:
        message = install(str(args.entry_id))
    except ValueError as exc:
        printer.out(f"未执行：{exc}")
        return 1
    printer.out(message)
    return 0


def _cmd_market_uninstall(args, printer: Printer) -> int:
    from core.market import uninstall

    try:
        message = uninstall(str(args.entry_id))
    except ValueError as exc:
        printer.out(f"未执行：{exc}")
        return 1
    printer.out(message)
    return 0


def _cmd_runs(args, printer: Printer) -> int:
    from core.runs import (
        format_run_detail,
        format_run_line,
        list_runs,
        load_run_detail,
    )

    if getattr(args, "runs_command", None) == "audit":
        from core.runs import format_audit_line, read_audit_entries

        workspace = Path(args.workspace).expanduser()
        log_path = workspace / ".drbug" / "audit.log"
        if not log_path.is_file():
            printer.out(f"该工作区暂无审计日志：{workspace}")
            return 0
        entries, corrupt = read_audit_entries(
            workspace, limit=int(getattr(args, "limit", 50) or 50)
        )
        if not entries:
            printer.out("审计日志是空的。")
            return 0
        for entry in entries:
            printer.out(format_audit_line(entry))
        if corrupt:
            printer.out(f"（另有 {corrupt} 行损坏已跳过）")
        return 0
    if getattr(args, "runs_command", None) == "show":
        prefix = str(args.prefix)
        summaries, _ = list_runs(limit=500)
        matches = [s for s in summaries if s.run_id.startswith(prefix)]
        if not matches:
            printer.out(f"找不到运行记录：{prefix}")
            return 1
        if len(matches) > 1:
            printer.out(f"前缀 {prefix} 命中多条，请补全：")
            for summary in matches:
                printer.out(format_run_line(summary))
            return 1
        for line in format_run_detail(load_run_detail(matches[0])):
            printer.out(line)
        return 0
    shown, skipped = list_runs(limit=int(getattr(args, "limit", 20) or 20))
    if not shown:
        printer.out("暂无运行记录")
        return 0
    for summary in shown:
        printer.out(format_run_line(summary))
    if skipped:
        printer.out(f"（另有 {skipped} 个损坏记录文件已跳过）")
    return 0


def _cmd_adopt(args, printer: Printer) -> int:
    """记录用户对结果包的采纳决定；绝不把 patch 应用到原仓库。"""
    from core.results import record_adoption

    bundle_dir = Path(args.bundle_dir)
    note = str(getattr(args, "note", "") or "")
    adopted = bool(args.adopted)

    printer.out("== drbug adopt — 采纳记录 ==")
    printer.out(f"结果包: {_display_path(bundle_dir)}")
    try:
        payload = record_adoption(bundle_dir, adopted, note)
    except Exception as exc:  # noqa: BLE001 - bad bundle path is reportable
        printer.out(
            f"采纳记录失败: {type(exc).__name__}: {exc}",
            style="bold red",
        )
        return 2

    printer.out(
        f"决定: {'采纳' if adopted else '拒绝'}",
        style="bold green" if adopted else "bold red",
    )
    printer.out(f"记录时间: {payload['decided_at']}")
    if note:
        printer.out(f"备注: {note}")
    printer.out(
        "说明: 这里只记录决定，不会把 patch.diff 应用到原仓库；"
        "是否合入由你决定。"
    )
    return 0


def _cmd_policy_check(args, printer: Printer) -> int:
    """Print one verdict and its tier. Does not write the audit log."""

    from core.policy import PolicyConfigError, PolicyEngine

    action_aliases = {
        "read": "read_file",
        "write": "write_file",
        "edit": "edit_file",
        "shell": "shell_exec",
        "exec": "shell_exec",
    }
    action = action_aliases.get(str(args.action), str(args.action))
    actor = str(args.actor).strip() if args.actor else "secretary"
    if not actor:
        actor = "secretary"
    engine = PolicyEngine(workspace=Path.cwd())
    try:
        decision = engine.evaluate(actor, action, args.target)
    except PolicyConfigError as exc:
        printer.out("verdict: deny")
        printer.out("tier: 3")
        printer.out(f"reason: 策略文件损坏，拒绝放行: {exc}")
        return 0
    printer.out(f"verdict: {decision.verdict}")
    printer.out(f"tier: {decision.tier}")
    printer.out(f"reason: {decision.reason}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drbug",
        description=(
            "drbug — 冻结验收与证据改派平台的命令行门面："
            "体检、机制自检、单臂运行、冻结验收与演示。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version="drbug 0.1.0"
    )
    subparsers = parser.add_subparsers(
        dest="command", required=False, metavar="<子命令>"
    )

    check_parser = subparsers.add_parser(
        "check",
        help="环境体检：AgentScope 基线、模型配置与验收金库",
        description=(
            "运行环境基线检查（复用 tools/env_check.py），"
            "报告模型 provider/model 与密钥环境变量是否设置（不显示密钥值），"
            "并统计验收金库文件数。"
        ),
    )
    check_parser.set_defaults(handler=_cmd_check)

    setup_parser = subparsers.add_parser(
        "setup",
        help="首次配置向导：选模型厂商、填模型与 API key",
        description=(
            "第一次使用先跑这个：选一家模型厂商、填模型名与 API key。"
            "密钥只存进用户密钥文件（~/.config/drbug/.env，权限 600），"
            "配置文件里只记厂商、模型与密钥变量名。"
        ),
    )
    setup_parser.add_argument(
        "--scripted",
        action="store_true",
        help="脚本模式：从 DRBUG_SETUP_PROVIDER/MODEL/API_KEY 环境变量读入（自动化验收用）",
    )
    setup_parser.set_defaults(handler=_cmd_setup)

    model_parser = subparsers.add_parser(
        "model",
        help="列出已配置模型 profiles / 切换 active_profile",
        description=(
            "不带参数时列出已配置的 profile、当前标记与密钥环境变量是否设置"
            "（绝不显示密钥值）；`drbug model use <name>` 把 active_profile "
            "写回用户级配置。"
        ),
    )
    model_parser.add_argument(
        "action",
        nargs="?",
        choices=["list", "use"],
        default=None,
        help="省略时等同 list；use 后必须跟 profile 名",
    )
    model_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="要激活的 profile 名（仅 use 时使用）",
    )
    model_parser.set_defaults(handler=_cmd_model)

    profile_parser = subparsers.add_parser(
        "profile",
        help="为任意 pytest 仓库自动建档（零模型调用）",
        description=(
            "程序化扫描仓库：发现包与测试、在副本上跑一次 pytest 采集失败证据、"
            "合并 import/traceback/命名约定三路映射，生成 "
            "profiles/<name>/dispatch_table.json、vault/、profile.json 与 PROFILE.md。"
        ),
    )
    profile_parser.add_argument(
        "repo",
        type=Path,
        nargs="?",
        default=Path("."),
        help="待建档的仓库路径（默认当前目录）",
    )
    profile_parser.add_argument(
        "--name",
        default=None,
        help="档位名称（默认取仓库目录名）",
    )
    profile_parser.set_defaults(handler=_cmd_profile)

    agents_parser = subparsers.add_parser(
        "agents",
        help="列出项目里的员工档案（IDENTITY / MEMORY）",
        description=(
            "只读列出工作区 profile 下的员工档案目录"
            "（agents/<员工名>/IDENTITY.md 与 MEMORY.md）。不创建、不修改档案。"
        ),
    )
    agents_parser.add_argument(
        "workspace",
        type=Path,
        nargs="?",
        default=Path("."),
        help="项目目录（默认当前目录）",
    )
    agents_parser.set_defaults(handler=_cmd_agents)

    team_parser = subparsers.add_parser(
        "team",
        help="查看或增删项目名册（写入 profile 派单表，不改派活规则）",
        description=(
            "名册来自该项目当前 profile 的派单表与档案目录，不另建存储。"
            "add / remove 会把增删落盘到 dispatch_table.json；"
            "删除只把档案归档到 agents/_archived/，不物理销毁。"
            "派活仍只走证据表。"
        ),
    )
    team_parser.add_argument(
        "workspace",
        type=Path,
        help="项目目录",
    )
    team_parser.add_argument(
        "action",
        nargs="?",
        choices=["add", "remove"],
        default=None,
        help="省略时列名册；add 需 --module；remove 需员工名",
    )
    team_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="add / remove 的员工名",
    )
    team_parser.add_argument(
        "--module",
        default=None,
        help="add 时的源文件或模块（必须是工作区内真实存在的路径）",
    )
    team_parser.set_defaults(handler=_cmd_team)

    brief_parser = subparsers.add_parser(
        "brief",
        help="项目简报：查看现有简报，或 --generate 生成（调用模型）",
        description=(
            "简报由秘书通读项目文档与测试提炼，每条规则带出处并经"
            "程序逐条核对；凭据类文件扫描时跳过不读。默认只查看；"
            "--generate 才生成（调用模型产生费用）；已有简报被手"
            "改过时须 --confirm 才重写。"
        ),
    )
    brief_parser.add_argument(
        "workspace",
        type=Path,
        nargs="?",
        default=Path("."),
        help="工作区路径（默认当前目录）",
    )
    brief_parser.add_argument(
        "--generate",
        action="store_true",
        help="通读项目生成简报（调用模型，产生费用）",
    )
    brief_parser.add_argument(
        "--confirm",
        action="store_true",
        help="已有简报被手改过时，确认覆盖重写",
    )
    brief_parser.set_defaults(handler=_cmd_brief)

    repro_parser = subparsers.add_parser(
        "repro",
        help="为无测试仓库生成候选复现测试（程序验真，用户确认症状后才冻结）",
        description=(
            "分诊 Agent 用只读工具阅读仓库，按症状写一条 pytest 候选测试；"
            "程序在仓库副本上验真（必须断言失败）。冻结前先向用户呈现"
            "「你描述的症状 → 原始代码上的实际表现 → 期望结果」三栏对照，"
            "由用户本人确认这就是其问题后才写入仓库 tests/ 并自动建档。"
            "终端交互运行时默认拒绝，加 --confirm-freeze 表示已预先确认。"
        ),
    )
    repro_parser.add_argument(
        "repo",
        type=Path,
        nargs="?",
        default=Path("."),
        help="待分诊的仓库路径（默认当前目录）",
    )
    repro_parser.add_argument(
        "--symptom",
        required=True,
        help="症状描述（例如“parse_duration('1h30m') 应返回 90”）",
    )
    repro_parser.add_argument(
        "--confirm-freeze",
        action="store_true",
        help=(
            "用户已明确确认「这就是我遇到的问题」：把验真通过的候选冻结进"
            "仓库 tests/ 并自动建档（未确认时绝不要加这个参数）"
        ),
    )
    repro_parser.set_defaults(handler=_cmd_repro)

    run_parser = subparsers.add_parser(
        "run",
        help="运行一个策略臂并实时打印进度",
        description="在工作区的私有副本上运行指定策略臂。",
    )
    run_parser.add_argument(
        "--arm",
        required=True,
        choices=["A", "B", "C"],
        help="策略臂：A=通用专家、B=朴素交接、C=按证据改派（推荐）",
    )
    run_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="待修复工作区路径（会被复制，不会原地修改；默认当前目录）",
    )
    run_parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help=(
            "最大修复轮数（默认读 ~/.config/drbug 的 max_rounds，"
            "未配置时为 6）"
        ),
    )
    run_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="运行记录 JSON 路径（默认 runs/cli_run_<时间戳>.json）",
    )
    run_parser.add_argument(
        "--profile",
        default=None,
        help=(
            "档案名（默认 auto：<项目>/.drbug/profiles/<仓库名>/，"
            "不存在则先自动建档）；也可指定已有档案名"
        ),
    )
    run_parser.add_argument(
        "--vault", type=Path, default=None, help="覆盖 profile 的验收金库路径"
    )
    run_parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="并行尝试份数（1-8，默认 1 = 不并行）：隔离副本同时跑、判卷择优",
    )
    run_parser.add_argument(
        "--kb", type=Path, default=None, help="覆盖 profile 的派单表路径"
    )
    run_parser.set_defaults(handler=_run_command)

    accept_parser = subparsers.add_parser(
        "accept",
        help="对工作区执行冻结验收",
        description="复制工作区代码，用冻结判卷面验收，打印 PASS/FAIL/TAMPERED。",
    )
    accept_parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="待验收工作区路径（默认当前目录）",
    )
    accept_parser.add_argument(
        "--profile",
        default=None,
        help="档案名（默认取工作区自己的档案）",
    )
    accept_parser.add_argument(
        "--vault", type=Path, default=None, help="覆盖验收金库路径"
    )
    accept_parser.set_defaults(handler=_cmd_accept)

    adopt_parser = subparsers.add_parser(
        "adopt",
        help="记录对结果包的采纳/拒绝决定（只记录，不合入）",
        description=(
            "把用户对结果包的决定写进 adoption.json。只会更新这一条记录，"
            "永远不会把 patch.diff 应用回原仓库；是否合入由用户自己决定。"
        ),
    )
    adopt_parser.add_argument(
        "bundle_dir",
        type=Path,
        help="结果包目录（runs/bundles/<run_id>/）",
    )
    decision = adopt_parser.add_mutually_exclusive_group(required=True)
    decision.add_argument(
        "--yes",
        dest="adopted",
        action="store_true",
        help="采纳该修复（只记录决定，不合入）",
    )
    decision.add_argument(
        "--no",
        dest="adopted",
        action="store_false",
        help="拒绝该修复",
    )
    adopt_parser.add_argument(
        "--note",
        default="",
        help="可选备注，写入 adoption.json",
    )
    adopt_parser.set_defaults(handler=_cmd_adopt)

    apply_parser = subparsers.add_parser(
        "apply",
        help="把结果包的补丁应用回原仓库（基线指纹防覆盖）",
        description=(
            "把结果包的 patch.diff 应用回 --workspace 指定的原仓库。"
            "应用前逐个被改文件与结果包里的基线指纹核对：原件被"
            "改过、已消失，或要新建的文件已存在，整包拒绝、一个"
            "字不写。没有基线指纹的旧包、已标记拒绝的包、涉及"
            "密钥/凭据路径的补丁同样拒绝。--dry-run 只校验预览。"
        ),
    )
    apply_parser.add_argument(
        "bundle_dir",
        type=Path,
        help="结果包目录（或其中的 record.json）",
    )
    apply_parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="要应用到的原仓库目录",
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验与预览，不写任何文件",
    )
    apply_parser.set_defaults(handler=_cmd_apply)

    js_inventory_parser = subparsers.add_parser(
        "js-inventory",
        help="扫描 JS/TS 仓库的结构清单（函数/类一览）",
    )
    js_inventory_parser.add_argument("repo", type=Path)
    js_inventory_parser.set_defaults(handler=_cmd_js_inventory)

    mcp_parser = subparsers.add_parser(
        "mcp", help="MCP 外接工具：查看已登记服务器与工具"
    )
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_list_parser = mcp_sub.add_parser(
        "list",
        help="列出服务器并试连、列出工具（逐次调用仍过门禁）",
    )
    mcp_list_parser.set_defaults(handler=_cmd_mcp_list)

    market_parser = subparsers.add_parser(
        "market", help="扩展市场（本地目录）：浏览、安装、卸载扩展"
    )
    market_sub = market_parser.add_subparsers(dest="market_command")
    market_list_parser = market_sub.add_parser(
        "list", help="列出本地扩展目录与安装状态"
    )
    market_list_parser.set_defaults(handler=_cmd_market_list)
    market_install_parser = market_sub.add_parser(
        "install", help="安装一个扩展（MCP 登记进 mcp.json，仍需策略放行）"
    )
    market_install_parser.add_argument("entry_id")
    market_install_parser.set_defaults(handler=_cmd_market_install)
    market_uninstall_parser = market_sub.add_parser(
        "uninstall", help="卸载一个扩展"
    )
    market_uninstall_parser.add_argument("entry_id")
    market_uninstall_parser.set_defaults(handler=_cmd_market_uninstall)

    runs_parser = subparsers.add_parser(
        "runs", help="运行记录：历次修复运行的列表与详情（只读）"
    )
    runs_parser.add_argument("--limit", type=int, default=20)
    runs_sub = runs_parser.add_subparsers(dest="runs_command")
    runs_show_parser = runs_sub.add_parser(
        "show", help="按运行 id 前缀查看一次运行的详情"
    )
    runs_show_parser.add_argument("prefix")
    runs_audit_parser = runs_sub.add_parser(
        "audit", help="看某工作区的门禁审计日志（谁被放行/拦下）"
    )
    runs_audit_parser.add_argument("workspace")
    runs_audit_parser.add_argument("--limit", type=int, default=50)
    runs_audit_parser.set_defaults(handler=_cmd_runs)
    runs_parser.set_defaults(handler=_cmd_runs)
    runs_show_parser.set_defaults(handler=_cmd_runs)

    policy_parser = subparsers.add_parser(
        "policy",
        help="查看一条权限裁决（只读，不写审计）",
        description=(
            "对动作和目标做一次策略裁决，打印结论与命中层级。"
            "不写审计日志，不修改策略文件。"
        ),
    )
    policy_sub = policy_parser.add_subparsers(
        dest="policy_command", required=True, metavar="<子命令>"
    )
    policy_check = policy_sub.add_parser(
        "check",
        help="打印一条裁决与命中层级（零副作用）",
        description=(
            "查看策略引擎的裁决。不写审计、不改策略。"
            "动作可用 read / write / edit / shell，或引擎原名 read_file、write_file、shell_exec。"
        ),
    )
    policy_check.add_argument(
        "action", help="动作，如 read、write、shell_exec"
    )
    policy_check.add_argument("target", help="目标路径或命令")
    policy_check.add_argument(
        "--actor",
        default=None,
        help="裁决主体（默认 secretary）",
    )
    policy_check.set_defaults(handler=_cmd_policy_check)

    return parser


def _cmd_home(printer: Printer) -> int:
    """No-argument entry: friendly banner, config status and first-run guide."""
    printer.out("=" * BANNER_WIDTH, style="bold")
    printer.out("Doctor Bug — 冻结验收 + 按失败证据改派", style="bold cyan")
    printer.out("=" * BANNER_WIDTH, style="bold")
    for line in _model_config_lines():
        printer.out(f"  {line}")
    onboarding = _onboarding_lines()
    if onboarding:
        printer.out("")
        for line in onboarding:
            printer.out(f"  {line}")
    printer.out("")
    printer.out("常用命令（在任意项目目录运行）：")
    printer.out("  drbug check           环境体检")
    printer.out("  drbug run --arm C     在当前项目上检测并修复")
    printer.out("  drbug                 全屏交互页（直接运行，无需子命令）")
    printer.out("  drbug --help          全部子命令")
    return 0


def main(argv: list[str] | None = None) -> int:
    from core.model_config import load_local_env

    load_local_env()
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "chat":
        print(
            "没有 chat 子命令：直接运行 drbug 就会打开交互页。",
            file=sys.stderr,
        )
        return 2
    args = build_parser().parse_args(argv)
    printer = Printer()
    handler = getattr(args, "handler", None)
    if handler is None:
        if _stdout_is_tty():
            return _cmd_chat(
                argparse.Namespace(workspace_root=None, mode="ask"), printer
            )
        return _cmd_home(printer)
    return int(handler(args, printer))


def _stdout_is_tty() -> bool:
    return sys.stdout.isatty()


if __name__ == "__main__":
    raise SystemExit(main())
