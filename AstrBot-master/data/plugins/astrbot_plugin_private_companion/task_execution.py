"""Closed-by-default Windows task broker; this is not a filesystem namespace.

The host must configure an attested deployment and protected credential provider.
Standard users may read shared Windows/runtime files; task directories and bot
data are protected by real account ACLs, never merely by cwd or command filtering.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

try:
    from .task_permissions import TaskActor, TaskPermissionStore
except ImportError:
    from task_permissions import TaskActor, TaskPermissionStore


@dataclass(frozen=True)
class ExecutionIdentity:
    """Identify the account attested by trusted host deployment code."""

    domain: str
    username: str
    user_sid: str


@dataclass(frozen=True)
class ExecutionResult:
    """Return actual process evidence, without interpreting success of the task."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class DeploymentVerifier(Protocol):
    """Require fresh machine/SID/root/ACL/credential invariants on every call."""

    def verify(self, actor: TaskActor, workspace: Path) -> ExecutionIdentity: ...


class ClosedDeploymentVerifier:
    """Keep every capability closed until an audited host verifier is installed."""

    def verify(self, actor: TaskActor, workspace: Path) -> ExecutionIdentity:
        """Refuse execution regardless of markers in task-controlled directories.

        Raises:
            PermissionError: Production isolation has not been attested.
        """
        raise PermissionError(
            "Task account deployment is not attested; execution closed"
        )


class WindowsDeploymentVerifier:
    """Validate host-controlled ready evidence against live Windows state.

    Args:
        broker_root: SYSTEM/Administrators-only host directory.
        protected_roots: Exact bot/credential directories protected by deployment.
    """

    def __init__(self, broker_root: Path, *, protected_roots: Sequence[Path]):
        self.root = Path(broker_root).absolute()
        self.protected_roots = tuple(Path(p).resolve() for p in protected_roots)

    def verify(self, actor: TaskActor, workspace: Path) -> ExecutionIdentity:
        """Recheck evidence ownership, account SID, machine SID and ACL checksums.

        Raises:
            PermissionError: Evidence or live deployment invariants do not match.
        """
        account = (
            "abt_"
            + hashlib.sha256(
                "\0".join((actor.bot_id, actor.group_id, actor.user_id)).encode("utf-8")
            ).hexdigest()[:16]
        )
        ready = self.root / (account + ".ready.json")
        vault = self.root / (account + ".credential.dpapi")
        if not ready.is_file() or not vault.is_file():
            raise PermissionError("Host deployment attestation is missing")
        _host_acl_probe(self.root, (ready, vault))
        record = json.loads(ready.read_text(encoding="utf-8-sig"))
        if (
            record.get("enabled") is not True
            or record.get("verification") != "passed"
            or (record.get("bot_id"), record.get("group_id"), record.get("user_id"))
            != (actor.bot_id, actor.group_id, actor.user_id)
            or Path(record.get("workspace", "")).resolve() != workspace.resolve()
            or tuple(Path(p).resolve() for p in record.get("protected_roots", ()))
            != self.protected_roots
        ):
            raise PermissionError("Deployment attestation differs from task scope")
        with _ACL_GUARD_LOCK:
            key = (account, str(self.root), *(str(p) for p in self.protected_roots))
            guard = _ACL_GUARDS.get(key)
            if guard is None:
                guard = _WindowsAclWatch((self.root, *self.protected_roots))
                _ACL_GUARDS[key] = guard
            full_scan = not guard.validated or guard.changed()
            if full_scan:
                guard.validated = False
            live = _host_acl_probe(
                self.root,
                (workspace, *self.protected_roots),
                account=account,
                scan=full_scan,
            )
            if guard.changed():
                guard.validated = False
                raise PermissionError("Protected ACL tree changed during validation")
            guard.validated = True
        if (
            any(
                live.get(key) != record.get(key)
                for key in ("machine_sid", "user_sid", "acl_sha256")
            )
            or live.get("enabled") is not True
            or live.get("admin") is not False
        ):
            raise PermissionError("Account or ACL deployment invariant changed")
        _check_account_rights(live["user_sid"])
        return ExecutionIdentity(live["domain"], account, live["user_sid"])


class WindowsCredentialProvider:
    """Read an administrator-selected DPAPI vault after checking host ACL.

    Args:
        vault: Absolute host-controlled credential file outside task roots.
    """

    def __init__(self, vault: Path):
        self.vault = Path(vault).absolute()

    def __call__(self, identity: ExecutionIdentity):
        if os.name != "nt" or not self.vault.is_file():
            raise PermissionError("Protected task credential vault is missing")
        _host_acl_probe(self.vault.parent, (self.vault,))

        class Blob(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.DWORD),
                ("data", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt.CryptUnprotectData.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        crypt.CryptUnprotectData.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        encrypted = self.vault.read_bytes()
        data = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
        source, result = Blob(len(encrypted), data), Blob()
        if not crypt.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
        ):
            raise PermissionError("Task credential could not be decrypted")
        try:
            secret = json.loads(
                ctypes.string_at(result.data, result.length).decode("utf-8")
            )
            if (
                secret.get("domain"),
                secret.get("username"),
                secret.get("user_sid"),
            ) != (identity.domain, identity.username, identity.user_sid):
                raise PermissionError("Task credential binding changed")
            return secret["domain"], secret["username"], secret["password"]
        finally:
            ctypes.memset(result.data, 0, result.length)
            kernel.LocalFree(result.data)


_ACL_GUARDS = {}
_ACL_GUARD_LOCK = threading.Lock()


class _WindowsAclWatch:
    """Invalidate full scans after recursive create/rename/security ACL changes."""

    def __init__(self, roots):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.FindFirstChangeNotificationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.kernel.FindFirstChangeNotificationW.restype = wintypes.HANDLE
        self.kernel.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
        self.kernel.FindNextChangeNotification.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        self.handles = []
        self.validated = False
        for root in dict.fromkeys(roots):
            native = self.kernel.FindFirstChangeNotificationW(
                str(root), True, 1 | 2 | 0x100
            )
            if not native or native == ctypes.c_void_p(-1).value:
                self.close()
                raise PermissionError("Protected ACL monitor could not be established")
            self.handles.append(native)

    def changed(self):
        changed = False
        for native in self.handles:
            status = self.kernel.WaitForSingleObject(native, 0)
            if status == 0:
                changed = True
                if not self.kernel.FindNextChangeNotification(native):
                    raise PermissionError("Protected ACL monitoring failed")
            elif status != 0x102:
                raise PermissionError("Protected ACL monitoring state is unknown")
        return changed

    def close(self):
        for native in self.handles:
            self.kernel.FindCloseChangeNotification(native)
        self.handles.clear()


def _host_acl_probe(
    root: Path, paths: Sequence[Path], *, account: str = "", scan: bool = True
) -> dict:
    """Run fixed host ACL inspection without task payload or inherited host env."""
    if os.name != "nt" or any(not p.exists() or _is_reparse(p) for p in (root, *paths)):
        raise PermissionError("Host deployment path is missing or reparsed")
    if any(_is_reparse(parent) for path in (root, *paths) for parent in path.parents):
        raise PermissionError("Host deployment ancestor is reparsed")
    request = base64.b64encode(
        json.dumps(
            {
                "root": str(root),
                "paths": [str(p) for p in paths],
                "account": account,
                "scan": scan,
            }
        ).encode("utf-8")
    ).decode("ascii")
    code = r"""
$ErrorActionPreference='Stop'
$q=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('REQUEST')) | ConvertFrom-Json
if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne 'S-1-5-18') { throw 'SYSTEM required' }
$trusted=@('S-1-5-18','S-1-5-32-544')
$hostPaths=@($q.root)
if (-not $q.account) { $hostPaths += @($q.paths) }
foreach($p in $hostPaths) {
 $acl=Get-Acl -LiteralPath $p
 if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { throw 'Untrusted evidence owner' }
 foreach($r in $acl.Access) {
  $sid=$r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
  if ($r.AccessControlType -eq 'Allow' -and $sid -notin $trusted) { throw 'Host evidence readable by untrusted identity' }
 }
}
if (-not $q.account) { '{}' ; exit }
$u=Get-LocalUser -Name $q.account
$taskSid=$u.SID.Value
if($q.scan) { foreach($protected in @($q.paths | Select-Object -Skip 1)) {
 foreach($item in @((Get-Item -LiteralPath $protected -Force)) + @(Get-ChildItem -LiteralPath $protected -Force -Recurse -ErrorAction Stop)) {
  if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparsed protected tree' }
  $acl=Get-Acl -LiteralPath $item.FullName
  if(-not $acl.AreAccessRulesCanonical) { throw 'Protected ACL order is not canonical' }
  $rules=$acl.Access
  $denials=@($rules | Where-Object { $_.AccessControlType -eq 'Deny' -and $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $taskSid -and ([int]$_.FileSystemRights -band 2032127) -eq 2032127 -and -not ($_.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) })
  $explicitAllow=@($rules | Where-Object { $_.AccessControlType -eq 'Allow' -and -not $_.IsInherited -and $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $trusted }).Count -gt 0
  $denied=(@($denials | Where-Object {-not $_.IsInherited}).Count -gt 0) -or ($denials.Count -gt 0 -and -not $explicitAllow)
  if (-not $denied) { throw 'Protected tree lacks explicit task denial' }
 }
}
}
$admin=$false
foreach($g in Get-LocalGroup) {
 if ($g.SID.Value -ne 'S-1-5-32-545' -and (@(Get-LocalGroupMember -Group $g.Name | ForEach-Object {$_.SID.Value}) -contains $u.SID.Value)) { $admin=$true }
}
$sddl=''
foreach($p in $q.paths) { $sddl += (Get-Acl -LiteralPath $p).Sddl }
$h=[Security.Cryptography.SHA256]::Create()
try { $hash=([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($sddl)))).Replace('-','').ToLowerInvariant() } finally { $h.Dispose() }
@{machine_sid=$u.SID.AccountDomainSid.Value;user_sid=$u.SID.Value;domain=[Environment]::MachineName;enabled=$u.Enabled;admin=$admin;acl_sha256=$hash} | ConvertTo-Json -Compress
""".replace("REQUEST", request)
    ps = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    env = {
        "SystemRoot": str(ps.parents[3]),
        "WINDIR": str(ps.parents[3]),
        "PATH": str(ps.parents[3] / "System32"),
    }
    result = subprocess.run(
        [str(ps), "-NoProfile", "-NonInteractive", "-Command", code],
        env=env,
        capture_output=True,
        timeout=300 if account and scan else 30,
        creationflags=0x08000000,
    )
    if result.returncode:
        raise PermissionError("Host deployment ACL/account validation failed")
    return json.loads(result.stdout.decode("utf-8-sig"))


def _check_account_rights(sid: str) -> None:
    """Require batch logon and deny interactive, RDP and network logon."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class Unicode(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum", wintypes.USHORT),
            ("buffer", ctypes.c_void_p),
        ]

    class Attributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root", wintypes.HANDLE),
            ("name", ctypes.c_void_p),
            ("attributes", wintypes.ULONG),
            ("descriptor", ctypes.c_void_p),
            ("qos", ctypes.c_void_p),
        ]

    advapi.LsaOpenPolicy.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(Attributes),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.LsaEnumerateAccountRights.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(Unicode)),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi.LsaFreeMemory.argtypes = [ctypes.c_void_p]
    advapi.LsaClose.argtypes = [wintypes.HANDLE]
    advapi.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    pointer, policy, rights, count = (
        ctypes.c_void_p(),
        wintypes.HANDLE(),
        ctypes.POINTER(Unicode)(),
        wintypes.ULONG(),
    )
    attributes = Attributes()
    attributes.length = ctypes.sizeof(attributes)
    try:
        if not advapi.ConvertStringSidToSidW(
            sid, ctypes.byref(pointer)
        ) or advapi.LsaOpenPolicy(
            None, ctypes.byref(attributes), 0x800, ctypes.byref(policy)
        ):
            raise PermissionError("Task logon policy could not be inspected")
        if advapi.LsaEnumerateAccountRights(
            policy, pointer, ctypes.byref(rights), ctypes.byref(count)
        ):
            raise PermissionError("Task logon rights are missing")
        actual = {
            ctypes.wstring_at(rights[i].buffer, rights[i].length // 2)
            for i in range(count.value)
        }
        required = {
            "SeBatchLogonRight",
            "SeDenyInteractiveLogonRight",
            "SeDenyRemoteInteractiveLogonRight",
            "SeDenyNetworkLogonRight",
        }
        if not required <= actual or not actual <= required | {
            "SeDenyServiceLogonRight"
        }:
            raise PermissionError("Task logon policy is incomplete")
    finally:
        if rights:
            advapi.LsaFreeMemory(rights)
        if policy:
            advapi.LsaClose(policy)
        if pointer:
            kernel.LocalFree(pointer)


def actor_workspace(task_root: str | Path, actor: TaskActor) -> Path:
    """Derive bot/group/account directories without accepting path-like identities.

    Args:
        task_root: Host-approved task root, normally C:\\AstrBotTasks.
        actor: Identity obtained from the real platform event.

    Returns:
        A deterministic directory independent of nicknames or model arguments.
    """
    parts = (actor.bot_id, actor.group_id, actor.user_id)
    return (
        Path(task_root)
        .absolute()
        .joinpath(*(hashlib.sha256(part.encode("utf-8")).hexdigest() for part in parts))
    )


class TaskExecutionBroker:
    """Apply fresh grants and host deployment policy before isolated operations.

    Args:
        permissions: Host-owned SQLite grants store, outside task roots.
        task_root: Host-configured root, never supplied by a model.
        protected_roots: Bot install, data, credentials, and broker directories.
        verifier: Trusted deployment invariant verifier; default denies all calls.
        backend: Native process provider; fake providers are only for unit tests.
        powershell: Trusted absolute Windows PowerShell executable.
    """

    def __init__(
        self,
        permissions: TaskPermissionStore,
        *,
        task_root: str | Path,
        protected_roots: Sequence[str | Path],
        verifier: DeploymentVerifier | None = None,
        backend=None,
        powershell: Path | None = None,
    ) -> None:
        self.permissions = permissions
        self.task_root = Path(task_root).absolute()
        self.protected_roots = tuple(Path(p).resolve() for p in protected_roots)
        if permissions.db_path.resolve().is_relative_to(self.task_root.resolve()):
            raise ValueError("Permission database must be host-owned outside task root")
        if not self.protected_roots or any(
            self.task_root.resolve().is_relative_to(p)
            or p.is_relative_to(self.task_root.resolve())
            for p in self.protected_roots
        ):
            raise ValueError("Task and protected roots must be disjoint")
        self.verifier = verifier or ClosedDeploymentVerifier()
        self.backend = backend or WindowsNativeBackend()
        self.powershell = powershell or Path(
            "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        if not self.powershell.is_absolute():
            raise ValueError("PowerShell must be an absolute trusted host path")

    def checked_path(
        self,
        actor: TaskActor,
        capability: str,
        relative: str = ".",
        *,
        _verify: bool = True,
    ) -> Path:
        """Validate one path and current authorization immediately before use.

        Args:
            actor: Trusted platform identity.
            capability: Current requested capability.
            relative: Task-relative path; device names, ADS and links are rejected.

        Returns:
            The validated absolute path in this exact actor's workspace.

        Raises:
            PermissionError: Grant, path, or deployment invariant is invalid.
        """
        path = Path(relative)
        if (
            path.is_absolute()
            or ":" in str(relative)
            or str(relative).startswith(("\\", "/"))
        ):
            raise PermissionError("Only task-relative file paths are allowed")
        for part in path.parts:
            if (
                part == ".."
                or part.rstrip(" .") != part
                or re.fullmatch(
                    r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part
                )
            ):
                raise PermissionError("Unsafe Windows path component")
        root = actor_workspace(self.task_root, actor)
        requested = root / path
        # Reject every reparse point, including junctions that resolve inside root.
        # This is defense in depth; an OS broker still must prevent link races.
        for entry in (self.task_root, *self.task_root.parents):
            if entry.exists() and _is_reparse(entry):
                raise PermissionError("Reparse task root is forbidden")
        current = self.task_root
        for part in requested.relative_to(self.task_root).parts:
            current /= part
            if current.exists() and _is_reparse(current):
                raise PermissionError("Reparse task path is forbidden")
        resolved = requested.resolve()
        if not root.resolve().is_relative_to(
            self.task_root.resolve()
        ) or not resolved.is_relative_to(root.resolve()):
            raise PermissionError("Path escaped actor workspace")
        if any(resolved.is_relative_to(p) for p in self.protected_roots):
            raise PermissionError("Bot install, data and credentials are protected")
        if not self.permissions.check(actor, capability, resolved):
            raise PermissionError("Current task grant denied this operation")
        if _verify:
            self.verifier.verify(actor, root)
        return resolved

    async def run_powershell(
        self,
        actor: TaskActor,
        code: str,
        *,
        timeout: float = 60,
        capability: str = "shell",
    ) -> ExecutionResult:
        """Run an explicit PowerShell payload as the attested standard user.

        Args:
            actor: Trusted actor, not model-provided authority.
            code: PowerShell source passed as a single argv payload.
            timeout: Finite upper bound in seconds, at most one hour.
            capability: Required tool capability; shell remains mandatory.

        Returns:
            Actual process exit code and bounded captured output.

        Raises:
            PermissionError: The fresh grant or deployment check fails.
            TimeoutError: Process job exceeded the limit and was terminated.
            asyncio.CancelledError: Entire process job is terminated on cancellation.
        """
        return await self._run_payload(
            actor,
            code,
            timeout=timeout,
            capability=capability,
            required=("shell", "network"),
        )

    async def _run_payload(
        self,
        actor: TaskActor,
        code: str,
        *,
        timeout: float,
        capability: str,
        required: tuple[str, ...] = (),
    ) -> ExecutionResult:
        """Run host-generated payloads; public arbitrary code always checks shell."""
        if not isinstance(code, str) or not code or "\0" in code:
            raise ValueError("A nonempty NUL-free command payload is required")
        if not 0 < timeout <= 3600:
            raise ValueError("Timeout must be within (0, 3600]")
        root = await asyncio.to_thread(
            self.checked_path, actor, capability, _verify=False
        )
        for extra in required:
            await asyncio.to_thread(self.checked_path, actor, extra, _verify=False)
        identity = await asyncio.to_thread(self.verifier.verify, actor, root)
        windows = self.powershell.parents[3]
        temp = root / ".tmp"
        env = {
            "SystemRoot": str(windows),
            "WINDIR": str(windows),
            "TEMP": str(temp),
            "TMP": str(temp),
            "PATH": str(windows / "System32"),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "USERPROFILE": str(root),
            "PIP_CONFIG_FILE": "NUL",
            "PYTHONNOUSERSITE": "1",
        }
        starting = asyncio.create_task(
            asyncio.to_thread(
                self.backend.start,
                identity,
                (
                    str(self.powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    code,
                ),
                root,
                env,
            )
        )
        try:
            process = await asyncio.shield(starting)
        except BaseException:
            # Cancelling a worker-thread await cannot cancel CreateProcessAsUser.
            # Acquire its eventual handle and terminate before propagating cancel.
            process = await asyncio.shield(starting)
            process.kill_tree()
            process.close()
            raise
        try:
            return await asyncio.wait_for(process.wait(), timeout=timeout)
        except BaseException:
            process.kill_tree()
            raise
        finally:
            process.close()

    async def read_text(self, actor: TaskActor, relative: str) -> ExecutionResult:
        """Read UTF-8 through the task account; host never opens user-selected files.

        Returns:
            Process result containing file text in stdout.
        """
        target = await asyncio.to_thread(
            self.checked_path, actor, "read", relative, _verify=False
        )
        encoded = base64.b64encode(str(target).encode("utf-8")).decode("ascii")
        return await self._run_payload(
            actor,
            "$ErrorActionPreference='Stop'; $p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
            + encoded
            + "')); [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "[Console]::Write([IO.File]::ReadAllText($p,[Text.Encoding]::UTF8))",
            capability="read",
            timeout=60,
        )

    async def write_text(
        self, actor: TaskActor, relative: str, content: str
    ) -> ExecutionResult:
        """Write UTF-8 as the task account, never under the host SYSTEM token.

        Returns:
            Actual process result; caller must check exit_code.
        """
        target = await asyncio.to_thread(
            self.checked_path, actor, "write", relative, _verify=False
        )
        path64 = base64.b64encode(str(target).encode("utf-8")).decode("ascii")
        data64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return await self._run_payload(
            actor,
            "$ErrorActionPreference='Stop'; $p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
            + path64
            + "')); [IO.File]::WriteAllBytes($p,[Convert]::FromBase64String('"
            + data64
            + "'))",
            capability="write",
            timeout=60,
        )

    async def install_dependencies(
        self, actor: TaskActor, requirements: Sequence[str]
    ) -> ExecutionResult:
        """Create a local venv then invoke its isolated pip, never host pip.

        Args:
            actor: Trusted actor with shell/install/network grants.
            requirements: Explicit distribution specifications, not pip options.

        Returns:
            Actual pip exit result, or the failed venv creation result.
        """
        if not requirements or any(
            not isinstance(item, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\[\],<>=!~+-]{0,199}", item)
            for item in requirements
        ):
            raise ValueError("Only explicit package requirements are accepted")
        root = await asyncio.to_thread(
            self.checked_path, actor, "install", _verify=False
        )
        python = root / ".venv/Scripts/python.exe"
        # The interpreter is provisioned inside this account's workspace by the
        # deployment administrator; it is never the bot's credential-bearing venv.
        bootstrap = root / ".runtime/python.exe"
        created = await self.run_powershell(
            actor,
            f"& '{bootstrap}' -I -m venv '{root / '.venv'}'; exit $LASTEXITCODE",
            capability="install",
            timeout=300,
        )
        if created.exit_code:
            return created
        args = " ".join("'" + item + "'" for item in requirements)
        return await self.run_powershell(
            actor,
            f"& '{python}' -I -m pip --isolated install --no-input {args}; exit $LASTEXITCODE",
            capability="install",
            timeout=600,
        )

    async def list_tools(self, actor: TaskActor) -> ExecutionResult:
        """List the host-managed shared tools directory through a task account.

        Returns:
            JSON stdout with name, size and modified fields; no parent listing.
        """
        await asyncio.to_thread(self.checked_path, actor, "read", _verify=False)
        target = self.task_root / "tools"
        if target.exists() and _is_reparse(target):
            raise PermissionError("Shared tools directory cannot be a reparse point")
        encoded = base64.b64encode(str(target).encode("utf-8")).decode("ascii")
        code = (
            "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
            + encoded
            + "')); if(Test-Path -LiteralPath $p) { $r=@(Get-ChildItem -LiteralPath $p -File | Where-Object {-not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)} | ForEach-Object { @{name=$_.Name;size=$_.Length;modified=$_.LastWriteTimeUtc.ToString('o')} }); ConvertTo-Json -InputObject $r -Compress } else { '[]' }"
        )
        return await self._run_payload(actor, code, timeout=60, capability="read")

    async def read_tool(self, actor: TaskActor, name: str) -> ExecutionResult:
        """Read one shared UTF-8 tool file, at most 128 KiB, as the task account.

        Args:
            actor: Trusted actor with current read permission.
            name: A plain filename, never an absolute or parent-relative path.

        Returns:
            Process evidence with bounded script text in stdout.
        """
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or len(name) > 180
            or name.rstrip(" .") != name
            or any(c in name for c in '/\\:<>"|?*\0')
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)
        ):
            raise ValueError("A plain shared tool filename is required")
        await asyncio.to_thread(self.checked_path, actor, "read", _verify=False)
        target = self.task_root / "tools" / name
        if any(p.exists() and _is_reparse(p) for p in (target.parent, target)):
            raise PermissionError("Shared tool path cannot be reparsed")
        encoded = base64.b64encode(str(target).encode("utf-8")).decode("ascii")
        code = (
            "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
            + encoded
            + "')); $f=Get-Item -LiteralPath $p -Force; if($f.PSIsContainer -or ($f.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $f.Length -gt 131072){throw 'Tool must be a plain UTF-8 file up to 131072 bytes'}; [Console]::Write([IO.File]::ReadAllText($p,(New-Object Text.UTF8Encoding($false,$true))))"
        )
        return await self._run_payload(actor, code, timeout=60, capability="read")


def _is_reparse(path: Path) -> bool:
    """Inspect Windows reparse attributes without following the selected entry."""
    return (
        bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        or path.is_symlink()
    )


class WindowsNativeBackend:
    """Start a job-owned standard-user process from SYSTEM without host env.

    Args:
        credentials: Trusted provider invoked only after host attestation; it must
            verify ACL and DPAPI binding each call and return (domain,user,password).
            It must never accept a filename or password from model/tool arguments.
    """

    def __init__(self, credentials: Callable | None = None) -> None:
        self.credentials = credentials

    def start(
        self,
        identity: ExecutionIdentity,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
    ):
        """Create suspended, assign kill-on-close Job, then resume the child.

        Raises:
            PermissionError: Host credential provider is absent or mismatched.
            OSError: A native call fails; no fallback to host subprocess exists.
        """
        if os.name != "nt" or self.credentials is None:
            raise PermissionError(
                "Native task execution requires protected credentials"
            )
        domain, username, password = self.credentials(identity)
        if (domain, username) != (identity.domain, identity.username):
            raise PermissionError(
                "Credential identity differs from deployment attestation"
            )
        return _NativeProcess(identity, domain, username, password, argv, cwd, env)


class _NativeProcess:
    """Own every native handle and tear down the entire job on every exit path."""

    def __init__(self, identity, domain, username, password, argv, cwd, env):
        # ctypes definitions are local so Linux/test import never loads WinDLL.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel, self.handles, self.job, self.process = kernel, [], None, None
        self.reads, self._reader_tasks = [], []
        self.output_limit = 1024 * 1024
        handle = wintypes.HANDLE
        size = ctypes.c_size_t

        class Startup(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("reserved", wintypes.LPWSTR),
                ("desktop", wintypes.LPWSTR),
                ("title", wintypes.LPWSTR),
                *[
                    (name, wintypes.DWORD)
                    for name in (
                        "x",
                        "y",
                        "xsize",
                        "ysize",
                        "xchars",
                        "ychars",
                        "fill",
                        "flags",
                    )
                ],
                ("show", wintypes.WORD),
                ("reserved_size", wintypes.WORD),
                ("reserved2", ctypes.c_void_p),
                ("stdin", handle),
                ("stdout", handle),
                ("stderr", handle),
            ]

        class StartupEx(ctypes.Structure):
            _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [
                ("process", handle),
                ("thread", handle),
                ("pid", wintypes.DWORD),
                ("tid", wintypes.DWORD),
            ]

        class Security(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.DWORD),
                ("descriptor", ctypes.c_void_p),
                ("inherit", wintypes.BOOL),
            ]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_work", size),
                ("max_work", size),
                ("active", wintypes.DWORD),
                ("affinity", size),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_mem", size),
                ("job_mem", size),
                ("peak_process", size),
                ("peak_job", size),
            ]

        signatures = [
            (kernel.CloseHandle, [handle], wintypes.BOOL),
            (kernel.CreateJobObjectW, [ctypes.c_void_p, wintypes.LPCWSTR], handle),
            (
                kernel.SetInformationJobObject,
                [handle, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            (kernel.AssignProcessToJobObject, [handle, handle], wintypes.BOOL),
            (kernel.TerminateJobObject, [handle, wintypes.UINT], wintypes.BOOL),
            (kernel.TerminateProcess, [handle, wintypes.UINT], wintypes.BOOL),
            (kernel.ResumeThread, [handle], wintypes.DWORD),
            (kernel.WaitForSingleObject, [handle, wintypes.DWORD], wintypes.DWORD),
            (
                kernel.GetExitCodeProcess,
                [handle, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
            (
                kernel.CreatePipe,
                [
                    ctypes.POINTER(handle),
                    ctypes.POINTER(handle),
                    ctypes.POINTER(Security),
                    wintypes.DWORD,
                ],
                wintypes.BOOL,
            ),
            (
                kernel.SetHandleInformation,
                [handle, wintypes.DWORD, wintypes.DWORD],
                wintypes.BOOL,
            ),
            (
                kernel.ReadFile,
                [
                    handle,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            (
                kernel.CreateFileW,
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(Security),
                    wintypes.DWORD,
                    wintypes.DWORD,
                    handle,
                ],
                handle,
            ),
            (
                kernel.InitializeProcThreadAttributeList,
                [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(size)],
                wintypes.BOOL,
            ),
            (
                kernel.UpdateProcThreadAttribute,
                [
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    size,
                    ctypes.c_void_p,
                    size,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            (kernel.DeleteProcThreadAttributeList, [ctypes.c_void_p], None),
            (
                advapi.LogonUserW,
                [
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(handle),
                ],
                wintypes.BOOL,
            ),
            (
                advapi.CreateProcessAsUserW,
                [
                    handle,
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.LPCWSTR,
                    ctypes.POINTER(StartupEx),
                    ctypes.POINTER(ProcessInfo),
                ],
                wintypes.BOOL,
            ),
        ]
        for func, args, result in signatures:
            func.argtypes, func.restype = args, result
        token, info, attributes = handle(), ProcessInfo(), None

        def checked(ok):
            if not ok:
                # Native error code only; password, argv and env are never logged.
                raise OSError(
                    ctypes.get_last_error(), "Windows task process operation failed"
                )

        try:
            # LOGON32_LOGON_BATCH requires batch logon, not interactive/remote.
            checked(
                advapi.LogonUserW(username, domain, password, 4, 0, ctypes.byref(token))
            )
            self.handles.append(token)
            # The host credential verifier must already bind the account SID;
            # additionally compare the actual logon token SID before spawning.
            _check_token_sid(advapi, kernel, token, identity.user_sid)
            self.job = kernel.CreateJobObjectW(None, None)
            checked(self.job)
            self.handles.append(self.job)
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            checked(
                kernel.SetInformationJobObject(
                    self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
            security = Security(ctypes.sizeof(Security), None, True)
            writers = []
            for _ in range(2):
                read, write = handle(), handle()
                checked(
                    kernel.CreatePipe(
                        ctypes.byref(read),
                        ctypes.byref(write),
                        ctypes.byref(security),
                        0,
                    )
                )
                self.handles.extend([read, write])
                checked(kernel.SetHandleInformation(read, 1, 0))
                self.reads.append(read)
                writers.append(write)
            stdin = kernel.CreateFileW(
                "NUL", 0x80000000, 3, ctypes.byref(security), 3, 0, None
            )
            checked(stdin and stdin != ctypes.c_void_p(-1).value)
            self.handles.append(stdin)
            needed = size()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(needed))
            attribute_buffer = ctypes.create_string_buffer(needed.value)
            attributes = ctypes.cast(attribute_buffer, ctypes.c_void_p)
            checked(
                kernel.InitializeProcThreadAttributeList(
                    attributes, 1, 0, ctypes.byref(needed)
                )
            )
            inherited = (handle * 3)(stdin, writers[0], writers[1])
            checked(
                kernel.UpdateProcThreadAttribute(
                    attributes,
                    0,
                    0x20002,
                    inherited,
                    ctypes.sizeof(inherited),
                    None,
                    None,
                )
            )
            startup = StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = (
                inherited
            )
            startup.attributes = attributes
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = ctypes.create_unicode_buffer(
                "\0".join(
                    f"{k}={v}"
                    for k, v in sorted(env.items(), key=lambda item: item[0].upper())
                )
                + "\0\0"
            )
            # No console, no profile load, no parent environment. Handle-list
            # inheritance excludes all unrelated host handles.
            checked(
                advapi.CreateProcessAsUserW(
                    token,
                    argv[0],
                    command,
                    None,
                    None,
                    True,
                    0x80000 | 0x400 | 0x4 | 0x08000000,
                    environment,
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(info),
                )
            )
            self.process = info.process
            self.handles.extend([info.process, info.thread])
            checked(kernel.AssignProcessToJobObject(self.job, self.process))
            checked(kernel.ResumeThread(info.thread) != 0xFFFFFFFF)
            for write in [*writers, stdin]:
                kernel.CloseHandle(write)
                self.handles.remove(write)
        except BaseException:
            if info.process:
                kernel.TerminateProcess(info.process, 1)
            self.close()
            raise
        finally:
            if attributes:
                kernel.DeleteProcThreadAttributeList(attributes)
            password = None

    def _read(self, pipe):
        data = bytearray()
        buffer = ctypes.create_string_buffer(8192)
        count = wintypes.DWORD()
        while (
            self.kernel.ReadFile(pipe, buffer, len(buffer), ctypes.byref(count), None)
            and count.value
        ):
            # Continue draining after limit so a verbose child cannot deadlock.
            remaining = self.output_limit - len(data)
            if remaining > 0:
                data.extend(buffer.raw[: min(remaining, count.value)])
        return bytes(data).decode("utf-8", errors="replace")

    async def wait(self):
        """Drain both streams while polling so cancellation never orphans a job."""
        self._reader_tasks = [
            asyncio.create_task(asyncio.to_thread(self._read, pipe))
            for pipe in self.reads
        ]
        streams = asyncio.gather(*self._reader_tasks)
        try:
            while True:
                status = self.kernel.WaitForSingleObject(self.process, 0)
                if status == 0:
                    break
                if status != 0x102:
                    raise OSError("Waiting for task process failed")
                await asyncio.sleep(0.05)
            self.kill_tree()
            stdout, stderr = await asyncio.shield(streams)
        finally:
            # Do not close/reuse pipe handles while a reader thread owns them.
            self.kill_tree()
            await asyncio.shield(streams)
        exit_code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, ctypes.byref(exit_code)):
            raise OSError("Reading task exit code failed")
        return ExecutionResult(exit_code.value, stdout, stderr)

    def kill_tree(self):
        """Terminate all descendants before closing native resources."""
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)

    def close(self):
        """Close the Job even on exceptions; kill-on-close is the final backstop."""
        self.kill_tree()
        for native_handle in reversed(self.handles):
            self.kernel.CloseHandle(native_handle)
        self.handles.clear()
        self.job = None


def _check_token_sid(advapi, kernel, token, expected):
    """Match the actual logged-on token to the trusted attested SID."""
    needed = wintypes.DWORD()
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise PermissionError("Task token identity could not be verified")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi.GetTokenInformation(
        token, 1, buffer, needed.value, ctypes.byref(needed)
    ):
        raise PermissionError("Task token identity could not be verified")
    sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    sid_string = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
        raise PermissionError("Task token SID could not be verified")
    try:
        if sid_string.value != expected:
            raise PermissionError("Actual task token SID differs from attestation")
    finally:
        kernel.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))
    elevation = wintypes.DWORD()
    if (
        not advapi.GetTokenInformation(
            token,
            20,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(needed),
        )
        or elevation.value
    ):
        raise PermissionError("Task process token must not be elevated")
