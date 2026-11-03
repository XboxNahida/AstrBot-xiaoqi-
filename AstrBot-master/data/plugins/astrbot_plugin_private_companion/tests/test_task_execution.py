import ast
import asyncio
import base64
import hashlib
import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from task_permissions import TaskActor, TaskPermissionStore


def test_execution_module_exists():
    assert importlib.util.find_spec("task_execution") is not None


def test_fixed_pathext_restores_ps51_native_command_pipeline():
    windows = Path(os.environ["SystemRoot"])
    powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    cmd = str(windows / "System32/cmd.exe")
    code = (
        "& '"
        + cmd.replace("'", "''")
        + "' /d /c 'echo native-pathext-probe & exit /b 7'; [Console]::WriteLine('probe-exit='+$LASTEXITCODE)"
    )
    baseline = {
        "SystemRoot": str(windows),
        "WINDIR": str(windows),
        "PATH": str(windows / "System32"),
    }
    missing = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", code],
        env=baseline,
        capture_output=True,
        timeout=15,
    )
    restored = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", code],
        env=dict(baseline, PATHEXT=".COM;.EXE;.BAT;.CMD"),
        capture_output=True,
        timeout=15,
    )
    assert b"probe-exit=7" not in missing.stdout
    assert b"native-pathext-probe" in restored.stdout
    assert b"probe-exit=7" in restored.stdout


def test_all_native_launch_environments_define_fixed_pathext(setup_broker):
    _, broker, actor, _, _, backend, _ = setup_broker
    asyncio.run(broker.run_powershell(actor, "Get-Date"))
    assert backend.calls[-1][3]["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    plugin = Path(__file__).resolve().parents[1]
    setup = (plugin / "task_account_setup.py").read_text(encoding="utf-8")
    script = plugin.parents[3] / "deployment" / "配置受限任务执行.ps1"
    verify = script.read_text(encoding="utf-8-sig")
    assert '"PATHEXT": ".COM;.EXE;.BAT;.CMD"' in setup
    assert "'PATHEXT':'.COM;.EXE;.BAT;.CMD'" in verify


@pytest.fixture
def setup_broker(tmp_path):
    api = importlib.import_module("task_execution")
    actor = TaskActor("bot", "group", "real-account", True)
    tasks = tmp_path / "tasks"
    root = api.actor_workspace(tasks, actor)
    root.mkdir(parents=True)
    protected = tmp_path / "bot"
    protected.mkdir()
    store = TaskPermissionStore(
        protected / "permissions.sqlite3",
        allowed_roots=[tasks],
        allowed_capabilities={"read", "write", "shell", "network", "install"},
    )

    class Verifier:
        def __init__(self):
            self.calls = 0
            self.valid = True

        def verify(self, who, workspace):
            self.calls += 1
            if not self.valid:
                raise PermissionError("deployment changed")
            assert who == actor and workspace == root
            return api.ExecutionIdentity("machine", "task-user", "S-1-5-21-1-2-3-4")

    class Process:
        def __init__(self):
            self.killed = False
            self.closed = False
            self.pending = False

        async def wait(self):
            if self.pending:
                await asyncio.Event().wait()
            return api.ExecutionResult(0, "ok", "")

        def kill_tree(self):
            self.killed = True

        def close(self):
            self.closed = True

    class Backend:
        def __init__(self):
            self.calls = []
            self.process = Process()
            self.started = threading.Event()

        def start(self, identity, argv, cwd, env, *, input_data=None):
            self.calls.append((identity, argv, cwd, env))
            self.started.set()
            return self.process

    backend, verifier = Backend(), Verifier()
    broker = api.TaskExecutionBroker(
        store,
        task_root=tasks,
        protected_roots=[protected],
        verifier=verifier,
        backend=backend,
        powershell=Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe",
    )
    return api, broker, actor, root, store, backend, verifier


def test_default_verifier_cannot_be_replaced_by_task_marker(setup_broker):
    api, broker, actor, root, _, backend, _ = setup_broker
    (root / "attestation.json").write_text('{"ready":true}', encoding="utf-8")
    broker.verifier = api.ClosedDeploymentVerifier()
    with pytest.raises(PermissionError, match="not attested"):
        asyncio.run(broker.run_powershell(actor, "Get-Date"))
    assert not backend.calls


def test_explicit_environment_and_command_payload(setup_broker, monkeypatch):
    _, broker, actor, root, _, backend, verifier = setup_broker
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    result = asyncio.run(broker.run_powershell(actor, "Write-Output '$env:PATH; hi'"))
    _, argv, cwd, env = backend.calls[-1]
    assert result.exit_code == 0 and cwd == root
    assert argv[-2:] == ("-Command", "Write-Output '$env:PATH; hi'")
    assert "-NoProfile" in argv and "-NonInteractive" in argv
    assert "OPENAI_API_KEY" not in env and "host-secret" not in str(env)
    assert set(env) == {
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATH",
        "PATHEXT",
        "USERPROFILE",
        "PIP_CONFIG_FILE",
        "PYTHONNOUSERSITE",
    }
    assert verifier.calls >= 1 and backend.process.closed


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_cancel_and_timeout_kill_entire_job(setup_broker, action):
    _, broker, actor, _, _, backend, _ = setup_broker
    backend.process.pending = True

    async def run():
        task = asyncio.create_task(
            broker.run_powershell(actor, "Start-Sleep 999", timeout=0.01)
        )
        if action == "cancel":
            assert await asyncio.to_thread(backend.started.wait, 1)
            task.cancel()
        with pytest.raises((asyncio.CancelledError, TimeoutError)):
            await task

    asyncio.run(run())
    assert backend.process.killed and backend.process.closed


def test_permission_and_attestation_rechecked_each_call(setup_broker):
    _, broker, actor, _, _, backend, verifier = setup_broker
    asyncio.run(broker.run_powershell(actor, "Get-Date"))
    verifier.valid = False
    with pytest.raises(PermissionError):
        asyncio.run(broker.run_powershell(actor, "Get-Date"))
    assert len(backend.calls) == 1


def test_member_requires_current_grant_for_every_operation(setup_broker):
    api, broker, owner, root, store, backend, _ = setup_broker
    member = TaskActor("bot", "group", "member", False)
    member_root = api.actor_workspace(broker.task_root, member)
    member_root.mkdir(parents=True)
    grant = store.grant(
        owner, member.user_id, roots=[member_root], capabilities={"shell", "network"}
    )
    store.revoke(owner, grant.grant_id)
    with pytest.raises(PermissionError):
        asyncio.run(broker.run_powershell(member, "Get-Date"))
    assert not backend.calls
    with pytest.raises(PermissionError):
        broker.checked_path(member, "read", root / "private.txt")


@pytest.mark.parametrize(
    "name", ["../escape", "C:\\Windows\\file", "file:stream", "CON", "file. "]
)
def test_file_path_rejects_traversal_device_and_streams(setup_broker, name):
    _, broker, actor, _, _, _, _ = setup_broker
    with pytest.raises((PermissionError, ValueError)):
        broker.checked_path(actor, "write", name)


def test_junction_cannot_reach_protected_root(setup_broker, tmp_path):
    _, broker, actor, root, _, _, _ = setup_broker
    target = tmp_path / "outside"
    target.mkdir()
    link = root / "escape"
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=True,
    )
    try:
        with pytest.raises(PermissionError):
            broker.checked_path(actor, "write", "escape/file.txt")
    finally:
        os.rmdir(link)


def test_file_operations_are_executed_under_task_account(setup_broker):
    _, broker, actor, root, _, backend, verifier = setup_broker
    asyncio.run(broker.write_text(actor, "result.txt", "a' ; $env:SECRET"))
    asyncio.run(broker.read_text(actor, "result.txt"))
    assert len(backend.calls) == 2 and verifier.calls >= 2
    assert not (root / "result.txt").exists()
    assert "a' ; $env:SECRET" not in backend.calls[0][1][-1]


def test_venv_install_never_uses_host_pip(setup_broker):
    _, broker, actor, root, _, backend, _ = setup_broker
    asyncio.run(broker.install_dependencies(actor, ["httpx==0.28.1"]))
    assert len(backend.calls) == 2
    payload = backend.calls[-1][1][-1]
    assert str(root / ".venv/Scripts/python.exe") in payload
    assert "--isolated" in payload and "-m" in payload and "pip" in payload
    with pytest.raises(ValueError):
        asyncio.run(broker.install_dependencies(actor, ["--target=C:\\bot"]))


def test_databases_and_protected_paths_cannot_be_task_owned(setup_broker):
    api, broker, _, root, store, _, _ = setup_broker
    store.db_path = root / "permissions.sqlite3"
    with pytest.raises(ValueError, match="database"):
        api.TaskExecutionBroker(
            store, task_root=broker.task_root, protected_roots=broker.protected_roots
        )


def test_identity_workspaces_are_not_display_names(setup_broker):
    api, broker, actor, root, _, _, _ = setup_broker
    assert root != api.actor_workspace(
        broker.task_root, TaskActor(actor.bot_id, "other-group", actor.user_id, True)
    )
    assert root != api.actor_workspace(
        broker.task_root, TaskActor("other-bot", actor.group_id, actor.user_id, True)
    )


def test_native_backend_is_not_a_host_subprocess_fallback(setup_broker):
    api, _, _, _, _, _, _ = setup_broker
    with pytest.raises(PermissionError):
        api.WindowsNativeBackend().start(
            api.ExecutionIdentity("host", "user", "sid"), ("cmd.exe",), Path.cwd(), {}
        )


def test_native_backend_checks_credentials_before_native_api(setup_broker):
    api, _, _, root, _, _, _ = setup_broker
    backend = api.WindowsNativeBackend(
        lambda _: ("host", "wrong-account", "not-a-real-password")
    )
    with pytest.raises(PermissionError, match="identity"):
        backend.start(
            api.ExecutionIdentity("host", "expected-account", "sid"),
            ("cmd.exe",),
            root,
            {},
        )


def test_network_permission_is_required_for_arbitrary_code(setup_broker):
    _, broker, actor, _, store, backend, _ = setup_broker
    store.allowed_capabilities = frozenset({"shell"})
    with pytest.raises(PermissionError):
        asyncio.run(broker.run_powershell(actor, "Invoke-WebRequest example.com"))
    assert not backend.calls


def test_deployment_verifier_denies_missing_host_attestation(setup_broker, tmp_path):
    api, _, actor, root, _, _, _ = setup_broker
    verifier = api.WindowsDeploymentVerifier(
        tmp_path / "host-broker", protected_roots=[tmp_path / "bot"]
    )
    with pytest.raises(PermissionError):
        verifier.verify(actor, root)


def test_dpapi_provider_has_no_plaintext_fallback(setup_broker, tmp_path):
    api, _, _, _, _, _, _ = setup_broker
    with pytest.raises(PermissionError):
        api.WindowsCredentialProvider(tmp_path / "missing.credential.dpapi")(
            api.ExecutionIdentity("host", "user", "sid")
        )


def test_fixed_file_tool_does_not_require_shell_or_network_grant(setup_broker):
    _, broker, actor, _, store, backend, _ = setup_broker
    store.allowed_capabilities = frozenset({"read"})
    asyncio.run(broker.read_text(actor, "text.txt"))
    assert len(backend.calls) == 1


def test_tools_listing_only_enumerates_fixed_tools_path(setup_broker):
    _, broker, actor, _, _, backend, _ = setup_broker
    asyncio.run(broker.list_tools(actor))
    code = backend.calls[-1][1][-1]
    assert (
        "Get-ChildItem" in code
        and "name=" in code
        and "size=" in code
        and "modified=" in code
    )


def test_shared_tools_are_not_writable_by_task_file_api(setup_broker):
    _, broker, actor, _, _, _, _ = setup_broker
    with pytest.raises(PermissionError):
        asyncio.run(broker.write_text(actor, "../../../../tools/tool.ps1", "bad"))


def test_read_tool_is_fixed_shared_path_and_bounded(setup_broker):
    _, broker, actor, _, _, backend, _ = setup_broker
    asyncio.run(broker.read_tool(actor, "helper.py"))
    assert "131072" in backend.calls[-1][1][-1]
    assert "ReadAllText" in backend.calls[-1][1][-1]
    for name in ("../secret", "a/b", "a:b", "CON", ".", "x. "):
        with pytest.raises((ValueError, PermissionError)):
            asyncio.run(broker.read_tool(actor, name))


def test_attestation_binds_machine_sid_and_acl_with_change_invalidation(
    setup_broker, tmp_path, monkeypatch
):
    api, _, actor, root, _, _, _ = setup_broker
    host = tmp_path / "host-broker"
    host.mkdir()
    protected = tmp_path / "bot"
    account = (
        "abt_"
        + hashlib.sha256(
            "\0".join((actor.bot_id, actor.group_id, actor.user_id)).encode()
        ).hexdigest()[:16]
    )
    (host / (account + ".credential.dpapi")).write_bytes(b"fake DPAPI envelope")
    record = {
        "enabled": True,
        "verification": "passed",
        "bot_id": actor.bot_id,
        "group_id": actor.group_id,
        "user_id": actor.user_id,
        "workspace": str(root),
        "protected_roots": [str(protected)],
        "machine_sid": "machine",
        "user_sid": "sid",
        "acl_sha256": "acl",
    }
    (host / (account + ".ready.json")).write_text(json.dumps(record), encoding="utf-8")
    scans = []
    live = dict(record, domain="host", admin=False)

    class Watch:
        validated = False
        dirty = False

        def changed(self):
            old, self.dirty = self.dirty, False
            return old

    watch = Watch()
    monkeypatch.setattr(api, "_ACL_GUARDS", {})
    monkeypatch.setattr(api, "_WindowsAclWatch", lambda _: watch)
    monkeypatch.setattr(api, "_check_account_rights", lambda _: None)

    def probe(root, paths, *, account="", scan=True):
        if account:
            scans.append(scan)
        return live

    monkeypatch.setattr(api, "_host_acl_probe", probe)
    verifier = api.WindowsDeploymentVerifier(host, protected_roots=[protected])
    verifier.verify(actor, root)
    verifier.verify(actor, root)
    assert scans == [True, False]
    watch.dirty = True
    verifier.verify(actor, root)
    assert scans[-1] is True
    live["machine_sid"] = "another-machine"
    with pytest.raises(PermissionError):
        verifier.verify(actor, root)


@pytest.mark.parametrize(
    "exists,owner,allow,expected",
    [
        (True, "S-1-5-18", "S-1-5-32-544", "preserved"),
        (False, "S-1-5-18", "S-1-5-32-544", "created"),
        (True, "S-1-5-32-545", "S-1-5-18", "denied"),
        (True, "S-1-5-18", "S-1-5-32-545", "denied"),
    ],
)
def test_broker_directory_initialization_preserves_existing_acl(
    exists, owner, allow, expected
):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    code = (
        r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('SCRIPT',[ref]$tokens,[ref]$errors)
$function=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Initialize-BrokerRoot'},$true)
if($null -eq $function){throw 'Initialize-BrokerRoot function is missing'}
Invoke-Expression $function.Extent.Text
$global:created=$false
function Test-Path { param($LiteralPath) return EXISTS }
function Assert-PlainPath { param($Path) }
function Set-PrivateDirectory { param($Path) $global:created=$true }
function Get-Acl {
 param($LiteralPath)
 $identity=New-Object Security.Principal.SecurityIdentifier('ALLOW')
 $deny=New-Object Security.Principal.SecurityIdentifier('S-1-5-21-1-2-3-1001')
 $acl=[pscustomobject]@{Access=@([pscustomobject]@{AccessControlType='Allow';IdentityReference=$identity},[pscustomobject]@{AccessControlType='Deny';IdentityReference=$deny})}
 $acl | Add-Member -MemberType ScriptMethod -Name GetOwner -Value {param($type) New-Object Security.Principal.SecurityIdentifier('OWNER')}
 return $acl
}
try { Initialize-BrokerRoot 'C:\fake-broker'; if($global:created){'created'}else{'preserved'} }
catch { 'denied' }
""".replace("SCRIPT", str(script))
        .replace("EXISTS", "$true" if exists else "$false")
        .replace("OWNER", owner)
        .replace("ALLOW", allow)
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("exit_code", [0, 7])
def test_verify_native_diagnostic_preserves_exit_and_stderr(exit_code):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    source = (
        script.read_text(encoding="utf-8-sig")
        .split("$testCode = @'", 1)[1]
        .split("\n'@", 1)[0]
    )
    tree = ast.parse(source)
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "native_probe"
            for target in node.targets
        )
    ]
    assert values, "Verify must expose the native diagnostic payload"
    helper = ast.literal_eval(values[0])
    command = (
        helper
        + f"; $r=Invoke-NativeDiagnostic -Exe $env:ComSpec -Arguments @('/d','/c','echo native-out & echo native-err 1>&2 & exit /b {exit_code}'); $r | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["exit_code"] == exit_code
    assert "native-out" in evidence["output"]
    assert "native-err" in evidence["output"]
    assert evidence["exception"] is None


@pytest.mark.parametrize(
    "probe_kind,expected_exit", [("directory", 0), ("readable", 2), ("writable", 3)]
)
def test_verify_protected_probes_preserve_success_and_denial(
    tmp_path, probe_kind, expected_exit
):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    source = (
        script.read_text(encoding="utf-8-sig")
        .split("$testCode = @'", 1)[1]
        .split("\n'@", 1)[0]
    )
    additions = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "code"
    ]
    probe = tmp_path / "protected-probe"
    if probe_kind == "directory":
        probe.mkdir()
    elif probe_kind == "readable":
        probe.write_text("fake protected data", encoding="utf-8")
    namespace = {"code": "", "probes": "'" + str(probe).replace("'", "''") + "'"}
    for node in additions:
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), str(script), "exec"),
            namespace,
        )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            namespace["code"].lstrip("; "),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == expected_exit, result.stderr


@pytest.mark.parametrize(
    "checked,target",
    [
        (r"C:\runtime\junction", r"C:\runtime\junction"),
        (r"C:\runtime\junction\python.exe", r"C:\runtime\junction"),
    ],
)
def test_reparse_refusal_identifies_actual_entry_without_bypass(checked, target):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    code = (
        r"""
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('SCRIPT',[ref]$tokens,[ref]$errors)
$f=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-PlainPath'},$true)
Invoke-Expression $f.Extent.Text
function Test-Path {param($LiteralPath) return $true}
function Get-Item {param($LiteralPath,[switch]$Force) if($LiteralPath -eq 'TARGET'){return [pscustomobject]@{Attributes=[IO.FileAttributes]::ReparsePoint}};return [pscustomobject]@{Attributes=[IO.FileAttributes]::Normal}}
try{Assert-PlainPath 'CHECKED'; 'unexpectedly-allowed'}catch{$_.Exception.Message}
""".replace("SCRIPT", str(script))
        .replace("TARGET", target)
        .replace("CHECKED", checked)
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert target in result.stdout
    assert "unexpectedly-allowed" not in result.stdout


def test_runtime_discovery_resolves_uv_alias_before_plain_tree_validation():
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    source = script.read_text(encoding="utf-8-sig")
    assert "Path(sys.base_prefix).resolve(strict=True)" in source
    assert source.index("Path(sys.base_prefix).resolve(strict=True)") < source.index(
        "Assert-PlainPath $basePython"
    )


@pytest.mark.parametrize(
    "sid,enabled,artifact,contents,expected",
    [
        ("same", False, False, ".runtime", "allowed"),
        ("other", False, False, ".runtime", "denied"),
        ("same", True, False, ".runtime", "denied"),
        ("same", False, True, ".runtime", "denied"),
        ("same", False, False, "business.txt", "denied"),
    ],
)
def test_resume_provision_requires_same_disabled_empty_attempt(
    sid, enabled, artifact, contents, expected
):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    code = (
        r"""
$ErrorActionPreference='Stop'; $tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('SCRIPT',[ref]$tokens,[ref]$errors)
$f=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-ResumeProvisionState'},$true)
if($null -eq $f){throw 'Resume validator is missing'}
Invoke-Expression $f.Extent.Text
function Assert-PlainPath {param($Path)}
function Get-LocalUser {param($Name) return [pscustomobject]@{SID=[pscustomobject]@{Value='SID'};Enabled=ENABLED}}
function Test-Path {param($LiteralPath) return ARTIFACT}
function Get-ChildItem {param($LiteralPath,[switch]$Force,[switch]$Recurse) return [pscustomobject]@{Name='CONTENTS';FullName='C:\fake\.runtime';PSIsContainer=$true}}
try{[void](Assert-ResumeProvisionState 'account' 'same' 'C:\fake' @('C:\host\vault')); 'allowed'}catch{'denied'}
""".replace("SCRIPT", str(script))
        .replace("SID", sid)
        .replace("ENABLED", "$true" if enabled else "$false")
        .replace("ARTIFACT", "$true" if artifact else "$false")
        .replace("CONTENTS", contents)
    )
    # Keep the property name SID intact while substituting the fake SID value.
    code = code.replace(sid + "=[pscustomobject]", "SID=[pscustomobject]")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_runtime_worker_handles_long_paths_and_resumes_without_overwrite(tmp_path):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    text = script.read_text(encoding="utf-8-sig")
    assert "$runtimeCode = @'" in text
    code = text.split("$runtimeCode = @'", 1)[1].split("\n'@", 1)[0]
    source = tmp_path / "source"
    source.mkdir()
    (source / "python.exe").write_bytes(b"fake interpreter")
    workspace = tmp_path / ("b" * 64) / ("g" * 64) / ("u" * 64)
    (workspace / ".runtime").mkdir(parents=True)
    (workspace / ".tmp").mkdir()
    long_file = source / "include" / "internal" / "pycore_blocks_output_buffer.h"
    long_file.parent.mkdir(parents=True)
    long_file.write_bytes(b"test header")

    def run(mode):
        request = base64.b64encode(
            json.dumps(
                {
                    "mode": mode,
                    "workspace": str(workspace),
                    "source": str(source),
                    "resume": True,
                }
            ).encode()
        ).decode()
        return subprocess.run(
            [sys.executable, "-I", "-c", code, request],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )

    assert run("validate").returncode == 0
    result = run("copy")
    assert result.returncode == 0, result.stderr
    copied = workspace / ".runtime/include/internal/pycore_blocks_output_buffer.h"
    assert len(str(copied)) > 260 and copied.read_bytes() == b"test header"
    assert run("copy").returncode == 0
    copied.write_bytes(b"do not overwrite")
    assert run("copy").returncode != 0
    assert copied.read_bytes() == b"do not overwrite"


@pytest.mark.parametrize(
    "rules,expected",
    [
        ("(D;ID;FA;;;TASK)(A;ID;FR;;;BU)", "False"),
        ("(A;;FR;;;BU)(D;ID;FA;;;TASK)", "True"),
        ("(A;;FA;;;SY)(A;;FA;;;BA)", "True"),
        ("(D;;FA;;;TASK)(A;;FR;;;BU)", "False"),
        ("(D;OIIOID;FA;;;TASK)", "True"),
    ],
)
def test_acl_optimization_only_skips_effective_safe_deny(rules, expected):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    code = r"""
$ErrorActionPreference='Stop'; $tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('SCRIPT',[ref]$tokens,[ref]$errors)
$f=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Set-TaskDenyIfMissing'},$true)
if($null -eq $f){throw 'ACL optimization function is missing'}
Invoke-Expression $f.Extent.Text
$global:acl=New-Object Security.AccessControl.DirectorySecurity
$global:acl.SetSecurityDescriptorSddlForm('D:RULES')
$global:writes=0
function Get-Acl {param($LiteralPath) return $global:acl}
function Set-Acl {param($LiteralPath,$AclObject) $global:writes++}
$item=[pscustomobject]@{FullName='C:\fake';PSIsContainer=$true}
$result=Set-TaskDenyIfMissing $item (New-Object Security.Principal.SecurityIdentifier('S-1-5-21-1-2-3-1001'))
if($global:writes -gt 1){throw 'Duplicate ACL writes'}
$result.ToString()
""".replace("SCRIPT", str(script)).replace(
        "RULES", rules.replace("TASK", "S-1-5-21-1-2-3-1001")
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_acl_optimization_observes_real_parent_inheritance(tmp_path):
    script = Path(__file__).resolve().parents[5] / "deployment" / "配置受限任务执行.ps1"
    root = tmp_path / "fake-protected-tree"
    root.mkdir()
    (root / "normal.txt").write_text("fake", encoding="utf-8")
    (root / "explicit.txt").write_text("fake", encoding="utf-8")
    code = r"""
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$ast=[System.Management.Automation.Language.Parser]::ParseFile('SCRIPT',[ref]$tokens,[ref]$errors)
$f=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Set-TaskDenyIfMissing'},$true)
Invoke-Expression $f.Extent.Text
$root='ROOT'
$explicit=Join-Path $root 'explicit.txt'
$acl=Get-Acl -LiteralPath $explicit
$everyone=New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($everyone,'Read','Allow')))
Set-Acl -LiteralPath $explicit -AclObject $acl
$sid=New-Object Security.Principal.SecurityIdentifier('S-1-5-21-1-2-3-1001')
$results=@()
foreach($path in @($root,(Join-Path $root 'normal.txt'),$explicit)) { $results += (Set-TaskDenyIfMissing (Get-Item -LiteralPath $path -Force) $sid) }
ConvertTo-Json -InputObject $results -Compress
""".replace("SCRIPT", str(script)).replace("ROOT", str(root))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, False, True]


@pytest.mark.parametrize(
    "rules,expected",
    [
        ("(D;ID;FA;;;TASK)(A;ID;FR;;;BU)", "allowed"),
        ("(D;OIIOID;FA;;;TASK)", "denied"),
        ("(A;;FR;;;BU)(D;;FA;;;TASK)", "denied"),
    ],
)
def test_host_acl_scanner_rejects_inherit_only_deny(rules, expected):
    plugin = Path(__file__).resolve().parents[1] / "task_execution.py"
    tree = ast.parse(plugin.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_host_acl_probe"
    )
    assignment = next(
        node
        for node in function.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "code"
            for target in node.targets
        )
    )
    script = assignment.value.func.value.value
    start = script.index("  $acl=Get-Acl")
    end = script.index("\n }", start)
    check = script[start:end]
    code = r"""
$ErrorActionPreference='Stop'
$taskSid='S-1-5-21-1-2-3-1001';$trusted=@('S-1-5-18','S-1-5-32-544')
$acl=New-Object Security.AccessControl.DirectorySecurity
$acl.SetSecurityDescriptorSddlForm('D:RULES')
$item=[pscustomobject]@{FullName='C:\fake'}
function Get-Acl {param($LiteralPath) return $acl}
try { CHECK; 'allowed' } catch { 'denied' }
""".replace("RULES", rules.replace("TASK", "S-1-5-21-1-2-3-1001")).replace(
        "CHECK", check
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
