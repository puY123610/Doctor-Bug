# -*- coding: utf-8 -*-
"""MARKET1：本地扩展目录——可浏览、可安装，不做在线商店。

目录是随平台发布的 market/catalog.json（条目以 MCP 服务器与
技能包为主）。安装 MCP 条目 = 把服务器规格登记进
~/.config/drbug/mcp.json（MCP1 的配置面）；装好不等于放行，
调用仍逐次过 PolicyEngine。安装技能包 = 把包复制进受管目录
~/.config/drbug/skills/<id>/。本模块不联网、不写任何策略规则。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.mcp_tools import DEFAULT_MCP_CONFIG_PATH, load_mcp_servers

_KIND_LABELS = {"mcp": "MCP 服务器", "skill": "技能包"}


def default_catalog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "market" / "catalog.json"


def default_mcp_config_path() -> Path:
    return DEFAULT_MCP_CONFIG_PATH


def default_skills_dir() -> Path:
    return Path.home() / ".config" / "drbug" / "skills"


def load_catalog(path=None) -> list[dict]:
    catalog_path = Path(path) if path is not None else default_catalog_path()
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # 没有目录文件 = 没有随包目录（例如精简安装）：市场为空，
        # 不能因为缺一个可选目录就让秘书起不来。文件在但读不出/
        # 写坏了仍按下面的错误路径报出来。
        return []
    except OSError as exc:
        raise ValueError(f"扩展目录读不到：{catalog_path}（{exc}）") from exc
    except ValueError as exc:
        raise ValueError(f"扩展目录不是合法 JSON：{catalog_path}（{exc}）") from exc
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("扩展目录缺 entries 列表")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("扩展目录里有非对象条目")
        entry_id = entry.get("id")
        if not entry_id or not entry.get("name"):
            raise ValueError(f"扩展条目缺 id 或 name：{entry!r}")
        if entry_id in seen:
            raise ValueError(f"扩展条目 id 重复：{entry_id}")
        seen.add(entry_id)
        kind = entry.get("kind")
        if kind not in _KIND_LABELS:
            raise ValueError(f"条目 {entry_id} 的 kind 不合法：{kind!r}")
        if kind == "mcp":
            server = entry.get("server")
            if (
                not isinstance(server, dict)
                or not server.get("name")
                or not (server.get("command") or server.get("url"))
            ):
                raise ValueError(f"MCP 条目 {entry_id} 缺 server.name/command")
        elif not entry.get("pack"):
            raise ValueError(f"技能包条目 {entry_id} 缺 pack 路径")
    return entries


def _find_entry(entries: list[dict], entry_id: str) -> dict:
    for entry in entries:
        if entry["id"] == entry_id:
            return entry
    raise ValueError(f"目录里没有这个条目：{entry_id}（先 drbug market list 看目录）")


def _read_mcp_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {"servers": []}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(
            f"现有 mcp.json 不是合法 JSON，已停手不覆盖：{config_path}（{exc}）"
        ) from exc
    if not isinstance(data, dict) or not isinstance(
        data.get("servers", []), list
    ):
        raise ValueError(f"现有 mcp.json 结构不对，已停手不覆盖：{config_path}")
    data.setdefault("servers", [])
    return data


def _write_mcp_config(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def installed_ids(*, mcp_config_path=None, skills_dir=None) -> set[str]:
    entries = load_catalog()
    config_path = (
        Path(mcp_config_path)
        if mcp_config_path is not None
        else default_mcp_config_path()
    )
    skills_root = (
        Path(skills_dir) if skills_dir is not None else default_skills_dir()
    )
    server_names: set[str] = set()
    if config_path.exists():
        try:
            server_names = {
                spec["name"] for spec in load_mcp_servers(config_path)
            }
        except ValueError:
            server_names = set()
    installed: set[str] = set()
    for entry in entries:
        if entry["kind"] == "mcp":
            if entry["server"]["name"] in server_names:
                installed.add(entry["id"])
        else:
            manifest = skills_root / entry["id"] / "manifest.json"
            if manifest.is_file():
                installed.add(entry["id"])
    return installed


def install(
    entry_id: str,
    *,
    catalog_path=None,
    mcp_config_path=None,
    skills_dir=None,
) -> str:
    entries = load_catalog(catalog_path)
    entry = _find_entry(entries, entry_id)
    if entry["kind"] == "mcp":
        config_path = (
            Path(mcp_config_path)
            if mcp_config_path is not None
            else default_mcp_config_path()
        )
        data = _read_mcp_config(config_path)
        server = entry["server"]
        if any(s.get("name") == server["name"] for s in data["servers"]):
            raise ValueError(f"已安装：{entry_id}（服务器 {server['name']} 已在 mcp.json 里）")
        new_server = {
            "name": server["name"],
            "command": server["command"],
            "args": list(server.get("args") or []),
        }
        if server.get("env"):
            new_server["env"] = dict(server["env"])
        data["servers"].append(new_server)
        _write_mcp_config(config_path, data)
        load_mcp_servers(config_path)  # 复读验证：写坏了立刻报错
        return (
            f"已安装 {entry_id}：服务器 {server['name']} 已登记进 {config_path}。"
            "调用仍需策略放行（安装不等于放行）。"
        )
    skills_root = (
        Path(skills_dir) if skills_dir is not None else default_skills_dir()
    )
    catalog_dir = (
        Path(catalog_path)
        if catalog_path is not None
        else default_catalog_path()
    ).parent
    source = catalog_dir / entry["pack"]
    if not source.is_dir():
        raise ValueError(f"技能包源目录不存在：{source}")
    target = skills_root / entry_id
    if target.exists():
        raise ValueError(f"已安装：{entry_id}（{target} 已存在）")
    skills_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return f"已安装 {entry_id}：技能包已复制到 {target}。"


def uninstall(
    entry_id: str,
    *,
    catalog_path=None,
    mcp_config_path=None,
    skills_dir=None,
) -> str:
    entries = load_catalog(catalog_path)
    entry = _find_entry(entries, entry_id)
    if entry["kind"] == "mcp":
        config_path = (
            Path(mcp_config_path)
            if mcp_config_path is not None
            else default_mcp_config_path()
        )
        data = _read_mcp_config(config_path)
        name = entry["server"]["name"]
        kept = [s for s in data["servers"] if s.get("name") != name]
        if len(kept) == len(data["servers"]):
            raise ValueError(f"未安装：{entry_id}（mcp.json 里没有 {name}）")
        data["servers"] = kept
        _write_mcp_config(config_path, data)
        return f"已卸载 {entry_id}：服务器 {name} 已从 {config_path} 移除。"
    skills_root = (
        Path(skills_dir) if skills_dir is not None else default_skills_dir()
    )
    target = skills_root / entry_id
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"未安装：{entry_id}（{target} 不存在或不是本市场装的）")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"拒绝卸载：{target} 的 manifest 读不出（{exc}）") from exc
    if manifest.get("id") != entry_id:
        raise ValueError(
            f"拒绝卸载：{target} 的 manifest id 是 {manifest.get('id')!r}，与 {entry_id} 不符"
        )
    shutil.rmtree(target)
    return f"已卸载 {entry_id}：已删除 {target}。"


def format_entry_line(entry: dict, installed: bool) -> str:
    mark = "已安装" if installed else "未安装"
    kind_label = _KIND_LABELS.get(entry["kind"], entry["kind"])
    return (
        f"[{mark}] {entry['id']} · {entry['name']} · {kind_label}"
        f" · {entry.get('description', '')}"
    )


def installed_skill_texts(
    *, skills_dir=None, max_chars: int = 4000
) -> str:
    """已安装技能包的 SKILL.md 正文拼成的参考段；无已装技能包时为空串。

    技能包是老板从扩展市场装回来的操作手册：秘书构建时带上，
    性质与项目简报相同——参考资料、不是指令。
    """
    root = (
        Path(skills_dir) if skills_dir is not None else default_skills_dir()
    )
    installed = installed_ids(skills_dir=root)
    parts: list[str] = []
    for entry in load_catalog():
        if entry["kind"]!= "skill" or entry["id"] not in installed:
            continue
        try:
            text = (root / entry["id"] / "SKILL.md").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            continue
        if text:
            parts.append(
                chr(0x3010) + "技能包 " + entry["id"] + " "
                + chr(0xB7) + " " + entry["name"] + chr(0x3011)
                + chr(10) + text
            )
    return (chr(10) + chr(10)).join(parts)[:max_chars]
