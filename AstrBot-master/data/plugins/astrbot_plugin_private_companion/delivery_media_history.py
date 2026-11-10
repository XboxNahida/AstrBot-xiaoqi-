"""保存实际发送组件的顺序和独立原件，不把内部来源混入模型协议。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from pathlib import Path

from astrbot.api.message_components import File, Image, Plain, Record

from .task_attachments import _copy

file_history_directory: ContextVar[Path | None] = ContextVar("pc_sent_file_history_directory", default=None)


async def capture_sequence(chain, directory, *, send_index, timestamp):
    """发送成功后立即冻结附件；单个原件不可用不抹掉整条发送事实。"""
    directory = Path(directory)
    sequence = []
    for position, component in enumerate(chain):
        row = {"send_index": send_index, "position": position, "timestamp": timestamp}
        if isinstance(component, Plain):
            sequence.append({**row, "type": "text", "text": component.text})
            continue
        kind = next((name for cls, name in ((Image, "image"), (File, "file"), (Record, "record"))
                     if isinstance(component, cls)), None)
        if kind is None:
            continue
        row.update(type=kind, availability="unavailable")
        if kind == "file":
            row["name"] = str(component.name or "附件")
        try:
            if isinstance(component, Image):
                source = await component.convert_to_file_path()
            elif isinstance(component, File):
                source = await component.get_file()
            else:
                # 语音只记录位置；本轮不扩展音频历史读取接口。
                sequence.append(row)
                continue
            source = Path(source)
            if not source.is_file():
                raise FileNotFoundError(source)
            target = (file_history_directory.get() if isinstance(component, File) else None) or directory
            target.mkdir(parents=True, exist_ok=True)
            snapshot = await asyncio.to_thread(_copy, source, target, [source.parent], lambda: True)
            row.update(source=snapshot["path"], size=snapshot["size"],
                       sha256=snapshot["sha256"], availability="available")
        except Exception as exc:
            row["error_type"] = type(exc).__name__
        sequence.append(row)
    return sequence


def ordered_text(chain, fallback_text=""):
    """把发送组件顺序投影为简短上下文，实际来源只存元数据。"""
    if not any(isinstance(c, (Image, File)) for c in chain):
        return fallback_text
    parts = []
    counts = {"图片": 0, "文件": 0, "语音": 0}
    for component in chain:
        if isinstance(component, Plain):
            parts.append(component.text)
            continue
        label = next((name for cls, name in ((Image, "图片"), (File, "文件"), (Record, "语音"))
                      if isinstance(component, cls)), None)
        if label:
            counts[label] += 1
            name = f"：{component.name}" if isinstance(component, File) else ""
            parts.append(f"[机器人发送的{label}{counts[label]}{name}]")
    return "\n".join(parts).strip()
