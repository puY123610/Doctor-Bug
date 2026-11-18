"""/status 与 /mcp 弹窗的行数据（TUIE1）。

统一五态（文案写死）：正常 / 异常 / 未配置 / 未知 / 检查中。
本单只显示同步可得的状态，不做异步探测，所以「检查中」定义了但不使用；
需要联网才能确认的项一律标「未知」。

硬规矩：
- 每行都来自现成后端，取不到就标「未知」并写原因，绝不编；
- 「已配置」不等于「正常」：MCP 未测连通只能是「未知」；
- 密钥只问有无，只显示「已配置，未验证」，永不显示密钥值；
- MCP 命令与 env 值不出原文，只给命令名、参数个数、env 条数。
"""

from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

STATE_OK = "正常"
STATE_BAD = "异常"
STATE_UNSET = "未配置"
STATE_UNKNOWN = "未知"
STATE_CHECKING = "检查中"  # 本单不使用：不做异步探测
STATES: tuple[str, ...] = (
    STATE_OK,
    STATE_BAD,
    STATE_UNSET,
    STATE_UNKNOWN,
    STATE_CHECKING,
)

KEY_UNVERIFIED = "已配置，未验证"
MCP_NOT_CHECKED = "未检查连通性"

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_DIST_NAME = "frozen-accept-platform"


@dataclass(frozen=True)
class StatusRow:
    name: str
    state: str
    note: str

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"非法状态：{self.state!r}")


def format_rows(rows: Iterable[StatusRow]) -> str:
    return "\n".join(f"{row.name}：{row.state} · {row.note}" for row in rows)


# ----------------------------------------------------------------------
# 版本
# ----------------------------------------------------------------------
def version_row(pyproject: Path | None = None) -> StatusRow:
    """先读已安装包元数据；源码运行没装包时读 pyproject.toml（同一份来源）。"""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return StatusRow("版本", STATE_OK, f"{version(_DIST_NAME)}（包元数据）")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 - 元数据模块不可用时走 pyproject
        pass
    path = pyproject or (PLATFORM_ROOT / "pyproject.toml")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        value = data["project"]["version"]
    except Exception as exc:  # noqa: BLE001 - 读不到就如实标未知
        return StatusRow("版本", STATE_UNKNOWN, f"读不到版本号（{type(exc).__name__}）")
    return StatusRow("版本", STATE_OK, f"{value}（源码 pyproject.toml，包未安装）")


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------
def model_row(
    *,
    provider: str | None,
    model: str | None,
    effort_label: str,
    layer: str | None,
    last_actual: tuple[str, str, str | None] | None = None,
) -> StatusRow:
    """layer：会话覆盖 / 已切换档案 / 默认 / 测试替身；None 表示未配模型。"""
    if not model or layer is None:
        return StatusRow("模型", STATE_UNSET, "未配置模型（/model 或 drbug setup 配置）")
    note = f"{model}（{provider or '提供方未知'}）· 思考 {effort_label} · 生效层：{layer}"
    if last_actual is not None and (last_actual[0], last_actual[1]) != (
        provider or "",
        model,
    ):
        note += f" · 上次单次实际：{last_actual[1]}（{last_actual[0]}）"
    return StatusRow("模型", STATE_OK, note)


# ----------------------------------------------------------------------
# 密钥
# ----------------------------------------------------------------------
def key_row(
    *,
    key_env: str | None,
    key_present: bool,
    verified: bool = False,
    error: str | None = None,
) -> StatusRow:
    """只问有无。有值未验 → 未知「已配置，未验证」；无值 → 未配置。

    参数里没有密钥值这一项：这一层根本拿不到值，界面也就不可能显示。
    """
    if error is not None:
        return StatusRow("密钥", STATE_BAD, f"模型配置读不出：{error}")
    if not key_env:
        return StatusRow("密钥", STATE_UNSET, "没有可用的模型档案")
    if not key_present:
        return StatusRow("密钥", STATE_UNSET, f"环境变量 {key_env} 未设置")
    if verified:
        return StatusRow("密钥", STATE_OK, f"已验证（环境变量 {key_env}）")
    return StatusRow("密钥", STATE_UNKNOWN, f"{KEY_UNVERIFIED}（环境变量 {key_env}）")


# ----------------------------------------------------------------------
# 工作区 / 权限模式
# ----------------------------------------------------------------------
def workspace_row(active: Any, trusted: Iterable[str]) -> StatusRow:
    if active is None:
        return StatusRow("工作区", STATE_UNSET, "未绑定待命")
    path = str(active)
    trusted_set = {str(item) for item in trusted}
    try:
        resolved = str(Path(path).resolve())
    except (OSError, RuntimeError):
        resolved = path
    if path in trusted_set or resolved in trusted_set:
        trust = "本次会话已信任"
    else:
        trust = "启动时按项目目录自动绑定（本次会话未单独确认信任）"
    return StatusRow("工作区", STATE_OK, f"{path} · {trust}")


def mode_row(mode: str, labels: dict[str, str]) -> StatusRow:
    label = labels.get(mode)
    if label is None:
        return StatusRow("权限模式", STATE_BAD, f"未知模式值：{mode}")
    return StatusRow("权限模式", STATE_OK, f"{label}（{mode}）")


# ----------------------------------------------------------------------
# MCP 外接工具
# ----------------------------------------------------------------------
def _url_host(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "地址无法解析"
    host = parts.hostname or "?"
    # 只给协议 + 主机；路径、查询串、用户名口令都可能带凭据，不显示。
    return f"{parts.scheme}://{host}"


def _command_found(command: str) -> bool:
    path = Path(command).expanduser()
    if path.is_absolute() or "/" in command:
        return path.is_file()
    return shutil.which(command) is not None


def mcp_server_rows(
    config_path: Path | None = None,
) -> tuple[list[StatusRow], str | None]:
    """返回 (每台服务器一行, 整体错误)。整体错误非空时行列表为空。

    配置损坏 / 名字重复 / 结构不对：load_mcp_servers 抛 ValueError，
    原样作为异常原因（它的报错不含 env/headers 的值）。
    """
    from core import market
    from core.mcp_tools import load_mcp_servers

    path = Path(config_path) if config_path is not None else market.default_mcp_config_path()
    try:
        specs = load_mcp_servers(path)
    except ValueError as exc:
        return [], str(exc)
    rows: list[StatusRow] = []
    for spec in specs:
        name = spec["name"]
        if "url" in spec:
            headers = spec.get("headers") or {}
            structure = f"http · {_url_host(spec['url'])} · 请求头 {len(headers)} 条（值不显示）"
            rows.append(StatusRow(name, STATE_UNKNOWN, f"{structure} · {MCP_NOT_CHECKED}"))
            continue
        command = spec["command"]
        command_name = Path(command).name or "?"
        structure = (
            f"stdio · 命令 {command_name} · 参数 {len(spec.get('args') or [])} 个"
            f" · env {len(spec.get('env') or {})} 条（值不显示）"
        )
        if not _command_found(command):
            rows.append(
                StatusRow(name, STATE_BAD, f"{structure} · 命令在本机找不到")
            )
            continue
        rows.append(StatusRow(name, STATE_UNKNOWN, f"{structure} · {MCP_NOT_CHECKED}"))
    return rows, None


def mcp_summary_row(rows: list[StatusRow], error: str | None) -> StatusRow:
    if error is not None:
        return StatusRow("外接工具", STATE_BAD, f"MCP 配置异常：{error}")
    if not rows:
        return StatusRow("外接工具", STATE_UNSET, "未安装 MCP 服务器")
    checkable = sum(1 for row in rows if row.state in (STATE_OK, STATE_BAD))
    bad = sum(1 for row in rows if row.state == STATE_BAD)
    note = f"已装 {len(rows)} 台，其中状态可查 {checkable} 台"
    if bad:
        return StatusRow("外接工具", STATE_BAD, note + f"（{bad} 台异常，详见 /mcp）")
    return StatusRow("外接工具", STATE_UNKNOWN, note + f"（其余{MCP_NOT_CHECKED}）")


def mcp_modal_text(rows: list[StatusRow], error: str | None) -> str:
    if error is not None:
        return format_rows([StatusRow("MCP 配置", STATE_BAD, error)])
    if not rows:
        return "未配置 MCP 服务器"
    return format_rows(rows)


# ----------------------------------------------------------------------
# 市场
# ----------------------------------------------------------------------
def market_row() -> StatusRow:
    from core import market

    try:
        installed = market.installed_ids()
    except Exception as exc:  # noqa: BLE001 - 目录损坏等：如实标异常
        return StatusRow("市场", STATE_BAD, f"扩展目录读不出：{exc}")
    if not installed:
        return StatusRow("市场", STATE_UNSET, "未安装扩展")
    return StatusRow("市场", STATE_OK, f"已装 {len(installed)} 个扩展")


# ----------------------------------------------------------------------
# 本会话用量（G）：与侧边栏同一读取口 core.usage.summarize
# ----------------------------------------------------------------------
def usage_row(summary: Any = None) -> StatusRow:
    """没有调用记「未知」并写明暂无调用；有调用但全估算也照实标出。"""
    from .usage import summarize

    data = summary if summary is not None else summarize()
    if not data.calls:
        return StatusRow("本会话用量", STATE_UNKNOWN, data.one_line())
    return StatusRow("本会话用量", STATE_OK, data.one_line())
