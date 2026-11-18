"""应用内错误记录（TUIE2）：最近 20 条，退出即消失，不落盘。

每条：原文消息、异常类型（无异常记「无异常」）、简要堆栈（有异常才有，
只取最后 8 帧）、发生时间、触发来源（取不到记 None，界面写「未记录」）。

脱敏（入表即做，表里从不存原文敏感信息）：
- 形如 sk-xxx 的密钥、key/token/secret/password/authorization/bearer
  后面跟的值、当前进程环境变量里名字像密钥的值 → 「‹已脱敏›」；
- 用户主目录绝对路径 → 「~」；堆栈帧文件只显示相对路径
  （相对工作区 / 包目录 / site-packages），都不在则只留文件名。
"""

from __future__ import annotations

import os
import re
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAX_RECORDS = 20
MAX_FRAMES = 8
NO_EXCEPTION = "无异常"
REDACTED = "‹已脱敏›"

PLATFORM_ROOT = Path(__file__).resolve().parents[1]

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|passwd|"
        r"authorization|bearer)(\s*[:=]\s*|\s+)(\"?)([^\s\"',;]{4,})"
    ),
)


def _env_secret_values() -> list[str]:
    values = []
    for name, value in os.environ.items():
        if _SECRET_NAME.search(name) and value and len(value.strip()) >= 6:
            values.append(value.strip())
    # 长的先换，避免短值先把长值切碎后残留片段
    return sorted(set(values), key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for value in _env_secret_values():
        out = out.replace(value, REDACTED)
    out = _PATTERNS[0].sub(REDACTED, out)
    out = _PATTERNS[1].sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", out
    )
    home = str(Path.home())
    if home and home != "/":
        out = out.replace(home, "~")
    return out


def relative_frame_path(filename: str, roots: Iterable[Path] = ()) -> str:
    try:
        path = Path(filename).resolve()
    except (OSError, RuntimeError):
        return Path(filename).name
    for root in (*roots, PLATFORM_ROOT):
        try:
            return str(path.relative_to(Path(root).resolve()))
        except (ValueError, OSError, RuntimeError):
            continue
    parts = path.parts
    if "site-packages" in parts:
        index = len(parts) - 1 - parts[::-1].index("site-packages")
        return str(Path(*parts[index + 1 :])) if index + 1 < len(parts) else path.name
    return path.name


@dataclass(frozen=True)
class ErrorRecord:
    message: str
    exc_type: str
    frames: tuple[str, ...]
    at: str
    source: str | None

    @property
    def has_exception(self) -> bool:
        return self.exc_type != NO_EXCEPTION


def format_record(record: ErrorRecord) -> str:
    lines = [
        f"异常类型：{record.exc_type}",
        f"消息：{record.message}",
        f"时间：{record.at}",
        f"触发来源：{record.source or '未记录'}",
    ]
    if record.has_exception:
        lines.append(f"堆栈（最后 {len(record.frames)} 帧）：")
        lines.extend(f"  {frame}" for frame in record.frames)
    return "\n".join(lines)


class ErrorLog:
    def __init__(self, maxlen: int = MAX_RECORDS) -> None:
        self._records: deque[ErrorRecord] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> tuple[ErrorRecord, ...]:
        return tuple(self._records)

    def latest(self) -> ErrorRecord | None:
        return self._records[-1] if self._records else None

    def record(
        self,
        message: str,
        exc: BaseException | None = None,
        source: str | None = None,
        roots: Iterable[Path] = (),
    ) -> ErrorRecord:
        roots = tuple(roots)
        frames: tuple[str, ...] = ()
        if exc is not None:
            exc_type = type(exc).__name__
            extracted = traceback.extract_tb(exc.__traceback__)[-MAX_FRAMES:]
            frames = tuple(
                redact(
                    f"{relative_frame_path(frame.filename, roots)}:{frame.lineno}"
                    f" in {frame.name}"
                    + (f" — {frame.line.strip()}" if frame.line else "")
                )
                for frame in extracted
            )
        else:
            exc_type = NO_EXCEPTION
        entry = ErrorRecord(
            message=redact(message),
            exc_type=exc_type,
            frames=frames,
            at=time.strftime("%H:%M:%S"),
            source=source,
        )
        self._records.append(entry)
        return entry
