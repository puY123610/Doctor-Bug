"""Doctor Bug 全屏终端交互页（Textual）：OpenCode TUI 双页结构仿制。

页面结构与 OpenCode v2.2.7 同构（规格见
``../research/2026-11-14-opencode-tui-teardown.md``）：

* **Home**：深色整屏、垂直居中的双色块字 Logo（拼作 DRBUG，与下方 Logo
  常量一致）、75 列居中输入框、页脚行与右下角版本号；首次提交后
  切到会话页。
* **Session**：消息流占满上方、全宽输入框固定底部；用户消息带左侧
  ``┃`` 竖条与铺满行宽的微亮背景，工具/运行事件为单行彩色圆点，运行中有
  spinner 与 esc 中断。

输入框是 Home/Session 共用的自绘复合组件 :class:`PromptBox`：只有左边框
``┃``、左下角 ``╹``、框下 1 行 ``▀`` 底座、左右内边距 2、上内边距 1，
空态/短输入时输入区只占 1 行（整框 5 行）；输入随逻辑行数长高、上限 6 行，
超过后在输入区内部滚动。↑/↓ 在首/末行召回本会话已发送消息。

斜杠命令本地执行（不耗模型）；其余文本交给
:func:`core.dispatcher.build_dispatcher` 构造的「调度员」Agent，走
``Agent.reply_stream`` 事件流：TextBlockDelta 增量显示（流式期间纯文本），
落定后仍走既有 Markdown 渲染路径，并紧跟一条「用时 / 输出 token」元信息。
会话页顶部有 slim 标题栏（首条用户消息 + 消息计数）。测试可注入 ``model``
（离线替身）与 ``workspace_root``；导入本模块不会构建真实模型，也不会发起
任何网络请求。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import webbrowser
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from rich.cells import cell_len
from rich.markdown import Markdown
from rich.segment import Segment
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual import events
from textual.events import TextSelected
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from . import keybinds
from . import prompt_extras
from . import file_mention
from . import status_view
from . import error_log
from . import recents
from . import diff_view

PLATFORM_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 主题色（dark 默认；对应规格 teardown.md 第四节）
# 色阶引用取 neutral / red / green / cyan 量级的近似 RGB。
# ---------------------------------------------------------------------------
COLOR_BACKGROUND_BASE = "#141414"  # background.base = neutral.800（近黑灰）
COLOR_BACKGROUND_RAISED = "#1e1e1e"  # background.raised = neutral.700
COLOR_BACKGROUND_PANEL = "#191919"  # raised 压暗一档：输入框面板
# BLK1：用户消息专属背景 = neutral.600 近似；比 raised(#1e1e1e) 再亮一档多，
# 与主治医生回复（无底色）拉开可感知差距；正文 #e6e6e6 对比仍约 11:1。
COLOR_BACKGROUND_USER = "#2c2c2c"
COLOR_TEXT_BASE = "#e6e6e6"  # text.base = neutral.200
COLOR_TEXT_MUTED = "#9c9c9c"  # text.muted = neutral.400
COLOR_BORDER = "#484848"  # 规格写死：border #484848
COLOR_ACCENT = "#7aa2f7"  # interactive/accent 蓝紫的近似
COLOR_SUCCESS = "#56d364"  # 反馈 success = green.200 近似
COLOR_ERROR = "#ff7b72"  # 反馈 error = red.200 近似
COLOR_WARNING = "#f5a742"  # 规格写死：warning #f5a742

# 占位符示例池：规格指定的三句中文场景句，按固定顺序轮换。
PLACEHOLDER_EXAMPLES = (
    "把这个仓库的回归修复跑一遍",
    "这个仓库为什么验收不过？",
    "给 durlib 写个复现测试",
)

# HINT1：输入框提示按会话状态确定性生成（不走模型）。只读现成状态：
# _awaiting_confirm、_active_workspace、项目档案文件、last_bundle、运行记录目录。
# 每条提示点名的动作都必须是已实现的命令（见 HINT_ACTIONS，测试对照 COMMANDS）
# 或确认答复（Y/N）。空闲时沿用原来的示例句轮换。
HINT_CONFIRM = "confirm"
HINT_UNBOUND = "unbound"
HINT_NO_PROFILE = "no_profile"
HINT_RESULT = "result"
HINT_NO_RUN = "no_run"
HINT_IDLE = "idle"

HINT_TEXTS: dict[str, str] = {
    HINT_CONFIRM: "在等你答复：打 Y 放行，打 N 拒绝，然后回车",
    HINT_UNBOUND: "还没选项目：输入 /cd 加上项目路径，进到要修的项目",
    HINT_NO_PROFILE: "这个项目还没建档：输入 /profile 先建档，不花钱",
    HINT_RESULT: "刚跑完一轮：输入 /diff 看改了哪些代码，/runs 看历次记录",
    HINT_NO_RUN: "档建好了：输入 /accept 先检查一遍，或输入 /run 开始修",
}
# 每个状态的提示点名了哪些动作（命令必须在 COMMANDS 里；Y/N 是确认答复）。
HINT_ACTIONS: dict[str, tuple[str, ...]] = {
    HINT_CONFIRM: ("Y", "N"),
    HINT_UNBOUND: ("/cd",),
    HINT_NO_PROFILE: ("/profile",),
    HINT_RESULT: ("/diff", "/runs"),
    HINT_NO_RUN: ("/accept", "/run"),
    HINT_IDLE: (),
}

# 命令面板收纳全部斜杠命令（顺序即展示顺序）。
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/check", "平台体检（环境基线 + 模型配置 + 验收引擎）"),
    ("/run", "跑一轮修复"),
    ("/accept", "用事先定好的检查验一遍（默认当前工作区）"),
    ("/profile", "自动建档（零模型、零费用）"),
    ("/repro", "照症状写一条复现测试（不会定下来）"),
    ("/model", "打开模型选择器（切换已配置 profile）"),
    ("/cd", "切换会话工作区到指定目录"),
    ("/workspace", "列出、纳入或移出本次会话已纳管目录"),
    ("/team", "查看名册、添加或移除员工"),
    ("/brief", "查看项目简报"),
    ("/permissions", "查看权限规则、撤销会话或文件规则"),
    ("/mode", "查看或切换权限模式（逐次确认/自动改/自动审核）"),
    ("/pending", "查看待批准清单、批准并重新执行或丢弃"),
    ("/runs", "查看历次运行记录与详情"),
    ("/diff", "重显最近一轮运行的补丁（只读）"),
    ("/copy", "复制上一条主治医生回复的原文到系统剪贴板"),
    ("/sessions", "会话列表：恢复、重命名或删除本工作区存档"),
    ("/save", "立刻存档当前会话"),
    ("/market", "浏览扩展市场、安装/卸载扩展"),
    ("/status", "状态总览（版本、模型、密钥、工作区、模式、外接工具、市场）"),
    ("/mcp", "外接工具服务器清单与状态"),
    ("/help", "命令清单"),
    ("/quit", "退出"),
)


PERM_MODES: tuple[str, ...] = ("ask", "auto-edit", "auto-review")

MODE_LABELS: dict[str, str] = {
    "ask": "逐次确认",
    "auto-edit": "自动改",
    "auto-review": "自动审核",
}


def _permissions_lines(
    *,
    session_rules: list,
    global_rules: list,
    project_rules: list | None,
) -> list[str]:
    """权限规则一览的展示行（纯函数，命令处理只负责取数）。"""

    lines = [
        "权限规则一览：",
        "红线（程序写死、不可撤销）：密钥与凭据路径禁读禁写 · "
        "保险库目录禁入 · 高危命令禁行",
        "本会话规则（弹窗里按 A 加的；程序关掉就没了）：",
    ]
    if session_rules:
        for i, rule in enumerate(session_rules, 1):
            lines.append(
                f"  [会话 {i}] {rule.actor} · {rule.action} · "
                f"{rule.glob} → {rule.decision}"
            )
    else:
        lines.append("  （无）")
    lines.append("全局规则（弹窗里按 P 加的；存 ~/.config/drbug/policies.toml）：")
    if global_rules:
        for i, rule in enumerate(global_rules, 1):
            lines.append(
                f"  [全局 {i}] {rule.actor} · {rule.action} · "
                f"{rule.glob} → {rule.decision}"
            )
    else:
        lines.append("  （无）")
    if project_rules is not None:
        lines.append("本项目规则（存 <工作区>/.drbug/policies.toml）：")
        if project_rules:
            for i, rule in enumerate(project_rules, 1):
                lines.append(
                    f"  [项目 {i}] {rule.actor} · {rule.action} · "
                    f"{rule.glob} → {rule.decision}"
                )
        else:
            lines.append("  （无）")
    lines.append("撤销一条：/permissions revoke 会话|全局|项目 <序号>（撤销会落审计）")
    return lines


RUN_USAGE = (
    "用法: /run [--arm A|B|C] "
    "[--max-rounds N] [--workspace 路径] [--yes]"
)
CD_USAGE = "用法: /cd <路径>"
WORKSPACE_USAGE = (
    "用法: /workspace | /workspace add <路径> | /workspace remove <路径>"
)
TEAM_USAGE = (
    "用法: /team | /team add <名字> <模块文件路径> | /team remove <名字>"
    " | /team versions <名字> | /team rollback <名字> <版本号>"
)
STANDBY_COMMAND_HINT = (
    "当前工作区未绑定，不能默认改用内置 demo。"
    "请先执行 /cd <项目路径> 绑定项目目录。"
)

_NOTE_LIMIT = 120
_ACTOR_BACKENDS = ("agentscope",)  # 产品后端定版：只用真实后端
# 状态行展示名：与 core.model_config.SUPPORTED_PROVIDERS 保持同步。
_PROVIDER_DISPLAY = {
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic",
    "dashscope": "DashScope",
    "minimax": "MiniMax",
    "moonshot": "Moonshot",
    "openai_chat": "OpenAI Chat",
    "openai_response": "OpenAI Responses",
    "volcengine": "Volcengine",
}

# 会话标题截断长度（字）与中断标记（esc 后落定的 muted 事件）。
TITLE_LIMIT = 28
INTERRUPT_MARKER = "（已中断）"
NO_TEXT_REPLY = "（主治医生没有返回文本）"

# 页脚右侧键位提示：Home 与会话页共用同一句中文。
FOOTER_HINT = "ctrl+p 命令面板"

# 提交路由：首 token 恰好命中这些命令才走本地斜杠命令路径（/exit 是别名）。
SLASH_COMMANDS: frozenset[str] = frozenset(
    command for command, _ in COMMANDS
) | {"/exit"}


def _tui_confirm_responder(question: str) -> str:
    """界面占位应答者：只负责让秘书的权限模式进入可确认状态。

    真正的答复由界面在事件流里服务（老板在输入框里答），
    这个函数不会被界面调用；万一被别的路径调用，一律按
    拒绝回答，绝不放行。
    """
    return "N"


def _confirm_modal_lines(
    target: str,
    *,
    question: str,
    pending_reason: str = "",
    p_allowed: bool = True,
    p_ban_reason: str = "",
) -> list[str]:
    """审批弹窗的文案行：全部由程序按真实参数生成。

    ``question`` 是 policy.confirm_line 的产物；模型自带的任何
    描述都不许传进来——这个函数的参数表里根本没有描述的位置。
    """
    from .policy import is_credential_file, is_secret_path

    lines = [
        "需要你确认",
        "发起者：主治医生",
        "动作：读项目外的文件",
        f"目标：{target}",
        question,
    ]
    marks: list[str] = ["在当前项目之外"]
    probe = Path(target) if target else None
    if probe is not None and is_secret_path(probe):
        marks.append("敏感路径")
    if probe is not None and is_credential_file(probe):
        marks.append("凭据文件")
    lines.append("风险标记：" + "、".join(marks))
    if pending_reason:
        lines.append(f"引擎理由：{pending_reason}")
    if p_allowed:
        lines.append(
            "按键：Enter/Y 本次放行 · A 本次会话内放行同类"
            " · P 永久放行（先预览规则） · Esc/N 拒绝"
        )
    else:
        lines.append(
            "按键：Enter/Y 本次放行 · A 本次会话内放行同类"
            " · Esc/N 拒绝"
        )
        lines.append(f"永久放行不可用：{p_ban_reason}")
    return lines


def _confirm_rule_preview(rule_text: str) -> list[str]:
    """P 的第二屏：将写入的规则原文预览。"""

    return [
        "将写入一条永久规则（对以后所有会话生效）：",
        "文件：~/.config/drbug/policies.toml",
        "",
        *rule_text.rstrip("\n").split("\n"),
        "",
        "再按一次 P 确认写入并放行 · Esc 返回上一屏",
    ]


def directory_has_project_markers(path: Path) -> bool:
    """目录自身是否带项目特征，不向上查找父目录。

    特征：``.git``（文件或目录）、``pyproject.toml``、``requirements.txt``、
    ``.drbug/`` 目录。任一存在即为真。
    """
    try:
        return (
            (path / ".git").exists()
            or (path / "pyproject.toml").is_file()
            or (path / "requirements.txt").is_file()
            or (path / ".drbug").is_dir()
        )
    except (OSError, RuntimeError):
        return False


def _home_drbug_dir_is_tool_cache(path: Path) -> bool:
    """用户主目录上的 ``~/.drbug`` 若只是工具缓存，则不是项目档。

    drbug 把工作副本放在 ``~/.drbug/work``。那份目录不是项目。
    主目录里已经有 ``.drbug/profiles`` 或 ``.drbug/runs`` 时，视为已建档。
    """
    try:
        if path.resolve() != Path.home().resolve():
            return False
        drbug = path / ".drbug"
        if (drbug / "profiles").is_dir() or (drbug / "runs").is_dir():
            return False
        return drbug.is_dir()
    except (OSError, RuntimeError):
        return False


def startup_directory_is_project(path: Path) -> bool:
    """启动时是否自动绑定该目录。

    四类特征与 :func:`directory_has_project_markers` 相同。唯一例外：用户主目录
    只因为工具自己的 ``~/.drbug`` 缓存而命中时，仍视为未绑定，避免把家目录
    当成项目。主目录另有 ``.git`` / ``pyproject.toml`` / ``requirements.txt``，
    或 ``.drbug`` 里已有 profiles、runs 时，照常绑定。
    """
    try:
        other = (
            (path / ".git").exists()
            or (path / "pyproject.toml").is_file()
            or (path / "requirements.txt").is_file()
        )
        has_drbug = (path / ".drbug").is_dir()
    except (OSError, RuntimeError):
        return False
    if other:
        return True
    if not has_drbug:
        return False
    return not _home_drbug_dir_is_tool_cache(path)


# 补全候选 = COMMANDS 全部命令 + /exit 别名；说明文案同取自 COMMANDS（单一来源）。
COMPLETION_COMMANDS: tuple[tuple[str, str], ...] = (
    *COMMANDS,
    ("/exit", dict(COMMANDS)["/quit"] + "（/quit 的别名）"),
)

# SLASH1：带参数的命令——填入时命令名后补一个空格，好接着打参数。
# 判定照 CifixChatApp._handle_slash_inner 的实际分派逐个核过：只收处理器
# 会消费命令名之后文本的命令（依据见各行注释）；其余命令（/check /model
# /brief /diff /save /status /mcp /help /quit /exit）的处理器不读参数。
# 名单成员必须都在 COMPLETION_COMMANDS 里（test_slash1 钉住）。
ARG_COMMANDS: frozenset[str] = frozenset(
    {
        "/run",  # _handle_run(args) → _parse_run_args：--arm/--max-rounds/--workspace/--yes
        "/accept",  # args[0] 作检查目标路径
        "/profile",  # _parse_profile_args(args)：目标路径与 --name
        "/repro",  # args[0] 工作区、args[1:] 症状描述
        "/cd",  # _cmd_cd：_message_tokens(text)[1] 作目标路径
        "/workspace",  # _cmd_workspace：add/remove <路径>
        "/team",  # _cmd_team：_message_tokens(text)[1:] 添加/移除员工
        "/mode",  # _cmd_mode：tokens[1] 作模式名
        "/permissions",  # _cmd_permissions：text.split()[1:] 撤销子命令
        "/pending",  # _cmd_pending：text.split()[1:] 批准/丢弃子命令
        "/runs",  # _cmd_runs：text.split()[1:] 看某次详情
        "/sessions",  # _cmd_sessions：restore/rename/delete 子命令
        "/market",  # _cmd_market：parts[1:] 安装/卸载子命令
    }
)


def completion_fill(command: str) -> str:
    """SLASH1：补全填入的文本——带参数的命令补一个尾随空格，其余原样。"""
    return f"{command} " if command in ARG_COMMANDS else command


_SCOPE_LABELS: dict[str, str] = {
    "global": "全局",
    "page": "页面",
    "modal": "弹窗",
    "input": "输入框",
}


def help_view_text() -> str:
    """/help 弹窗正文（TUIE2）：命令表读 COMMANDS，快捷键表读生效注册表。

    两张表都不手写：命令与面板、补全同一来源；快捷键走
    keybinds.describe()（active_registry 的生效键，用户覆盖后跟着变）。
    """
    width = max(len(command) for command, _ in COMMANDS)
    lines = ["命令"]
    lines.extend(
        f"  {command:<{width}}  {description}" for command, description in COMMANDS
    )
    lines.append("")
    lines.append("快捷键")
    for key, description, scope in keybinds.describe():
        shown = key if key else "未绑定"
        lines.append(f"  {shown:<12}  {description}（{_SCOPE_LABELS.get(scope, scope)}）")
    return "\n".join(lines)


def _completion_candidates(text: str) -> list[tuple[str, str]]:
    """Candidates only for a single command-type token, never for paths/messages."""
    if not text.startswith("/") or "\n" in text or text != text.strip():
        return []
    tokens = text.split()
    if len(tokens) != 1:
        return []
    token = tokens[0]
    if "/" in token[1:]:
        return []
    needle = token.lower()
    exact_command = needle in SLASH_COMMANDS
    if not exact_command and token != "/" and _is_path_like_token(token):
        return []
    return [
        (command, description)
        for command, description in COMPLETION_COMMANDS
        if command.lower().startswith(needle)
    ]

# 图片路径只做事实提示，不假装能读图。
IMAGE_HINT = "提示：目前还不能看图，截图路径已作为文字发给主治医生。"
# PST1：粘贴成附件块的图片在非视觉模型下不发送，必须明说。
PASTED_IMAGE_NOT_SENT = "图片未发送：当前模型不支持看图"
PASTED_FILE_MAX_BYTES = 100 * 1024
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
)


def _first_token(text: str) -> str:
    """按空白切出首个 token（仅用于路由，不改动发给调度员的原文）。"""
    parts = text.split()
    return parts[0] if parts else text


def _is_known_command(text: str) -> bool:
    """首 token（大小写不敏感）是否恰好是已知斜杠命令。"""
    return _first_token(text).lower() in SLASH_COMMANDS


def _is_path_like_token(token: str) -> bool:
    """粘贴路径判定：/Users/... 这类含斜杠，或指向真实存在的文件/目录。"""
    if "/" in token[1:]:
        return True
    try:
        return Path(token).expanduser().exists()
    except (OSError, RuntimeError):
        return False


def _message_tokens(text: str) -> list[str]:
    """按 shell 规则切词：能识别 ``\\ `` 转义与引号包住的带空格路径。"""
    try:
        return shlex.split(text)
    except ValueError:  # 引号不配对等：退回普通空白切分
        return text.split()


def _is_image_file(token: str) -> bool:
    candidate = token.strip("\"'`()[]<>，。；：、！？")
    if not candidate:
        return False
    try:
        path = Path(candidate).expanduser()
        return path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    except (OSError, RuntimeError):
        return False


# 路径状子串：绝对路径或 ~/ 路径，以图片后缀收尾；字符类排除空白、
# 引号、括号与中文标点，保证粘连在汉字之间的路径能被完整捞出。
_IMAGE_PATH_RE = re.compile(
    r"(?:~)?/[^\s\"'()\[\]<>，。；：、！？]*?\.(?:png|jpe?g|gif|webp|bmp)",
    re.IGNORECASE,
)


def _image_candidates(text: str) -> list[str]:
    """图片路径候选：先按 shell 切词，再正则从粘连文本里捞路径状子串。

    中文消息常把路径直接粘在汉字后面（中间没有空格），切词会把
    「看这张图/…/a.png」当成一个 token 而漏检；第二遍只负责产生
    候选，是否算数仍由 _is_image_file 的真实文件校验决定，
    不存在的路径永不算数。
    """
    return [*_message_tokens(text), *_IMAGE_PATH_RE.findall(text)]


def _message_has_image_path(text: str) -> bool:
    """消息里是否有指向真实存在图片文件的 token。"""
    return any(_is_image_file(token) for token in _image_candidates(text))


def _image_paths_in_message(text: str) -> list[Path]:
    """按出现顺序返回消息里真实存在的图片路径（去重）。"""
    found: list[Path] = []
    seen: set[str] = set()
    for token in _image_candidates(text):
        if not _is_image_file(token):
            continue
        candidate = token.strip("\"'`()[]<>，。；：、！？")
        try:
            path = Path(candidate).expanduser()
            key = str(path.resolve())
        except (OSError, RuntimeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def _attachment_tokens(text: str) -> list[str]:
    """消息里已附图片的原样路径 token（按出现顺序去重）。草稿暂存用。"""
    found: list[str] = []
    for token in _image_candidates(text):
        if not _is_image_file(token):
            continue
        candidate = token.strip("\"'`()[]<>，。；：、！？")
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def _attachment_exists(token: str) -> bool:
    try:
        return Path(token).expanduser().is_file()
    except (OSError, RuntimeError):
        return False


def _attached_image_note(paths: list[Path]) -> str:
    """vision=true 时的记录行。PST1 起多图全发，逐个列出。"""
    return "已附图：" + "、".join(path.name for path in paths)


def _read_pasted_file(
    kind: str, number: int, path: Path
) -> tuple[Any, str]:
    """PST1：粘贴文件的内存快照（文本，≤100 KB，UTF-8，无 NUL）。"""
    try:
        stat = path.stat()
        if stat.st_size > PASTED_FILE_MAX_BYTES:
            return None, "超过 100 KB"
        data = path.read_bytes()
    except OSError:
        return None, "读不到"
    if len(data) > PASTED_FILE_MAX_BYTES:
        return None, "超过 100 KB"
    if b"\x00" in data:
        return None, "不是文本文件"
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "不是 UTF-8 文本"
    return (
        file_mention.FileAttachment(
            rel=f"{kind} {number}（{path.name}）",
            resolved=path,
            size=len(data),
            mtime_ns=stat.st_mtime_ns,
            text=content,
        ),
        "",
    )


def _pasted_file_paths(text: str) -> list[Path]:
    """PST1：整段粘贴是否全是真实存在的文件路径（沿用粘贴路径判定）。

    逐 token 判定：须是路径状（_is_path_like_token）且指向真实文件；
    只要有一个 token 不是，就整段当普通文本处理，返回空表。
    """
    tokens = _message_tokens(text.strip())
    if not tokens:
        return []
    found: list[Path] = []
    for token in tokens:
        candidate = token.strip("\"'`")
        if not candidate or not _is_path_like_token(candidate):
            return []
        try:
            path = Path(candidate).expanduser()
            if not path.is_file():
                return []
        except (OSError, RuntimeError):
            return []
        found.append(path)
    return found


def _short(text: str, limit: int = _NOTE_LIMIT) -> str:
    compact = " ".join(str(text).split())
    if len(compact) > limit:
        return compact[:limit] + "…"
    return compact


def _truncate_cells(text: str, limit: int) -> str:
    """按显示列宽截断，超出时末尾加「…」（整体不超过 limit 列）。"""
    if limit <= 0:
        return ""
    if cell_len(text) <= limit:
        return text
    out = ""
    for char in text:
        if cell_len(out + char) > limit - 1:
            break
        out += char
    return out + "…"


def _thought_summary(full: str) -> str:
    """BLK1：思考全文的首个非空行，空白折叠成一行。"""
    for line in full.splitlines():
        compact = " ".join(line.split())
        if compact:
            return compact
    return ""


def _wrap_cells(text: str, width: int) -> list[str]:
    """按显示列宽硬折行，保留宽字符完整（不把 CJK 字拆成两半）。"""
    limit = max(width, 1)
    if not text:
        return [""]
    chunks: list[str] = []
    current = ""
    current_width = 0
    for char in text:
        char_width = cell_len(char)
        if current and current_width + char_width > limit:
            chunks.append(current)
            current = ""
            current_width = 0
        current += char
        current_width += char_width
    chunks.append(current)
    return chunks


def _clip_cells(text: str, limit: int) -> str:
    """按显示列宽截断，宽字符整字保留；用于窄屏标题栏。"""
    limit = max(limit, 0)
    if cell_len(text) <= limit:
        return text
    clipped = ""
    for char in text:
        if cell_len(clipped) + cell_len(char) > limit:
            break
        clipped += char
    return clipped


@lru_cache(maxsize=1)
def _platform_version() -> str:
    """读取 pyproject.toml 的项目版本（失败时退回 0.1.0）。"""
    try:
        with (PLATFORM_ROOT / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
        return str(data["project"]["version"])
    except Exception:  # noqa: BLE001 - 版本行只做展示
        return "0.1.0"


VERSION = _platform_version()


# ---------------------------------------------------------------------------
# Logo：4 行块字，拼作 DRBUG（D、R、B、U、G 五个字形，双空格分隔）。
# 左半 DR 用 muted 灰不加粗，右半 BUG 用亮白加粗；框线字符充当块字阴影。
# （NAME1：对外名 Doctor Bug 按同款字形约需 97 列，超出首页卡片 75 列上限，故用 DRBUG。）
# ---------------------------------------------------------------------------
_LOGO_B = (
    "██████╗ ",
    "██╔══██╗",
    "██████╔╝",
    "╚█████╔╝",
)
_LOGO_R = (
    "██████╗ ",
    "██╔══██╗",
    "██████╔╝",
    "██║  ██║",
)
_LOGO_U = (
    "██╗   ██╗",
    "██║   ██║",
    "██║   ██║",
    "╚██████╔╝",
)
_LOGO_G = (
    " ██████╗ ",
    "██╔════╝ ",
    "██║  ███╗",
    "╚██████╔╝",
)
_LOGO_D = (
    "██████╗  ",
    "██╔══██╗ ",
    "██║  ██║ ",
    "╚██████╔╝",
)
_LOGO_O = (
    " ██████╗ ",
    "██╔═══██╗",
    "██║   ██║",
    "╚██████╔╝",
)
_LOGO_C = (
    " ██████╗ ",
    "██╔════╝ ",
    "██║      ",
    "╚██████╗ ",
)

# 字形顺序即拼写：D R B U G。左半取前两个字母（DR），右半取后三个（BUG）。
_LOGO_GLYPHS = (_LOGO_D, _LOGO_R, _LOGO_B, _LOGO_U, _LOGO_G)
_LOGO_SPLIT = 2
_LOGO_SEPARATOR = "  "


def _logo_rows() -> tuple[tuple[str, str], ...]:
    """返回 4 行 logo 的（左半 DR，右半 BUG），五字母合起来拼作 DRBUG。"""
    rows: list[tuple[str, str]] = []
    for index in range(4):
        left = _LOGO_SEPARATOR.join(
            glyph[index] for glyph in _LOGO_GLYPHS[:_LOGO_SPLIT]
        )
        right = _LOGO_SEPARATOR.join(
            glyph[index] for glyph in _LOGO_GLYPHS[_LOGO_SPLIT:]
        )
        rows.append((left, right))
    return tuple(rows)


LOGO_ROWS = _logo_rows()
LOGO_LINES = tuple(
    left + _LOGO_SEPARATOR + right for left, right in LOGO_ROWS
)


# 规格「阴影占位」：转角/边长字符与背景混色画暗一档（约 25% 原色），造立体感。
_LOGO_SHADOW_CHARS = frozenset("╔╗╚╝═║╠╣╦╩╬┌┐└┘─│")


def _mix_hex(color: str, other: str, weight: float) -> str:
    """按 weight 混合两个 ``#rrggbb`` 颜色（0 → other，1 → color）。"""
    left = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(other[index : index + 2], 16) for index in (1, 3, 5))
    return "#" + "".join(
        f"{round(a * weight + b * (1 - weight)):02x}"
        for a, b in zip(left, right)
    )


_LOGO_SHADOW = {
    COLOR_TEXT_MUTED: _mix_hex(COLOR_TEXT_MUTED, COLOR_BACKGROUND_BASE, 0.30),
    COLOR_TEXT_BASE: _mix_hex(COLOR_TEXT_BASE, COLOR_BACKGROUND_BASE, 0.30),
}


def _append_logo_run(target: Text, text: str, color: str, bold: bool) -> None:
    """把一段字形写入 Text：阴影字符用混色，其余用主色（右半加粗）。"""
    shadow = _LOGO_SHADOW[color]
    for char in text:
        if char == " ":
            target.append(char)
        elif char in _LOGO_SHADOW_CHARS:
            target.append(
                char, style=f"bold {shadow}" if bold else shadow
            )
        else:
            target.append(char, style=f"bold {color}" if bold else color)


def _build_logo() -> Text:
    logo = Text()
    for index, (left, right) in enumerate(LOGO_ROWS):
        _append_logo_run(logo, left, COLOR_TEXT_MUTED, bold=False)
        logo.append(_LOGO_SEPARATOR)
        _append_logo_run(logo, right, COLOR_TEXT_BASE, bold=True)
        if index < len(LOGO_ROWS) - 1:
            logo.append("\n")
    return logo


LOGO_TEXT = _build_logo()
BANNER = LOGO_TEXT.plain  # 兼容旧导入名；内容即 4 行块字 DRBUG
POSITIONING = "Doctor Bug · 事先定好的检查 · 按检查结果派活 — 帮你修回归的小队"


# ---------------------------------------------------------------------------
# 自绘输入框组件（Home 与 Session 共用）
# ---------------------------------------------------------------------------
class PromptInput(TextArea):
    """多行输入区；enter 提交，shift+enter 换行，↑/↓ 召回历史。

    高度随逻辑行数增长（1 → 最多 ``MAX_LINES`` 行），清空后收回 1 行。
    ↑ 只在光标位于首行时进入历史，↓ 只在光标位于末行时前进，
    因此多行编辑里的上下移动不被抢走；发送后历史指针复位。
    """

    MAX_LINES = 6

    class Submitted(Message):
        """输入区提交消息。"""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self, host: "CifixChatApp", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.host = host
        self.box: "PromptBox | None" = None
        self._line_height = 0
        self._history_index: int | None = None
        self._history_draft = ""

    def on_mount(self) -> None:
        self._sync_height()

    def on_text_area_changed(self, event: Any) -> None:
        self._sync_height()
        if self.box is not None:
            self.box.update_suggestions(self.text)

    # ------------------------------------------------------------------
    # 高度
    # ------------------------------------------------------------------
    def _sync_height(self) -> None:
        lines = max(1, min(self.document.line_count, self.MAX_LINES))
        if lines != self._line_height:
            self._line_height = lines
            self.styles.height = lines

    # ------------------------------------------------------------------
    # 历史召回
    # ------------------------------------------------------------------
    def reset_history_pointer(self) -> None:
        """发送后复位：下一次 ↑ 从最新一条重新开始。"""
        self._history_index = None
        self._history_draft = ""

    def recall_history(self, step: int) -> bool:
        """在已发送消息里前后移动；返回是否消费了这次按键。"""
        history = self.host.input_history
        if not history:
            return False

        if self._history_index is None:
            if step > 0:  # 还没进历史时按 ↓ 无副作用
                return False
            self._history_draft = self.text
            index = len(history) - 1
        else:
            index = self._history_index + (1 if step > 0 else -1)
            if index < 0:  # 已在最早一条：吞掉按键，光标不动
                return True

        if index >= len(history):
            # 越过最新一条：回到进入历史前的草稿。
            self._history_index = None
            value = self._history_draft
        else:
            self._history_index = index
            value = history[index]
        self._apply_text(value)
        return True

    def _apply_text(self, value: str) -> None:
        self.text = value
        rows = value.split("\n")
        self.cursor_location = (len(rows) - 1, len(rows[-1]))
        self._sync_height()

    async def _on_key(self, event: Any) -> None:
        dismiss = keybinds.key_of("prompt_dismiss_suggestions")
        up = keybinds.key_of("prompt_up")
        down = keybinds.key_of("prompt_down")
        complete = keybinds.key_of("prompt_complete")
        submit = keybinds.key_of("prompt_submit")
        newline = keybinds.key_of("prompt_newline")
        # 命令补全浮层优先吃掉 esc/↑/↓/tab/enter；浮层可见时 enter 是填入
        # （SLASH1，与 @ 模式同一写法，复用 prompt_submit 键位），填入后浮层
        # 关闭，再按 enter 才提交；浮层不可见时 enter 照旧提交。
        if self.box is not None and self.box.suggestions_visible:
            if dismiss is not None and event.key == dismiss:
                event.stop()
                event.prevent_default()
                self.box.hide_suggestions()
                return
            if event.key is not None and event.key in (up, down):
                event.stop()
                event.prevent_default()
                self.box.move_suggestion(-1 if event.key == up else 1)
                return
            if complete is not None and event.key == complete:
                event.stop()
                event.prevent_default()
                self.box.complete_suggestion(self)
                return
            # TUID2：@ 文件补全时 enter 选中；SLASH1：斜杠补全同样 enter 填入
            # （复用 prompt_submit 键位，不新增条目）。
            if submit is not None and event.key == submit:
                event.stop()
                event.prevent_default()
                self.box.complete_suggestion(self)
                return
        stash = keybinds.key_of("stash_draft")
        paste_view = keybinds.key_of("paste_view")
        if stash is not None and event.key == stash:
            event.stop()
            event.prevent_default()
            self.host.toggle_draft_stash(self)
            return
        if paste_view is not None and event.key == paste_view:
            event.stop()
            event.prevent_default()
            self.host.open_paste_view(self.text)
            return
        # TextArea 在 _on_key 里把 enter 当作换行插入，优先于 Widget 绑定；
        # 这里必须先截住 enter，才能实现 OpenCode 的「enter 提交」。
        if submit is not None and event.key == submit:
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if newline is not None and event.key == newline:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if up is not None and event.key == up and self.cursor_location[0] == 0:
            if self.recall_history(-1):
                event.stop()
                event.prevent_default()
                return
        if (
            down is not None
            and event.key == down
            and self.cursor_location[0] >= self.document.line_count - 1
        ):
            if self.recall_history(1):
                event.stop()
                event.prevent_default()
                return
        await super()._on_key(event)


    async def _on_paste(self, event: Any) -> None:
        """长粘贴折叠成占位符；超限拒绝；其余交给 TextArea 照常粘贴。"""
        if self.read_only:
            return
        if self.host.handle_paste(self, event.text):
            event.stop()
            event.prevent_default()
            return
        # 不折叠：不在这里插入，交给 MRO 分发里的 TextArea._on_paste 照常粘贴
        # （这里再调 super 会插两遍）。


class PromptBase(Widget):
    """输入框正下方的 1 行底座：左端 ╹ + 整行 ▀（框背景色）。"""

    def render(self) -> Text:
        width = max(self.size.width, 1)
        base = Text()
        base.append("╹", style=f"bold {COLOR_ACCENT}")
        if width > 1:
            base.append(
                "▀" * (width - 1),
                style=f"{COLOR_BACKGROUND_PANEL} on {COLOR_BACKGROUND_BASE}",
            )
        return base

    def on_resize(self) -> None:
        self.refresh()


class SuggestionList(Static):
    """补全浮层（SLASH1）：仍是原来那块 Static，渲染一字不变，只多接鼠标单击。

    浮层每个候选恰好占一行（renderer 逐行 append，候选文本不含换行），
    因此点中的内容行号就是候选下标。
    """

    def __init__(self, box: "PromptBox", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.box = box

    def on_click(self, event: Any) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        event.stop()
        self.box.click_suggestion(offset.y)


class PromptBox(Vertical):
    """Home/Session 共用的自绘复合输入框。"""

    def __init__(
        self,
        host: "CifixChatApp",
        form: str = "home",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.host = host
        self.form = form
        self._input = PromptInput(
            host=host,
            placeholder=host.example_placeholder(),
            classes="prompt-input",
        )
        self._input.box = self
        self._status = Static(
            host.status_line(),
            classes="prompt-status",
            markup=False,
        )
        self._suggest = SuggestionList(
            self,
            "",
            classes="slash-suggest",
            markup=False,
        )
        self._suggestion_items: list[tuple[str, str]] = []
        self._suggestion_index = 0
        self._mention_mode = False
        # SLASH1：刚填入的文本——它触发的那次文本变化不再把浮层弹回来。
        self._filled_text: str | None = None

    def compose(self) -> ComposeResult:
        yield self._suggest
        with Vertical(classes="prompt-panel"):
            yield self._input
            yield Static("", classes="prompt-gap")
            yield self._status
        yield PromptBase()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        event.stop()
        self._input.reset_history_pointer()
        self._input.text = ""
        self.hide_suggestions()
        self.host.submit_text(self.host.expand_pastes_for_send(event.value))

    # ------------------------------------------------------------------
    # 斜杠命令补全浮层
    # ------------------------------------------------------------------
    @property
    def suggestions_visible(self) -> bool:
        return bool(self._suggestion_items)

    @property
    def mention_mode(self) -> bool:
        """当前浮层是否 @ 文件补全（且有可选条目）。"""
        return self._mention_mode and bool(self._suggestion_items)

    def update_suggestions(self, text: str) -> None:
        # SLASH1：等待确认时禁用浮层（确认优先），输入框打的就是答复。
        if getattr(self.host, "_awaiting_confirm", False):
            self._filled_text = None
            self.hide_suggestions()
            return
        if self._filled_text is not None:
            filled, self._filled_text = self._filled_text, None
            if text == filled:
                self.hide_suggestions()
                return
        query = file_mention.mention_query(text)
        if query is not None:
            self._update_mention_suggestions(query)
            return
        self._mention_mode = False
        items = _completion_candidates(text)
        if not items:
            self.hide_suggestions()
            return
        if items != self._suggestion_items:
            self._suggestion_index = 0
        self._suggestion_items = items
        self._suggestion_index = min(
            self._suggestion_index, len(items) - 1
        )
        self._render_suggestions()
        self._suggest.styles.display = "block"

    def _update_mention_suggestions(self, query: str) -> None:
        """@ 补全：只列相对路径，不读内容；无匹配给一行提示。"""
        self._mention_mode = True
        paths = self.host.mention_candidates(query)
        if not paths:
            self._suggestion_items = []
            self._suggestion_index = 0
            hint = (
                file_mention.UNBOUND_TEXT
                if paths is None
                else file_mention.NO_MATCH_TEXT
            )
            self._suggest.update(Text(hint, style=COLOR_TEXT_MUTED))
            self._suggest.styles.display = "block"
            return
        items = [(path, "") for path in paths]
        if items != self._suggestion_items:
            self._suggestion_index = 0
        self._suggestion_items = items
        self._render_suggestions()
        self._suggest.styles.display = "block"

    def hide_suggestions(self) -> None:
        self._mention_mode = False
        self._suggestion_items = []
        self._suggestion_index = 0
        self._suggest.update("")
        self._suggest.styles.display = "none"

    def move_suggestion(self, step: int) -> None:
        if not self._suggestion_items:
            return
        self._suggestion_index = (
            self._suggestion_index + step
        ) % len(self._suggestion_items)
        self._render_suggestions()

    def complete_suggestion(self, prompt_input: PromptInput) -> None:
        if not self._suggestion_items:
            return
        if self._mention_mode:
            rel = self._suggestion_items[self._suggestion_index][0]
            self.hide_suggestions()
            self.host.select_mention(prompt_input, rel)
            return
        command = self._suggestion_items[self._suggestion_index][0]
        value = completion_fill(command)
        self.hide_suggestions()
        self._filled_text = value
        prompt_input.text = value
        prompt_input.cursor_location = (0, len(value))

    def click_suggestion(self, row: int) -> None:
        """SLASH1：鼠标单击第 row 行 = 选中该行并填入（与键盘填入同一路径）。"""
        if not 0 <= row < len(self._suggestion_items):
            return
        self._suggestion_index = row
        self.complete_suggestion(self._input)
        self._input.focus()

    def _render_suggestions(self) -> None:
        rendered = Text()
        for index, (command, description) in enumerate(
            self._suggestion_items
        ):
            if index:
                rendered.append("\n")
            if index == self._suggestion_index:
                rendered.append("› ", style=f"bold {COLOR_ACCENT}")
                rendered.append(
                    command if self._mention_mode else f"{command:<12}",
                    style=f"bold {COLOR_ACCENT} on {COLOR_BACKGROUND_RAISED}",
                )
                rendered.append(
                    description,
                    style=f"{COLOR_TEXT_BASE} on {COLOR_BACKGROUND_RAISED}",
                )
            else:
                rendered.append("  ")
                rendered.append(
                    command if self._mention_mode else f"{command:<12}",
                    style=COLOR_ACCENT,
                )
                rendered.append(description, style=COLOR_TEXT_MUTED)
        self._suggest.update(rendered)

    def update_status(self) -> None:
        self._status.update(self.host.status_line())

    def set_placeholder(self, text: str) -> None:
        self._input.placeholder = text


class FooterLine(Widget):
    """页脚行：左当前目录，右 shift+tab / ctrl+p 键位提示。"""

    RIGHT_TEXT = FOOTER_HINT

    def __init__(self, cwd: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cwd = cwd

    def render(self) -> Text:
        width = max(self.size.width, 1)
        right = self.RIGHT_TEXT + "  "
        left = "  " + self.cwd
        # 窄屏先收左端目录，保住右侧键位提示（OpenCode 页脚同理）。
        budget = width - cell_len(right) - 1
        if budget >= 3 and cell_len(left) > budget:
            tail = self.cwd
            while tail and cell_len("  …" + tail) > budget:
                tail = tail[1:]
            left = "  …" + tail
        gap = width - cell_len(left) - cell_len(right)
        if gap < 1:
            gap = 1
        line = Text(left, style=COLOR_TEXT_MUTED)
        line.append(" " * gap, style=COLOR_TEXT_MUTED)
        line.append(right, style=COLOR_TEXT_MUTED)
        return line

    def on_resize(self) -> None:
        self.refresh()


# ----------------------------------------------------------------------
# COPY1：回复区拖选复制 + 系统剪贴板
# ----------------------------------------------------------------------
COPY_DONE = "已复制"
COPY_EMPTY = "还没有主治医生的回复可复制"
PBCOPY_TIMEOUT = 3.0


def _stylize_chars(strip: Strip, start: int, end: int, style: Any) -> Strip:
    """给 strip 里字符下标 [start, end) 的文字叠加 style；字符与格宽不变。"""
    segments: list[Segment] = []
    position = 0
    for segment in strip:
        text, base, control = segment
        if control:
            segments.append(segment)
            continue
        length = len(text)
        lo, hi = max(start, position), min(end, position + length)
        if lo < hi:
            if lo > position:
                segments.append(Segment(text[: lo - position], base, control))
            segments.append(
                Segment(
                    text[lo - position : hi - position],
                    base + style if base else style,
                    control,
                )
            )
            if hi < position + length:
                segments.append(Segment(text[hi - position :], base, control))
        else:
            segments.append(segment)
        position += length
    return Strip(segments, strip.cell_length)


class ReplyLog(RichLog):
    """#log 用的 RichLog 子类（COPY1）：只补拖选所需的三样，写入与渲染不改。

    参照 textual/widgets/_log.py 的 Log：
    * ``_render_line`` 末尾 ``apply_offsets``（Log :312）——每段带上
      (字符下标, 行号)，compositor 把指针所在格子按逐字格宽换算回字符
      下标，中文两格宽由此对齐，不按字符数硬切；
    * ``get_selection``（Log :265）——按行存储（self.lines 的 Strip.text）
      拼成全文交给 Selection.extract，坐标同为（行, 字符下标）；
    * 选中高亮用框架的 screen--selection 样式，叠加在已渲染的格子上，
      不改文字本身。
    选出的是「渲染后的样子」：软折行按行断开、列表符号等渲染字符包含在内，
    行尾补齐用的空格去掉。
    """

    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        line = super()._render_line(y, scroll_x, width)
        if y >= len(self.lines):
            return line
        prefix = (
            len(self.lines[y].crop(0, scroll_x).text) if scroll_x > 0 else 0
        )
        selection = self.text_selection
        if selection is not None:
            span = selection.get_span(y)
            if span is not None:
                start, end = span
                if end == -1:
                    end = prefix + len(line.text)
                style = self.screen.get_component_rich_style("screen--selection")
                line = _stylize_chars(line, start - prefix, end - prefix, style)
        return line.apply_offsets(prefix, y)

    def get_selection(self, selection: Any) -> tuple[str, str] | None:
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Any) -> None:
        self._line_cache.clear()
        self.refresh()

    # ---- LINK1：点击 Markdown 渲染出的链接 ----
    # 判定照 OpenCode：左键按下与松手落在同一格、期间没移到别的格、没按
    # 修饰键（ctrl/meta/shift；终端里 alt/option 也报为 meta），且松手时
    # 没有成形的选区，才算一次点击。拖过再松手归 COPY1 的复制，绝不打开。
    _link_press: list | None = None  # [按下时屏幕坐标, 是否带修饰键, 是否移动过]

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            self._link_press = None
            return
        self._link_press = [
            event.screen_offset,
            bool(event.ctrl or event.meta or event.shift),
            False,
        ]

    def on_mouse_move(self, event: events.MouseMove) -> None:
        press = self._link_press
        if press is not None and event.screen_offset != press[0]:
            press[2] = True

    def on_mouse_up(self, event: events.MouseUp) -> None:
        press, self._link_press = self._link_press, None
        if press is None:
            return
        offset, modified, moved = press
        if moved or modified or event.ctrl or event.meta or event.shift:
            return
        if event.screen_offset != offset:
            return
        try:
            if self.screen.selections:  # 有选区＝拖选，归复制
                return
        except Exception:  # noqa: BLE001 - 切页瞬间
            return
        # URL 只取点击处文字段的 style.link（框架在转发前按屏幕坐标填好
        # event.style，见 textual/screen.py _forward_event）。
        url = event.style.link
        if url:
            open_reply_link(self.app, url)


def is_web_link(url: str) -> bool:
    """LINK1 白名单：urlparse 后 scheme 只认 http/https，且 netloc 非空。"""
    try:
        parts = urlparse((url or "").strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def open_reply_link(app: Any, url: str) -> bool:
    """白名单内用标准库 webbrowser.open 打开；其余只给一行提示，不崩。"""
    if not is_web_link(url):
        app.notify(f"这个链接不是网址，没有打开：{url}", severity="warning")
        return False
    try:
        opened = bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - 浏览器起不来也不崩
        opened = False
    if not opened:
        app.notify(f"没能打开浏览器，链接是：{url}", severity="warning")
    return opened


def _write_system_clipboard(text: str) -> tuple[bool, str]:
    """本机剪贴板：pbcopy（文本走 stdin，utf-8，显式 UTF-8 locale）。

    返回 (是否写成, 失败原因)。找不到 pbcopy、超时、非零退出都算失败，
    不抛异常。
    """
    command = shutil.which("pbcopy")
    if command is None:
        return False, "找不到 pbcopy"
    env = os.environ.copy()
    # pbcopy 按 locale 解码 stdin；TUI 进程的 LANG/LC_* 可能不是 UTF-8，
    # 不显式指定时中文会乱码。
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    env["LC_CTYPE"] = "en_US.UTF-8"
    try:
        result = subprocess.run(
            [command],
            input=text.encode("utf-8"),
            env=env,
            capture_output=True,
            timeout=PBCOPY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"pbcopy 没跑成（{type(exc).__name__}）"
    if result.returncode != 0:
        return False, f"pbcopy 退出码 {result.returncode}"
    return True, ""


class HistoryRecord(tuple):
    """一条历史记录：(kind, text, tone)，外加可选的单次实际模型。

    仍是三元组，旧的解包、切片与相等比较不用改。``actual`` 是这次
    主治医生调用真正用的 (provider, model, effort)；effort 为 None
    表示厂商默认。没有医生调用的记录是 None。不许从状态行文本回填。
    """

    actual: tuple[str, str, str | None] | None

    def __new__(
        cls,
        kind: str,
        text: str,
        tone: str = "plain",
        actual: tuple[str, str, str | None] | None = None,
    ) -> "HistoryRecord":
        record = super().__new__(cls, (kind, text, tone))
        record.actual = actual
        # TUIC2：会话内序号与写入时间，由宿主在写入历史时分配；
        # 只作跳转定位，不进三元组、不参与相等比较、不落盘。
        record.seq = None
        record.ts = None
        # PST1：本条用户消息纳入的粘贴附件 ((占位, 真实路径), …)；随存档落盘。
        record.attachments = None
        # BLK2：工具块元数据 {"call_id", "name", "state"}；随存档落盘。
        record.tool = None
        return record


# ---------------------------------------------------------------------------
# 流式回复的累积状态（每次回复一份，不跨消息复用）
# ---------------------------------------------------------------------------
@dataclass
class _StreamReply:
    """一次 ``reply_stream`` 往返里 TUI 需要记住的东西。

    ``text`` 是当前模型调用已到达的文本（流式期间的增量累积，纯文本展示）；
    ``final_text`` 来自最终 Msg（落定态的唯一权威，与 ``reply()`` 等价）；
    ``output_tokens`` 累计本条回复所有 ModelCallEndEvent 的实报输出 token。
    """

    text: str = ""
    final_text: str | None = None
    output_tokens: int = 0
    interrupted: bool = False
    error: str | None = None
    # 思考只在真有 ThinkingBlock 增量时记下。时长是首个思考增量到首个
    # 正文增量的墙钟；正文增量没到就留空，落定时不编造秒数。
    live_thinking: str = ""
    # TUIC1：整轮思考全文（各次模型调用的 live_thinking 原样按序拼接，
    # 调用之间空一行）。只在内存里给展开用，不进 HistoryRecord。
    all_thinking: str = ""
    _thinking_call_open: bool = False
    saw_thinking: bool = False
    thinking_started: float | None = None
    text_started: float | None = None
    # 调用开始时钉死的 effective 三元组。切换发生在 await 之后也不改它。
    actual: tuple[str, str, str | None] | None = None

    def begin_call(self) -> None:
        """新一轮模型调用开始：增量缓冲清空，只显示这一轮的输出。"""
        self.text = ""
        self.live_thinking = ""
        self._thinking_call_open = False

    def append_delta(self, delta: str) -> None:
        if not delta:
            return
        if self.text_started is None:
            self.text_started = time.monotonic()
        self.text += delta

    def append_thinking(self, delta: str) -> None:
        if not delta:
            return
        if self.thinking_started is None:
            self.thinking_started = time.monotonic()
        self.live_thinking += delta
        if not self._thinking_call_open:
            if self.all_thinking:
                self.all_thinking += "\n\n"
            self._thinking_call_open = True
        self.all_thinking += delta
        self.saw_thinking = True

    def thinking_line(self) -> str:
        """落定后的折叠行。测不到正文增量起点时不写时长。"""
        start = self.thinking_started
        end = self.text_started
        if start is None or end is None or end < start:
            return "思考"
        return f"思考 · {end - start:.1f}s"


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
class SessionHeader(Widget):
    """会话页顶部 slim 标题栏：左会话标题，右 user+assistant 消息计数。"""

    def __init__(self, host: "CifixChatApp", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.host = host

    def render(self) -> Text:
        width = max(self.size.width, 1)
        right = f"共 {self.host.message_count()} 条消息" + "  "
        available = max(1, width - cell_len(right) - 1)
        title = _clip_cells(self.host.session_title(), max(1, available - 1))
        left = " " + title
        gap = width - cell_len(left) - cell_len(right)
        if gap < 1:
            gap = 1
        line = Text(left, style=f"bold {COLOR_TEXT_BASE}")
        line.append(" " * gap, style=COLOR_TEXT_MUTED)
        line.append(right, style=COLOR_TEXT_MUTED)
        return line

    def on_resize(self) -> None:
        self.refresh()


SIDEBAR_WIDTH = 32
TRANSCRIPT_MIN_WIDTH = 40
SIDEBAR_MAX_FILES = 5
NO_CHANGES_TEXT = "暂无改动"
OUT_OF_WORKSPACE_TEXT = "工作区变更"
UNKNOWN_TEXT = "未知"


class SessionSidebar(Static):
    """TUIC1：会话页右侧侧边栏（默认关闭）。四段全读真实后端，不编数。"""

    def __init__(self, host: "CifixChatApp", **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.host = host

    def refresh_content(self) -> None:
        self.update(self.host.sidebar_text())


class HomeScreen(Screen):
    """启动页：居中 logo + 输入框，底部页脚与版本号。"""

    BINDINGS = keybinds.bindings_for(keybinds.SCOPE_PAGE)

    def action_open_error_view(self) -> None:
        self.host.open_error_view()

    def action_toggle_sidebar(self) -> None:
        """侧边栏只在会话页；首页按下不做事。"""

    def action_toggle_thoughts(self) -> None:
        """思考块只在会话页消息流里；首页按下不做事。"""

    def action_open_timeline(self) -> None:
        """消息跳转只在会话页；首页没有消息流，按下不做事（TUIC2 评审）。"""

    def __init__(self, host: "CifixChatApp") -> None:
        super().__init__(id="home")
        self.host = host
        self.recent_paths: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="home-body"):
            with Vertical(id="home-card"):
                yield Static(LOGO_TEXT, id="home-logo", markup=False)
                yield Static("", id="home-gap")
                yield Static("", id="home-notice", markup=False)
                yield PromptBox(self.host, form="home", id="home-prompt")
        with Vertical(id="home-bottom"):
            # TUIB1：提示卡下方的最近工作区（访问历史，不是信任记录；
            # 进入仍先问信任）。放在底栏顶部：卡片仍在内容区垂直居中、高度不变。
            with Vertical(id="home-recents-box"):
                yield Static(
                    recents.EMPTY_TEXT, id="home-recents-empty", markup=False
                )
                yield OptionList(
                    id="home-recents",
                    markup=False,
                    compact=True,
                )
            yield FooterLine(str(self.host.cwd), id="home-footer")
            yield Static(VERSION, id="version", markup=False)

    def on_mount(self) -> None:
        self._layout_prompt()
        self.refresh_notice()
        self.refresh_recents()
        self.query_one(PromptInput).focus()

    def refresh_recents(self) -> None:
        """读表并校验：失效条目从文件里清掉（不弹窗、不落提示行）。"""
        try:
            option_list = self.query_one("#home-recents", OptionList)
            empty = self.query_one("#home-recents-empty", Static)
        except Exception:  # noqa: BLE001 - 尚未挂载完成
            return
        entries = self.host.load_recents()
        self.recent_paths = [entry.path for entry in entries]
        option_list.clear_options()
        if entries:
            option_list.add_options(
                [
                    Option(recents.display_row(entry), id=f"recent-{index}")
                    for index, entry in enumerate(entries)
                ]
            )
            option_list.highlighted = 0
            option_list.styles.display = "block"
            empty.styles.display = "none"
        else:
            option_list.styles.display = "none"
            empty.styles.display = "block"

    @on(OptionList.OptionSelected, "#home-recents")
    def _recent_picked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self.recent_paths):
            self.host.enter_recent(self.recent_paths[index])

    def refresh_notice(self) -> None:
        try:
            notice = self.query_one("#home-notice", Static)
        except Exception:  # noqa: BLE001 - 尚未挂载完成
            return
        if self.host._active_workspace is None:
            notice.update(
                "未绑定工作区：当前目录不是项目目录，主治医生先待命。\n"
                "到项目目录下打开，或在会话里输入 /cd <项目路径>"
                "（会先问你是否信任）。"
            )
            notice.styles.display = "block"
        else:
            notice.update("")
            notice.styles.display = "none"

    def on_resize(self) -> None:
        self._layout_prompt()

    def _layout_prompt(self) -> None:
        width = max(20, min(self.app.size.width - 4, 75))
        self.query_one("#home-card").styles.width = width
        self.query_one("#home-recents-box").styles.width = width


class SessionScreen(Screen):
    """会话页：标题栏 + 消息流占满上方 + 底部全宽输入框。"""

    BINDINGS = keybinds.bindings_for(keybinds.SCOPE_PAGE)

    def action_open_error_view(self) -> None:
        self.host.open_error_view()

    def action_toggle_sidebar(self) -> None:
        self.host.sidebar_open = not self.host.sidebar_open
        self.apply_sidebar()

    def action_open_timeline(self) -> None:
        self.host.open_timeline()

    def action_toggle_thoughts(self) -> None:
        self.host.thoughts_expanded = not self.host.thoughts_expanded
        if self._ready:
            self._reflow_log()

    def action_toggle_thought_block(self) -> None:
        if self.host.toggle_thought_block() and self._ready:
            self._reflow_log()

    def action_toggle_tool_block(self) -> None:
        if self.host.toggle_tool_block() and self._ready:
            self._reflow_log()

    def __init__(self, host: "CifixChatApp") -> None:
        super().__init__(id="session")
        self.host = host
        self._ready = False

    def compose(self) -> ComposeResult:
        yield SessionHeader(self.host, id="session-header")
        with Horizontal(id="session-main"):
            with Vertical(id="transcript"):
                yield ReplyLog(id="log", wrap=True, markup=False)
                with VerticalScroll(id="stream"):
                    yield Static("", id="stream-text", markup=False)
            yield SessionSidebar(self.host, id="sidebar")
        yield Static("", id="busy", markup=False)
        with Vertical(id="session-prompt-wrap"):
            yield PromptBox(self.host, form="session", id="session-prompt")
            yield FooterLine(str(self.host.cwd), id="session-footer")

    def apply_sidebar(self) -> None:
        """开关状态只在会话内（App 上），不落盘。宽度按终端算，消息区保 40 列。"""
        try:
            sidebar = self.query_one("#sidebar", SessionSidebar)
        except Exception:  # noqa: BLE001 - 尚未挂载
            return
        if not self.host.sidebar_open:
            sidebar.styles.display = "none"
            return
        total = self.app.size.width
        width = min(SIDEBAR_WIDTH, max(0, total - TRANSCRIPT_MIN_WIDTH))
        sidebar.styles.width = width
        sidebar.styles.display = "block" if width > 0 else "none"
        sidebar.refresh_content()

    def on_mount(self) -> None:
        self.apply_sidebar()
        self.query_one(PromptInput).focus()
        # 历史重放必须等首次布局完成：on_mount 时 RichLog 还没拿到真实宽度，
        # 用户消息块会按默认 80 列渲染，铺不满整行。
        self.call_after_refresh(self._replay_history)

    def _replay_history(self) -> None:
        log = self.query_one("#log", RichLog)
        # 重放按新宽度重算各记录起始行；序号本身不变。
        self.host.record_lines.clear()
        for record in self.host.history:
            self.host.render_record(log, record)
        self._ready = True
        # 首条消息可能在切页完成前就已开始：补上忙碌指示与已到达的增量。
        if self.host._agent_busy:
            self.host._set_busy_widget(True)
        if self.host.stream_text:
            self.host._render_stream(self.host.stream_text)

    def on_resize(self) -> None:
        # 宽度变化后重放历史：用户消息块按写入时宽度预排，不重放就留在
        # 旧宽度。防抖 0.3 秒，拖拽窗口时只在停下后重排一次。
        self.apply_sidebar()
        if not self._ready:
            return
        timer = getattr(self, "_reflow_timer", None)
        if timer is not None:
            timer.stop()
        self._reflow_timer = self.set_timer(0.3, self._reflow_log)

    def _reflow_log(self) -> None:
        try:
            log = self.query_one("#log", RichLog)
        except Exception:  # noqa: BLE001 - 页已切走
            return
        log.clear()
        self._replay_history()


# ---------------------------------------------------------------------------
# 命令面板（ctrl+p）
# ---------------------------------------------------------------------------
class CommandPalette(ModalScreen[str | None]):
    """居中命令面板：可输入过滤 + 命令列表，enter 执行，esc 关闭。"""

    BINDINGS = keybinds.bindings_for(keybinds.SCREEN_PALETTE)

    def __init__(self) -> None:
        super().__init__(id="palette")

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-card"):
            yield Static("命令面板 · 输入筛选", id="palette-title", markup=False)
            yield Input(placeholder="搜索命令…", id="palette-filter")
            yield OptionList(
                *self._make_options(""),
                id="palette-list",
                markup=False,
                compact=True,
            )

    def on_mount(self) -> None:
        self.query_one("#palette-filter", Input).focus()

    @staticmethod
    def _make_options(query: str) -> list[Option]:
        needle = query.strip().lower()
        options: list[Option] = []
        for command, description in COMMANDS:
            haystack = f"{command} {description}".lower()
            if needle and needle not in haystack:
                continue
            prompt = Text()
            prompt.append(f"{command:<12}", style=f"bold {COLOR_ACCENT}")
            prompt.append(description, style=COLOR_TEXT_MUTED)
            options.append(Option(prompt, id=command))
        return options

    def _refresh_options(self, query: str) -> None:
        option_list = self.query_one("#palette-list", OptionList)
        option_list.clear_options()
        option_list.add_options(self._make_options(query))
        if option_list.option_count:
            option_list.highlighted = 0

    @on(Input.Changed, "#palette-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._refresh_options(event.value)

    def _selected_command(self) -> str | None:
        option_list = self.query_one("#palette-list", OptionList)
        option = option_list.highlighted_option
        if option is None or option.id is None:
            return None
        return str(option.id)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(self._selected_command())

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        self.dismiss(str(event.option.id) if event.option.id else None)

    def action_close_palette(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# 模型选择器（/model）
# ---------------------------------------------------------------------------
class SetupScreen(ModalScreen[None]):
    """首启配置引导：选厂商 → 模型名 → 粘贴 API key（掩码）。

    保存走 core.setup_wizard.run_setup：配置只记厂商/模型/变量名，
    密钥只进用户 env 文件。Esc 或退出 = 直接退出应用——没配模型时
    产品没有可用的大脑，停在主页只会逐条报错。
    """

    BINDINGS = keybinds.bindings_for(keybinds.SCREEN_SETUP)

    def __init__(self, host: "CifixChatApp") -> None:
        super().__init__(id="setup-screen")
        self.host = host
        self._choices: list = []

    def compose(self) -> ComposeResult:
        from .setup_wizard import provider_choices

        self._choices = provider_choices()
        with Vertical(id="model-card"):
            yield Static(
                "先配好模型，产品才能干活 · 选厂商 → 填模型 → 粘贴 API key",
                id="model-title",
                markup=False,
            )
            yield OptionList(
                *[
                    Option(
                        f"{c.provider}（"
                        + ("已真测" if c.verified else "未端到端验证")
                        + "）",
                        id=c.provider,
                    )
                    for c in self._choices
                ],
                id="setup-providers",
                markup=False,
                compact=True,
            )
            yield Input(
                value=self._choices[0].default_model or "",
                placeholder="模型名",
                id="setup-model",
            )
            yield Input(
                placeholder="粘贴 API key（不回显），回车保存",
                id="setup-key",
                password=True,
            )
            yield Static("", id="setup-error", markup=False)
            yield Static("Esc 退出 · 密钥只存用户 env 文件（权限 600）", markup=False)

    def on_mount(self) -> None:
        option_list = self.query_one("#setup-providers", OptionList)
        if option_list.option_count:
            option_list.highlighted = 0
        option_list.focus()

    def _choice(self) -> Any:
        option_list = self.query_one("#setup-providers", OptionList)
        index = option_list.highlighted or 0
        return self._choices[index]

    @on(OptionList.OptionHighlighted, "#setup-providers")
    def _provider_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        choice = self._choice()
        model_input = self.query_one("#setup-model", Input)
        defaults = {c.default_model for c in self._choices}
        if not model_input.value.strip() or model_input.value in defaults:
            model_input.value = choice.default_model or ""

    @on(Input.Submitted, "#setup-key")
    def _key_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def _save(self) -> None:
        from .setup_wizard import run_setup

        choice = self._choice()
        model = self.query_one("#setup-model", Input).value.strip()
        api_key = self.query_one("#setup-key", Input).value
        try:
            result = run_setup(
                choice.provider, model or (choice.default_model or ""), api_key
            )
        except ValueError as exc:
            self.query_one("#setup-error", Static).update(str(exc))
            return
        self.host._event(
            f"配置完成：{result.provider}/{result.model}，密钥已设置。",
            "success",
        )
        self.dismiss(None)

    def action_quit_setup(self) -> None:
        self.app.exit()


class WorkspaceTrustScreen(ModalScreen[bool]):
    """进工作区先问：同意才绑定，拒绝就退出（启动）或取消（/cd）。"""

    BINDINGS = keybinds.bindings_for(keybinds.SCREEN_TRUST)

    def __init__(
        self,
        host: "CifixChatApp",
        path: Path,
        cancel_label: str = "不用，退出",
    ) -> None:
        super().__init__(id="workspace-trust")
        self.host = host
        self.path = path
        self.cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="model-card"):
            yield Static(
                "把这个目录作为工作区？",
                id="model-title",
                markup=False,
            )
            yield Static(str(self.path), markup=False)
            yield Static(
                "同意后，主治医生会在这个目录里读代码、建档并跑修复；"
                "拒绝则不进入。每次打开都会重新问一次。",
                markup=False,
            )
            yield OptionList(
                Option("使用这个目录", id="trust-yes"),
                Option(self.cancel_label, id="trust-no"),
                id="trust-list",
                markup=False,
                compact=True,
            )

    def on_mount(self) -> None:
        option_list = self.query_one("#trust-list", OptionList)
        if option_list.option_count:
            option_list.highlighted = 0
        option_list.focus()

    @on(OptionList.OptionSelected, "#trust-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(getattr(event, "option_id", None) == "trust-yes")

    def action_deny(self) -> None:
        self.dismiss(False)



class _PickerFilter(Input):
    """选择器搜索框。空框时把「仅本次会话」键让给屏幕绑定。

    Textual 在派键前会把 Input 能吃掉的可打印键从父级绑定表删掉，
    ``priority=True`` 也救不回来。空框时不声明消费该键，注册表里的
    ``picker_session`` 才能在默认焦点下生效。框里已有字时照常吃，
    搜索含该字母的型号不受影响。键名走注册表，不写死。
    """

    def check_consume_key(self, key: str, character: str | None) -> bool:
        session = keybinds.key_of("picker_session")
        if session and key == session and not self.value:
            return False
        return super().check_consume_key(key, character)


class ModelPicker(ModalScreen[str | None]):
    """模型选择器：单段列表（目录型号 + 已配置 profile + 重新配置）。

    目录型号行上 ←/→ 轮换该型号的思考档位（目录 efforts，含「默认」，
    循环）。底部状态行显示当前将生效的等级；切到 max 时多一行警告。
    Enter 设为默认（落盘 + 切换），s 仅本次会话（不落盘 + 切换），
    Esc 取消。profile 行与「重新配置」行只保留 Enter 旧行为，
    ←/→ 与 s 在这两类行上无效。键位全部来自 keybinds 注册表。
    """

    BINDINGS = keybinds.bindings_for(keybinds.SCREEN_PICKER)

    def __init__(self, host: "CifixChatApp", catalog: Any) -> None:
        super().__init__(id="model-picker")
        self.host = host
        self.catalog = catalog
        # 每个目录型号自己的待选思考等级。None = 默认。
        self._efforts: dict[str, str | None] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="model-card"):
            yield Static(self._title(), id="model-title", markup=False)
            yield _PickerFilter(
                placeholder="搜索型号 / profile…", id="model-filter"
            )
            yield OptionList(
                *self._make_options(""),
                id="model-list",
                markup=False,
                compact=True,
            )
            yield Static("", id="model-effort-status", markup=False)
            yield Static("", id="model-max-warning", markup=False)

    def on_mount(self) -> None:
        option_list = self.query_one("#model-list", OptionList)
        if option_list.option_count:
            option_list.highlighted = 0
        self.query_one("#model-filter", Input).focus()
        self._refresh_effort_status()

    def _active_profile(self) -> Any:
        for profile in self.catalog.profiles:
            if profile.name == self.catalog.active:
                return profile
        return None

    def _title(self) -> str:
        active = self._active_profile()
        provider = active.provider if active is not None else ""
        display = _PROVIDER_DISPLAY.get(provider.lower(), provider)
        return (
            f"选择模型 · 当前厂商 {display}"
            "（←/→ 思考等级 · Enter 设为默认 · s 仅本次会话）"
        )

    def _provider(self) -> str:
        active = self._active_profile()
        return active.provider if active is not None else ""

    def _make_options(self, query: str) -> list[Option]:
        return (
            self._model_options(query)
            + self._profile_options(query)
            + [self._setup_option()]
        )

    def _model_options(self, query: str) -> list[Option]:
        from .model_catalog import format_context, format_price_pair, models_for

        active = self._active_profile()
        if active is None:
            return []
        needle = query.strip().lower()
        options: list[Option] = []
        for row in models_for(active.provider):
            current = row.id == active.model
            haystack = f"{row.id} {row.name}".lower()
            if needle and needle not in haystack:
                continue
            prompt = Text()
            prompt.append(
                f"{'●' if current else ' '} {row.id:<30}",
                style=f"bold {COLOR_ACCENT}",
            )
            prompt.append(row.name, style=COLOR_TEXT_BASE)
            if current:
                prompt.append("  当前", style=f"bold {COLOR_SUCCESS}")
            prompt.append(
                f"  看图 {'有' if row.vision else '无'}"
                f" · 思考 {'有' if row.thinking else '无'}",
                style=COLOR_TEXT_MUTED,
            )
            if row.efforts:
                prompt.append(
                    f" · 思考等级 {len(row.efforts)} 档可选",
                    style=COLOR_TEXT_MUTED,
                )
            # TUIM1b：上下文与价格只在目录有数据时显示，未知不显示 0 / 不编造。
            context = format_context(row.context_limit)
            if context is not None:
                prompt.append(f" · 上下文 {context}", style=COLOR_TEXT_MUTED)
            price = format_price_pair(row)
            if price is not None:
                prompt.append(f" · {price}", style=COLOR_TEXT_MUTED)
            options.append(Option(prompt, id=f"model:{row.id}"))
        return options

    def _setup_option(self) -> Option:
        prompt = Text()
        prompt.append("  重新配置（换厂商 / 换密钥）…", style=COLOR_TEXT_BASE)
        return Option(prompt, id="__setup__")

    def _profile_options(self, query: str) -> list[Option]:
        from .model_config import key_is_set, model_capabilities

        needle = query.strip().lower()
        options: list[Option] = []
        for profile in self.catalog.profiles:
            active = profile.name == self.catalog.active
            key_state = "已设置" if key_is_set(profile) else "未设置"
            haystack = (
                f"{profile.name} {profile.provider} {profile.model} "
                f"{key_state} {'当前' if active else ''}"
            ).lower()
            if needle and needle not in haystack:
                continue
            prompt = Text()
            prompt.append(
                f"{'●' if active else ' '} {profile.name:<14}",
                style=f"bold {COLOR_ACCENT}",
            )
            prompt.append(
                f"{profile.provider}/{profile.model}",
                style=COLOR_TEXT_BASE,
            )
            if active:
                prompt.append("  当前", style=f"bold {COLOR_SUCCESS}")
            prompt.append(
                f"  密钥 {key_state}",
                style=COLOR_SUCCESS if key_is_set(profile) else COLOR_ERROR,
            )
            if profile.reasoning_effort:
                prompt.append(
                    f"  effort={profile.reasoning_effort}",
                    style=COLOR_TEXT_MUTED,
                )
            caps = model_capabilities(profile.model)
            prompt.append(
                f"  看图 {'有' if caps.vision else '无'}"
                f" · 思考 {'有' if caps.thinking else '无'}",
                style=COLOR_TEXT_MUTED,
            )
            if getattr(profile, "auto_generated", False):
                prompt.append("  自动生成", style=COLOR_TEXT_MUTED)
            options.append(Option(prompt, id=profile.name))
        return options

    def _refresh_options(self, query: str) -> None:
        option_list = self.query_one("#model-list", OptionList)
        option_list.clear_options()
        option_list.add_options(self._make_options(query))
        if option_list.option_count:
            option_list.highlighted = 0
        self._refresh_effort_status()

    @on(Input.Changed, "#model-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._refresh_options(event.value)

    @on(OptionList.OptionHighlighted, "#model-list")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._refresh_effort_status()

    def on_key(self, event: Any) -> None:
        """Filter input keeps focus; ↑/↓ still move the option highlight."""
        up = keybinds.key_of("picker_cursor_up")
        down = keybinds.key_of("picker_cursor_down")
        if event.key is None or event.key not in (up, down):
            return
        option_list = self.query_one("#model-list", OptionList)
        if not option_list.option_count:
            return
        event.stop()
        event.prevent_default()
        if event.key == up:
            option_list.action_cursor_up()
        else:
            option_list.action_cursor_down()

    def _selected_id(self) -> str | None:
        option_list = self.query_one("#model-list", OptionList)
        option = option_list.highlighted_option
        if option is None or option.id is None:
            return None
        return str(option.id)

    def _initial_effort(self, model_id: str) -> str | None:
        """该型号打开选择器时的档位：与当前 effective 同型号则用其等级，否则默认。"""
        provider = self._provider().strip().lower()
        getter = getattr(self.host, "effective_model", None)
        current = None
        if callable(getter):
            try:
                current = getter()
            except Exception:  # noqa: BLE001 - 读不出配置就从默认档起
                current = None
        if (
            current is not None
            and current.model == model_id
            and (current.provider or "").strip().lower() == provider
        ):
            return current.effort
        return None

    def _effort_for(self, model_id: str) -> str | None:
        if model_id not in self._efforts:
            self._efforts[model_id] = self._initial_effort(model_id)
        return self._efforts[model_id]

    def _cycle_highlighted(self, step: int) -> None:
        """目录型号行上轮换思考档位。profile / 重新配置行直接返回。"""
        oid = self._selected_id()
        if not oid or not oid.startswith("model:"):
            return
        model_id = oid[len("model:") :]
        from .model_state import effort_rungs

        rungs = effort_rungs(self._provider(), model_id)
        if not rungs:
            return
        current = self._effort_for(model_id)
        index = rungs.index(current) if current in rungs else 0
        self._efforts[model_id] = rungs[(index + step) % len(rungs)]
        self._refresh_effort_status()

    def _refresh_effort_status(self) -> None:
        from .model_state import MAX_EFFORT_WARNING, effort_label

        try:
            status = self.query_one("#model-effort-status", Static)
            warning = self.query_one("#model-max-warning", Static)
        except Exception:  # noqa: BLE001 - compose 尚未完成
            return
        oid = self._selected_id()
        if not oid or not oid.startswith("model:"):
            status.update("")
            status.display = False
            warning.update("")
            warning.display = False
            return
        effort = self._effort_for(oid[len("model:") :])
        status.display = True
        status.update(f"思考等级：{effort_label(effort)}")
        if effort == "max":
            warning.display = True
            warning.update(MAX_EFFORT_WARNING)
        else:
            warning.update("")
            warning.display = False

    def action_picker_effort_prev(self) -> None:
        self._cycle_highlighted(-1)

    def action_picker_effort_next(self) -> None:
        self._cycle_highlighted(1)

    def action_picker_session(self) -> None:
        """s：仅本次会话。非目录型号行无效，不许误触。"""
        self._activate(self._selected_id(), persist=False)

    def _activate(self, oid: str | None, *, persist: bool = True) -> None:
        if not oid:
            return
        if oid.startswith("model:"):
            from .model_catalog import catalog_model

            row = catalog_model(self._provider(), oid[len("model:") :])
            if row is None:
                return
            self._apply(row, self._effort_for(row.id), persist=persist)
            return
        if not persist:
            return
        if oid == "__setup__":
            self.dismiss(None)
            self.host.call_after_refresh(
                lambda: self.host.push_screen(SetupScreen(self.host))
            )
            return
        self._choose(oid)

    def _apply(self, row: Any, effort: str | None, *, persist: bool) -> None:
        provider = self._provider()
        model_id = row.id
        self.dismiss(None)
        self.host.call_after_refresh(
            lambda provider=provider, model_id=model_id, effort=effort, persist=persist: (
                self.host._apply_model_selection(
                    provider, model_id, effort, persist=persist
                )
            )
        )

    def _choose(self, name: str | None) -> None:
        """关闭浮层，再把选择交回应用（不依赖 push_screen 的结果回调）。"""
        self.dismiss(None)
        if name:
            self.host.call_after_refresh(
                lambda name=name: self.host._model_selected(name)
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._activate(self._selected_id())

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        self._activate(str(event.option.id) if event.option.id else None)

    def action_close_picker(self) -> None:
        self.dismiss(None)


def _paste_view_text(blocks: list[prompt_extras.PasteBlock]) -> str:
    return "\n\n".join(
        f"#{block.block_id}（{block.lines} 行）\n{block.text}" for block in blocks
    )


class InfoViewModal(ModalScreen[None]):
    """只读信息弹窗（/status、/mcp 共用，TUIE1）。关闭键走注册表。"""

    DEFAULT_CSS = """
    InfoViewModal { align: center middle; }
    #info-card { width: 90%; height: auto; max-height: 80%; border: round $accent; }
    """

    def __init__(
        self, *, screen_id: str, close_id: str, title: str, body: str
    ) -> None:
        super().__init__(id=screen_id)
        self.close_id = close_id
        self.title_text = title
        self.body_text = body

    def compose(self) -> ComposeResult:
        close = keybinds.key_of(self.close_id) or "—"
        with Vertical(id="info-card"):
            yield Static(
                f"{self.title_text}（只读，{close} 关闭）",
                id="info-title",
                markup=False,
            )
            yield Static(self.body_text, id="info-body", markup=False)

    def on_key(self, event: Any) -> None:
        close = keybinds.key_of(self.close_id)
        if close is not None and event.key == close:
            event.stop()
            event.prevent_default()
            self.dismiss(None)


TIMELINE_KIND_LABELS = {
    "user": "用户",
    "assistant": "主治医生",
    "event": "事件",
    "plain": "系统",
    "diff": "补丁",
}
TIMELINE_EMPTY_TEXT = "暂无消息"
TIMELINE_SUMMARY_CHARS = 30


def timeline_row(record: "HistoryRecord") -> str:
    """序号 + 类型 + 首行摘要前 30 字 + 时间（HH:MM:SS）。"""
    kind, text, _tone = record
    first = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
    summary = first[:TIMELINE_SUMMARY_CHARS]
    label = TIMELINE_KIND_LABELS.get(kind, kind)
    ts = getattr(record, "ts", None)
    when = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
    return f"#{record.seq}  {label}  {summary}  {when}"


class TimelineModal(ModalScreen[int | None]):
    """消息跳转列表（TUIC2）。Enter 跳转、关闭键走注册表。"""

    DEFAULT_CSS = """
    TimelineModal { align: center middle; }
    #timeline-card { width: 90%; height: 80%; border: round $accent; }
    #timeline-list { height: 1fr; }
    """

    def __init__(self, host: "CifixChatApp") -> None:
        super().__init__(id=keybinds.SCREEN_TIMELINE)
        self.host = host
        self.seqs = [
            r.seq for r in host.history if getattr(r, "seq", None) is not None
        ]

    def compose(self) -> ComposeResult:
        close = keybinds.key_of("timeline_close") or "—"
        with Vertical(id="timeline-card"):
            yield Static(
                f"消息跳转（Enter 跳转，{close} 关闭）",
                id="timeline-title",
                markup=False,
            )
            records = [
                r for r in self.host.history if getattr(r, "seq", None) is not None
            ]
            if not records:
                yield Static(TIMELINE_EMPTY_TEXT, id="timeline-empty", markup=False)
                return
            yield OptionList(
                *[Option(timeline_row(r), id=f"seq-{r.seq}") for r in records],
                id="timeline-list",
                markup=False,
                compact=True,
            )

    def on_mount(self) -> None:
        try:
            option_list = self.query_one("#timeline-list", OptionList)
        except Exception:  # noqa: BLE001 - 空会话没有列表
            return
        option_list.highlighted = option_list.option_count - 1
        option_list.focus()

    @on(OptionList.OptionSelected, "#timeline-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self.seqs):
            self.dismiss(self.seqs[index])

    def on_key(self, event: Any) -> None:
        close = keybinds.key_of("timeline_close")
        if close is not None and event.key == close:
            event.stop()
            event.prevent_default()
            self.dismiss(None)


SESSIONS_SCREEN_ID = "sessions-view"
SESSIONS_USAGE = (
    "用法：/sessions 列出本工作区存档的会话；/sessions restore <id> 恢复；"
    "/sessions rename <id> <新标题> 重命名；/sessions delete <id> 删除"
)
SESSIONS_EMPTY_TEXT = "本工作区还没有存档的会话"
THOUGHT_NOT_ARCHIVED_TEXT = "（思考全文不在存档里，恢复后只剩折叠行）"
# BLK2：工具块。参数与结果摘录只存内存，存档只留折叠行 + 状态。
TOOL_NOT_ARCHIVED_TEXT = "（工具参数与结果不在存档里，恢复后只剩折叠行）"
TOOL_EXCERPT_HEAD = 40
TOOL_EXCERPT_TAIL = 20
TOOL_EXCERPT_MAX_BYTES = 8 * 1024
_TOOL_STATE_LABELS = {
    "pending": "等待",
    "running": "进行中",
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "unknown": "未知",
}


def _tool_excerpt(text: str) -> str:
    """BLK2：结果摘录 = 头 40 行 + 尾 20 行（中间明示省略行数），总量 ≤ 8KB。"""
    lines = text.splitlines()
    if len(lines) > TOOL_EXCERPT_HEAD + TOOL_EXCERPT_TAIL:
        omitted = len(lines) - TOOL_EXCERPT_HEAD - TOOL_EXCERPT_TAIL
        lines = (
            lines[:TOOL_EXCERPT_HEAD]
            + [f"…中间省略 {omitted} 行…"]
            + lines[-TOOL_EXCERPT_TAIL:]
        )
    out = "\n".join(lines)
    data = out.encode("utf-8")
    if len(data) > TOOL_EXCERPT_MAX_BYTES:
        note = "\n…已截断（超过 8KB）…"
        keep = TOOL_EXCERPT_MAX_BYTES - len(note.encode("utf-8"))
        out = data[:keep].decode("utf-8", "ignore") + note
    return out


def _tool_args_pretty(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        return raw


def _tool_args_brief(raw: str) -> str:
    """折叠行用的参数摘要：关键参数拼短串（已脱敏的原文上取）。"""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return " ".join(str(raw).split())
    if isinstance(value, dict):
        return "，".join(
            f"{key}={' '.join(str(item).split())}" for key, item in value.items()
        )
    return " ".join(str(value).split())


def _is_thinking_line(text: str) -> bool:
    return text == "思考" or str(text).startswith("思考 · ")


class SessionsModal(ModalScreen[str | None]):
    """F1：会话存档列表。Enter 恢复所选；关闭键沿用既有弹窗的 Esc，不新增键位。"""

    DEFAULT_CSS = """
    SessionsModal { align: center middle; }
    #sessions-card { width: 90%; height: 80%; border: round $accent; }
    #sessions-list { height: 1fr; }
    """

    def __init__(self, items: list) -> None:
        super().__init__(id=SESSIONS_SCREEN_ID)
        self.items = list(items)

    def compose(self) -> ComposeResult:
        from .session_admin import format_row

        close = keybinds.key_of("timeline_close") or "—"
        with Vertical(id="sessions-card"):
            yield Static(
                f"本工作区存档的会话（Enter 恢复，{close} 关闭）",
                id="sessions-title",
                markup=False,
            )
            yield OptionList(
                *[Option(format_row(item), id=f"s-{i}") for i, item in enumerate(self.items)],
                id="sessions-list",
                markup=False,
                compact=True,
            )

    def on_mount(self) -> None:
        option_list = self.query_one("#sessions-list", OptionList)
        option_list.highlighted = 0
        option_list.focus()

    @on(OptionList.OptionSelected, "#sessions-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self.items):
            self.dismiss(self.items[index].session_id)

    def on_key(self, event: Any) -> None:
        close = keybinds.key_of("timeline_close")
        if close is not None and event.key == close:
            event.stop()
            event.prevent_default()
            self.dismiss(None)


class PasteViewModal(ModalScreen[None]):
    """只读查看当前输入里的粘贴块全文（TUID1）。关闭键走注册表。"""

    DEFAULT_CSS = """
    PasteViewModal { align: center middle; }
    #paste-card { width: 90%; height: 80%; border: round $accent; }
    #paste-body { height: 1fr; }
    """

    def __init__(self, blocks: list[prompt_extras.PasteBlock]) -> None:
        super().__init__(id=keybinds.SCREEN_PASTE_VIEW)
        self.blocks = list(blocks)

    def compose(self) -> ComposeResult:
        close = keybinds.key_of("paste_view_close") or "—"
        with Vertical(id="paste-card"):
            yield Static(
                f"粘贴块全文（只读，{close} 关闭）",
                id="paste-title",
                markup=False,
            )
            yield TextArea(
                _paste_view_text(self.blocks),
                read_only=True,
                id="paste-body",
            )

    def on_key(self, event: Any) -> None:
        close = keybinds.key_of("paste_view_close")
        if close is not None and event.key == close:
            event.stop()
            event.prevent_default()
            self.dismiss(None)


def _done_future(value: Any) -> Any:
    """把已拿到的答复包成一个已完成的 future，沿用既有 *_answered 回调。"""
    future = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


class ConfirmModal(ModalScreen[None]):
    """权限确认弹窗（PERM2a）。

    文案在构造时由程序生成完毕（见 _confirm_modal_lines），
    本类只负责按键与两屏切换。答复经 ``host._resolve_confirm``
    交回 _service_confirm 正在等的 future，形状是
    ``(代码, 理由)``：代码 Y/A/P/N，理由只在拒绝时有意义。
    理由输入框默认不抢焦点：字母键直接是答复键；点入或 Tab
    进输入框后，字母只打字，Enter = 带理由拒绝。
    """

    BINDINGS = keybinds.bindings_for(keybinds.SCREEN_CONFIRM)

    AUTO_FOCUS = ""

    _seq = 0

    def __init__(
        self,
        host: "CifixChatApp",
        *,
        lines: list[str],
        preview_lines: list[str] | None,
        future: Any = None,
    ) -> None:
        # CFM1：id 仍是键位注册表的屏幕名（test_keybinds 契约）；
        # 序号另存在 seq 上。撞号由 _present_confirm 排队并等上一个
        # 卸载完再推下一个来杜绝——任何时刻栈里至多一个确认弹窗。
        ConfirmModal._seq += 1
        self.seq = ConfirmModal._seq
        super().__init__(id="confirm-modal")
        self.host = host
        # CFM1：本弹窗服务的那个答复通道；答复只回它自己的请求者。
        self._future = future
        self._lines = lines
        self._preview_lines = preview_lines
        self._in_preview = False

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-card"):
            yield Static(
                "\n".join(self._lines), id="confirm-body", markup=False
            )
            yield Input(
                placeholder="拒绝理由（可选，填了会转告秘书）",
                id="confirm-reason",
            )

    def _reason(self) -> str:
        try:
            return self.query_one("#confirm-reason", Input).value.strip()
        except Exception:  # noqa: BLE001 - 输入框不在时理由为空
            return ""

    def _answer(self, code: str) -> None:
        reason = self._reason() if code == "N" else ""
        self.dismiss(None)
        future = self._future
        if future is not None:
            if not future.done():
                future.set_result((code, reason))
            return
        self.host._resolve_confirm((code, reason))

    def action_deny(self) -> None:
        if self._in_preview:
            self._in_preview = False
            self.query_one("#confirm-body", Static).update(
                "\n".join(self._lines)
            )
            return
        self._answer("N")

    def on_key(self, event: Any) -> None:
        reason_input = self.query_one("#confirm-reason", Input)
        enter_key = keybinds.key_of("confirm_enter")
        if reason_input.has_focus:
            if enter_key is not None and event.key == enter_key:
                event.stop()
                self._answer("N")
            return
        key = event.key.lower()
        preview_key = keybinds.key_of("confirm_preview")
        if self._in_preview:
            if preview_key is not None and key == preview_key:
                event.stop()
                self._answer("P")
            return
        yes_key = keybinds.key_of("confirm_yes")
        always_key = keybinds.key_of("confirm_always")
        no_key = keybinds.key_of("confirm_no")
        if (enter_key is not None and key == enter_key) or (
            yes_key is not None and key == yes_key
        ):
            event.stop()
            self._answer("Y")
        elif always_key is not None and key == always_key:
            event.stop()
            self._answer("A")
        elif (
            preview_key is not None
            and key == preview_key
            and self._preview_lines is not None
        ):
            event.stop()
            self._in_preview = True
            self.query_one("#confirm-body", Static).update(
                "\n".join(self._preview_lines)
            )
        elif no_key is not None and key == no_key:
            event.stop()
            self._answer("N")


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
class CifixChatApp(App):
    """Doctor Bug 全屏交互页。

    Args:
        model: 注入的 ``ChatModelBase`` 替身；``None`` 时首次对话才经
            ``model_config.build_model()`` 构建（惰性、离线导入）。
        workspace_root: 启动目录。含项目特征时绑定该目录；都不含（或
            主目录只有工具自己的 ``~/.drbug`` 缓存）时进入未绑定待命态，
            不默认使用任何示例。测试可注入临时目录。默认本仓库根目录。
    """

    CSS = f"""
    Screen {{
        background: {COLOR_BACKGROUND_BASE};
        color: {COLOR_TEXT_BASE};
    }}
    HomeScreen {{
        layout: vertical;
        background: {COLOR_BACKGROUND_BASE};
    }}
    #home-body {{
        height: 1fr;
        align: center middle;
    }}
    #home-card {{
        width: 75;
        height: auto;
    }}
    #home-logo {{
        width: 100%;
        height: 4;
        text-align: center;
    }}
    #home-gap {{
        height: 1;
    }}
    #home-recents-box {{
        width: 75;
        height: auto;
        margin-bottom: 1;
    }}
    #home-recents-empty {{
        margin-top: 1;
        color: {COLOR_TEXT_MUTED};
    }}
    #home-recents {{
        margin-top: 1;
        height: auto;
        max-height: 10;
    }}
    #home-bottom {{
        height: auto;
        background: {COLOR_BACKGROUND_BASE};
    }}
    FooterLine {{
        width: 100%;
        height: 1;
    }}
    #version {{
        width: 100%;
        height: 1;
        padding: 0 2;
        text-align: right;
        color: {COLOR_TEXT_MUTED};
    }}
    SessionScreen {{
        layout: vertical;
        background: {COLOR_BACKGROUND_BASE};
    }}
    #session-header {{
        width: 100%;
        height: 1;
        background: {COLOR_BACKGROUND_RAISED};
        color: {COLOR_TEXT_MUTED};
    }}
    #session-main {{
        height: 1fr;
        width: 100%;
    }}
    #transcript {{
        height: 100%;
        width: 1fr;
        min-width: 40;
    }}
    #sidebar {{
        display: none;
        width: 32;
        height: 100%;
        padding: 0 1;
        background: {COLOR_BACKGROUND_PANEL};
        color: {COLOR_TEXT_MUTED};
    }}
    #log {{
        height: 1fr;
        background: {COLOR_BACKGROUND_BASE};
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }}
    #stream {{
        width: 100%;
        height: auto;
        max-height: 12;
        padding: 0 2;
        background: {COLOR_BACKGROUND_BASE};
        scrollbar-size-vertical: 1;
        display: none;
    }}
    #stream-text {{
        width: 100%;
        height: auto;
        background: {COLOR_BACKGROUND_BASE};
    }}
    #busy {{
        height: 1;
        padding: 0 2;
        color: {COLOR_WARNING};
        display: none;
    }}
    #session-prompt-wrap {{
        height: auto;
        padding: 0 2;
        background: {COLOR_BACKGROUND_BASE};
    }}
    PromptBox {{
        width: 100%;
        height: auto;
        background: {COLOR_BACKGROUND_BASE};
    }}
    .prompt-panel {{
        height: auto;
        background: {COLOR_BACKGROUND_PANEL};
        border-left: heavy {COLOR_ACCENT};
        padding: 1 2 0 2;
    }}
    .prompt-input {{
        height: 1;
        width: 100%;
        background: {COLOR_BACKGROUND_PANEL};
        border: none;
        padding: 0;
    }}
    .prompt-gap {{
        height: 1;
    }}
    .prompt-status {{
        height: 1;
        width: 100%;
        background: {COLOR_BACKGROUND_PANEL};
    }}
    .slash-suggest {{
        height: auto;
        max-height: 12;
        width: 100%;
        padding: 0 2;
        background: {COLOR_BACKGROUND_PANEL};
        color: {COLOR_TEXT_BASE};
        display: none;
    }}
    PromptBase {{
        width: 100%;
        height: 1;
    }}
    CommandPalette, ModelPicker, SetupScreen, WorkspaceTrustScreen, ConfirmModal {{
        align: center middle;
        background: rgba(0,0,0,0.6);
    }}
    #palette-card, #model-card, #confirm-card {{
        width: 64;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        background: {COLOR_BACKGROUND_RAISED};
        border: solid {COLOR_BORDER};
        padding: 1 2;
    }}
    #palette-title, #model-title {{
        height: 1;
        color: {COLOR_TEXT_MUTED};
    }}
    #palette-filter, #model-filter {{
        height: 1;
        border: none;
        padding: 0 1;
        background: {COLOR_BACKGROUND_PANEL};
        color: {COLOR_TEXT_BASE};
    }}
    #palette-list, #model-list {{
        height: auto;
        max-height: 14;
        margin-top: 1;
        background: {COLOR_BACKGROUND_PANEL};
        border: none;
    }}
    #palette-list > .option-list--option,
    #model-list > .option-list--option {{
        padding: 0 1;
    }}
    #palette-list > .option-list--option-highlighted,
    #model-list > .option-list--option-highlighted {{
        background: {COLOR_BACKGROUND_RAISED};
    }}
    #model-effort-status {{
        height: 1;
        margin-top: 1;
        color: {COLOR_TEXT_MUTED};
    }}
    #model-max-warning {{
        height: 1;
        color: {COLOR_WARNING};
    }}
    """

    BINDINGS = keybinds.bindings_for(keybinds.SCOPE_GLOBAL)

    TITLE = "Doctor Bug"

    def __init__(
        self,
        model: Any = None,
        workspace_root: str | Path | None = None,
        perm_mode: str = "ask",
        interactive_gates: bool = False,
    ) -> None:
        super().__init__()
        self._injected_model = model
        self._interactive_gates = interactive_gates
        # 信任只在本次会话内有效：同意过也只算这一次，下次打开重新问。
        self._session_trusted: set[str] = set()
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else PLATFORM_ROOT
        )
        # 活跃工作区：None 待命；Path 是目录。
        # 纳管清单只活在本次进程的这个 App 上，不写任何文件。
        self._active_workspace: Path | str | None = (
            self._classify_startup_workspace()
        )
        self._managed_workspaces: list[Path] = []
        resolved_active = self._resolved_active_path()
        if resolved_active is not None:
            self._managed_workspaces.append(resolved_active)
        self._apply_process_workspace_binding()
        self.cwd = Path.cwd()
        self.page = "home"
        self.actor_backend = "agentscope"
        self.history: list[HistoryRecord] = []
        self.input_history: list[str] = []
        # TUID1：草稿暂存单槽位（只在内存，退出即消失）+ 粘贴块表。
        self.draft_stash: prompt_extras.StashedDraft | None = None
        self.paste_blocks = prompt_extras.PasteTable()
        # TUID2：@ 选中后已过门禁的文本附件（一轮一个，与附图通道一致）。
        self._mention_attachment: file_mention.FileAttachment | None = None
        # TUIE2：最近 20 条错误（只在内存，退出即消失）+ 当前触发来源。
        self.error_log = error_log.ErrorLog()
        self._error_lock = threading.Lock()
        self._error_source: str | None = None
        # TUIC2：消息序号（会话内单调递增）+ 各记录在 RichLog 的起始行；
        # 有待追加的 diff 块（结果包已落盘、等本次运行/回复落定）。
        self._next_seq = 1
        self.record_lines: dict[int, int] = {}
        self._pending_diff = False
        # TUIC1：侧边栏开关与思考块展开态，只活在本次会话（不落盘）。
        self.sidebar_open = False
        self.thoughts_expanded = False
        # 思考全文：键为该条「思考」事件记录对象的 id（记录只增不删，id 稳定）。
        self._thought_texts: dict[int, str] = {}
        # BLK1：按块展开集合（键同 _thought_texts，只活在本次会话、不落盘）。
        self._thought_blocks_expanded: set[int] = set()
        # BLK1 B2：TUIC2 跳转落点（记录 seq）；按块切换作用于它。
        self._jumped_seq: int | None = None
        # BLK2：工具块。按 tool_call_id 配对的进行中数据（原文只在这里暂存），
        # 以及按记录 id 存的脱敏后参数/结果（只存内存，不落盘）、按块展开集合。
        self._tool_pending: dict[str, dict] = {}
        self._tool_texts: dict[int, dict] = {}
        self._tool_blocks_expanded: set[int] = set()
        # F2：当前会话的存档 id（首次存档时分配，恢复时沿用存档的 id）、
        # 上次存档时的消息数（判断有无未存档内容）、本会话工具记录。
        self._session_id: str | None = None
        self._saved_messages = 0
        self._tool_runs: dict[str, Any] = {}
        # 最近一轮本会话运行的结果包目录（由调度员落盘回调告知）。
        self.last_bundle: Path | None = None
        # TUIB1：最近工作区文件；None = user_config_dir()/recents.json。
        self.recents_file: Path | None = None
        self.stream_text = ""
        self._dispatcher: Any = None
        self._dispatcher_worker: Any = None
        self._dispatcher_generation = 0
        self._active_profile: Any = None
        self._active_model: Any = None
        from .model_state import SessionModelState

        # 会话覆盖只活在这个对象上。退出应用即消失，不写配置文件。
        self._model_state = SessionModelState()
        self._context_restart_pending = False
        if perm_mode not in PERM_MODES:
            raise ValueError(f"未知权限模式：{perm_mode!r}")
        self._perm_mode = perm_mode
        self._agent_busy = False
        self._awaiting_confirm = False
        self._confirm_future: Any = None
        # CFM1：所有确认入口共用的排队锁（首次使用时在应用循环上创建）。
        self._confirm_lock: asyncio.Lock | None = None
        self._spinner_index = 0
        self._example_index = 0
        self._reply_started: float | None = None
        self._interrupt_requested = False
        self._main_thread = threading.get_ident()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_mount(self) -> None:
        self._main_thread = threading.get_ident()
        self.push_screen(HomeScreen(self))
        from . import dispatcher as _dispatcher_module

        _dispatcher_module.set_bundle_listener(self._on_bundle_written)
        self.set_interval(0.12, self._tick_spinner)
        self.set_interval(12.0, self._rotate_placeholder)
        if self._interactive_gates:
            self._run_startup_gates()

    def _rotate_placeholder(self) -> None:
        self._example_index = (self._example_index + 1) % len(
            PLACEHOLDER_EXAMPLES
        )
        self.refresh_placeholder()

    def _hide_completion_menus(self) -> None:
        """SLASH1：进入等待确认时收起已弹出的补全浮层（确认优先）。"""
        try:
            for box in self.screen.query(PromptBox):
                box.hide_suggestions()
        except Exception:  # noqa: BLE001 - 切页瞬间可能没有输入框
            pass

    def refresh_placeholder(self) -> None:
        """HINT1：按当前会话状态重算输入框提示并写回输入框（只改提示）。"""
        placeholder = self.example_placeholder()
        try:
            for box in self.screen.query(PromptBox):
                box.set_placeholder(placeholder)
        except Exception:  # noqa: BLE001 - 切页瞬间可能没有输入框
            pass

    def _hint_has_profile(self, workspace: Path) -> bool:
        """只读：该项目的派活规则表是否已在任一档案目录里（/profile 写处或
        /accept、/run 读处）。"""
        from .autoprofile import default_profile_name
        from .dispatcher import _profiles_root_for

        name = default_profile_name(workspace)
        roots = (self.workspace_root / "profiles", _profiles_root_for(workspace))
        return any((root / name / "dispatch_table.json").is_file() for root in roots)

    def _hint_has_runs(self, workspace: Path) -> bool:
        """只读：该项目的运行记录目录里是否已有记录。"""
        from .dispatcher import _runs_root_for

        root = _runs_root_for(workspace)
        try:
            return root.is_dir() and any(root.iterdir())
        except OSError:
            return False

    def hint_state(self) -> str:
        """HINT1：从现成会话状态判定提示该说哪一条（优先级从上到下）。"""
        if self._awaiting_confirm:
            return HINT_CONFIRM
        workspace = self._resolved_active_path()
        if workspace is None:
            return HINT_UNBOUND
        try:
            if not self._hint_has_profile(workspace):
                return HINT_NO_PROFILE
            if self.last_bundle is not None:
                return HINT_RESULT
            if not self._hint_has_runs(workspace):
                return HINT_NO_RUN
        except Exception:  # noqa: BLE001 - 提示只是提示，读不到就走默认
            return HINT_IDLE
        return HINT_IDLE

    def example_placeholder(self) -> str:
        state = self.hint_state()
        if state in HINT_TEXTS:
            return HINT_TEXTS[state]
        example = PLACEHOLDER_EXAMPLES[
            self._example_index % len(PLACEHOLDER_EXAMPLES)
        ]
        return f'问点什么，或让我办事… "{example}"'

    # ------------------------------------------------------------------
    # 状态行
    # ------------------------------------------------------------------
    def effective_model(self):
        """当前生效三元组。界面只调 core 这一处，不自己拼 ??。"""
        return self._model_state.effective()

    def _status_triple(self):
        """状态行要显示的三元组。

        有会话覆盖时就是 effective（覆盖整组）。已落盘切换过的档案
        用那份档案。注入替身且两者都没有时返回 None，沿用旧的替身
        展示（既有测试钉死 fake-chat-model）。其余走 effective()。
        """
        from .model_state import ModelTriple

        if self._model_state.override is not None:
            return self.effective_model()
        if self._active_profile is not None:
            profile = self._active_profile
            return ModelTriple(
                (profile.provider or "").strip().lower(),
                profile.model,
                profile.reasoning_effort,
            )
        if self._injected_model is not None:
            return None
        try:
            return self.effective_model()
        except Exception:  # noqa: BLE001 - 状态行只做展示
            return None

    def _model_label(self) -> str:
        shown = self._status_triple()
        if shown is not None:
            return shown.model or "未配置"
        if self._injected_model is not None:
            return str(getattr(self._injected_model, "model", None) or "测试替身")
        return "未配置"

    def _provider_label(self) -> str:
        """模型提供方：覆盖 / 已切换档案 / 注入替身 / 配置文件。"""
        shown = self._status_triple()
        if shown is not None:
            provider = shown.provider.strip()
            return _PROVIDER_DISPLAY.get(provider.lower(), provider or "未配置")
        if self._injected_model is not None:
            provider = str(getattr(self._injected_model, "provider", "") or "")
            return _PROVIDER_DISPLAY.get(provider.lower(), provider or "DeepSeek")
        return "未配置"

    def _effort_label(self) -> str:
        """当前思考等级实值。有覆盖或已解析的三元组时用 effective。"""
        from .model_state import effort_label

        shown = self._status_triple()
        if shown is not None:
            return effort_label(shown.effort)
        return "默认"

    def status_line(self) -> Text:
        """输入框内状态行：主治医生 · 模型实值 提供方 · 思考等级实值。"""
        from .model_state import SESSION_MARKER

        line = Text()
        line.append("主治医生", style=f"bold {COLOR_ACCENT}")
        line.append(" · ", style=COLOR_TEXT_MUTED)
        line.append(self._model_label(), style=COLOR_TEXT_BASE)
        effort = f" {self._provider_label()} · 思考 {self._effort_label()}"
        if self._model_state.override is not None:
            effort += f" {SESSION_MARKER}"
        line.append(effort, style=COLOR_TEXT_MUTED)
        line.append(
            f" · 工作区：{self._workspace_status_label()}",
            style=COLOR_TEXT_MUTED,
        )
        line.append(
            f" · 模式：{MODE_LABELS[self._perm_mode]}",
            style=COLOR_TEXT_MUTED,
        )
        return line

    def _refresh_status_lines(self) -> None:
        try:
            for status in self.screen.query(".prompt-status"):
                status.update(self.status_line())
        except Exception:  # noqa: BLE001 - 切页瞬间可能没有状态行
            pass
        self._refresh_sidebar()

    # ------------------------------------------------------------------
    # TUIC1：侧边栏
    # ------------------------------------------------------------------
    def _refresh_sidebar(self) -> None:
        if not self.sidebar_open:
            return
        try:
            for sidebar in self.screen.query(SessionSidebar):
                sidebar.refresh_content()
        except Exception:  # noqa: BLE001 - 切页瞬间
            pass

    def _on_bundle_written(self, bundle: Path) -> None:
        """调度员落盘结果包的回调（可能在 worker 线程）。"""
        def _apply() -> None:
            self.last_bundle = Path(bundle)
            self._pending_diff = True
            self._refresh_sidebar()
            self.refresh_placeholder()

        if threading.get_ident() == self._main_thread:
            _apply()
            return
        try:
            self.call_from_thread(_apply)
        except Exception:  # noqa: BLE001 - app already shutting down
            pass

    # ------------------------------------------------------------------
    # TUIC2：diff 补丁块（只展示，不提供合入、不调 apply_bundle）
    # ------------------------------------------------------------------
    def diff_color_enabled(self) -> bool:
        if diff_view.no_color_requested():
            return False
        try:
            return self.console.color_system is not None
        except Exception:  # noqa: BLE001 - 取不到就按无色
            return False

    def _append_diff_block(self) -> bool:
        patch = diff_view.read_patch(self.last_bundle)
        if patch is None or not patch.strip():
            return False
        self._append_record("diff", patch, "plain")
        return True

    def flush_pending_diff(self) -> None:
        """运行/回复落定后调用：本轮落过结果包就追加一个 diff 块。"""
        if not self._pending_diff:
            return
        self._pending_diff = False
        self._append_diff_block()

    def _cmd_diff(self) -> None:
        if not self._append_diff_block():
            self._log(diff_view.EMPTY_TEXT, tone="muted")

    # ------------------------------------------------------------------
    # TUIC2：消息跳转
    # ------------------------------------------------------------------
    def open_timeline(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if not isinstance(self.screen, SessionScreen):
            # 首页等没有消息流的页面不开弹窗，避免选中后无处可跳。
            return
        self.push_screen(TimelineModal(self), self._timeline_picked)

    def _timeline_picked(self, seq: int | None) -> None:
        if seq is not None:
            self._jumped_seq = seq
            self.jump_to_record(seq)

    def _thought_block_target(self) -> tuple[Any, str]:
        """BLK1 B2：按块切换的目标思考块（落点口径见 _landing_target）。"""
        return self._landing_target(
            lambda r: r[0] == "event" and _is_thinking_line(r[1]),
            "当前没有可展开的思考块",
            "跳转落点之后没有思考块",
        )

    def _landing_target(
        self, is_target: Callable[[Any], bool], empty: str, after: str
    ) -> tuple[Any, str]:
        """BLK1/BLK2 共用的落点口径：落点 → 顺延 → 最近；失效退回最近。"""
        targets = [record for record in self.history if is_target(record)]
        if not targets:
            return None, empty
        seq = self._jumped_seq
        if seq is not None:
            landing = next(
                (i for i, r in enumerate(self.history) if r.seq == seq), None
            )
            if landing is not None:
                for record in self.history[landing:]:
                    if is_target(record):
                        return record, ""
                return None, after
        return targets[-1], ""

    def toggle_tool_block(self) -> bool:
        """BLK2：切换落点工具块的按块展开态；返回是否需要重排。"""
        record, note = self._landing_target(
            lambda r: r[0] == "tool",
            "当前没有可展开的工具块",
            "跳转落点之后没有工具块",
        )
        if record is None:
            self._log(note, tone="muted")
            return False
        key = id(record)
        if key in self._tool_blocks_expanded:
            self._tool_blocks_expanded.discard(key)
        else:
            self._tool_blocks_expanded.add(key)
        return True

    def toggle_thought_block(self) -> bool:
        """切换目标思考块的按块展开态；返回是否有变化（需重排）。"""
        record, note = self._thought_block_target()
        if record is None:
            self._log(note, tone="muted")
            return False
        key = id(record)
        if key in self._thought_blocks_expanded:
            self._thought_blocks_expanded.discard(key)
        else:
            self._thought_blocks_expanded.add(key)
        return True

    def _render_tool_block(self, log: RichLog, record: Any, width: int) -> None:
        """BLK2：工具块。收起一行（名 + 参数摘要 + 状态），展开看参数与结果摘录。"""
        meta = getattr(record, "tool", None) or {}
        state = str(meta.get("state") or "unknown")
        label = _TOOL_STATE_LABELS.get(state, state)
        color = {
            "succeeded": COLOR_SUCCESS,
            "failed": COLOR_ERROR,
            "cancelled": COLOR_WARNING,
        }.get(state, COLOR_ACCENT)
        line = Text()
        line.append("⚙ ", style=color)
        body = f"{record[1]} · {label}"
        line.append(
            _truncate_cells(body, max(width - 2, 1)), style=COLOR_TEXT_MUTED
        )
        log.write(line, width=width)
        if id(record) not in self._tool_blocks_expanded:
            return
        texts = self._tool_texts.get(id(record))
        if texts is None:
            if getattr(record, "restored", False):
                log.write(
                    Text(TOOL_NOT_ARCHIVED_TEXT, style=COLOR_TEXT_MUTED),
                    width=width,
                )
            else:
                log.write(Text("  执行中…", style=COLOR_TEXT_MUTED), width=width)
            return
        args = texts.get("args")
        result = texts.get("result")
        log.write(
            Text(
                "参数：\n" + (_tool_args_pretty(args) if args else "（未收到）"),
                style=COLOR_TEXT_MUTED,
            ),
            width=width,
        )
        if result is None:
            tail = "执行中…" if state in ("pending", "running") else "（无结果）"
            log.write(Text("结果：" + tail, style=COLOR_TEXT_MUTED), width=width)
        else:
            log.write(
                Text("结果：\n" + _tool_excerpt(result), style=COLOR_TEXT_MUTED),
                width=width,
            )

    def _tool_scrub_ws(self) -> str | None:
        try:
            active = self._resolved_active_path()
            return str(Path(active).expanduser().resolve()) if active else None
        except (OSError, RuntimeError):
            return None

    def _tool_block_record(self, call_id: str) -> Any:
        """该 call_id 的工具块记录；还没有就以当前已知参数新建一条。"""
        from .session_store import scrub_text

        pending = self._tool_pending.setdefault(
            call_id, {"name": "", "args": "", "result": None, "record": None}
        )
        record = pending["record"]
        if record is not None:
            return record
        ws = self._tool_scrub_ws()
        args = scrub_text(pending["args"], ws) if pending["args"] else ""
        brief = _tool_args_brief(args) if args else ""
        text = pending["name"] or "工具"
        if brief:
            text += f"（{brief}）"
        self._append_record(
            "tool",
            text,
            "muted",
            tool={"call_id": call_id, "name": pending["name"], "state": "running"},
        )
        record = self.history[-1]
        pending["record"] = record
        self._tool_texts[id(record)] = {"args": args or None, "result": None}
        return record

    def _track_tool_block(self, event: Any) -> None:
        """BLK2：流消费层按 tool_call_id 把调用块与结果块配成一条工具记录。

        数据源只认 AgentScope 的工具调用/结果事件（秘书工具与 MCP 同流），
        不接 dispatcher 的 to_thread 包装层，也不碰 _on_platform_event。
        原文只在 _tool_pending 暂存，进块之前一律过 session_store.scrub_text。
        """
        from agentscope.event import (
            ToolCallDeltaEvent,
            ToolCallEndEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
            ToolResultStartEvent,
            ToolResultTextDeltaEvent,
        )

        from . import session_autosave
        from .session_store import scrub_text

        call_id = str(getattr(event, "tool_call_id", "") or "")
        if not call_id:
            return
        pending = self._tool_pending.setdefault(
            call_id, {"name": "", "args": "", "result": None, "record": None}
        )
        name = getattr(event, "tool_call_name", None)
        if name and not pending["name"]:
            pending["name"] = str(name)
        if isinstance(event, ToolCallStartEvent):
            return
        if isinstance(event, ToolCallDeltaEvent):
            pending["args"] += str(getattr(event, "delta", "") or "")
            return
        if isinstance(event, (ToolCallEndEvent, ToolResultStartEvent)):
            had_record = pending["record"] is not None
            record = self._tool_block_record(call_id)
            if had_record and isinstance(event, ToolCallEndEvent) and pending["args"]:
                # 结果先到、调用后到：参数补进原记录的内存摘录（折叠行已落定不改）。
                texts = self._tool_texts.setdefault(
                    id(record), {"args": None, "result": None}
                )
                texts["args"] = scrub_text(pending["args"], self._tool_scrub_ws())
                pending["args"] = ""
            return
        if isinstance(event, ToolResultTextDeltaEvent):
            pending["result"] = (pending["result"] or "") + str(
                getattr(event, "delta", "") or ""
            )
            return
        if isinstance(event, ToolResultEndEvent):
            record = self._tool_block_record(call_id)
            record.tool["state"] = session_autosave.tool_state(
                getattr(event, "state", None)
            )
            ws = self._tool_scrub_ws()
            texts = self._tool_texts.setdefault(
                id(record), {"args": None, "result": None}
            )
            if pending["result"] is not None:
                texts["result"] = scrub_text(pending["result"], ws)
            # 配对表保留到会话清空：后到的调用事件仍认这条记录，不另起一条；
            # 原文结果已脱敏入块，暂存的原文清掉。
            pending["result"] = None
            screen = self.screen
            if isinstance(screen, SessionScreen) and screen._ready:
                screen._reflow_log()

    def jump_to_record(self, seq: int) -> bool:
        """把该记录起始行滚到视图顶部附近；不改历史、不碰流式状态。"""
        line = self.record_lines.get(seq)
        if line is None:
            return False
        for screen in self.screen_stack:
            if isinstance(screen, SessionScreen):
                try:
                    log = screen.query_one("#log", RichLog)
                except Exception:  # noqa: BLE001
                    return False
                log.scroll_to(y=line, animate=False)
                return True
        return False

    def sidebar_workspace_line(self) -> str:
        active = self._resolved_active_path()
        if active is None:
            return "未绑定"
        return recents.shorten_home(active)

    def sidebar_model_lines(self) -> list[str]:
        from .model_state import SESSION_MARKER  # noqa: F401 - 同源标记

        lines = [
            f"{self._provider_label()} · {self._model_label()} · "
            f"思考 {self._effort_label()}"
        ]
        if self._model_state.override is not None:
            lines.append("（本次会话覆盖）")
        return lines

    def sidebar_usage_lines(self) -> list[str]:
        """G：与 /status 同一读取口（core.usage.summarize），不在这里重算。"""
        from .usage import summarize

        return summarize().lines()

    def sidebar_changed_lines(self) -> list[str]:
        """只读最近一轮本会话运行的 record.json → review.attempts[-1].changed_files。"""
        bundle = self.last_bundle
        if bundle is None:
            return [NO_CHANGES_TEXT]
        try:
            data = json.loads(
                (Path(bundle) / "record.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, UnicodeDecodeError):
            return [NO_CHANGES_TEXT]
        review = data.get("review") if isinstance(data, dict) else None
        attempts = review.get("attempts") if isinstance(review, dict) else None
        files: list[str] = []
        if isinstance(attempts, list) and attempts:
            last = attempts[-1]
            raw = last.get("changed_files") if isinstance(last, dict) else None
            if isinstance(raw, list):
                files = [str(item) for item in raw if str(item).strip()]
        if not files:
            return [NO_CHANGES_TEXT]
        active = self._resolved_active_path()
        if active is None:
            return [OUT_OF_WORKSPACE_TEXT]
        for name in files:
            candidate = Path(name)
            target = (
                candidate if candidate.is_absolute() else active / candidate
            ).resolve()
            if target != active and active not in target.parents:
                return [OUT_OF_WORKSPACE_TEXT]
        lines = files[:SIDEBAR_MAX_FILES]
        if len(files) > SIDEBAR_MAX_FILES:
            lines.append(f"共 {len(files)} 个文件")
        return lines

    def sidebar_text(self) -> Text:
        text = Text()
        sections = [
            ("工作区", [self.sidebar_workspace_line()]),
            ("当前模型", self.sidebar_model_lines()),
            ("本会话用量", self.sidebar_usage_lines()),
            ("最近一轮改动文件", self.sidebar_changed_lines()),
        ]
        for index, (title, lines) in enumerate(sections):
            if index:
                text.append("\n\n")
            text.append(title, style=f"bold {COLOR_TEXT_BASE}")
            for line in lines:
                text.append("\n" + line, style=COLOR_TEXT_MUTED)
        return text

    # ------------------------------------------------------------------
    # 键位动作
    # ------------------------------------------------------------------
    def action_cycle_effort(self) -> None:
        """ctrl+t：轮换当前 effective 模型的思考等级，只写会话覆盖。"""
        if isinstance(self.screen, ModalScreen):
            return
        from .model_state import (
            CUSTOM_EFFORT_NOTICE,
            CustomModelError,
            cycle_effort,
            session_effort_line,
        )

        try:
            current = self.effective_model()
        except Exception as exc:  # noqa: BLE001 - 读配置失败要看得见
            self._log(
                f"无法读取当前模型：{type(exc).__name__}: {exc}",
                tone="error",
            )
            return
        try:
            nxt = cycle_effort(
                current.provider, current.model, current.effort, step=1
            )
        except CustomModelError:
            self._event(CUSTOM_EFFORT_NOTICE, "warning")
            return
        if not self._commit_session(
            current.provider, current.model, nxt, announce=False
        ):
            return
        self._event(session_effort_line(nxt), "muted")

    def action_open_commands(self) -> None:
        if isinstance(self.screen, CommandPalette):
            return
        self.push_screen(CommandPalette(), self._command_selected)

    def _command_selected(self, command: str | None) -> None:
        if command:
            self.submit_text(command)

    def action_open_model_picker(self) -> None:
        if isinstance(self.screen, ModelPicker):
            return
        if self._injected_model is None:
            from .setup_wizard import needs_setup

            if needs_setup():
                self._log("还没配模型，先完成配置引导。", tone="warning")
                self.push_screen(SetupScreen(self))
                return
        from .model_config import load_profile_catalog

        try:
            catalog = load_profile_catalog()
        except Exception as exc:  # noqa: BLE001 - config errors are reportable
            self._log(
                f"模型配置读取失败: {type(exc).__name__}: {exc}\n"
                "请先运行 drbug check 查看配置来源与修复指引。",
                tone="error",
            )
            return
        self.push_screen(ModelPicker(self, catalog))

    def _apply_model_selection(
        self,
        provider: str,
        model: str,
        effort: str | None,
        *,
        persist: bool = True,
    ) -> None:
        """目录选型。persist=True 落盘后走切换；False 只换会话覆盖。"""
        if not persist:
            self._commit_session(provider, model, effort)
            return
        from .model_config import update_active_model

        try:
            profile = update_active_model(provider, model, effort)
        except Exception as exc:  # noqa: BLE001 - report and keep current model
            self._log(f"切换失败: {type(exc).__name__}: {exc}", tone="error")
            return
        # 位置参数调用，persist 默认 True。不要加关键字，既有测试替身只收 name。
        self._model_selected(profile.name)

    def _profile_for_triple(self, triple: Any) -> Any:
        """用当前档案的密钥环境变量拼一份内存档案。不写文件。"""
        from .model_config import ModelProfile, load_config

        base = self._active_profile
        if base is None:
            try:
                base = load_config()
            except Exception:  # noqa: BLE001 - 下面用空缺兜住
                base = None
        api_key_env = getattr(base, "api_key_env", None) or "DEEPSEEK_API_KEY"
        name = (
            getattr(base, "name", None)
            or getattr(base, "profile", None)
            or "session"
        )
        return ModelProfile(
            name=str(name),
            provider=triple.provider,
            model=triple.model,
            api_key_env=api_key_env,
            base_url=getattr(base, "base_url", None),
            reasoning_effort=triple.effort,
        )

    def _rebuild_doctor(self, profile: Any) -> bool:
        """构建新模型并换上主治医生。不写盘。失败时旧医生原样保留。"""
        from .model_config import build_model, key_is_set, user_env_path

        self._context_restart_pending = False
        if not key_is_set(profile):
            self._log(
                f"未切换：profile {profile.name!r} 的密钥环境变量 "
                f"{profile.api_key_env!r} 未设置。\n"
                f"一步到位：在 {user_env_path()} 写入一行 "
                f"{profile.api_key_env}=<你的密钥>，保存后重试。\n"
                "（不会显示密钥值；drbug check 可复核当前状态。）",
                tone="warning",
            )
            return False
        try:
            new_model = build_model(profile)
        except Exception as exc:  # noqa: BLE001 - report and keep current model
            self._log(
                f"切换失败: {type(exc).__name__}: {exc}",
                tone="error",
            )
            return False

        old_agent = self._dispatcher
        old_state = (
            getattr(old_agent, "state", None) if old_agent is not None else None
        )
        try:
            from .dispatcher import build_dispatcher

            new_agent = build_dispatcher(
                model=new_model,
                event_sink=self._on_platform_event,
                default_actors=self.actor_backend,
                workspace=self._dispatcher_workspace(),
                state=old_state,
                secretary_files=True,
                confirm_responder=_tui_confirm_responder,
                mode=self._perm_mode,
            )
        except Exception as exc:  # noqa: BLE001 - keep old dispatcher intact
            self._log(
                f"切换失败: 主治医生重建出错: {type(exc).__name__}: {exc}",
                tone="error",
            )
            return False

        self._dispatcher = new_agent
        self._active_model = new_model
        self._active_profile = profile
        self._context_restart_pending = (
            old_agent is not None and old_state is None
        )
        return True

    def _announce_context_restart(self) -> None:
        if self._context_restart_pending:
            self._event("本场对话上下文重新开始", "warning")
            self._context_restart_pending = False

    def _commit_session(
        self,
        provider: str,
        model: str,
        effort: str | None,
        *,
        announce: bool = True,
    ) -> bool:
        """只换内存覆盖 + 重建主治医生。不调用任何落盘函数。"""
        from .model_state import ModelTriple

        provider = (provider or "").strip().lower()
        model = (model or "").strip()
        if effort is not None:
            effort = effort.strip().lower() or None
        triple = ModelTriple(provider, model, effort)
        profile = self._profile_for_triple(triple)
        if not self._rebuild_doctor(profile):
            return False
        self._model_state.set_override(triple)
        self._refresh_status_lines()
        if announce:
            self._event(
                f"已切换到 {provider}/{model}（仅本次会话）",
                "success",
            )
        self._announce_context_restart()
        return True

    def _model_selected(self, name: str | None, *, persist: bool = True) -> None:
        """切换 profile。persist=True 走现有落盘；False 只换会话覆盖。

        两条路都进 :meth:`_rebuild_doctor`，不各写一份重建。
        """
        if not name:
            return
        from .model_config import activate_role_profile, resolve_profile

        try:
            profile = resolve_profile(name)
        except Exception as exc:  # noqa: BLE001 - report and keep current model
            self._log(f"切换失败: {type(exc).__name__}: {exc}", tone="error")
            return
        if not persist:
            self._commit_session(
                profile.provider, profile.model, profile.reasoning_effort
            )
            return
        if not self._rebuild_doctor(profile):
            return
        self._model_state.clear()
        try:
            activate_role_profile(profile.name)
        except Exception as exc:  # noqa: BLE001 - in-memory switch still valid
            self._event(
                f"警告：模型切换未写回用户配置（"
                f"{type(exc).__name__}: {exc}）；本次会话仍已切换。",
                "warning",
            )
        self._refresh_status_lines()
        self._event(
            f"已切换到 {profile.name}"
            f"（{profile.provider}/{profile.model}）",
            "success",
        )
        self._announce_context_restart()

    # ------------------------------------------------------------------
    # COPY1：拖选松手自动复制 / /copy 原文兜底
    # ------------------------------------------------------------------
    def copy_text_out(self, text: str) -> bool:
        """双通道写剪贴板：pbcopy 为主（本机），OSC52 为补充（远程）。

        pbcopy 写成才说「已复制」；失败给一行如实提示，不崩、不假装成功。
        """
        try:
            self.copy_to_clipboard(text)  # OSC52 + app.clipboard
        except Exception:  # noqa: BLE001 - 补充通道失败不影响主通道
            pass
        ok, reason = _write_system_clipboard(text)
        if ok:
            self.notify(COPY_DONE, timeout=2)
            return True
        self.notify(
            f"没能写进系统剪贴板（{reason}），已改用终端剪贴板通道，"
            "你的终端不支持时可能没复制上",
            severity="warning",
            timeout=4,
        )
        return False

    def on_text_selected(self, event: TextSelected) -> None:
        """拖选松手：选区非空才复制并提示，选区保留；原地单击不复制。

        框架在按下与松手同一格时已先清掉选区（screen._forward_event），
        所以单击走到这里选区为空，什么也不做。同一选区重复松手（如点滚动
        条）不重复复制。
        """
        try:
            screen = self.screen
            selections = dict(screen.selections)
        except Exception:  # noqa: BLE001 - 切页瞬间
            return
        if not selections:
            self._last_copied_selection = None
            return
        key = (id(screen), tuple(sorted((id(w), s) for w, s in selections.items())))
        if key == getattr(self, "_last_copied_selection", None):
            return
        text = screen.get_selected_text()
        if not text or not text.strip():
            return
        self._last_copied_selection = key
        self.copy_text_out(text)

    def last_reply_text(self) -> str | None:
        """上一条主治医生回复的原文：history 里 kind=assistant 的记录正文，
        即 render_record 里喂给 Markdown(text) 的同一个字符串。"""
        for record in reversed(self.history):
            kind, text, _tone = record
            if kind == "assistant" and text and text != NO_TEXT_REPLY:
                return text
        return None

    def action_copy_last_reply(self) -> None:
        text = self.last_reply_text()
        if text is None:
            self._log(COPY_EMPTY, tone="muted")
            return
        self.copy_text_out(text)

    def action_interrupt(self) -> None:
        future = self._confirm_future
        if future is not None and not future.done():
            # 在等确认答复时 esc = 拒绝，不取消整条回复。
            self._event("已按 esc 拒绝本次确认", "warning")
            future.set_result("N")
            return
        worker = self._dispatcher_worker
        if not self._agent_busy or worker is None:
            return
        # 标记中断：worker 收敛后把已到达的文本落定为该条回复。
        self._interrupt_requested = True
        if not getattr(worker, "is_finished", True):
            worker.cancel()
        self._agent_busy = False
        self._set_busy(False)
        self._log("⏹ 已按 esc 中断主治医生", tone="warning")

    # ------------------------------------------------------------------
    # 提交入口
    # ------------------------------------------------------------------
    def submit_text(self, text: str) -> None:
        value = text.strip()
        if not value:
            return
        future = self._confirm_future
        if future is not None and not future.done():
            # 在等确认答复：这次输入就是答复，不走命令与派发。
            self.remember_input(value)
            self._append_record("user", value)
            future.set_result(value)
            return
        self.remember_input(value)
        if value.lower() in {"/quit", "/exit"}:
            self.exit()
            return
        if self.page == "home":
            self.page = "session"
            self._append_record("user", value)
            self.switch_screen(SessionScreen(self))
        else:
            self._append_record("user", value)
        if not value.startswith("/"):
            self._send_to_dispatcher(value)
            return
        if _is_known_command(value):
            self._handle_slash(value)
            return
        # 粘贴的绝对/相对路径不是命令：原样交给调度员，不截断、不改大小写。
        if _is_path_like_token(_first_token(value)):
            self._send_to_dispatcher(value)
            return
        # 其余 /单词 输入仍按未知命令提示，帮用户纠正错别字。
        self._handle_slash(value)

    # ------------------------------------------------------------------
    # 输入框：草稿暂存（TUID1）
    # ------------------------------------------------------------------
    def _capture_draft(self, prompt: "PromptInput") -> prompt_extras.StashedDraft:
        text = prompt.text
        row, column = prompt.cursor_location
        return prompt_extras.StashedDraft(
            text=text,
            attachments=tuple(_attachment_tokens(text)),
            cursor=(int(row), int(column)),
        )

    def toggle_draft_stash(self, prompt: "PromptInput") -> None:
        """切换式：输入非空 → 暂存并清空；空且有暂存 → 取回；空且无 → 提示。

        「空」指去掉空白后为空。取回只在输入为空时发生，不存在覆盖。
        """
        if prompt.text.strip():
            try:
                draft = self._capture_draft(prompt)
                self.draft_stash = draft
            except Exception as exc:  # noqa: BLE001 - 暂存失败绝不清输入
                self._log(f"草稿暂存失败，输入已保留：{exc}", tone="error")
                return
            prompt.reset_history_pointer()
            prompt._apply_text("")
            self._log("草稿已暂存", tone="muted")
            return
        draft = self.draft_stash
        if draft is None:
            self._log("没有暂存的草稿", tone="muted")
            return
        self.draft_stash = None
        text = draft.text
        dropped: list[str] = []
        for token in draft.attachments:
            if _attachment_exists(token):
                continue
            dropped.append(token)
            text = text.replace(token, "", 1)
        prompt.reset_history_pointer()
        prompt.text = text
        rows = text.split("\n")
        row = min(draft.cursor[0], len(rows) - 1)
        column = min(draft.cursor[1], len(rows[row]))
        prompt.cursor_location = (row, column)
        prompt._sync_height()
        line = "草稿已取回"
        if dropped:
            line += "（" + "；".join(
                f"附件 {token} 已不存在，已剔除" for token in dropped
            ) + "）"
        self._log(line, tone="warning" if dropped else "muted")

    # ------------------------------------------------------------------
    # 输入框：@ 提及文件（TUID2）
    # ------------------------------------------------------------------
    def mention_candidates(self, query: str) -> list[str] | None:
        """@ 补全候选：工作区内相对路径。只发现、不读内容；未绑定返回 None。"""
        workspace = self._resolved_active_path()
        if workspace is None:
            return None
        try:
            return file_mention.discover_files(workspace, query)
        except OSError:
            return []

    def select_mention(self, prompt: "PromptInput", rel: str) -> bool:
        """选中：过门禁读取；成功才插入提及标记，失败明确报错、不插入。"""
        workspace = self._resolved_active_path()
        if workspace is None:
            self._log(file_mention.UNBOUND_TEXT, tone="error")
            return False
        try:
            attachment = file_mention.read_attachment(workspace, rel)
        except file_mention.MentionError as exc:
            self._log(str(exc), tone="error", exc=exc)
            return False
        previous = self._mention_attachment
        self._mention_attachment = attachment
        prompt._apply_text(file_mention.replace_mention_tail(prompt.text, attachment.rel))
        line = f"已选附件：{attachment.rel}（{file_mention.format_size(attachment.size)}），发送时附上"
        if previous is not None and previous.rel != attachment.rel:
            line += f"；一条消息只附一个文件，已替换 {previous.rel}"
        self._log(line, tone="muted")
        return True

    def _take_mention_attachment(
        self, text: str
    ) -> tuple[bool, file_mention.FileAttachment | None]:
        """发送前复验。返回 (可以发送, 附件)。复验不过：报错、不发送。"""
        attachment = self._mention_attachment
        self._mention_attachment = None
        if attachment is None or attachment.marker not in text:
            return True, None
        workspace = self._resolved_active_path()
        try:
            if workspace is None:
                raise file_mention.MentionError(
                    f"附件 {attachment.rel} 所在工作区已解绑，未发送"
                )
            file_mention.recheck_attachment(workspace, attachment)
        except file_mention.MentionError as exc:
            self._log(str(exc), tone="error", exc=exc)
            return False, None
        return True, attachment

    # ------------------------------------------------------------------
    # 输入框：粘贴折叠（TUID1）
    # ------------------------------------------------------------------
    def handle_paste(self, prompt: "PromptInput", text: str) -> bool:
        """返回 True 表示已处理（折叠或拒绝），TextArea 不再插入。"""
        if prompt_extras.paste_too_big(text):
            size_kb = len(text.encode("utf-8")) / 1024
            self._log(
                f"粘贴内容 {size_kb:.1f} KB 超过单块上限 100 KB，"
                "已拒绝：未折叠、未粘贴、未截断",
                tone="error",
            )
            return True
        pasted_paths = _pasted_file_paths(text)
        if pasted_paths:
            # PST1：用户亲手粘贴的图片/文件变附件块，输入框只见编号。
            placeholders = []
            for path in pasted_paths:
                kind = (
                    "Image" if path.suffix.lower() in IMAGE_SUFFIXES else "File"
                )
                attachment = self.paste_blocks.add_attachment(kind, str(path))
                placeholders.append(attachment.placeholder)
            start, end = prompt.selection
            result = prompt.replace(" ".join(placeholders), start, end)
            prompt.move_cursor(result.end_location)
            return True
        if not prompt_extras.paste_should_fold(text):
            return False
        block = self.paste_blocks.add(text)
        start, end = prompt.selection
        result = prompt.replace(block.placeholder, start, end)
        prompt.move_cursor(result.end_location)
        return True

    def expand_pastes_for_send(self, text: str) -> str:
        """提交前展开占位符（改坏的不展开），并清理不再被引用的块。"""
        expanded = self.paste_blocks.expand(text)
        keep = [self.draft_stash.text] if self.draft_stash is not None else []
        self.paste_blocks.prune(keep)
        return expanded

    def open_paste_view(self, text: str) -> None:
        blocks = self.paste_blocks.referenced(text)
        if not blocks:
            self._log("当前输入里没有粘贴块", tone="muted")
            return
        self.push_screen(PasteViewModal(blocks))

    def remember_input(self, text: str) -> None:
        """记一条已发送消息；连续重复只存一条。"""
        if not self.input_history or self.input_history[-1] != text:
            self.input_history.append(text)

    # ------------------------------------------------------------------
    # 会话标题栏
    # ------------------------------------------------------------------
    def session_title(self) -> str:
        """首条用户消息截断到 28 字；没有消息时叫「新会话」。"""
        for kind, text, _ in self.history:
            if kind != "user":
                continue
            compact = " ".join(str(text).split())
            if len(compact) > TITLE_LIMIT:
                return compact[:TITLE_LIMIT] + "…"
            return compact or "新会话"
        return "新会话"

    def message_count(self) -> int:
        """user + assistant 记录数（事件与命令输出不计）。"""
        return sum(
            1 for kind, _, _ in self.history if kind in ("user", "assistant")
        )

    def _refresh_header(self) -> None:
        if not isinstance(self.screen, SessionScreen):
            return
        try:
            self.screen.query_one("#session-header", SessionHeader).refresh()
        except Exception:  # noqa: BLE001 - on_mount 尚未挂载
            pass

    # ------------------------------------------------------------------
    # 消息渲染
    # ------------------------------------------------------------------
    def _snapshot_actual(self) -> tuple[str, str, str | None] | None:
        """调用开始时的 effective。读 core 解析，不读状态行。"""
        try:
            return self.effective_model().as_tuple()
        except Exception:  # noqa: BLE001 - 读不出就不编造
            return None

    def _append_record(
        self,
        kind: str,
        text: str,
        tone: str = "plain",
        actual: tuple[str, str, str | None] | None = None,
        tool: dict | None = None,
    ) -> None:
        record = HistoryRecord(kind, text, tone, actual=actual)
        if tool is not None:
            record.tool = tool
        record.seq = self._next_seq
        record.ts = time.time()
        self._next_seq += 1
        self.history.append(record)
        self._refresh_header()
        screen = self.screen
        if isinstance(screen, SessionScreen) and screen._ready:
            try:
                log = screen.query_one("#log", RichLog)
            except Exception:  # noqa: BLE001 - on_mount 尚未挂载
                return
            self.render_record(log, record)

    def _append_threadsafe(self, kind: str, text: str, tone: str) -> None:
        """Append a record from the app thread or a worker thread."""
        if threading.get_ident() == self._main_thread:
            self._append_record(kind, text, tone)
            return
        try:
            self.call_from_thread(self._append_record, kind, text, tone)
        except Exception:  # noqa: BLE001 - app already shutting down
            pass

    def _log(
        self,
        text: str,
        tone: str = "plain",
        *,
        exc: BaseException | None = None,
        source: str | None = None,
    ) -> None:
        if tone == "error":
            # 没显式传异常时取「正在处理的异常」（在 except 块里调用才有）；
            # 都没有就记「无异常」，不编堆栈。历史区仍只这一行。
            if exc is None:
                exc = sys.exc_info()[1]
            self._record_error(text, exc, source or self._error_source)
        self._append_threadsafe("plain", text, tone)

    def _record_error(
        self, text: str, exc: BaseException | None, source: str | None
    ) -> None:
        roots = [self.workspace_root]
        active = self._resolved_active_path()
        if active is not None:
            roots.insert(0, active)
        with self._error_lock:
            self.error_log.record(str(text), exc=exc, source=source, roots=roots)

    def _event(self, text: str, tone: str = "muted") -> None:
        self._append_threadsafe("event", text, tone)

    def _spinner_text(self, index: int) -> Text:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        line = Text()
        if self._awaiting_confirm:
            line.append("在等你答复", style=COLOR_WARNING)
            line.append(
                "：在输入框里打 Y 放行、N 拒绝，然后回车",
                style=COLOR_TEXT_BASE,
            )
            return line
        line.append(frames[index % len(frames)] + " ", style=COLOR_WARNING)
        line.append("主治医生思考中…", style=COLOR_TEXT_BASE)
        if self._reply_started is not None:
            elapsed = max(0.0, time.monotonic() - self._reply_started)
            line.append(f" {elapsed:.1f}s", style=COLOR_TEXT_MUTED)
        line.append("  esc 中断", style=COLOR_TEXT_MUTED)
        return line

    def _tick_spinner(self) -> None:
        if not self._agent_busy or not isinstance(
            self.screen, SessionScreen
        ):
            return
        self._spinner_index = (self._spinner_index + 1) % 10
        try:
            busy = self.screen.query_one("#busy", Static)
            busy.update(self._spinner_text(self._spinner_index))
        except Exception:  # noqa: BLE001 - 切页瞬间
            pass

    def _set_busy_widget(self, busy: bool) -> None:
        """只切 #busy 指示器的显示，不动 ``_agent_busy``。"""
        if not isinstance(self.screen, SessionScreen):
            return
        try:
            widget = self.screen.query_one("#busy", Static)
        except Exception:  # noqa: BLE001 - 切页瞬间
            return
        if busy:
            widget.update(self._spinner_text(self._spinner_index))
            widget.styles.display = "block"
        else:
            widget.styles.display = "none"

    def _set_busy(self, busy: bool) -> None:
        self._agent_busy = busy
        self._set_busy_widget(busy)

    # ------------------------------------------------------------------
    # 流式增量显示（纯文本；落定后走 render_record 的 Markdown 路径）
    # ------------------------------------------------------------------
    def _show_stream(self, content: Text) -> None:
        if not isinstance(self.screen, SessionScreen):
            return
        try:
            self.screen.query_one("#stream-text", Static).update(content)
            container = self.screen.query_one("#stream", VerticalScroll)
            container.styles.display = "block"
            container.scroll_end(animate=False)
        except Exception:  # noqa: BLE001 - 切页瞬间
            pass
        # 已有可见增量：收起忙碌指示，把流式区留给正在到达的内容。
        self._set_busy_widget(False)

    def _render_thinking(self, text: str) -> None:
        """流式区弱化显示思考文本。落定后由折叠行取代，不写入正文记录。"""
        content = Text()
        content.append("思考中", style=COLOR_TEXT_MUTED)
        content.append("\n")
        content.append(text, style=COLOR_TEXT_MUTED)
        self._show_stream(content)

    def _render_stream(self, text: str) -> None:
        """把当前累积正文画进 #stream（纯文本，避免 Markdown 半成品抖动）。"""
        self.stream_text = text
        content = Text()
        content.append("主治医生:", style=f"bold {COLOR_ACCENT}")
        content.append("\n")
        content.append(text, style=COLOR_TEXT_BASE)
        self._show_stream(content)

    def _hide_stream_view(self) -> None:
        self.stream_text = ""
        if not isinstance(self.screen, SessionScreen):
            return
        try:
            self.screen.query_one("#stream", VerticalScroll).styles.display = "none"
        except Exception:  # noqa: BLE001 - 切页瞬间
            pass

    def _render_user(self, text: str, width: int) -> Text:
        """用户消息块：左侧 ``┃`` 竖条 + 铺满整个日志内容宽度的专属背景。"""
        available = max(width, 1)
        inner = max(available - 2, 1)
        rows: list[str] = []
        for line in text.splitlines() or [""]:
            rows.extend(_wrap_cells(line, inner))
        rendered = Text()
        for index, row in enumerate(rows):
            # 「┃ + 空格 + 正文」占 2 列；补足空格让背景一直铺到行尾。
            padding = max(0, available - 2 - cell_len(row))
            rendered.append("┃", style=f"bold {COLOR_ACCENT}")
            rendered.append(
                " " + row + " " * padding,
                style=f"{COLOR_TEXT_BASE} on {COLOR_BACKGROUND_USER}",
            )
            if index < len(rows) - 1:
                rendered.append("\n")
        return rendered

    @staticmethod
    def _record_width(log: RichLog) -> int:
        """日志内容宽度；未挂载或测试替身没有尺寸时退回 80。"""
        size = getattr(log, "size", None)
        return getattr(size, "width", 0) or 80

    def _is_session_first(self, record: HistoryRecord) -> bool:
        """是否会话首条记录：首条不写前置空行，live 与重放共用同一判定。"""
        return bool(self.history) and record is self.history[0]

    def render_record(
        self, log: RichLog, record: HistoryRecord
    ) -> None:
        kind, text, tone = record
        # 显式给 RichLog 传宽度：它默认按 app.console 的 80 列测量，宽屏会被
        # 提前折行、窄屏会溢出；user/assistant/plain 一律按日志内容宽度渲染。
        # 带宽写入的行会钉死在写入时宽度——窗口尺寸变化后由会话页重放
        # 历史按新宽度重排（见 SessionScreen.on_resize）。
        width = self._record_width(log)
        # 记录间距：user / assistant / plain 前留一行空行（会话首条除外）；
        # event 是连续状态流，不加前置空行。
        if kind not in ("event", "tool") and not self._is_session_first(record):
            log.write(Text(""), width=width)
        seq = getattr(record, "seq", None)
        lines = getattr(log, "lines", None)
        if seq is not None and lines is not None:
            # 起始行 = 写入本条第一行之前日志的行数（前置空行之后）。
            # 只对真实 RichLog 记；测试替身等没有行缓冲的目标跳过。
            self.record_lines[seq] = len(lines)
        if kind == "diff":
            # 纯文本原样渲染（不走 Markdown），着色只用 Text 样式。
            log.write(
                diff_view.render(text, color=self.diff_color_enabled()),
                width=width,
            )
            return
        if kind == "user":
            # 用户消息必须整行全宽：竖条 + 铺满行宽的背景。
            log.write(self._render_user(text, width), width=width)
            return
        if kind == "assistant":
            # 「调度员」标签独立首行；正文仍原样交给 Markdown 渲染
            # （只解释 ** / 列表等语法，不增删正文文字）。
            label = Text()
            label.append("主治医生", style=f"bold {COLOR_ACCENT}")
            label.append(":", style=COLOR_TEXT_MUTED)
            log.write(label, width=width)
            log.write(Markdown(text), width=width)
            return
        if kind == "tool":
            self._render_tool_block(log, record, width)
            return
        if kind == "event":
            colors = {
                "success": COLOR_SUCCESS,
                "error": COLOR_ERROR,
                "warning": COLOR_WARNING,
                "accent": COLOR_ACCENT,
                "muted": COLOR_TEXT_MUTED,
            }
            line = Text()
            line.append(
                "● ", style=colors.get(tone, COLOR_TEXT_MUTED)
            )
            line.append(text, style=COLOR_TEXT_MUTED)
            full_text = self._thought_texts.get(id(record))
            # BLK1：实际展开 = 全局开关 或 该块在按块集合里。
            expanded = self.thoughts_expanded or (
                id(record) in self._thought_blocks_expanded
            )
            if not expanded and full_text and _is_thinking_line(text):
                # 收起态：折叠行后追加首行摘要，按行宽截断（无全文不编造）。
                summary = _thought_summary(full_text)
                room = width - cell_len(line.plain) - 3
                if summary and room >= 2:
                    line.append(
                        " · " + _truncate_cells(summary, room),
                        style=COLOR_TEXT_MUTED,
                    )
            log.write(line, width=width)
            if expanded:
                full = full_text
                if full:
                    # 展开：该轮思考全文原样（不截断、不改写），弱化显示。
                    log.write(Text(full, style=COLOR_TEXT_MUTED), width=width)
                elif getattr(record, "restored", False) and _is_thinking_line(text):
                    # F1：思考全文不进存档，恢复后只剩折叠行，如实说明。
                    log.write(
                        Text(THOUGHT_NOT_ARCHIVED_TEXT, style=COLOR_TEXT_MUTED),
                        width=width,
                    )
            return
        style = {
            "success": COLOR_SUCCESS,
            "error": COLOR_ERROR,
            "warning": COLOR_WARNING,
            "muted": COLOR_TEXT_MUTED,
        }.get(tone, COLOR_TEXT_BASE)
        # plain 保持纯文本：/help 等文案含命令与符号，走 Markdown 会被误解析。
        log.write(Text(text, style=style), width=width)

    # ------------------------------------------------------------------
    # 斜杠命令
    # ------------------------------------------------------------------
    def _handle_slash(self, text: str) -> None:
        command = text.split()[0].lower() if text.split() else ""
        previous = self._error_source
        self._error_source = command or None
        try:
            self._handle_slash_inner(text)
        finally:
            self._error_source = previous

    def _handle_slash_inner(self, text: str) -> None:
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        if command in {"/quit", "/exit"}:
            self.exit()
            return
        if command == "/help":
            # 同 /model：从 Home 首次提交时先让会话页切完再推弹窗。
            self.call_after_refresh(self.open_help_view)
            return
        if command == "/model":
            # 从 Home 首次提交时同一 tick 还要切会话页；把浮层推到刷新后，
            # 确保 dismiss 回调绑定在仍存活的输入框消息泵上。
            self.call_after_refresh(self.action_open_model_picker)
            return
        if command == "/check":
            self._start_local_worker("体检", self._run_check)
            return
        if command == "/accept":
            if args:
                target = args[0]
            elif self._active_workspace is None:
                self._log(
                    f"/accept 未执行。{STANDBY_COMMAND_HINT}", tone="error"
                )
                return
            else:
                target = str(self._resolved_active_path())
            self._start_local_worker(
                "检查",
                lambda: self._run_accept(target),
            )
            return
        if command == "/profile":
            target, name, saw_target = self._parse_profile_args(args)
            if self._active_workspace is None and not saw_target:
                self._log(
                    f"/profile 未执行。{STANDBY_COMMAND_HINT}", tone="error"
                )
                return
            self._start_local_worker(
                "自动建档",
                lambda: self._run_profile(target, name),
            )
            return
        if command == "/repro":
            if args:
                target = self._workspace_arg(args[0])
                symptom = " ".join(args[1:]) if len(args) > 1 else ""
            elif self._active_workspace is None:
                self._log(
                    f"/repro 未执行。{STANDBY_COMMAND_HINT}", tone="error"
                )
                return
            else:
                target = str(self._resolved_active_path())
                symptom = ""
            self._start_local_worker(
                "复现分诊",
                lambda: self._run_repro(target, symptom),
            )
            return
        if command == "/run":
            self._handle_run(args)
            return
        if command == "/cd":
            self._cmd_cd(text)
            return
        if command == "/workspace":
            self._cmd_workspace(text)
            return
        if command == "/team":
            self._cmd_team(text)
            return
        if command == "/brief":
            self._cmd_brief(text)
            return
        if command == "/permissions":
            self._cmd_permissions(text)
            return
        if command == "/mode":
            self._cmd_mode(text)
            return
        if command == "/pending":
            self._cmd_pending(text)
            return
        if command == "/runs":
            self._cmd_runs(text)
            return
        if command == "/diff":
            self._cmd_diff()
            return
        if command == "/copy":
            self.action_copy_last_reply()
            return
        if command == "/sessions":
            self._cmd_sessions(text)
            return
        if command == "/save":
            self._cmd_save()
            return
        if command == "/market":
            self._cmd_market(text)
            return
        if command == "/status":
            # 同 /model：从 Home 首次提交时先让会话页切完再推弹窗。
            self.call_after_refresh(self.open_status_view)
            return
        if command == "/mcp":
            self.call_after_refresh(self.open_mcp_view)
            return
        self._log(
            f"未知命令: {command}。输入 /help 查看命令清单。",
            tone="warning",
        )

    # ------------------------------------------------------------------
    # /status /mcp 弹窗（TUIE1）：打开时现场取数，不缓存
    # ------------------------------------------------------------------
    def _status_model_row(self) -> "status_view.StatusRow":
        from .model_state import effort_label

        shown = self._status_triple()
        if self._model_state.override is not None:
            layer = "会话覆盖"
        elif self._active_profile is not None:
            layer = "已切换档案"
        elif shown is not None:
            layer = "默认"
        else:
            layer = None
        if shown is None and self._injected_model is not None:
            return status_view.model_row(
                provider=str(getattr(self._injected_model, "provider", "") or ""),
                model=str(getattr(self._injected_model, "model", None) or "测试替身"),
                effort_label="默认",
                layer="测试替身（注入模型）",
            )
        last_actual = None
        for record in reversed(self.history):
            actual = getattr(record, "actual", None)
            if actual is not None:
                last_actual = actual
                break
        return status_view.model_row(
            provider=shown.provider if shown is not None else None,
            model=shown.model if shown is not None else None,
            effort_label=effort_label(shown.effort) if shown is not None else "默认",
            layer=layer,
            last_actual=last_actual,
        )

    def _status_key_row(self) -> "status_view.StatusRow":
        from .model_config import key_is_set, load_config

        profile = self._active_profile
        if profile is None:
            try:
                profile = load_config()
            except Exception as exc:  # noqa: BLE001 - 报错不含密钥值
                return status_view.key_row(
                    key_env=None, key_present=False, error=str(exc)
                )
        key_env = getattr(profile, "api_key_env", None)
        return status_view.key_row(
            key_env=key_env,
            key_present=bool(key_env) and key_is_set(profile),
        )

    def collect_status_rows(self) -> list["status_view.StatusRow"]:
        mcp_rows, mcp_error = status_view.mcp_server_rows()
        return [
            status_view.version_row(),
            self._status_model_row(),
            self._status_key_row(),
            status_view.workspace_row(
                self._active_workspace, self._session_trusted
            ),
            status_view.mode_row(self._perm_mode, MODE_LABELS),
            status_view.mcp_summary_row(mcp_rows, mcp_error),
            status_view.market_row(),
            status_view.usage_row(),
        ]

    def open_status_view(self) -> None:
        text = status_view.format_rows(self.collect_status_rows())
        self.push_screen(
            InfoViewModal(
                screen_id=keybinds.SCREEN_STATUS_VIEW,
                close_id="status_view_close",
                title="状态总览",
                body=text,
            )
        )

    def open_help_view(self) -> None:
        self.push_screen(
            InfoViewModal(
                screen_id=keybinds.SCREEN_HELP_VIEW,
                close_id="help_view_close",
                title="帮助",
                body=help_view_text(),
            )
        )

    def open_error_view(self) -> None:
        """展开最近一条错误。无记录时只落一行提示（不记入错误表）。"""
        record = self.error_log.latest()
        if record is None:
            self._log("暂无可展开的错误", tone="muted")
            return
        self.push_screen(
            InfoViewModal(
                screen_id=keybinds.SCREEN_ERROR_VIEW,
                close_id="error_view_close",
                title="错误详情（最近一条）",
                body=error_log.format_record(record),
            )
        )

    def open_mcp_view(self) -> None:
        rows, error = status_view.mcp_server_rows()
        self.push_screen(
            InfoViewModal(
                screen_id=keybinds.SCREEN_MCP_VIEW,
                close_id="mcp_view_close",
                title="外接工具（MCP）",
                body=status_view.mcp_modal_text(rows, error),
            )
        )

    def _model_config_text(self) -> str:
        try:
            from core.cli import _model_config_lines

            return "\n".join(_model_config_lines())
        except Exception as exc:  # noqa: BLE001 - display only
            return f"模型配置读取失败: {type(exc).__name__}: {exc}"

    def _classify_startup_workspace(self) -> Path | str | None:
        """启动绑定：项目目录绑定自身；否则待命（不默认任何示例）。"""
        root = self.workspace_root
        if startup_directory_is_project(root):
            return root
        return None

    def _resolved_active_path(self) -> Path | None:
        active = self._active_workspace
        if active is None:
            return None
        return Path(active).resolve()

    def _workspace_status_label(self) -> str:
        if self._active_workspace is None:
            return "未绑定"
        return str(self._active_workspace)

    def _relative_base(self) -> Path:
        """/cd 与 /workspace 的相对路径基准：当前绑定目录，否则进程 cwd。"""
        active = self._resolved_active_path()
        if active is None:
            return Path.cwd().resolve()
        return active

    def _resolve_session_path(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self._relative_base() / path
        return path.resolve()

    def _dispatcher_workspace(self) -> Path | str | Any:
        """交给 build_dispatcher 的工作区。待命态是哨兵，不是 demo_repo。"""
        from .dispatcher import UNBOUND_WORKSPACE

        if self._active_workspace is None:
            return UNBOUND_WORKSPACE
        return self._active_workspace

    def _apply_process_workspace_binding(self) -> None:
        from .dispatcher import _bind_default_workspace

        _bind_default_workspace(self._dispatcher_workspace())

    def _rebind_dispatcher(self, restored_state: Any = None) -> bool:
        """按当前活跃工作区重建秘书并迁移会话状态。尚无秘书时只更新绑定。

        重建发生在应用自己的事件循环上（与 /model 相同，不另开循环）。
        失败时恢复进程级绑定，调用方负责恢复 App 上的活跃目录与纳管清单。
        """
        from . import dispatcher as dispatcher_module

        saved = dispatcher_module._BOUND_WORKSPACE
        target = self._dispatcher_workspace()
        if self._dispatcher is None:
            dispatcher_module._bind_default_workspace(target)
            if restored_state is not None:
                # F1：还没有秘书，首次创建时再用恢复的对话状态。
                self._restored_agent_state = restored_state
            return True
        old_agent = self._dispatcher
        old_state = (
            restored_state
            if restored_state is not None
            else getattr(old_agent, "state", None)
        )
        model = (
            self._active_model
            if self._active_model is not None
            else self._injected_model
        )
        if model is None:
            model = getattr(old_agent, "model", None)
        try:
            new_agent = dispatcher_module.build_dispatcher(
                model=model,
                event_sink=self._on_platform_event,
                default_actors=self.actor_backend,
                workspace=target,
                state=old_state,
                secretary_files=True,
                confirm_responder=_tui_confirm_responder,
                mode=self._perm_mode,
            )
        except Exception as exc:  # noqa: BLE001 - keep the previous binding
            dispatcher_module._bind_default_workspace(saved)
            self._log(
                f"切换工作区失败: {type(exc).__name__}: {exc}。"
                "工作区绑定保持不变。",
                tone="error",
            )
            return False
        self._dispatcher = new_agent
        if old_state is None:
            self._event("本场对话上下文重新开始", "warning")
        return True

    def _remember_managed(self, path: Path) -> None:
        if path not in self._managed_workspaces:
            self._managed_workspaces.append(path)

    # ------------------------------------------------------------------
    # 首启闸门：配置引导 + 工作区信任（仅真实启动路径开启）
    # ------------------------------------------------------------------
    def _run_startup_gates(self) -> None:
        if self._injected_model is None:
            from .setup_wizard import needs_setup

            if needs_setup():
                self.push_screen(SetupScreen(self), self._setup_gate_done)
                return
        self._trust_gate()

    def _setup_gate_done(self, _result: Any = None) -> None:
        from .setup_wizard import needs_setup

        if needs_setup():
            # 引导没配成又被关掉：没有可用的大脑，退出好过逐条报错。
            self.exit()
            return
        self._trust_gate()

    def _trust_gate(self) -> None:
        candidate = self._resolved_active_path()
        if candidate is None:
            self._standby_notice()
            return
        if str(candidate) in self._session_trusted:
            return
        self.push_screen(
            WorkspaceTrustScreen(self, candidate),
            self._trust_gate_done,
        )

    def _standby_notice(self) -> None:
        """待命也要说清楚：在非项目目录打开时明示下一步怎么走。"""
        self._log(
            "未绑定工作区：当前目录不是项目目录，主治医生先待命。"
            "到项目目录下打开，或输入 /cd <项目路径> 切换"
            "（会先问你是否信任）。",
            tone="warning",
        )
        self._refresh_home_notice()

    def _refresh_home_notice(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, HomeScreen):
                screen.refresh_notice()
                return

    def _trust_gate_done(self, ok: bool | None) -> None:
        if not ok:
            self.exit()
            return
        candidate = self._resolved_active_path()
        if candidate is not None:
            self._session_trusted.add(str(candidate))
            self._record_recent(candidate)

    def _cmd_cd(self, text: str) -> None:
        tokens = _message_tokens(text)
        if len(tokens) != 2 or not tokens[1].strip():
            self._log(CD_USAGE, tone="error")
            return
        raw = tokens[1]
        try:
            resolved = self._resolve_session_path(raw)
        except (OSError, RuntimeError, ValueError) as exc:
            self._log(
                f"/cd 失败: 无法解析路径 {raw!r}（{type(exc).__name__}: {exc}）。"
                "工作区绑定保持不变。",
                tone="error",
            )
            return
        if not resolved.exists():
            self._log(
                f"/cd 失败: 路径不存在: {resolved}。工作区绑定保持不变。",
                tone="error",
            )
            return
        if not resolved.is_dir():
            self._log(
                f"/cd 失败: 不是目录: {resolved}。工作区绑定保持不变。",
                tone="error",
            )
            return
        previous_active = self._active_workspace
        previous_managed = list(self._managed_workspaces)
        if self._interactive_gates:
            if str(resolved) not in self._session_trusted:
                self.push_screen(
                    WorkspaceTrustScreen(
                        self, resolved, cancel_label="不用，取消切换"
                    ),
                    lambda ok: self._cd_trust_decided(
                        ok, resolved, previous_active, previous_managed
                    ),
                )
                return
            # 本次会话里已经点过同意：同样算经信任问答绑定，记表。
            self._finish_cd(
                resolved, previous_active, previous_managed, record=True
            )
            return
        self._finish_cd(resolved, previous_active, previous_managed)

    def _cd_trust_decided(
        self,
        ok: bool | None,
        resolved: Path,
        previous_active: Any,
        previous_managed: list,
    ) -> None:
        if not ok:
            self._log("已取消切换：未同意把该目录作为工作区。", tone="warning")
            return
        self._session_trusted.add(str(resolved))
        self._finish_cd(resolved, previous_active, previous_managed, record=True)

    def _finish_cd(
        self,
        resolved: Path,
        previous_active: Any,
        previous_managed: list,
        *,
        record: bool = False,
    ) -> bool:
        self._active_workspace = resolved
        self._remember_managed(resolved)
        if not self._rebind_dispatcher():
            self._active_workspace = previous_active
            self._managed_workspaces = previous_managed
            return False
        self._refresh_status_lines()
        self._event(f"已切换工作区：{resolved}", "success")
        if record:
            # 只在信任问答点了同意、且绑定落地之后记一次。
            self._record_recent(resolved)
        return True

    # ------------------------------------------------------------------
    # TUIB1：最近工作区（访问历史，不是信任记录）
    # ------------------------------------------------------------------
    def load_recents(self) -> list["recents.RecentEntry"]:
        """首页挂载时调用：校验并清理失效条目；写不动就只读不清。"""
        try:
            return recents.prune(self.recents_file)
        except OSError:
            return [
                entry
                for entry in recents.load(self.recents_file)
                if Path(entry.path).is_dir()
            ]

    def _record_recent(self, path: Path) -> None:
        try:
            recents.record(path, file=self.recents_file)
        except OSError as exc:
            self._log(
                f"最近工作区记录写入失败（{type(exc).__name__}），不影响本次绑定。",
                tone="warning",
            )

    def enter_recent(self, raw: str) -> None:
        """首页列表进入：走 /cd 同一条路，且**总是**先问信任。

        「最近进过」不是信任：不看 recents，也不因本次会话同意过就跳过。
        """
        resolved = Path(raw).expanduser().resolve()
        if not resolved.is_dir():
            # 挂载后才被删掉的目录：静默清掉并刷新列表。
            self._refresh_home_recents()
            return
        previous_active = self._active_workspace
        previous_managed = list(self._managed_workspaces)
        self.push_screen(
            WorkspaceTrustScreen(self, resolved, cancel_label="不用，取消切换"),
            lambda ok: self._recent_trust_decided(
                ok, resolved, previous_active, previous_managed
            ),
        )

    def _recent_trust_decided(
        self,
        ok: bool | None,
        resolved: Path,
        previous_active: Any,
        previous_managed: list,
    ) -> None:
        if not ok:
            # 拒绝：留在首页，列表与输入框原样不动；不记表、不清表。
            return
        self._session_trusted.add(str(resolved))
        if not self._finish_cd(
            resolved, previous_active, previous_managed, record=True
        ):
            return
        self._refresh_home_notice()
        self._refresh_home_recents()

    def _refresh_home_recents(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, HomeScreen):
                screen.refresh_recents()
                return

    def _format_workspace_list(self) -> str:
        active = self._resolved_active_path()
        if not self._managed_workspaces:
            return (
                "本次会话已纳管目录: （无）\n"
                "当前活跃工作区: 未绑定\n"
                "用 /cd <路径> 绑定并切换，或 /workspace add <路径> 只纳入不切换。"
            )
        lines = ["本次会话已纳管目录:"]
        for path in self._managed_workspaces:
            if active is not None and path == active:
                lines.append(f"* {path} （当前）")
            else:
                lines.append(f"  {path}")
        if active is None:
            lines.append("当前活跃工作区: 未绑定")
        return "\n".join(lines)

    def _cmd_workspace(self, text: str) -> None:
        tokens = _message_tokens(text)
        args = tokens[1:]
        if not args:
            self._log(self._format_workspace_list())
            return
        sub = args[0].lower()
        if sub == "add" and len(args) == 2:
            self._workspace_add(args[1])
            return
        if sub == "remove" and len(args) == 2:
            self._workspace_remove(args[1])
            return
        self._log(WORKSPACE_USAGE, tone="error")

    def _cmd_team(self, text: str) -> None:
        workspace = self._resolved_active_path()
        if workspace is None:
            self._log(f"/team 未执行。{STANDBY_COMMAND_HINT}", tone="error")
            return
        from .roster import (
            RosterError,
            add_worker,
            format_add_result,
            format_roster_listing,
            list_roster,
        )

        tokens = _message_tokens(text)
        args = tokens[1:]
        if not args:
            try:
                rows = list_roster(workspace)
            except Exception as exc:  # noqa: BLE001 - 只报不崩
                self._log(
                    f"/team 未执行: 名册读取失败: "
                    f"{type(exc).__name__}: {exc}",
                    tone="error",
                )
                return
            self._log(format_roster_listing(rows))
            return
        sub = args[0].lower()
        if sub == "add" and len(args) == 3:
            try:
                result = add_worker(
                    workspace, args[1], args[2], channel="boss"
                )
            except RosterError as exc:
                self._log(f"/team add 失败: {exc}", tone="error")
                return
            self._log(format_add_result(result))
            return
        if sub in {"versions", "rollback"}:
            self._team_versions(workspace, sub, args)
            return
        if sub == "remove" and len(args) == 2:
            name = args[1]
            try:
                rows = list_roster(workspace)
            except Exception as exc:  # noqa: BLE001 - 只报不崩
                self._log(
                    f"/team remove 未执行: 名册读取失败: "
                    f"{type(exc).__name__}: {exc}",
                    tone="error",
                )
                return
            row = next(
                (item for item in rows if item["name"] == name), None
            )
            if row is None:
                self._log(
                    f"/team remove 失败: 名册里没有 {name}。", tone="error"
                )
                return
            future = self._confirm_future
            if future is not None and not future.done():
                self._log(
                    "正在等另一个确认的答复，先答完那个再移除。",
                    tone="warning",
                )
                return
            files = "、".join(row["scope_files"]) or "—"
            question = (
                f"确认移除员工 {name}（负责模块 {row['module'] or '—'}，"
                f"负责的文件 {files}）？ 输入 Y 放行 / N 拒绝"
            )
            self.run_worker(
                self._service_team_remove(workspace, name, question),
                name="移除员工确认",
                exit_on_error=False,
            )
            return
        self._log(TEAM_USAGE, tone="error")

    def _team_versions(self, workspace: Path, sub: str, args: list[str]) -> None:
        """VER1：查版本/变更记录、执行回滚。回滚与 /team add 同口径，不加确认。"""
        from .agent_versions import (
            VersionError,
            format_rollback,
            format_versions,
            rollback,
        )
        from .roster import _profile_dir as _roster_profile_dir

        if (sub == "versions" and len(args) != 2) or (
            sub == "rollback" and len(args) != 3
        ):
            self._log(TEAM_USAGE, tone="error")
            return
        try:
            _ws, profile_dir = _roster_profile_dir(workspace)
            if sub == "versions":
                self._log(format_versions(profile_dir, args[1]))
                return
            result = rollback(profile_dir, args[1], args[2])
        except (VersionError, OSError, ValueError) as exc:
            self._log(f"/team {sub} 失败: {exc}", tone="error")
            return
        self._log(format_rollback(result))

    async def _service_team_remove(
        self, workspace: Path, name: str, question: str
    ) -> None:
        """删人确认：与秘书确认共用同一套停驻/答复机制，不另建第二套。

        问题文案由程序按名册行的真实参数生成（_cmd_team 组装）；
        等待期间界面显示「在等你答复」，老板在输入框里答 Y/N，
        esc = 拒绝（action_interrupt 已处理）；答复只对这一次
        有效，答 Y 才执行移除，移除经覆盖层留痕。
        """
        from .roster import RosterError, format_remove_result, remove_worker

        self._event(question, "warning")
        self._set_busy_widget(True)
        try:
            raw_answer = await self._present_confirm()
        finally:
            self._set_busy_widget(False)
        answer = (
            "Y" if str(raw_answer).strip().upper() in {"Y", "YES"} else "N"
        )
        if answer != "Y":
            self._event(f"已取消移除 {name}（答复 {answer}）。", "warning")
            return
        try:
            result = remove_worker(workspace, name)
        except RosterError as exc:
            self._log(f"/team remove 失败: {exc}", tone="error")
            return
        self._log(format_remove_result(result))

    def _cmd_brief(self, text: str) -> None:
        workspace = self._resolved_active_path()
        if workspace is None:
            self._log(
                f"/brief 未执行。{STANDBY_COMMAND_HINT}", tone="error"
            )
            return
        from core import brief as brief_mod

        profile_dir = brief_mod.profile_dir_for(workspace)
        self._log(
            brief_mod.format_brief_for_display(workspace, profile_dir)
        )

    def _apply_perm_mode(self, mode: str) -> None:
        """切换权限模式：记状态；已有调度员时按既有重建法换新
        （保留会话 state），失败保留旧调度员并报错。"""

        self._perm_mode = mode
        self._refresh_status_lines()
        if self._dispatcher is None:
            return
        old_agent = self._dispatcher
        old_state = getattr(old_agent, "state", None)
        try:
            from .dispatcher import build_dispatcher

            model = (
                self._active_model
                if self._active_model is not None
                else self._injected_model
            )
            self._dispatcher = build_dispatcher(
                model=model,
                event_sink=self._on_platform_event,
                default_actors=self.actor_backend,
                workspace=self._dispatcher_workspace(),
                state=old_state,
                secretary_files=True,
                confirm_responder=_tui_confirm_responder,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001 - 保留旧调度员
            self._dispatcher = old_agent
            self._log(
                f"模式切换失败，仍用旧调度员："
                f"{type(exc).__name__}: {exc}",
                tone="error",
            )

    def _cmd_mode(self, text: str) -> None:
        tokens = text.split()
        arg = tokens[1].lower() if len(tokens) > 1 else ""
        if not arg:
            self._log(
                f"当前权限模式：{MODE_LABELS[self._perm_mode]}"
                f"（{self._perm_mode}）"
            )
            self._log(
                "切换：/mode ask 逐次确认 · /mode auto-edit 自动改"
                "（工作区内普通文件免问） · /mode auto-review "
                "自动审核（清单内只读自动放行）",
                tone="muted",
            )
            return
        if arg not in PERM_MODES:
            self._log(
                f"没有这个模式：{arg}"
                "（只认 ask / auto-edit / auto-review）",
                tone="error",
            )
            return
        if arg == self._perm_mode:
            self._log(f"已经是{MODE_LABELS[arg]}模式了。", tone="muted")
            return
        self._apply_perm_mode(arg)
        if self._dispatcher is not None and getattr(
            self._dispatcher, "_drbug_mode", arg
        ) != arg:
            return  # 重建失败已在 _apply_perm_mode 里报错
        self._log(f"权限模式已切换为：{MODE_LABELS[arg]}（{arg}）")

    def _cmd_permissions(self, text: str) -> None:
        from .policy import (
            Decision,
            PolicyConfigError,
            PolicyEngine,
            default_global_policy_path,
            load_policies,
            project_policy_path,
            remove_policy_rule,
        )

        parts = text.split()[1:]
        agent = self._dispatcher
        tool = (
            getattr(agent, "_drbug_secretary_read", None)
            if agent is not None
            else None
        )
        engine = (
            getattr(tool, "_engine", None) if tool is not None else None
        )
        bound = self._resolved_active_path()
        global_path = default_global_policy_path()
        try:
            global_rules = (
                load_policies(global_path) if global_path.exists() else []
            )
        except PolicyConfigError as exc:
            self._log(f"全局策略文件损坏：{exc}", tone="error")
            return
        project_rules = None
        project_path = None
        if bound is not None:
            project_path = project_policy_path(bound)
            try:
                project_rules = (
                    load_policies(project_path)
                    if project_path.exists()
                    else []
                )
            except PolicyConfigError as exc:
                self._log(f"项目策略文件损坏：{exc}", tone="error")
                return
        session_rules = (
            list(engine.session_rules) if engine is not None else []
        )
        if not parts:
            for line in _permissions_lines(
                session_rules=session_rules,
                global_rules=global_rules,
                project_rules=project_rules,
            ):
                self._log(line)
            if engine is None:
                self._log(
                    "（秘书还没启动：会话规则要等第一次对话后才有）",
                    tone="muted",
                )
            return
        if parts[0] == "revoke" and len(parts) == 3 and parts[2].isdigit():
            group, index = parts[1], int(parts[2])
            try:
                if group in {"会话", "session"}:
                    if engine is None:
                        self._log("秘书还没启动，没有会话规则可撤销。", tone="error")
                        return
                    rule = session_rules[index - 1]
                    engine.remove_session_rule(rule)
                    engine.audit(
                        "secretary",
                        "revoke_rule",
                        f"{rule.action} {rule.glob}",
                        Decision(
                            verdict="allow",
                            tier=2,
                            reason="老板在 /permissions 撤销会话规则",
                        ),
                        workspace=engine.workspace,
                    )
                    self._log(f"已撤销会话规则 [{index}]：{rule.glob}")
                    return
                if group in {"全局", "global"}:
                    removed = remove_policy_rule(global_path, index - 1)
                    PolicyEngine(workspace=bound).audit(
                        "secretary",
                        "revoke_rule",
                        f"{removed.action} {removed.glob}",
                        Decision(
                            verdict="allow",
                            tier=3,
                            reason="老板在 /permissions 撤销全局规则",
                        ),
                        workspace=bound,
                    )
                    self._log(f"已撤销全局规则 [{index}]：{removed.glob}")
                    return
                if group in {"项目", "project"}:
                    if project_path is None:
                        self._log("还没绑定项目目录，没有项目规则可撤销。", tone="error")
                        return
                    removed = remove_policy_rule(project_path, index - 1)
                    PolicyEngine(workspace=bound).audit(
                        "secretary",
                        "revoke_rule",
                        f"{removed.action} {removed.glob}",
                        Decision(
                            verdict="allow",
                            tier=3,
                            reason="老板在 /permissions 撤销项目规则",
                        ),
                        workspace=bound,
                    )
                    self._log(f"已撤销项目规则 [{index}]：{removed.glob}")
                    return
            except IndexError:
                self._log(f"没有第 {index} 条这种规则。", tone="error")
                return
            except PolicyConfigError as exc:
                self._log(f"策略文件损坏，撤销中止：{exc}", tone="error")
                return
        self._log(
            "用法：/permissions 看清单 · "
            "/permissions revoke 会话|全局|项目 <序号> 撤销",
            tone="error",
        )

    def _cmd_runs(self, text: str) -> None:
        from core.runs import (
            format_run_detail,
            format_run_line,
            list_runs,
            load_run_detail,
        )

        parts = text.split()[1:]
        if not parts:
            summaries, skipped = list_runs(limit=10)
            if not summaries:
                self._log("暂无运行记录。")
                return
            self._log("最近的运行记录（/runs <id 前缀> 看详情）：")
            for summary in summaries:
                self._log(f"  {format_run_line(summary)}")
            if skipped:
                self._log(
                    f"（另有 {skipped} 个损坏记录文件已跳过）",
                    tone="muted",
                )
            return
        prefix = parts[0]
        summaries, _ = list_runs(limit=500)
        matches = [s for s in summaries if s.run_id.startswith(prefix)]
        if not matches:
            self._log(f"找不到运行记录：{prefix}", tone="error")
            return
        if len(matches) > 1:
            self._log(f"前缀 {prefix} 命中多条，请补全：", tone="warning")
            for summary in matches:
                self._log(f"  {format_run_line(summary)}")
            return
        for line in format_run_detail(load_run_detail(matches[0])):
            self._log(line)

    # ------------------------------------------------------------------
    # F1：/sessions 会话管理（后端只用 session_store / session_admin）
    # ------------------------------------------------------------------
    def _sessions_store(self) -> Any:
        from .session_store import SessionStore

        workspace = self._resolved_active_path()
        if workspace is None:
            self._log(
                "/sessions 未执行：未绑定工作区，只能管理本工作区的会话存档。",
                tone="error",
            )
            return None
        return SessionStore.for_workspace(workspace)

    def _cmd_sessions(self, text: str) -> None:
        from . import session_admin

        parts = text.split(maxsplit=3)
        store = self._sessions_store()
        if store is None:
            return
        if len(parts) == 1:
            items = session_admin.list_sessions(store)
            if not items:
                self._log(SESSIONS_EMPTY_TEXT)
                return
            self._log("本工作区存档的会话（/sessions restore <id> 恢复）：")
            for item in items:
                self._log(f"  {session_admin.format_row(item)}")
            self.push_screen(SessionsModal(items), self._sessions_picked)
            return
        sub = parts[1]
        if sub == "restore" and len(parts) == 3:
            self._sessions_restore(parts[2])
            return
        if sub == "rename" and len(parts) == 4:
            try:
                entry = session_admin.rename(store, parts[2], parts[3])
            except ValueError as exc:
                self._log(f"重命名未执行：{exc}", tone="error")
                return
            self._event(f"已重命名会话 {entry.session_id}：{entry.title}", "success")
            return
        if sub == "delete" and len(parts) == 3:
            self._sessions_delete(store, parts[2])
            return
        self._log(SESSIONS_USAGE, tone="error")

    def _sessions_picked(self, session_id: str | None) -> None:
        if session_id:
            self._sessions_restore(session_id)

    def _sessions_restore(self, session_id: str) -> None:
        from . import session_admin

        store = self._sessions_store()
        if store is None:
            return
        if self._agent_busy:
            self._log("恢复未执行：主治医生正在回复，等这轮结束再恢复。", tone="error")
            return
        try:
            loaded = session_admin.prepare_restore(store, session_id)
        except session_admin.RestoreRefused as exc:
            self._log(str(exc), tone="error")
            return
        except Exception as exc:  # noqa: BLE001 - 存档读不出就报，不崩
            self._log(f"恢复失败：{type(exc).__name__}: {exc}", tone="error")
            return
        if self._has_unsaved_content():
            self._ask_replace_before_restore(loaded)
            return
        self._sessions_restore_gated(loaded)

    def _ask_replace_before_restore(self, loaded: Any) -> None:
        """F2：当前对话有未存档内容时先问（B2 确认回路），拒绝不恢复。"""
        question = self._replace_confirm_line(loaded)
        self._event(question, "warning")

        async def _ask() -> None:
            raw = await self._present_confirm([question])
            self._replace_answered(_done_future(raw), loaded)

        self.run_worker(_ask(), name="恢复替换确认", exit_on_error=False)

    def _replace_confirm_line(self, loaded: Any) -> str:
        session = loaded.session
        return (
            f"当前对话有 {self._message_count()} 条消息尚未存档，"
            f"恢复「{session.title or '（无标题）'}」（id {session.session_id}）"
            "将整体替换它们。确认替换？输入 Y 替换 / N 取消"
        )

    def _replace_answered(self, future: Any, loaded: Any) -> None:
        try:
            raw = future.result()
        except Exception:  # noqa: BLE001 - 取消也当拒绝
            raw = ("N", "")
        if isinstance(raw, tuple):
            code = raw[0]
        else:
            code = "Y" if str(raw).strip().upper() in {"Y", "YES"} else "N"
        if code != "Y":
            self._log("已取消恢复，当前对话未动。", tone="warning")
            return
        self._sessions_restore_gated(loaded)

    def _sessions_restore_gated(self, loaded: Any) -> None:
        workspace = Path(loaded.session.workspace).expanduser().resolve()
        if self._interactive_gates:
            # 恢复不扩权：本次会话同意过也照问，旧授权不复活。
            self.push_screen(
                WorkspaceTrustScreen(self, workspace, cancel_label="不用，取消恢复"),
                lambda ok: self._sessions_trust_decided(ok, loaded, workspace),
            )
            return
        self._apply_restore(loaded, workspace)

    def _sessions_trust_decided(self, ok: bool | None, loaded: Any, workspace: Path) -> None:
        if not ok:
            self._log("已取消恢复：未同意把该目录作为工作区。", tone="warning")
            return
        self._session_trusted.add(str(workspace))
        self._apply_restore(loaded, workspace)

    def _apply_restore(self, loaded: Any, workspace: Path) -> bool:
        from . import session_admin
        from .usage import reset_current

        session = loaded.session
        previous_active = self._active_workspace
        previous_managed = list(self._managed_workspaces)
        self._active_workspace = workspace
        self._remember_managed(workspace)
        if not self._rebind_dispatcher(restored_state=session.agent_state):
            self._active_workspace = previous_active
            self._managed_workspaces = previous_managed
            return False
        override = session.model_override
        if override is not None:
            if not self._commit_session(
                override.provider, override.model, override.effort, announce=False
            ):
                self._event(
                    f"存档的模型覆盖 {override.provider}/{override.model} 未能回填，"
                    "沿用当前模型",
                    "warning",
                )
        elif self._model_state.override is not None:
            self._event("存档没有模型覆盖，沿用当前模型设置", "warning")
        accumulator = reset_current()
        for record in session.usage:
            accumulator.add(record)
        history = list(session.history)
        next_seq = 1
        for record in history:
            record.restored = True
            if record.seq is None:
                record.seq = next_seq
            next_seq = max(next_seq, int(record.seq) + 1)
        self.history = history
        self._next_seq = next_seq
        self._thought_texts.clear()
        self._thought_blocks_expanded.clear()
        self._jumped_seq = None
        self._tool_pending.clear()
        self._tool_texts.clear()
        self._tool_blocks_expanded.clear()
        self.record_lines.clear()
        self._pending_diff = False
        if self.page == "home":
            self.page = "session"
            self.switch_screen(SessionScreen(self))
        else:
            for screen in self.screen_stack:
                if isinstance(screen, SessionScreen) and screen._ready:
                    screen._reflow_log()
        self._refresh_status_lines()
        self._record_recent(workspace)
        self._event(
            f"已恢复会话「{session.title or '（无标题）'}」（id {session.session_id}，"
            f"{len(history)} 条记录），工作区：{workspace}",
            "success",
        )
        for line in session_admin.tool_run_lines(loaded.tool_runs):
            self._event(line, "warning")
        # F2：恢复后接着往这份存档里写；恢复来的内容视为已存档。
        self._session_id = session.session_id
        self._tool_runs = {run.call_id: run for run in loaded.tool_runs}
        self._saved_messages = self._message_count()
        return True

    # ------------------------------------------------------------------
    # F2：存档入口（自动存档 + /save）
    # ------------------------------------------------------------------
    @staticmethod
    def _is_message_record(record: Any) -> bool:
        """算作「消息」的记录：主治医生回复，及不是斜杠命令的用户输入。"""
        kind, text = record[0], str(record[1])
        if kind == "assistant":
            return True
        return kind == "user" and not text.startswith("/")

    def _message_count(self) -> int:
        return sum(1 for record in self.history if self._is_message_record(record))

    def _has_unsaved_content(self) -> bool:
        count = self._message_count()
        if count == 0:
            return False
        return self._session_id is None or count != self._saved_messages

    def _default_session_title(self) -> str:
        from . import session_autosave

        return session_autosave.default_title(self.history)

    def _save_session(self) -> Any:
        """把当前会话写进本工作区存档（同一 id 覆盖同一份）。出错原样抛。"""
        from . import session_autosave
        from .session_store import SessionStore, new_session_id
        from .usage import current_accumulator

        workspace = self._resolved_active_path()
        if workspace is None:
            raise RuntimeError("未绑定工作区")
        store = SessionStore.for_workspace(workspace)
        if self._session_id is None:
            self._session_id = new_session_id()
        dispatcher = self._dispatcher
        state = (
            getattr(dispatcher, "state", None)
            if dispatcher is not None
            else getattr(self, "_restored_agent_state", None)
        )
        session = session_autosave.build_session(
            store,
            session_id=self._session_id,
            workspace=workspace,
            history=self.history,
            agent_state=state,
            model_override=self._model_state.override,
            usage=current_accumulator().records(),
        )
        entry = store.save(session, list(self._tool_runs.values()))
        self._saved_messages = self._message_count()
        return entry

    def _autosave(self) -> None:
        """一轮落定后自动存档。失败只提示一行，绝不打断对话。"""
        if self._resolved_active_path() is None or not self._has_unsaved_content():
            return
        try:
            self._save_session()
        except Exception as exc:  # noqa: BLE001 - 存档失败不能中断对话
            self._log(
                f"自动存档失败（对话不受影响）：{type(exc).__name__}: {exc}",
                tone="warning",
            )

    def _autosave_threadsafe(self) -> None:
        if threading.get_ident() == self._main_thread:
            self._autosave()
            return
        try:
            self.call_from_thread(self._autosave)
        except Exception:  # noqa: BLE001 - app already shutting down
            pass

    def _cmd_save(self) -> None:
        workspace = self._resolved_active_path()
        if workspace is None:
            self._log("/save 未执行：未绑定工作区，存档只存在工作区里。", tone="error")
            return
        if self._message_count() == 0:
            self._log("没有可存档的内容：当前对话还没有消息，未写存档。")
            return
        try:
            entry = self._save_session()
        except Exception as exc:  # noqa: BLE001 - 报错不崩
            self._log(f"存档失败：{type(exc).__name__}: {exc}", tone="error")
            return
        from .session_store import SessionStore

        where = SessionStore.for_workspace(workspace).session_dir(entry.session_id)
        self._event(
            f"已存档到 {where}（{entry.message_count} 条记录，"
            f"其中消息 {self._saved_messages} 条；标题「{entry.title}」）",
            "success",
        )

    def _sessions_delete(self, store: Any, session_id: str) -> None:
        from . import session_admin
        from .session_store import valid_session_id

        if not valid_session_id(session_id):
            self._log(f"删除未执行：会话 id 不合法：{session_id}", tone="error")
            return
        item = session_admin.find(store, session_id)
        if item is None:
            self._log(f"删除未执行：找不到会话：{session_id}", tone="error")
            return
        impact = session_admin.delete_impact_lines(item, store)
        for line in impact:
            self._log(line, tone="warning")
        question = session_admin.delete_confirm_line(item)
        self._event(question, "warning")

        async def _ask() -> None:
            raw = await self._present_confirm(impact + [question])
            self._sessions_delete_answered(_done_future(raw), store, session_id)

        self.run_worker(_ask(), name="删除会话确认", exit_on_error=False)

    def _sessions_delete_answered(self, future: Any, store: Any, session_id: str) -> None:
        from . import session_admin

        try:
            raw = future.result()
        except Exception:  # noqa: BLE001 - 取消也当拒绝
            raw = ("N", "")
        if isinstance(raw, tuple):
            code = raw[0]
        else:
            code = "Y" if str(raw).strip().upper() in {"Y", "YES"} else "N"
        if code != "Y":
            self._log("已取消删除，存档一字未动。", tone="warning")
            return
        try:
            removed = session_admin.delete(store, session_id)
        except OSError as exc:
            self._log(f"删除失败：{type(exc).__name__}: {exc}", tone="error")
            return
        self._event(
            f"已删除会话 {session_id}（{len(removed)} 个文件 + 索引条目）", "success"
        )

    def _cmd_market(self, text: str) -> None:
        from core import market as market_mod

        parts = text.split()
        args = parts[1:] if len(parts) > 1 else []
        try:
            entries = market_mod.load_catalog()
        except ValueError as exc:
            self._log(f"扩展目录损坏: {exc}", tone="error")
            return
        installed = market_mod.installed_ids()
        if not args:
            for entry in entries:
                self._log(
                    market_mod.format_entry_line(
                        entry, entry["id"] in installed
                    )
                )
            self._log(
                "用法：/market install <id> 安装，"
                "/market uninstall <id> 卸载。",
                tone="muted",
            )
            return
        action = args[0]
        if action == "install" and len(args) == 2:
            try:
                market_mod.install(args[1])
            except ValueError as exc:
                self._log(f"安装失败: {exc}", tone="error")
                return
            entry = next(
                (e for e in entries if e["id"] == args[1]), None
            )
            if entry is not None and entry["kind"] == "skill":
                self._log(
                    f"已安装技能包 {args[1]}"
                    "（秘书下次构建时会自动带上它的手册）。"
                )
            else:
                self._log(
                    f"已安装 {args[1]}（已登记进工具配置；"
                    "调用时仍需门禁放行）。"
                )
            return
        if action == "uninstall" and len(args) == 2:
            if args[1] not in installed:
                self._log(f"{args[1]} 未安装。", tone="warning")
                return
            future = self._confirm_future
            if future is not None and not future.done():
                self._log(
                    "正在等另一个确认的答复，先答完那个再卸载。",
                    tone="warning",
                )
                return
            question = f"确认卸载扩展 {args[1]}？ 输入 Y 放行 / N 拒绝"
            self.run_worker(
                self._service_market_uninstall(args[1], question),
                name="卸载扩展确认",
                exit_on_error=False,
            )
            return
        self._log("用法：/market [install|uninstall <id>]", tone="error")

    async def _service_market_uninstall(
        self, entry_id: str, question: str
    ) -> None:
        """卸载确认：与删人确认共用同一套停驻/答复机制，不另建第二套。"""
        from core import market as market_mod

        self._event(question, "warning")
        self._set_busy_widget(True)
        try:
            raw_answer = await self._present_confirm()
        finally:
            self._set_busy_widget(False)
        answer = (
            "Y" if str(raw_answer).strip().upper() in {"Y", "YES"} else "N"
        )
        if answer != "Y":
            self._event(f"已取消卸载 {entry_id}（答复 {answer}）。", "warning")
            return
        try:
            market_mod.uninstall(entry_id)
        except ValueError as exc:
            self._log(f"卸载失败: {exc}", tone="error")
            return
        self._log(f"已卸载 {entry_id}。")

    def _cmd_pending(self, text: str) -> None:
        from .pending import drop_pending, execute_pending, list_pending

        bound = self._resolved_active_path()
        if bound is None:
            self._log(
                f"/pending 未执行。{STANDBY_COMMAND_HINT}",
                tone="error",
            )
            return
        parts = text.split()[1:]
        entries = list_pending(bound)
        if not parts:
            if not entries:
                self._log("待批准清单是空的。")
                return
            self._log("待批准清单（当时没人能答复、按拒绝落地的请求）：")
            for i, entry in enumerate(entries, 1):
                self._log(
                    f"  [{i}] {str(entry.get('ts', ''))[:19]} · "
                    f"{entry.get('action')} · {entry.get('target')}"
                )
            self._log(
                "批准并重新执行：/pending approve <序号>；"
                "丢弃：/pending drop <序号>",
                tone="muted",
            )
            return
        if (
            parts[0] in {"approve", "drop"}
            and len(parts) >= 2
            and parts[1].isdigit()
        ):
            index = int(parts[1])
            if not 1 <= index <= len(entries):
                self._log(f"没有第 {index} 条待批准。", tone="error")
                return
            entry = entries[index - 1]
            if parts[0] == "drop":
                drop_pending(bound, entry["id"])
                self._log(f"已丢弃待批准 [{index}]：{entry.get('target')}")
                return
            ack = len(parts) >= 3 and parts[2] == "confirm"
            result = execute_pending(
                bound, entry["id"], acknowledge_change=ack
            )
            status = result.get("status")
            if status == "executed":
                self._log(f"已批准并重新执行：读到了 {entry.get('target')}")
                content = str(result.get("content", ""))
                for line in content.splitlines():
                    self._log(f"  {line}", tone="muted")
                if result.get("truncated"):
                    self._log("（内容太长，只显示了前面一部分）", tone="muted")
            elif status == "changed":
                self._log(
                    "这个文件在记下之后被改过，先不执行。"
                    f"确认按现在的内容执行：/pending approve {index} confirm",
                    tone="warning",
                )
            elif status == "refused":
                self._log(
                    f"现在的政策不允许读它了：{result.get('reason')}",
                    tone="error",
                )
            elif status == "missing":
                self._log("文件或条目已经不在了。", tone="error")
            else:
                self._log(f"这条待批准暂不支持执行（{status}）。", tone="error")
            return
        self._log(
            "用法：/pending 看清单 · /pending approve <序号> · "
            "/pending drop <序号>",
            tone="error",
        )

    def _workspace_add(self, raw: str) -> None:
        try:
            resolved = self._resolve_session_path(raw)
        except (OSError, RuntimeError, ValueError) as exc:
            self._log(
                f"/workspace add 失败: 无法解析路径 {raw!r}"
                f"（{type(exc).__name__}: {exc}）。清单保持不变。",
                tone="error",
            )
            return
        if not resolved.is_dir():
            self._log(
                f"/workspace add 失败: 目录不存在: {resolved}。清单保持不变。",
                tone="error",
            )
            return
        if resolved in self._managed_workspaces:
            self._log(f"已在纳管清单中（未切换）: {resolved}", tone="warning")
            return
        self._managed_workspaces.append(resolved)
        self._log(f"已纳管目录（未切换）: {resolved}")

    def _workspace_remove(self, raw: str) -> None:
        try:
            resolved = self._resolve_session_path(raw)
        except (OSError, RuntimeError, ValueError) as exc:
            self._log(
                f"/workspace remove 失败: 无法解析路径 {raw!r}"
                f"（{type(exc).__name__}: {exc}）。清单与绑定保持不变。",
                tone="error",
            )
            return
        if resolved not in self._managed_workspaces:
            self._log(
                f"/workspace remove 失败: 纳管清单中没有 {resolved}。"
                "清单与绑定保持不变。",
                tone="error",
            )
            return
        active = self._resolved_active_path()
        if active is not None and resolved == active:
            previous_active = self._active_workspace
            previous_managed = list(self._managed_workspaces)
            self._managed_workspaces.remove(resolved)
            self._active_workspace = None
            if not self._rebind_dispatcher():
                self._active_workspace = previous_active
                self._managed_workspaces = previous_managed
                return
            self._refresh_status_lines()
            self._log(
                f"已移出纳管目录: {resolved}\n"
                "该目录是当前活跃工作区，会话已回落到未绑定待命态。\n"
                "请先 /cd <项目路径> 再执行需要工作区的操作。",
                tone="warning",
            )
            return
        self._managed_workspaces.remove(resolved)
        self._log(f"已移出纳管目录: {resolved}。当前工作区绑定保持不变。")

    def _workspace_arg(self, value: str) -> str:
        path = Path(value).expanduser()
        if path.is_absolute():
            return str(path)
        candidate = self.workspace_root / path
        if candidate.exists():
            return str(candidate)
        return value

    def _parse_profile_args(
        self, args: list[str]
    ) -> tuple[str, str | None, bool]:
        resolved = self._resolved_active_path()
        target = str(resolved) if resolved is not None else ""
        name: str | None = None
        saw_target = False
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--name" and index + 1 < len(args):
                name = args[index + 1]
                index += 2
                continue
            target = self._workspace_arg(token)
            saw_target = True
            index += 1
        return target, name, saw_target

    def _start_local_worker(
        self,
        label: str,
        work: Callable[[], str],
    ) -> None:
        def run() -> None:
            try:
                result = work()
                tone = "plain"
            except Exception as exc:  # noqa: BLE001 - report, never crash app
                result = f"{label}执行失败: {type(exc).__name__}: {exc}"
                self._log(result, tone="error", exc=exc, source=label)
                return
            self._log(result, tone=tone)
            # 运行完成：结果行之后追加最近一轮的 diff 块（若本次落了结果包）。
            try:
                self.call_from_thread(self.flush_pending_diff)
            except Exception:  # noqa: BLE001 - app already shutting down
                pass

        self.run_worker(run, thread=True, name=label, exit_on_error=False)

    def _run_check(self) -> str:
        from . import dispatcher

        dispatcher.set_event_sink(self._on_platform_event)
        return dispatcher.platform_check()

    def _run_accept(self, target: str) -> str:
        from . import dispatcher

        dispatcher.set_event_sink(self._on_platform_event)
        return dispatcher.accept_check(self._workspace_arg(target))

    def _run_profile(self, target: str, name: str | None) -> str:
        from .autoprofile import build_profile

        result = build_profile(
            self._workspace_arg(target),
            out_root=self.workspace_root / "profiles",
            name=name,
        )
        return "\n".join(
            [
                "== drbug profile — 自动建档 ==",
                f"源仓库: {result.repo_path}",
                f"档位目录: {result.profile_dir}",
                "检测包: "
                + ("、".join(result.packages) or "无")
                + f"（vault package={result.metadata.get('vault_package')}）",
                f"测试: {result.test_total} 条（通过 {result.passed_count}，"
                f"当前失败 {result.failed_count}）",
                "专家: " + "、".join(result.dispatch_table.get("actors", [])),
                "派活规则表: " + str(result.dispatch_table_path),
                "检查存放处: " + str(result.vault_dir),
                "结论: 建档完成（零模型调用、零 API 费用）。",
            ]
        )

    def _run_repro(self, target: str, symptom: str) -> str:
        from . import dispatcher

        dispatcher.set_event_sink(self._on_platform_event)
        return dispatcher.propose_repro(target, symptom)

    def _handle_run(self, args: list[str]) -> None:
        try:
            arm, actors, rounds, workspace, yes, workspace_given = (
                self._parse_run_args(args)
            )
        except ValueError as exc:
            self._log(f"/run 参数错误: {exc}\n{RUN_USAGE}", tone="error")
            return

        if self._active_workspace is None and not workspace_given:
            self._log(f"/run 未执行。{STANDBY_COMMAND_HINT}", tone="error")
            return

        if not yes:
            self._log(
                "⚠ 真实模型运行会消耗 API 额度，尚未执行。\n"
                f"确认后请输入: /run --arm {arm}"
                + (f" --max-rounds {rounds}" if rounds != 3 else "")
                + " --yes",
                tone="warning",
            )
            return

        self._log(
            f"开始修复: arm={arm} actors={actors} "
            f"工作区={workspace} max_rounds={rounds}"
            + ("（已确认付费）" if actors != "scripted" else "")
        )
        self._start_local_worker(
            "修复运行",
            lambda: self._run_repair(arm, actors, workspace, rounds, yes),
        )

    def _run_repair(
        self,
        arm: str,
        actors: str,
        workspace: str,
        rounds: int,
        yes: bool,
    ) -> str:
        from . import dispatcher

        dispatcher.set_event_sink(self._on_platform_event)
        return dispatcher.run_repair(
            arm=arm,
            actors=actors,
            workspace=workspace,
            max_rounds=rounds,
            confirm_paid=yes,
        )

    def _parse_run_args(
        self, args: list[str]
    ) -> tuple[str, str, int, str, bool, bool]:
        arm = "C"
        actors = self.actor_backend
        rounds = 3
        resolved = self._resolved_active_path()
        workspace = str(resolved) if resolved is not None else ""
        yes = False
        workspace_given = False

        index = 0
        while index < len(args):
            token = args[index]
            if token == "--yes":
                yes = True
            elif token in {"--arm", "--max-rounds", "--workspace"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{token} 缺少取值")
                value = args[index + 1]
                index += 1
                if token == "--arm":
                    arm = value.upper()
                elif token == "--max-rounds":
                    try:
                        rounds = int(value)
                    except ValueError:
                        raise ValueError(
                            f"--max-rounds 需要整数，收到 {value!r}"
                        ) from None
                else:
                    workspace = self._workspace_arg(value)
                    workspace_given = True
            else:
                raise ValueError(f"未知参数 {token!r}")

            index += 1

        if arm not in {"A", "B", "C"}:
            raise ValueError(f"--arm 只能是 A/B/C，收到 {arm!r}")
        if rounds < 0:
            raise ValueError("--max-rounds 必须 >= 0")
        return arm, actors, rounds, workspace, yes, workspace_given

    # ------------------------------------------------------------------
    # 调度员 Agent 路径
    # ------------------------------------------------------------------
    def _current_capabilities(self):
        """当前模型的能力行。界面不写死模型名，只读能力表。"""
        from .model_config import model_capabilities

        return model_capabilities(self._model_label())

    def _send_to_dispatcher(self, text: str) -> None:
        if self._agent_busy:
            self._log("主治医生还在处理上一条消息，请稍候…", tone="warning")
            return
        ok, file_attachment = self._take_mention_attachment(text)
        if not ok:
            self._restore_prompt_text(text)
            return
        self._dispatcher_generation += 1
        generation = self._dispatcher_generation
        self._interrupt_requested = False
        self.stream_text = ""
        self._reply_started = time.monotonic()
        self._set_busy(True)
        self._log("主治医生思考中…（工具活动会实时播报）", tone="muted")

        pasted_images, pasted_files, pasted_notes = self._take_pasted_attachments(
            text
        )
        image_paths = _image_paths_in_message(text)
        vision = self._current_capabilities().vision
        images: list[str] | None = None
        image_notes: list[str] = []
        all_images = list(pasted_images)
        for path in image_paths:
            if path not in all_images:
                all_images.append(path)
        if all_images and vision:
            images = [str(path) for path in all_images]
            image_notes.append(_attached_image_note(all_images))
        else:
            if pasted_images:
                image_notes.append(PASTED_IMAGE_NOT_SENT)
            if image_paths:
                image_notes.append(IMAGE_HINT)
        files_out: list[Any] = list(pasted_files)
        if file_attachment is not None:
            files_out.insert(0, file_attachment)

        # 在应用自己的事件循环上跑协程 worker：模型与 HTTP 客户端只绑定
        # 这一个循环，跨消息复用同一个 Agent，不能每条消息新建循环。
        if not files_out:
            job = self._respond_to_dispatcher(text, generation, images)
        else:
            job = self._respond_to_dispatcher(
                text, generation, images, files=files_out
            )
        self._dispatcher_worker = self.run_worker(
            job,
            name="调度员回复",
            exit_on_error=False,
        )
        for note in pasted_notes:
            self._event(note, "warning")
        for note in image_notes:
            self._event(note, "muted")
        for item in files_out:
            self._event(file_mention.attachment_note(item), "muted")

    def _take_pasted_attachments(
        self, text: str
    ) -> tuple[list[Path], list[Any], list[str]]:
        """PST1：取出本条消息原样引用的附件块，提交那一刻读入内存快照。

        不走权限层（用户亲手粘贴，零确认），也不给路径加任何放行标记。
        真实路径记在本条用户记录的 attachments 上（随存档落盘）。
        改坏的占位不算附件，原文照发；文件没了/读不了跳过并提示一行。
        """
        blocks = self.paste_blocks.attachments_in(text)
        if not blocks:
            return [], [], []
        images: list[Path] = []
        files: list[Any] = []
        notes: list[str] = []
        for block in blocks:
            path = Path(block.path)
            try:
                exists = path.is_file()
            except OSError:
                exists = False
            if not exists:
                notes.append(f"{block.placeholder} 的文件已不存在，已跳过")
                continue
            if block.is_image:
                images.append(path)
                continue
            attachment, problem = _read_pasted_file(block.kind, block.number, path)
            if attachment is None:
                notes.append(f"{block.placeholder} {problem}，已跳过")
            else:
                files.append(attachment)
        for record in reversed(self.history):
            if record[0] == "user":
                record.attachments = tuple(
                    (block.placeholder, block.path) for block in blocks
                )
                break
        self.paste_blocks.drop_attachments(blocks)
        return images, files, notes

    def _restore_prompt_text(self, text: str) -> None:
        """复验不过时把原文放回输入框，免得老板重打。

        首条消息会从 Home 切到 Session 页，新输入框要等刷新后才挂上，
        所以放到下一次刷新后再填。
        """

        def _fill() -> None:
            try:
                prompt = self.screen.query_one(PromptInput)
            except Exception:  # noqa: BLE001 - 没有输入框就算了
                return
            if not prompt.text.strip():
                prompt._apply_text(text)

        self.call_after_refresh(_fill)

    async def _respond_to_dispatcher(
        self,
        text: str,
        generation: int = 0,
        images: list[str] | None = None,
        files: list[Any] | None = None,
    ) -> None:
        started = time.monotonic()
        state = _StreamReply()
        # 钉在第一次 await 之前。之后的切换只影响下一次请求。
        state.actual = self._snapshot_actual()
        try:
            agent = self._ensure_dispatcher()
            await self._ensure_dispatcher_mcp(agent)
            if files:
                await self._consume_reply(agent, text, state, images, files=files)
            else:
                await self._consume_reply(agent, text, state, images)
        except asyncio.CancelledError:
            # esc 取消：已到达的文本在 finally 里落定为该条回复。
            state.interrupted = True
            raise
        except Exception as exc:  # noqa: BLE001 - report, never crash app
            state.error = f"主治医生暂时不可用: {type(exc).__name__}: {exc}"
        finally:
            self._finish_reply(state, time.monotonic() - started)
            self._autosave()
            self._hide_stream_view()
            self.flush_pending_diff()
            self._refresh_sidebar()
            if generation == self._dispatcher_generation:
                self._set_busy(False)
                self._dispatcher_worker = None
            self._interrupt_requested = False

    async def _ensure_dispatcher_mcp(self, agent: Any) -> None:
        """首轮对话前把已登记的 MCP 工具挂到秘书身上（每个 agent 只试一次）。

        没登记任何服务器时静默跳过；挂不上只记一行警告，不挡对话。
        """
        if getattr(agent, "_drbug_mcp_tried", False):
            return
        agent._drbug_mcp_tried = True
        from core.dispatcher import attach_secretary_mcp

        workspace = self._resolved_active_path() or Path(
            __file__
        ).resolve().parent.parent
        try:
            toolset = await attach_secretary_mcp(agent, workspace)
        except Exception as exc:  # noqa: BLE001 - 对话优先，MCP 降级
            self._log(
                f"MCP 工具未挂上：{type(exc).__name__}: {exc}",
                tone="warning",
            )
            return
        if toolset is not None:
            for notice in toolset.notices:
                self._log(notice, tone="warning")

    async def _consume_reply(
        self,
        agent: Any,
        text: str,
        state: _StreamReply,
        images: list[str] | None = None,
        files: list[Any] | None = None,
    ) -> None:
        """消费 ``reply_stream`` 事件；无流式能力时退回整段 ``reply()``。

        遇到框架的确认停驻事件就停下问老板（_service_confirm），
        答完拿结果事件续流；每条消息最多服务 3 次停驻。
        """
        from agentscope.event import (
            ModelCallEndEvent,
            ModelCallStartEvent,
            ReplyEndEvent,
            RequireUserConfirmEvent,
            TextBlockDeltaEvent,
            ThinkingBlockDeltaEvent,
            ToolCallDeltaEvent,
            ToolCallEndEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
            ToolResultStartEvent,
            ToolResultTextDeltaEvent,
        )
        from agentscope.message import Msg

        from .dispatcher import build_user_message
        from .model_config import model_capabilities
        from .policy import Decision, PolicyEngine

        vision = model_capabilities(
            str(getattr(getattr(agent, "model", None), "model", "") or "")
        ).vision
        if files:
            message = build_user_message(text, images, vision=vision, files=files)
        else:
            message = build_user_message(text, images, vision=vision)
        stream = getattr(agent, "reply_stream", None)
        if not callable(stream):
            # 兜底一：Agent 没有 reply_stream → 与改动前完全一致（整段）。
            reply = await agent.reply(message)
            state.final_text = reply.get_text_content() or ""
            return

        secretary_tool = getattr(agent, "_drbug_secretary_read", None)
        if secretary_tool is not None:
            secretary_tool._denied_targets = set()
            secretary_tool._denied_reason = ""
        pending_input: Any = message
        answered: list[tuple[str, str]] = []
        rounds = 0
        while True:
            try:
                events = stream(pending_input, yield_final_msg=True)
            except TypeError:  # 兜底二：不支持该关键字的旧签名
                events = stream(pending_input)

            confirm_event = None
            async for event in events:
                if isinstance(event, RequireUserConfirmEvent):
                    confirm_event = event
                    break
                if isinstance(event, Msg):
                    # 最终消息是落定态的唯一权威：与同输入 reply() 全文逐字一致。
                    state.final_text = event.get_text_content() or ""
                    continue
                if isinstance(event, ModelCallStartEvent):
                    # 新一轮模型调用：增量缓冲清空，思考指示带秒数重新出现。
                    state.begin_call()
                    self._hide_stream_view()
                    self._set_busy_widget(True)
                elif isinstance(event, ThinkingBlockDeltaEvent):
                    state.append_thinking(event.delta)
                    if not state.text:
                        self._render_thinking(state.live_thinking)
                elif isinstance(event, TextBlockDeltaEvent):
                    state.append_delta(event.delta)
                    self._render_stream(state.text)
                elif isinstance(event, ToolCallStartEvent):
                    # 工具活动走既有 event_sink 通道进日志；这里收起文本流视图，
                    # 让出空间并重新显示思考指示。
                    self._hide_stream_view()
                    self._set_busy_widget(True)
                    self._track_tool_start(event)
                    self._track_tool_block(event)
                elif isinstance(
                    event,
                    (
                        ToolCallDeltaEvent,
                        ToolCallEndEvent,
                        ToolResultStartEvent,
                        ToolResultTextDeltaEvent,
                    ),
                ):
                    self._track_tool_block(event)
                elif isinstance(event, ToolResultEndEvent):
                    self._track_tool_end(event)
                    self._track_tool_block(event)
                elif isinstance(event, ModelCallEndEvent):
                    try:
                        state.output_tokens += max(
                            int(event.output_tokens), 0
                        )
                    except (TypeError, ValueError):
                        pass
                elif isinstance(event, ReplyEndEvent):
                    if str(event.finished_reason) == "interrupted":
                        state.interrupted = True
            if answered:
                # 走到这里说明上一轮答复已被框架处理完毕：
                # 答 Y 的已经执行，答 N 的没有执行，逐个记审计。
                engine = PolicyEngine(
                    workspace=self._dispatcher_workspace()
                )
                for target, answer in answered:
                    engine.audit(
                        "secretary",
                        "cross_dir_read",
                        target,
                        Decision(
                            verdict="allow" if answer == "Y" else "deny",
                            tier=1,
                            reason=(
                                f"答复 {answer}；"
                                f"{'已执行' if answer == 'Y' else '未执行'}"
                            ),
                        ),
                    )
                answered = []
            if confirm_event is None:
                return
            rounds += 1
            if rounds > 3:
                raise RuntimeError(
                    "BLOCKER: 确认轮次超过上限（单条消息最多服务 3 次停驻）"
                )
            pending_input, answered = await self._service_confirm(
                agent, confirm_event
            )

    async def _present_confirm(
        self,
        lines: list[str] | None = None,
        preview_lines: list[str] | None = None,
    ) -> Any:
        """CFM1：所有确认入口的唯一呈现/答复通道。

        * 排队：共用一把锁，任一时刻只有一个确认在等答复；其余按到达
          顺序排队，每一个最终都会被单独呈现、单独答复，不合并不丢弃；
        * 路由：每个确认有自己的 future，弹窗只回答自己的那个；输入框
          与 esc 回答的是当前持锁者，因此后到的确认不会顶掉先到的；
        * 呈现：``lines`` 为 None 时只走输入框（删人/卸载的既有形态）；
          否则推一个编号唯一的弹窗，答复后等它卸载完才放下一个上来。

        只改呈现与答复路由；谁有权答、答什么、文案怎么生成都不在这里。
        """
        if self._confirm_lock is None:
            self._confirm_lock = asyncio.Lock()
        async with self._confirm_lock:
            future = asyncio.get_running_loop().create_future()
            self._confirm_future = future
            self._awaiting_confirm = True
            self.refresh_placeholder()
            self._hide_completion_menus()
            modal: ConfirmModal | None = None
            try:
                if lines is not None:
                    modal = ConfirmModal(
                        self,
                        lines=lines,
                        preview_lines=preview_lines,
                        future=future,
                    )
                    self.push_screen(modal)
                return await future
            finally:
                if self._confirm_future is future:
                    self._confirm_future = None
                self._awaiting_confirm = False
                self.refresh_placeholder()
                if modal is not None:
                    await self._retire_confirm_modal(modal)

    async def _retire_confirm_modal(self, modal: "ConfirmModal") -> None:
        """答复后（含 esc、输入框兜底、取消）确保弹窗已卸载再放行下一个。"""
        try:
            if modal in self.screen_stack and self.screen is modal:
                modal.dismiss(None)
        except Exception:  # noqa: BLE001 - 退出途中
            return
        for _ in range(200):
            if modal not in self.screen_stack and not modal.is_attached:
                return
            await asyncio.sleep(0.01)

    def _resolve_confirm(self, payload: Any) -> None:
        """弹窗（或输入框兜底）把答复交回正在等的 future。"""

        future = self._confirm_future
        if future is not None and not future.done():
            future.set_result(payload)

    async def _service_confirm(
        self,
        agent: Any,
        confirm_event: Any,
    ) -> tuple[Any, list[tuple[str, str]]]:
        """服务一次框架停驻：逐个问老板，答完打包结果事件续流。

        待确认调用 = 事件自带的 + 状态里等着的（框架一个
        事件只带一个调用，同轮其余的要从状态里取，口径与
        core/api.py 的驱动一致）。问题文案由程序按真实
        目标生成（confirm_line），模型自带描述不许进；
        等待期间界面显示「在等你答复」，老板在输入框里
        答 Y/N；批准只对这一次有效（rules=None）。
        """
        import json as _json

        from agentscope.event import (
            ConfirmResult,
            UserConfirmResultEvent,
        )

        from .policy import (
            Decision,
            Rule,
            append_global_rule,
            confirm_line,
            format_rule_toml,
            is_credential_file,
            is_secret_path,
            normalize_path,
        )

        pending_calls = list(confirm_event.tool_calls or [])
        seen_ids = {str(getattr(call, "id", "")) for call in pending_calls}
        agent_state = getattr(agent, "state", None)
        getter = (
            getattr(agent_state, "get_awaiting_tool_calls", None)
            if agent_state is not None
            else None
        )
        if callable(getter):
            try:
                awaiting = getter(str(getattr(agent, "name", "")))
            except Exception:  # noqa: BLE001 - 取不到就只服务事件自带的
                awaiting = []
            for call in awaiting or []:
                call_id = str(getattr(call, "id", ""))
                if call_id not in seen_ids:
                    seen_ids.add(call_id)
                    pending_calls.append(call)

        tool = getattr(agent, "_drbug_secretary_read", None)
        results: list[ConfirmResult] = []
        answered: list[tuple[str, str]] = []
        for tool_call in pending_calls:
            raw_input = getattr(tool_call, "input", None)
            if isinstance(raw_input, str):
                try:
                    raw_input = _json.loads(raw_input)
                except ValueError:
                    raw_input = {}
            target = ""
            if isinstance(raw_input, dict):
                target = str(raw_input.get("file_path") or "")
            question = confirm_line(
                "secretary", "cross_dir_read", target
            )
            self._event(question, "warning")
            pending = (
                getattr(tool, "_pending_decision", None)
                if tool is not None
                else None
            )
            p_ban_reason = ""
            if pending is not None and getattr(
                pending, "never_permanent", False
            ):
                p_ban_reason = "这类动作风险过高，只许逐次或本会话放行"
            elif target and (
                is_secret_path(Path(target))
                or is_credential_file(Path(target))
            ):
                p_ban_reason = "敏感路径或凭据文件不许永久放行"
            p_allowed = not p_ban_reason
            rule = None
            preview_lines = None
            if tool is not None and target:
                glob = str(
                    normalize_path(target, base=tool._workspace)
                )
                rule = Rule(
                    actor="secretary",
                    action="cross_dir_read",
                    glob=glob,
                    decision="allow",
                )
                if p_allowed:
                    preview_lines = _confirm_rule_preview(
                        format_rule_toml(rule)
                    )
            lines = _confirm_modal_lines(
                target,
                question=question,
                pending_reason=(
                    str(getattr(pending, "reason", "") or "")
                ),
                p_allowed=p_allowed,
                p_ban_reason=p_ban_reason,
            )
            self._set_busy_widget(True)
            raw_answer = await self._present_confirm(lines, preview_lines)
            if isinstance(raw_answer, tuple):
                code, reason = raw_answer
            else:
                code = (
                    "Y"
                    if str(raw_answer).strip().upper() in {"Y", "YES"}
                    else "N"
                )
                reason = ""
            answer = "Y" if code in {"Y", "A", "P"} else "N"
            if answer == "Y" and tool is not None:
                tool._ask_release = target
                if code == "A" and rule is not None:
                    tool._engine.add_session_rule(rule)
                    tool._engine.audit(
                        "secretary",
                        "add_session_rule",
                        target,
                        Decision(
                            verdict="allow",
                            tier=2,
                            reason="老板在确认弹窗选择：本次会话内放行同类",
                        ),
                        workspace=tool._workspace,
                    )
                elif code == "P" and rule is not None:
                    try:
                        append_global_rule(rule)
                    except Exception as exc:  # noqa: BLE001
                        answer = "N"
                        tool._ask_release = None
                        self._event(
                            f"永久规则写入失败，已按拒绝处理：{exc}",
                            "error",
                        )
                    else:
                        tool._engine.audit(
                            "secretary",
                            "add_global_rule",
                            target,
                            Decision(
                                verdict="allow",
                                tier=3,
                                reason="老板在确认弹窗选择：永久放行（规则已预览确认）",
                            ),
                            workspace=tool._workspace,
                        )
            elif tool is not None:
                tool._denied_targets.add(target)
                if reason:
                    tool._denied_reason = reason
            results.append(
                ConfirmResult(
                    confirmed=answer == "Y",
                    tool_call=tool_call,
                    rules=None,
                )
            )
            answered.append((target, answer))
        return (
            UserConfirmResultEvent(
                reply_id=confirm_event.reply_id,
                confirm_results=results,
            ),
            answered,
        )

    def _finish_reply(self, state: _StreamReply, elapsed: float) -> None:
        """回复落定：一条 assistant 记录（全文）+ 中断标记 + 元信息。"""
        interrupted = state.interrupted or self._interrupt_requested
        settled = False
        actual = state.actual
        if state.saw_thinking:
            # 思考正文不落成记录；只留这一条弱化事件行。全文只记在
            # 应用内存（键为该事件记录），供 thought_toggle 展开时重放。
            before = len(self.history)
            self._event(state.thinking_line(), "muted")
            if len(self.history) > before:
                self._thought_texts[id(self.history[before])] = (
                    state.all_thinking
                )
        if interrupted:
            # 已到达的文本落定为该条回复；一个字都没有就只留标记。
            if state.text:
                self._append_record("assistant", state.text, actual=actual)
                settled = True
            self._event(INTERRUPT_MARKER, "muted")
        elif state.final_text is not None or state.text:
            text = (
                state.final_text
                if state.final_text is not None
                else state.text
            )
            self._append_record(
                "assistant", text or NO_TEXT_REPLY, actual=actual
            )
            settled = True
        elif not state.error:
            # 既无文本也无异常：保持改动前的兜底文案。
            self._append_record("assistant", NO_TEXT_REPLY, actual=actual)
            settled = True
        if state.error:
            # 一条回复都没落定就失败时只报错误行，不伪造空回复。
            self._append_record("plain", state.error, "error", actual=actual)
        if settled:
            self._event(self._reply_meta(elapsed, state.output_tokens), "muted")

    def _track_tool_start(self, event: Any) -> None:
        """F2：工具调用开始记 running（H20 六态）。"""
        from .session_store import TOOL_RUNNING, ToolRun, now_iso

        call_id = str(getattr(event, "tool_call_id", "") or "")
        if not call_id:
            return
        self._tool_runs[call_id] = ToolRun(
            call_id=call_id,
            tool=str(getattr(event, "tool_call_name", "") or ""),
            args_summary="",
            state=TOOL_RUNNING,
            started_at=now_iso(),
        )

    def _track_tool_end(self, event: Any) -> None:
        from . import session_autosave
        from .session_store import now_iso

        call_id = str(getattr(event, "tool_call_id", "") or "")
        run = self._tool_runs.get(call_id)
        if run is None:
            return
        run.state = session_autosave.tool_state(getattr(event, "state", None))
        run.ended_at = now_iso()

    @staticmethod
    def _reply_meta(elapsed: float, tokens: int) -> str:
        """元信息行：耗时用 TUI 实测墙钟；token 只认实报值，不编造。"""
        meta = f"用时 {elapsed:.1f}s"
        if tokens > 0:
            meta += f" · 输出 {tokens} tok"
        return meta

    def _ensure_dispatcher(self) -> Any:
        if self._dispatcher is None:
            from .dispatcher import build_dispatcher

            model = (
                self._active_model
                if self._active_model is not None
                else self._injected_model
            )
            restored = getattr(self, "_restored_agent_state", None)
            self._restored_agent_state = None
            self._dispatcher = build_dispatcher(
                model=model,
                event_sink=self._on_platform_event,
                default_actors=self.actor_backend,
                workspace=self._dispatcher_workspace(),
                state=restored,
                secretary_files=True,
                confirm_responder=_tui_confirm_responder,
                mode=self._perm_mode,
            )
        return self._dispatcher

    # ------------------------------------------------------------------
    # 平台事件 → 消息流（CLI1 同款文案 + 绿/红圆点）
    # ------------------------------------------------------------------
    def _on_platform_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "tool_call":
            tool = event.get("tool")
            summary = event.get("summary")
            line = f"调用工具 {tool}"
            if summary:
                line += f"（{summary}）"
            self._event(line + " …", "accent")
        elif kind == "run_started":
            kb_state = "开启" if event.get("kb_enabled") else "关闭"
            self._event(
                f"工作副本: {event.get('workspace')}"
                f"（arm={event.get('arm')}, actors={event.get('actors')}, "
                f"kb={kb_state}）",
                "muted",
            )
        elif kind == "verdict":
            round_no = event.get("round", 0)
            label = "第 0 轮（首验）" if round_no == 0 else f"第 {round_no} 轮"
            passed = event.get("status") == "PASS"
            self._event(
                f"{label} 验收 {event.get('status')} — "
                f"{event.get('failed_count', 0)} 条失败",
                "success" if passed else "error",
            )
        elif kind == "dispatch":
            assignments = event.get("assignments", {})
            bits = "、".join(
                f"{actor_id}×{len(nodes)}"
                for actor_id, nodes in assignments.items()
            )
            self._event(
                f"派活（第 {event.get('round')} 轮）: {bits or '无'}",
                "warning",
            )
        elif kind == "actor_done":
            applied = bool(event.get("applied"))
            note = _short(event.get("note", ""))
            self._event(
                f"{event.get('actor_id')} 完成 — {note}",
                "success" if applied else "error",
            )
        elif kind == "run_finished":
            passed = event.get("outcome") == "PASS"
            self._event(
                f"收尾信号: outcome={event.get('outcome')} "
                f"轮次={event.get('rounds')} "
                f"遗留失败={len(event.get('final_failed', []))}",
                "success" if passed else "error",
            )
            # F2：一次运行结束也算一轮落定。
            self._autosave_threadsafe()


def run(
    workspace_root: str | Path | None = None,
    model: Any = None,
    perm_mode: str = "ask",
) -> int:
    """加载 .env.local 并启动全屏页面（供 CLI / 测试入口调用）。"""
    from .model_config import load_local_env

    load_local_env()
    CifixChatApp(
        model=model,
        workspace_root=workspace_root,
        perm_mode=perm_mode,
        interactive_gates=model is None,
    ).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.tui",
        description="Doctor Bug 全屏终端交互页：主治医生 + 平台工具。",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help="平台根目录，默认本仓库根目录",
    )
    args = parser.parse_args(argv)
    return run(workspace_root=args.workspace_root)


if __name__ == "__main__":
    raise SystemExit(main())
