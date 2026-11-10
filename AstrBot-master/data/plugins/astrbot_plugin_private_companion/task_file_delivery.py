"""向真实来源会话投递工作区产物，批量文件可打包，不改变源文件。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

import jsonschema

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import File
from astrbot.core.message.message_event_result import MessageChain

from .delivery_media_history import file_history_directory
from .task_ordinary_read import TaskOrdinaryReader


class TaskArtifactReader(TaskOrdinaryReader):
    """交付产物不按字段名/扩展名拦截，只保护实际程序密钥及文件边界。"""

    def _checked(self, path):
        path = Path(os.path.abspath(path))
        for part in path.parts[1:]:
            if (
                ":" in part
                or part.rstrip(" .") != part
                or re.fullmatch(
                    r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part
                )
            ):
                raise PermissionError("unsafe_path")
        for entry in (*reversed(path.parents), path):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise PermissionError("redirected_path")
        return path


class TaskFileDelivery:
    """检查完整批次后制作独立快照，再调用实际平台发送器。

    reader提供既有敏感路径、文件句柄和实时授权检查；sender必须返回
    平台发送事实，不能把仅找到平台等同为成功。
    """

    max_files = 500
    max_bytes = 100 * 1024 * 1024

    def __init__(self, reader, sender):
        self.reader, self.sender = reader, sender

    def _path(self, root, raw):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("invalid_path")
        path = Path(os.path.abspath(root / raw))
        if not path.is_relative_to(root) or ".." in Path(raw).parts:
            raise PermissionError("outside_current_workspace")
        if self.reader.redact(str(path), len(str(path)) + 1) != str(path):
            raise PermissionError("filename_contains_program_credential")
        return self.reader._checked(path)

    def _scan(self, raw, root, budget, depth=0):
        text = (
            raw.decode("utf-16")
            if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
            else raw.decode("utf-8", errors="replace")
        )
        if self.reader.redact(text, len(text) + 1) != text:
            raise PermissionError("file_contains_credentials")
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            return
        if depth >= 4:
            raise ValueError("archive_nesting_limit")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for item in archive.infolist():
                name = item.filename.replace("\\", "/")
                self._path(root, name)
                if (item.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                    raise PermissionError("archive_link_forbidden")
                if item.is_dir():
                    continue
                budget[0] += 1
                budget[1] += item.file_size
                if budget[0] > self.max_files or budget[1] > self.max_bytes:
                    raise ValueError("archive_resource_limit")
                if item.flag_bits & 1:
                    raise PermissionError("encrypted_archive_not_inspectable")
                self._scan(archive.read(item), root, budget, depth + 1)

    def _snapshot(self, actor, paths, package, archive_name):
        self.reader.check_access(actor)
        if package not in {"auto", "zip", "individual"}:
            raise ValueError("invalid_package")
        if not isinstance(paths, list) or not 1 <= len(paths) <= self.max_files:
            raise ValueError("invalid_paths")
        if archive_name is not None and (
            not isinstance(archive_name, str)
            or not re.fullmatch(r'[^<>:"/\\|?*\x00-\x1f]+\.zip', archive_name, re.I)
            or archive_name.rstrip(" .") != archive_name
        ):
            raise ValueError("invalid_archive_name")
        root = self.reader._checked(
            Path(os.path.abspath(self.reader.current_workspace(actor)))
        )
        sources, had_directory, total, scan_budget = {}, False, 0, [0, 0]

        def traversal_error(error):
            raise error

        def collect(path):
            nonlocal total
            self.reader._checked(path)
            relative = path.relative_to(root).as_posix()
            if relative in sources:
                return
            if len(sources) >= self.max_files:
                raise ValueError("too_many_files")
            fd = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                self.reader._handle_path(fd, path)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(self.max_bytes - total + 1)
                self.reader._handle_path(fd, path)
            finally:
                os.close(fd)
            total += len(raw)
            if total > self.max_bytes:
                raise ValueError("batch_size_limit_100_mib")
            self._scan(raw, root, scan_budget)
            sources[relative] = raw

        for raw_path in paths:
            path = self._path(root, raw_path)
            if not path.is_dir():
                collect(path)
                continue
            had_directory = True
            for parent, dirs, files in os.walk(
                path, followlinks=False, onerror=traversal_error
            ):
                self.reader._checked(Path(parent))
                for name in dirs:
                    self.reader._checked(Path(parent) / name)
                for name in sorted(files):
                    collect(self._path(root, str(Path(parent) / name)))
        if not sources:
            raise ValueError("no_files_selected")
        self.reader.check_access(actor)
        manifest = [{"path": name, "size": len(raw)} for name, raw in sources.items()]
        if (
            package == "zip"
            or package == "auto"
            and (had_directory or len(sources) > 1)
        ):
            output = io.BytesIO()
            with zipfile.ZipFile(
                output, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name, raw in sources.items():
                    archive.writestr(name, raw)
            return [(archive_name or "任务文件.zip", output.getvalue())], manifest
        return [(Path(name).name, raw) for name, raw in sources.items()], manifest

    async def send(self, actor, session, *, paths, package="auto", archive_name=None):
        """返回逐项真实回执；不因不确定结果自动重发，也不推断任务已完成。"""
        try:
            prepared, manifest = await asyncio.to_thread(
                self._snapshot, actor, paths, package, archive_name
            )
        except PermissionError:
            return {
                "success": False,
                "code": "file_access_denied",
                "not_executed": True,
                "hint": "仅投递当前账号工作区；检查是否包含机器人实际配置的密钥、越界/链接/硬链接。普通业务配置和其他有用信息不因字段名被阻止。",
            }
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            return {
                "success": False,
                "code": "file_preparation_failed",
                "not_executed": True,
                "recoverable": True,
                "error_type": type(exc).__name__,
                "hint": "核对实际文件路径和ZIP名称；单批最多500个文件、100MiB，压缩包展开同限，嵌套最多4层。",
            }
        items = []
        with tempfile.TemporaryDirectory(prefix="astrbot_delivery_") as directory:
            for index, (name, raw) in enumerate(prepared):
                try:
                    self.reader.check_access(actor)
                except PermissionError:
                    items.append(
                        {"name": name, "status": "denied", "not_executed": True}
                    )
                    break
                item = {"name": name, "size": len(raw)}
                try:
                    snapshot = Path(directory) / str(index) / name
                    snapshot.parent.mkdir()
                    await asyncio.to_thread(snapshot.write_bytes, raw)
                    self.reader.check_access(actor)
                except (OSError, ValueError) as exc:
                    items.append(
                        {
                            **item,
                            "status": "not_sent",
                            "not_executed": True,
                            "code": "file_staging_failed",
                            "error_type": type(exc).__name__,
                        }
                    )
                    break
                try:
                    component = File(name=name, file=str(snapshot))
                    history_token = file_history_directory.set(
                        Path(self.reader.current_workspace(actor)) / "sent_media_history"
                    )
                    try:
                        receipt = await self.sender(session, MessageChain(chain=[component]))
                    finally:
                        file_history_directory.reset(history_token)
                    if not isinstance(receipt, dict) or receipt.get("status") != "sent":
                        item.update(
                            status="unknown",
                            may_have_sent=not isinstance(receipt, dict)
                            or not receipt.get("not_executed", False),
                        )
                        if isinstance(receipt, dict):
                            item.update(receipt)
                    else:
                        item.update(receipt)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    item.update(
                        status="unknown",
                        may_have_sent=True,
                        error_type=type(exc).__name__,
                        error=self.reader.redact(str(exc), 1000),
                    )
                items.append(item)
                if item["status"] != "sent":
                    break
        success = len(items) == len(prepared) and all(
            item["status"] == "sent" for item in items
        )
        return {
            "success": success,
            "code": "files_sent" if success else "file_delivery_incomplete",
            "items": items,
            "source_files": manifest,
            "pending_count": len(prepared) - len(items),
            "hint": "sent表示平台适配器确认发送，不表示用户已下载；unknown可能已发出，先核对，勿自动重复。",
        }


class FileDeliveryTool(FunctionTool):
    """身份和目标会话来自宿主，模型只能选择产物和打包方式。"""

    def __init__(self, service, actor, session, validate=None):
        super().__init__(
            name="pc_task_send_files",
            description=(
                "向当前真实用户所在会话发送已存在的工作区文件附件。paths可用相对路径或当前工作区绝对路径，"
                "支持目录。package=auto时单文件原样发，多文件或目录打成ZIP；zip强制打包，individual逐个发送。"
                "archive_name可指定中文.zip名称。不要把代码正文当文件交付，也不要只给服务器路径。"
                "先用pc_task_workspace发现实际路径。仅保护机器人实际配置的密钥，普通配置/源码/业务资料原样保留；"
                "文件中命中实际程序密钥才拒绝，ZIP内容也检查。链接和越界文件不发送。"
                "返回逐项平台回执；unknown不等于失败且可能已发，不自动重发。仅发送附件，不代表整个任务完成。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 500,
                    },
                    "package": {
                        "type": "string",
                        "enum": ["auto", "zip", "individual"],
                    },
                    "archive_name": {"type": "string"},
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        )
        self.service, self.actor, self.session, self.validate = (
            service,
            actor,
            session,
            validate,
        )

    async def call(self, context, **kwargs):
        try:
            jsonschema.validate(kwargs, self.parameters)
        except jsonschema.ValidationError:
            return json.dumps(
                {"success": False, "code": "invalid_arguments", "not_executed": True}
            )
        if self.validate is not None and not self.validate():
            return json.dumps(
                {"success": False, "code": "untrusted_event", "not_executed": True}
            )
        return json.dumps(
            await self.service.send(self.actor, self.session, **kwargs),
            ensure_ascii=False,
        )
