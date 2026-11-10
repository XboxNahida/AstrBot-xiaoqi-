"""串行准备受限账户；仅由可信宿主调用，不接受模型命令或恢复指令。"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import socket
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

try:
    from .task_execution import (
        WindowsDeploymentVerifier,
        _check_token_sid,
        _host_acl_probe,
        actor_workspace,
    )
    from .task_permissions import TaskActor
except ImportError:
    from task_execution import (
        WindowsDeploymentVerifier,
        _check_token_sid,
        _host_acl_probe,
        actor_workspace,
    )
    from task_permissions import TaskActor


TASK_ROOT = Path("C:/AstrBotTasks")
BOT_ROOT = Path(__file__).resolve().parents[4]
BROKER_ROOT = Path("C:/ProgramData/AstrBotTaskBroker")
SCRIPT = BOT_ROOT / "deployment/配置受限任务执行.ps1"
_SETUP_LOCK = asyncio.Lock()
_CAPABILITIES = ("read", "write", "shell", "install")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountSetupResult:
    """提供状态码和失败账户；不包含密码或进程输出。"""

    ready: bool
    code: str
    failed_accounts: tuple[str, ...] = ()


class AccountSetupCoordinator:
    """协调可信 actor 的账户准备，不自动恢复历史失败账户。

    Args:
        permissions: 宿主持有的当前权限仓库。
        resolve_actor: 宿主按当前配置恢复真实身份，不能相信 candidate 的权限声明。
        runner: 仅供宿主测试注入的异步 (mode, actor) -> exit_code。
        task_root: 测试用根目录；生产 runner 固定使用 TASK_ROOT。
        broker_root: 测试用证据目录；生产固定使用 BROKER_ROOT。
        bot_root: 测试用保护目录；生产固定使用 BOT_ROOT。
    """

    def __init__(
        self,
        permissions,
        *,
        resolve_actor=None,
        runner=None,
        task_root=TASK_ROOT,
        broker_root=BROKER_ROOT,
        bot_root=BOT_ROOT,
    ):
        self.permissions = permissions
        self.resolve_actor = resolve_actor
        self.task_root, self.broker_root, self.bot_root = map(
            Path, (task_root, broker_root, bot_root)
        )
        if runner is None and (self.task_root, self.broker_root, self.bot_root) != (
            TASK_ROOT,
            BROKER_ROOT,
            BOT_ROOT,
        ):
            raise ValueError("Production account roots are fixed")
        self._production = runner is None
        self.runner = runner or _run_system_script

    def workspace(self, actor):
        """返回按真实身份派生的目录，不接受工作路径参数。"""
        return actor_workspace(self.task_root, actor)

    def _account(self, actor):
        return (
            "abt_"
            + hashlib.sha256(
                "\0".join((actor.bot_id, actor.group_id, actor.user_id)).encode("utf-8")
            ).hexdigest()[:16]
        )

    def _allowed(self, actor):
        return isinstance(actor, TaskActor) and any(
            self.permissions.check(actor, capability, self.workspace(actor))
            for capability in _CAPABILITIES
        )

    def _path(self, name, suffix):
        return self.broker_root / (name + suffix)

    def _record(self, path):
        for entry in (path, *path.parents):
            if entry.exists() and (
                entry.is_symlink()
                or getattr(entry.lstat(), "st_file_attributes", 0) & 0x400
            ):
                raise ValueError("Redirected host setup evidence")
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        actor = TaskActor(
            record["bot_id"], record["group_id"], record["user_id"], False
        )
        name = self._account(actor)
        if (
            not path.name.startswith(name + ".")
            or record.get("schema") != 1
            or Path(record.get("workspace", "")).resolve()
            != self.workspace(actor).resolve()
            or Path(record.get("credential", "")).resolve()
            != self._path(name, ".credential.dpapi").resolve()
            or tuple(Path(p).resolve() for p in record.get("protected_roots", ()))
            != (self.bot_root.resolve(), self.broker_root.resolve())
            or not str(record.get("user_sid", "")).startswith("S-1-5-21-")
        ):
            raise ValueError("Candidate identity binding differs")
        return actor, record

    def _audit(self, name, state):
        path = self._path(name, ".setup.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"state": state}), encoding="utf-8")
        temporary.replace(path)

    def _resolve(self, saved, requested):
        if (saved.bot_id, saved.group_id, saved.user_id) == (
            requested.bot_id,
            requested.group_id,
            requested.user_id,
        ):
            return requested
        if self.resolve_actor is None:
            return saved
        actor = self.resolve_actor(saved.bot_id, saved.group_id, saved.user_id)
        if not isinstance(actor, TaskActor) or (
            actor.bot_id,
            actor.group_id,
            actor.user_id,
        ) != (saved.bot_id, saved.group_id, saved.user_id):
            raise ValueError("Host actor resolution differs")
        return actor

    async def _verify(self, actor):
        name = self._account(actor)
        ready = self._path(name, ".ready.json")
        try:
            if not self._allowed(actor):
                raise PermissionError("Current account permission denied")
            self._audit(name, "verifying")
            if await self.runner("Verify", actor) != 0:
                raise RuntimeError("Account verification failed")
            _, record = self._record(ready)
            if (
                record.get("enabled") is not True
                or record.get("verification") != "passed"
            ):
                raise ValueError("Verification did not finalize host evidence")
            if not self._allowed(actor):
                raise PermissionError("Account permission revoked during verification")
            self._audit(name, "ready")
            return True
        except asyncio.CancelledError:
            ready.unlink(missing_ok=True)
            self._audit(name, "interrupted")
            raise
        except Exception:
            ready.unlink(missing_ok=True)
            self._audit(name, "failed")
            return False

    async def prepare(self, actor):
        """在后台执行前调用；不等待聊天消息循环，也不绕过权限。

        Returns:
            ready=True 只表示准备完成，实际工具仍须逐次 live attestation。
        """
        async with _SETUP_LOCK:
            if not self._allowed(actor):
                return AccountSetupResult(False, "permission_denied")
            if self._production:
                await asyncio.to_thread(_host_acl_probe, self.broker_root, ())
            name = self._account(actor)
            ready = self._path(name, ".ready.json")
            if ready.exists():
                try:
                    _, record = self._record(ready)
                    if (
                        record.get("enabled") is not True
                        or record.get("verification") != "passed"
                    ):
                        raise ValueError("Ready evidence is incomplete")
                    if self._production:
                        verifier = WindowsDeploymentVerifier(
                            self.broker_root,
                            protected_roots=(self.bot_root, self.broker_root),
                        )
                        await asyncio.to_thread(
                            verifier.verify, actor, self.workspace(actor)
                        )
                    return AccountSetupResult(True, "already_ready")
                except Exception as exc:
                    # 只记录本模块/验证器定义的固定原因，不记录命令、凭据或任意异常正文。
                    detail = str(exc)
                    known = {
                        "Candidate identity binding differs",
                        "Ready evidence is incomplete",
                        "Host deployment attestation is missing",
                        "Deployment attestation differs from task scope",
                        "Protected ACL tree changed during validation",
                        "Account or ACL deployment invariant changed",
                        "Host deployment ACL/account validation failed",
                        "ACL change escaped protected roots",
                        "ACL change has a reparsed ancestor",
                        "Protected ACL monitor could not be established",
                        "Protected ACL monitoring state is unknown",
                    }
                    logger.warning(
                        "Task account ready verification failed: account=%s reason=%s",
                        name, detail if detail in known else type(exc).__name__,
                    )
                    ready.unlink(missing_ok=True)
                    self._audit(name, "failed")
                    return AccountSetupResult(False, "audit_required")
            if any(
                self._path(name, suffix).exists()
                for suffix in (".candidate.json", ".credential.dpapi", ".setup.json")
            ):
                return AccountSetupResult(False, "audit_required")
            previous = []
            failed = []
            for path in sorted(self.broker_root.glob("*.ready.json")):
                candidate = path.with_name(
                    path.name.replace(".ready.json", ".candidate.json")
                )
                try:
                    saved, _ = self._record(candidate)
                    current = self._resolve(saved, actor)
                    if self._allowed(current):
                        previous.append(current)
                    else:
                        failed.append(path.name.removesuffix(".ready.json"))
                except Exception:
                    failed.append(path.name.removesuffix(".ready.json"))
                path.unlink(missing_ok=True)
            self._audit(name, "provisioning")
            try:
                provisioned = await self.runner("Provision", actor) == 0
                if provisioned:
                    self._record(self._path(name, ".candidate.json"))
                    previous.append(actor)
                else:
                    self._audit(name, "failed")
                for current in previous:
                    if not await self._verify(current):
                        failed.append(self._account(current))
                success = provisioned and ready.exists()
                return AccountSetupResult(
                    success, "ready" if success else "setup_failed", tuple(failed)
                )
            except asyncio.CancelledError:
                for current in previous:
                    self._path(self._account(current), ".ready.json").unlink(
                        missing_ok=True
                    )
                ready.unlink(missing_ok=True)
                self._audit(name, "interrupted")
                raise
            except Exception:
                ready.unlink(missing_ok=True)
                self._audit(name, "failed")
                for current in previous:
                    if not await self._verify(current):
                        failed.append(self._account(current))
                return AccountSetupResult(False, "setup_failed", tuple(failed))


async def _run_system_script(mode, actor):
    """固定命令启动后等待；取消及超时先结束整个 Job 再返回。"""
    starting = asyncio.create_task(asyncio.to_thread(_SystemSetupProcess, mode, actor))
    try:
        process = await asyncio.shield(starting)
    except BaseException:
        process = await asyncio.shield(starting)
        await asyncio.to_thread(process.close)
        raise
    try:
        async with asyncio.timeout(900):
            return await asyncio.to_thread(process.process.wait)
    finally:
        await asyncio.to_thread(process.close)


class _SystemSetupProcess:
    """挂起启动固定 PowerShell，先加入 kill-on-close Job 再恢复。"""

    def __init__(self, mode, actor):
        if os.name != "nt" or mode not in {"Provision", "Verify"}:
            raise PermissionError("Windows SYSTEM setup only")
        self.process, self.job = None, None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.kernel = kernel
        handle = wintypes.HANDLE
        for function, args, result in (
            (kernel.GetCurrentProcess, [], handle),
            (kernel.CreateJobObjectW, [ctypes.c_void_p, wintypes.LPCWSTR], handle),
            (kernel.CloseHandle, [handle], wintypes.BOOL),
            (kernel.TerminateJobObject, [handle, wintypes.UINT], wintypes.BOOL),
            (kernel.AssignProcessToJobObject, [handle, handle], wintypes.BOOL),
            (
                kernel.SetInformationJobObject,
                [handle, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            (
                advapi.OpenProcessToken,
                [handle, wintypes.DWORD, ctypes.POINTER(handle)],
                wintypes.BOOL,
            ),
            (ntdll.NtResumeProcess, [handle], wintypes.LONG),
        ):
            function.argtypes, function.restype = args, result
        token = handle()
        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), 8, ctypes.byref(token)
        ):
            raise PermissionError("Cannot verify setup host token")
        try:
            _check_token_sid(advapi, kernel, token, "S-1-5-18")
        finally:
            kernel.CloseHandle(token)

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        try:
            self.job = kernel.CreateJobObjectW(None, None)
            limits = Limits()
            limits.basic.flags = 0x2000
            if not self.job or not kernel.SetInformationJobObject(
                self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise OSError("Cannot establish setup Job")
            python = BOT_ROOT / "AstrBot-master/.venv/Scripts/python.exe"
            argv = [
                "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Mode",
                mode,
                "-TaskRoot",
                str(TASK_ROOT),
                "-BotRoot",
                str(BOT_ROOT),
                "-BrokerRoot",
                str(BROKER_ROOT),
                "-BotId",
                actor.bot_id,
                "-UserId",
                actor.user_id,
                "-Python",
                str(python),
            ]
            if actor.group_id:
                argv.extend(["-GroupId", actor.group_id])
            if mode == "Verify":
                argv.append("-FinalizeVerifiedDeployment")
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                cwd=BOT_ROOT,
                env={
                    "SystemRoot": "C:\\Windows",
                    "WINDIR": "C:\\Windows",
                    "PATH": "C:\\Windows\\System32",
                    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                    "COMPUTERNAME": socket.gethostname(),
                },
                creationflags=0x4 | 0x08000000,
            )
            native = handle(int(self.process._handle))
            if not kernel.AssignProcessToJobObject(self.job, native):
                raise OSError("Cannot assign setup process to Job")
            if ntdll.NtResumeProcess(native) != 0:
                raise OSError("Cannot resume setup process")
        except BaseException:
            self.close()
            raise

    def close(self):
        """终止整个 Job 并回收根进程；即使构造失败也可调用。"""
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)
            self.kernel.CloseHandle(self.job)
            self.job = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=30)
