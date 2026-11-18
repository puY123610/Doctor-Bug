"""TUIB1：首页最近工作区（访问历史，不是信任记录）。

数据文件：``user_config_dir()/recents.json``（遵循 XDG_CONFIG_HOME）。
只记两列：解析后的绝对路径 + 最近进入时间。不记信任状态、密钥与配置。

规则写死：
- 路径先 ``Path.resolve()`` 再记，``~``/符号链接/相对路径指向同一目录只留一条；
- 同一目录再次进入：删旧条目，在队首重记（只留最新时间）；
- 上限 ``MAX_RECENTS`` 条，超了丢最旧的；
- 文件读不到 JSON / 结构不对：当空表，不崩溃，下一次成功记录时重写；
- 目录不存在或不是目录：不记。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .model_config import user_config_dir

MAX_RECENTS = 8
FILENAME = "recents.json"
TIME_FORMAT = "%Y-%m-%d %H:%M"
EMPTY_TEXT = "暂无最近工作区"


@dataclass(frozen=True)
class RecentEntry:
    path: str
    entered_at: str  # ISO 秒精度，本地时间

    def display_time(self) -> str:
        try:
            return datetime.fromisoformat(self.entered_at).strftime(TIME_FORMAT)
        except ValueError:
            return self.entered_at


def recents_path() -> Path:
    return user_config_dir() / FILENAME


def _target(file: Path | None) -> Path:
    return Path(file) if file is not None else recents_path()


def load(file: Path | None = None) -> list[RecentEntry]:
    """读表；文件缺失或损坏一律当空表。"""
    target = _target(file)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    items = raw.get("recents") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    entries: list[RecentEntry] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        when = item.get("entered_at")
        if not isinstance(path, str) or not path or not isinstance(when, str):
            continue
        if path in seen:
            continue
        seen.add(path)
        entries.append(RecentEntry(path, when))
    return entries[:MAX_RECENTS]


def save(entries: list[RecentEntry], file: Path | None = None) -> None:
    """原子写：临时文件写好再替换，写一半崩掉也不留半截 JSON。"""
    target = _target(file)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "recents": [
            {"path": e.path, "entered_at": e.entered_at}
            for e in entries[:MAX_RECENTS]
        ]
    }
    fd, tmp = tempfile.mkstemp(prefix=".recents-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(
    directory: str | Path,
    *,
    now: datetime | None = None,
    file: Path | None = None,
) -> list[RecentEntry] | None:
    """成功绑定后记一条。目录不存在/不是目录时不记，返回 None。"""
    resolved = Path(directory).expanduser().resolve()
    if not resolved.is_dir():
        return None
    stamp = (now or datetime.now()).replace(microsecond=0).isoformat()
    key = str(resolved)
    entries = [e for e in load(file) if e.path != key]
    entries.insert(0, RecentEntry(key, stamp))
    entries = entries[:MAX_RECENTS]
    save(entries, file)
    return entries


def prune(file: Path | None = None) -> list[RecentEntry]:
    """校验每条路径仍是存在的目录；失效条目从文件里清掉，返回有效条目。"""
    entries = load(file)
    valid = [e for e in entries if Path(e.path).is_dir()]
    if len(valid) != len(entries):
        save(valid, file)
    return valid


def shorten_home(path: str | Path) -> str:
    """显示用：主目录缩写为 ``~``。"""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if home and text.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + text[len(home.rstrip(os.sep)):]
    return text


def display_row(entry: RecentEntry) -> str:
    return f"{shorten_home(entry.path)}  {entry.display_time()}"
