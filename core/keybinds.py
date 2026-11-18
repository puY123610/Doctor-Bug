"""键位注册表：TUI 快捷键的单一来源。

App、各弹窗与输入控件都读这张表，不在页面里再写死一份。注册表只标注
作用域并校验冲突，不自建分发器——按键仍由 Textual 的 BINDINGS / on_key
按焦点链分发。跨作用域优先级（高到低）：modal > input > page > global。

同一冲突域里两个动作不能占用同一个键。冲突域是
``(scope, screen)``：global / page 各一域；每个弹窗、每个输入控件各一域。
不同弹窗都用 escape 不算冲突，因为 Textual 不会把它们装进同一个
Screen.BINDINGS。若把五个弹窗的 escape 收成一个冲突域，默认表无法同时
满足「按弹窗分别登记」和「默认表无冲突」。

用户覆盖文件：:func:`keybinds_path`（``user_config_dir() / "keybinds.json"``，
默认 ``~/.config/drbug/keybinds.json``，遵循 ``XDG_CONFIG_HOME``）。

格式::

    {"cycle_effort": "f2", "open_commands": null}

字符串改键，``null`` 禁用。未知 action_id、非法键名、同冲突域撞键都拒绝，
写入 diagnostics；被拒绝的条目保留默认键，不许静默应用，也不许把别的
默认键挤掉。文件只在进程启动时读一次（:func:`active_registry`）。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from textual.binding import Binding
from textual.keys import KEY_NAME_REPLACEMENTS, KEY_TO_UNICODE_NAME, Keys

from .model_config import user_config_dir

SCOPE_GLOBAL = "global"
SCOPE_PAGE = "page"
SCOPE_MODAL = "modal"
SCOPE_INPUT = "input"
SCOPES: tuple[str, ...] = (SCOPE_GLOBAL, SCOPE_PAGE, SCOPE_MODAL, SCOPE_INPUT)
# 高到低。与 Textual 焦点链一致；这里只标注，不分发。
SCOPE_PRIORITY: tuple[str, ...] = (
    SCOPE_MODAL,
    SCOPE_INPUT,
    SCOPE_PAGE,
    SCOPE_GLOBAL,
)

# 与 core.tui 里 ModalScreen 的 id 一致。prompt-input 不是 Screen id，
# 只是 PromptInput.on_key 的冲突域。
SCREEN_PALETTE = "palette"
SCREEN_SETUP = "setup-screen"
SCREEN_TRUST = "workspace-trust"
SCREEN_PICKER = "model-picker"
SCREEN_CONFIRM = "confirm-modal"
SCREEN_PROMPT = "prompt-input"
SCREEN_PASTE_VIEW = "paste-view"
SCREEN_STATUS_VIEW = "status-view"
SCREEN_MCP_VIEW = "mcp-view"
SCREEN_HELP_VIEW = "help-view"
SCREEN_ERROR_VIEW = "error-view"
SCREEN_TIMELINE = "timeline-view"
KNOWN_CONTEXTS: frozenset[str] = frozenset(
    {
        SCREEN_PALETTE,
        SCREEN_SETUP,
        SCREEN_TRUST,
        SCREEN_PICKER,
        SCREEN_CONFIRM,
        SCREEN_PROMPT,
        SCREEN_PASTE_VIEW,
        SCREEN_STATUS_VIEW,
        SCREEN_MCP_VIEW,
        SCREEN_HELP_VIEW,
        SCREEN_ERROR_VIEW,
        SCREEN_TIMELINE,
    }
)

_MODIFIERS = frozenset({"ctrl", "shift", "alt", "meta", "super", "hyper"})
# 只收 Textual 事件实际使用的名字。不能拿 unicodedata.lookup 裸匹配：
# ``ESC`` 是控制字符的 Unicode 名，但事件键是 ``escape``，写成 esc 永远不会触发。
_KEY_NAMES = (
    frozenset(item.value for item in Keys)
    | frozenset(KEY_NAME_REPLACEMENTS.values())
    | frozenset(KEY_TO_UNICODE_NAME)
)

_ACTIVE: tuple[tuple["KeybindEntry", ...], tuple[str, ...]] | None = None


@dataclass(frozen=True)
class KeybindEntry:
    """一条键位。

    ``DEFAULTS`` 里的 key 是默认键。:func:`load_registry` 返回的条目里，
    key 是生效键；用户用 null 禁用时为 None。
    """

    id: str
    key: str | None
    description: str
    scope: str
    action: str = ""
    screen: str | None = None
    priority: bool = False
    show: bool = False
    # True 才会放进 Textual BINDINGS。输入框 / 确认字母键走 on_key。
    bind: bool = False
    # 装进 Binding 的说明。空则用 description。
    # 现有 BINDINGS 的说明文字保持原样（show=False，界面不显示）。
    binding_description: str = ""


def _bind(
    entry_id: str,
    key: str,
    description: str,
    scope: str,
    action: str,
    *,
    screen: str | None = None,
    priority: bool = False,
    binding_description: str,
) -> KeybindEntry:
    return KeybindEntry(
        id=entry_id,
        key=key,
        description=description,
        scope=scope,
        action=action,
        screen=screen,
        priority=priority,
        show=False,
        bind=True,
        binding_description=binding_description,
    )


def _handled(
    entry_id: str,
    key: str,
    description: str,
    scope: str,
    *,
    screen: str,
) -> KeybindEntry:
    """on_key 处理的键：登记进表，但不生成 Binding。"""
    return KeybindEntry(
        id=entry_id,
        key=key,
        description=description,
        scope=scope,
        screen=screen,
    )


# 顺序即 bindings_for / describe 的顺序。键字符串与 tui.py 改前一致。
DEFAULTS: tuple[KeybindEntry, ...] = (
    _bind(
        "quit",
        "ctrl+c",
        "退出应用",
        SCOPE_GLOBAL,
        "quit",
        priority=True,
        binding_description="退出",
    ),
    _bind(
        "open_commands",
        "ctrl+p",
        "打开命令面板",
        SCOPE_GLOBAL,
        "open_commands",
        priority=True,
        binding_description="命令面板",
    ),
    _bind(
        "cycle_effort",
        "ctrl+t",
        "轮换当前模型的思考等级（仅本次会话）",
        SCOPE_GLOBAL,
        "cycle_effort",
        priority=True,
        binding_description="轮换思考等级",
    ),
    _bind(
        "interrupt",
        "escape",
        "中断当前回复",
        SCOPE_GLOBAL,
        "interrupt",
        binding_description="中断",
    ),
    _bind(
        "palette_close",
        "escape",
        "关闭命令面板",
        SCOPE_MODAL,
        "close_palette",
        screen=SCREEN_PALETTE,
        binding_description="关闭",
    ),
    _bind(
        "setup_quit",
        "escape",
        "退出配置引导",
        SCOPE_MODAL,
        "quit_setup",
        screen=SCREEN_SETUP,
        binding_description="退出",
    ),
    _bind(
        "trust_deny",
        "escape",
        "拒绝把该目录作为工作区",
        SCOPE_MODAL,
        "deny",
        screen=SCREEN_TRUST,
        binding_description="拒绝",
    ),
    _bind(
        "picker_close",
        "escape",
        "关闭模型选择器",
        SCOPE_MODAL,
        "close_picker",
        screen=SCREEN_PICKER,
        binding_description="关闭",
    ),
    _bind(
        "picker_effort_prev",
        "left",
        "选择器思考等级上一档",
        SCOPE_MODAL,
        "picker_effort_prev",
        screen=SCREEN_PICKER,
        priority=True,
        binding_description="上一档",
    ),
    _bind(
        "picker_effort_next",
        "right",
        "选择器思考等级下一档",
        SCOPE_MODAL,
        "picker_effort_next",
        screen=SCREEN_PICKER,
        priority=True,
        binding_description="下一档",
    ),
    _bind(
        "picker_session",
        "s",
        "选择器设为仅本次会话",
        SCOPE_MODAL,
        "picker_session",
        screen=SCREEN_PICKER,
        priority=True,
        binding_description="仅本次会话",
    ),
    _bind(
        "confirm_deny",
        "escape",
        "拒绝本次权限请求",
        SCOPE_MODAL,
        "deny",
        screen=SCREEN_CONFIRM,
        binding_description="拒绝",
    ),
    _bind(
        "error_view",
        "ctrl+r",
        "展开最近一条错误详情",
        SCOPE_PAGE,
        "open_error_view",
        binding_description="错误详情",
    ),
    _bind(
        "sidebar_toggle",
        "ctrl+b",
        "开关会话页侧边栏",
        SCOPE_PAGE,
        "toggle_sidebar",
        binding_description="侧边栏",
    ),
    _bind(
        "thought_toggle",
        "ctrl+n",
        "展开/收起本会话全部已落定思考块",
        SCOPE_PAGE,
        "toggle_thoughts",
        binding_description="思考块",
    ),
    _bind(
        "thought_block_toggle",
        "ctrl+f",
        "展开/收起定位到的那条思考块（跳转落点；没跳过时为最近一条）",
        SCOPE_PAGE,
        "toggle_thought_block",
        binding_description="单块思考",
    ),
    _bind(
        "tool_block_toggle",
        "f8",
        "展开/收起定位到的那条工具块（跳转落点；没跳过时为最近一条）",
        SCOPE_PAGE,
        "toggle_tool_block",
        binding_description="单块工具",
    ),
    _bind(
        "timeline",
        "ctrl+l",
        "打开消息跳转列表",
        SCOPE_PAGE,
        "open_timeline",
        binding_description="消息跳转",
    ),
    _handled(
        "picker_cursor_up",
        "up",
        "模型选择器上移",
        SCOPE_INPUT,
        screen=SCREEN_PICKER,
    ),
    _handled(
        "picker_cursor_down",
        "down",
        "模型选择器下移",
        SCOPE_INPUT,
        screen=SCREEN_PICKER,
    ),
    _handled(
        "prompt_dismiss_suggestions",
        "escape",
        "关闭补全建议",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "prompt_up",
        "up",
        "上移补全或召回上一条",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "prompt_down",
        "down",
        "下移补全或召回下一条",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "prompt_complete",
        "tab",
        "接受补全建议",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "prompt_submit",
        "enter",
        "发送输入",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "prompt_newline",
        "shift+enter",
        "输入换行",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "stash_draft",
        "ctrl+s",
        "暂存或取回输入草稿",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "paste_view",
        "ctrl+o",
        "查看输入里的粘贴块全文",
        SCOPE_INPUT,
        screen=SCREEN_PROMPT,
    ),
    _handled(
        "paste_view_close",
        "escape",
        "关闭粘贴块查看",
        SCOPE_MODAL,
        screen=SCREEN_PASTE_VIEW,
    ),
    _handled(
        "status_view_close",
        "escape",
        "关闭状态总览",
        SCOPE_MODAL,
        screen=SCREEN_STATUS_VIEW,
    ),
    _handled(
        "mcp_view_close",
        "escape",
        "关闭外接工具清单",
        SCOPE_MODAL,
        screen=SCREEN_MCP_VIEW,
    ),
    _handled(
        "help_view_close",
        "escape",
        "关闭帮助",
        SCOPE_MODAL,
        screen=SCREEN_HELP_VIEW,
    ),
    _handled(
        "error_view_close",
        "escape",
        "关闭错误详情",
        SCOPE_MODAL,
        screen=SCREEN_ERROR_VIEW,
    ),
    _handled(
        "timeline_close",
        "escape",
        "关闭消息跳转列表",
        SCOPE_MODAL,
        screen=SCREEN_TIMELINE,
    ),
    _handled(
        "confirm_yes",
        "y",
        "同意本次请求",
        SCOPE_MODAL,
        screen=SCREEN_CONFIRM,
    ),
    _handled(
        "confirm_enter",
        "enter",
        "回车确认（理由框内则提交拒绝）",
        SCOPE_MODAL,
        screen=SCREEN_CONFIRM,
    ),
    _handled(
        "confirm_always",
        "a",
        "总是允许",
        SCOPE_MODAL,
        screen=SCREEN_CONFIRM,
    ),
    _handled(
        "confirm_preview",
        "p",
        "查看差异预览",
        SCOPE_MODAL,
        screen=SCREEN_CONFIRM,
    ),
    _handled(
        "confirm_no",
        "n",
        "拒绝本次请求",
        SCOPE_MODAL,
        screen=SCREEN_CONFIRM,
    ),
)


def keybinds_path() -> Path:
    """用户覆盖文件路径。文件不存在表示没有覆盖。"""
    return user_config_dir() / "keybinds.json"


def _character_to_key_name(character: str) -> str:
    """单字符收成 Textual 事件名（``?`` → ``question_mark``）。"""
    if character.isalnum():
        return character
    try:
        key = (
            unicodedata.name(character)
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
    except ValueError:
        return character
    return KEY_NAME_REPLACEMENTS.get(key, key)


def _split_key(key: str) -> list[str] | None:
    if not isinstance(key, str):
        return None
    text = key.strip()
    if not text or "," in text:
        return None
    parts = [part.strip().lower() for part in text.split("+")]
    if not parts or any(not part for part in parts):
        return None
    if any(any(ch.isspace() for ch in part) for part in parts):
        return None
    return parts


def _name_ok(name: str) -> bool:
    if name in _KEY_NAMES:
        return True
    # 单个可见字符（``?``、``c``）。``+`` 是分隔符，加号要写 plus。
    return (
        len(name) == 1
        and name.isprintable()
        and not name.isspace()
        and name != "+"
    )


def valid_key(key: str) -> bool:
    """Textual 能实际匹配的键名。空串、未知名字、残缺修饰符都算非法。"""
    parts = _split_key(key)
    if parts is None:
        return False
    *mods, name = parts
    if any(mod not in _MODIFIERS for mod in mods):
        return False
    if len(mods) != len(set(mods)):
        return False
    return _name_ok(name)


def normalize_key(key: str) -> str:
    """小写修饰符，单字符收成 Textual 事件名。调用前须先 :func:`valid_key`。"""
    parts = _split_key(key)
    if parts is None:
        raise ValueError(f"非法键名：{key!r}")
    *mods, name = parts
    if len(name) == 1:
        name = _character_to_key_name(name)
    return "+".join([*mods, name])


def _where(scope: str, screen: str | None) -> str:
    if screen is None:
        return scope
    return f"{scope}:{screen}"


def conflict_groups(
    entries: Sequence[KeybindEntry],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """同冲突域里共用一个生效键的组。禁用（key 为 None）不占键。"""
    occupied: dict[tuple[str, str | None, str], list[str]] = {}
    for entry in entries:
        if not entry.key:
            continue
        token = normalize_key(entry.key) if valid_key(entry.key) else entry.key
        domain = (entry.scope, entry.screen, token)
        occupied.setdefault(domain, []).append(entry.id)
    groups: list[tuple[str, str, tuple[str, ...]]] = []
    for (scope, screen, key), ids in occupied.items():
        if len(ids) < 2:
            continue
        groups.append((_where(scope, screen), key, tuple(ids)))
    return tuple(groups)


def find_conflicts(entries: Sequence[KeybindEntry]) -> tuple[str, ...]:
    """默认同作用域冲突的可读说明。空元组表示这张表干净。"""
    return tuple(
        f"{'、'.join(ids)} 在作用域 {where} 共用键 {key!r}"
        for where, key, ids in conflict_groups(entries)
    )


def _materialize(
    accepted: Mapping[str, str | None],
) -> tuple[KeybindEntry, ...]:
    rows: list[KeybindEntry] = []
    for entry in DEFAULTS:
        if entry.id not in accepted or accepted[entry.id] == entry.key:
            rows.append(entry)
            continue
        rows.append(replace(entry, key=accepted[entry.id]))
    return tuple(rows)


def _read_override_file(path: Path) -> tuple[Mapping[str, Any] | None, list[str]]:
    if not path.exists():
        return {}, []
    if not path.is_file():
        return None, [f"键位覆盖路径不是文件：{path}"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"无法读取键位覆盖 {path}：{exc.__class__.__name__}: {exc}"]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"键位覆盖不是合法 JSON（{path}）：{exc}"]
    if not isinstance(data, dict):
        return None, [
            f"键位覆盖必须是对象 {{action_id: key|null}}（{path}）"
        ]
    return data, []


def _resolve_raw(
    path: str | Path | None,
    overrides: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, list[str]]:
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            return None, ["覆盖必须是对象 {action_id: key|null}，已整份拒绝"]
        return overrides, []
    target = Path(path) if path is not None else keybinds_path()
    return _read_override_file(target)


def load_registry(
    path: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[tuple[KeybindEntry, ...], tuple[str, ...]]:
    """DEFAULTS + 用户覆盖 → ``(entries, diagnostics)``。

    ``overrides`` 显式传入时不读文件（测试用）。否则读 ``path``，
    省略 path 时读 :func:`keybinds_path`。文件不存在视为无覆盖。
    读失败或整份格式不对时，全部保留默认，不抛给启动流程。
    """
    diagnostics: list[str] = [
        f"默认表冲突：{msg}" for msg in find_conflicts(DEFAULTS)
    ]
    raw, read_diags = _resolve_raw(path, overrides)
    diagnostics.extend(read_diags)
    if raw is None:
        return DEFAULTS, tuple(diagnostics)

    by_id = {entry.id: entry for entry in DEFAULTS}
    accepted: dict[str, str | None] = {}
    for raw_id, raw_value in raw.items():
        action_id = raw_id if isinstance(raw_id, str) else str(raw_id)
        if action_id not in by_id:
            diagnostics.append(f"未知 action_id {action_id!r}，已拒绝")
            continue
        default_key = by_id[action_id].key
        if raw_value is None:
            accepted[action_id] = None
            continue
        if not isinstance(raw_value, str):
            diagnostics.append(
                f"{action_id} 的覆盖值必须是字符串或 null，已拒绝，"
                f"保留默认键 {default_key!r}"
            )
            continue
        if not valid_key(raw_value):
            diagnostics.append(
                f"{action_id} 的键名 {raw_value!r} 非法，已拒绝，"
                f"保留默认键 {default_key!r}"
            )
            continue
        accepted[action_id] = normalize_key(raw_value)

    # 撞键的覆盖整组拒绝，再复查：拒绝一条后，原先让开的默认键可能又被
    # 另一条覆盖占住。循环直到没有覆盖还在撞键，避免默认键被挤掉。
    while accepted:
        groups = conflict_groups(_materialize(accepted))
        to_reject: list[str] = []
        for where, key, ids in groups:
            override_ids = [entry_id for entry_id in ids if entry_id in accepted]
            if not override_ids:
                continue
            to_reject.extend(override_ids)
            kept = "、".join(
                f"{entry_id} 保留默认键 {by_id[entry_id].key!r}"
                for entry_id in override_ids
            )
            diagnostics.append(
                f"{'、'.join(override_ids)} 的覆盖键 {key!r} 在作用域 {where} "
                f"与 {'、'.join(ids)} 冲突，已拒绝；{kept}"
            )
        if not to_reject:
            break
        for entry_id in to_reject:
            accepted.pop(entry_id, None)

    return _materialize(accepted), tuple(diagnostics)


def active_registry() -> tuple[tuple[KeybindEntry, ...], tuple[str, ...]]:
    """启动时加载一次的生效表。之后不再读文件。"""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = load_registry()
    return _ACTIVE


def startup_diagnostics() -> tuple[str, ...]:
    return active_registry()[1]


def _source(
    entries: Sequence[KeybindEntry] | None,
) -> Sequence[KeybindEntry]:
    if entries is not None:
        return entries
    return active_registry()[0]


def key_of(
    action_id: str,
    entries: Sequence[KeybindEntry] | None = None,
) -> str | None:
    """生效键。被禁用时返回 None。未知 id 抛 KeyError。"""
    for entry in _source(entries):
        if entry.id == action_id:
            return entry.key
    raise KeyError(action_id)


def describe(
    entries: Sequence[KeybindEntry] | None = None,
) -> list[tuple[str | None, str, str]]:
    """帮助用全量列表：``(键, 说明, 作用域)``。与运行时同一份注册表。

    禁用条目仍在列表里，键为 None。
    """
    return [
        (entry.key, entry.description, entry.scope) for entry in _source(entries)
    ]


def bindings_for(
    scope_or_screen: str,
    entries: Sequence[KeybindEntry] | None = None,
) -> list[Binding]:
    """生成某个作用域或 screen id 的 Textual Binding 列表。

    ``global`` / ``page`` 返回该作用域里要装进 BINDINGS 的条目。
    screen id（如 ``palette``）返回该弹窗自己的 BINDINGS。
    输入控件的键不在这里（走 on_key，用 :func:`key_of`）。
    禁用的键不出现。
    """
    if scope_or_screen not in SCOPES and scope_or_screen not in KNOWN_CONTEXTS:
        raise KeyError(f"未知作用域或 screen id：{scope_or_screen}")
    selected: list[KeybindEntry] = []
    for entry in _source(entries):
        if not entry.bind or not entry.key or not entry.action:
            continue
        if scope_or_screen in SCOPES:
            if entry.scope != scope_or_screen:
                continue
        elif entry.screen != scope_or_screen:
            continue
        selected.append(entry)
    return [
        Binding(
            entry.key or "",
            entry.action,
            entry.binding_description or entry.description,
            priority=entry.priority,
            show=entry.show,
        )
        for entry in selected
    ]


def _validate_defaults() -> None:
    ids = [entry.id for entry in DEFAULTS]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"键位 id 重复：{ids}")
    descriptions = [entry.description for entry in DEFAULTS]
    if len(descriptions) != len(set(descriptions)):
        raise RuntimeError("键位说明重复，describe() 无法区分")
    for entry in DEFAULTS:
        if entry.scope not in SCOPES:
            raise RuntimeError(f"{entry.id} 的作用域非法：{entry.scope}")
        if entry.scope in (SCOPE_MODAL, SCOPE_INPUT) and not entry.screen:
            raise RuntimeError(f"{entry.id} 缺少 screen")
        if entry.scope in (SCOPE_GLOBAL, SCOPE_PAGE) and entry.screen is not None:
            raise RuntimeError(f"{entry.id} 不应带 screen")
        if entry.key is None or not valid_key(entry.key):
            raise RuntimeError(f"默认键非法：{entry.id}={entry.key!r}")
        if normalize_key(entry.key) != entry.key:
            raise RuntimeError(
                f"默认键未规范化：{entry.id} {entry.key!r} "
                f"→ {normalize_key(entry.key)!r}"
            )
        if entry.bind and not entry.action:
            raise RuntimeError(f"{entry.id} 要装进 BINDINGS 但没有 action")
    conflicts = find_conflicts(DEFAULTS)
    if conflicts:
        raise RuntimeError("默认键位表存在同作用域冲突：" + "；".join(conflicts))


_validate_defaults()
