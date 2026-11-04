"""Exercise the protected-tree policy against actual Windows ACLs."""

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration test")
def test_native_tree_scan_preserves_denial_and_reparse_policy(tmp_path):
    from task_acl_scan import WINDOWS_ACL_SCAN

    script = (
        WINDOWS_ACL_SCAN
        + r"""
$ErrorActionPreference='Stop'
$base='ROOT'
$sid=New-Object Security.Principal.SecurityIdentifier('S-1-5-21-1-2-3-1001')
$result=@{}
foreach($case in @('valid','missing','inherit_only','explicit_allow','explicit_deny','junction','junction_child')) {
 $root=Join-Path $base $case
 [void](New-Item -ItemType Directory -Path $root)
 if($case -ne 'missing') {
  $acl=[IO.Directory]::GetAccessControl($root)
  $propagation=if($case -eq 'inherit_only'){'InheritOnly'}else{'None'}
  $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit',$propagation,'Deny')))
  [IO.Directory]::SetAccessControl($root,$acl)
 }
 $child=Join-Path $root 'child.txt'
 [IO.File]::WriteAllText($child,'synthetic input')
 if($case -in @('explicit_allow','explicit_deny')) {
  $acl=[IO.File]::GetAccessControl($child)
  $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')))
  if($case -eq 'explicit_deny'){$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Deny')))}
  [IO.File]::SetAccessControl($child,$acl)
 }
 $junction=Join-Path $root 'redirect'
 try {
  if($case -in @('junction','junction_child')){[void](New-Item -ItemType Junction -Path $junction -Target $base)}
  $scan=if($case -eq 'junction_child'){Join-Path $junction 'valid/child.txt'}else{$root}
  try { [AstrBotProtectedAcl]::Verify(@($scan),$sid.Value); $result[$case]=$true } catch { $result[$case]=$false }
 } finally {if(Test-Path -LiteralPath $junction){[IO.Directory]::Delete($junction)}}
}
try {[AstrBotProtectedAcl]::Verify(@((Join-Path $base 'removed.txt')),$sid.Value,$true);$result['removed']=$true}catch{$result['removed']=$false}
try {[AstrBotProtectedAcl]::Verify(@((Join-Path $base 'removed.txt')),$sid.Value);$result['missing_root']=$true}catch{$result['missing_root']=$false}
$result | ConvertTo-Json -Compress
""".replace("ROOT", str(tmp_path).replace("'", "''"))
    )
    ps = (
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    env = dict(os.environ)
    env.pop("PSModulePath", None)
    result = subprocess.run(
        [str(ps), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "valid": True,
        "missing": False,
        "inherit_only": False,
        "explicit_allow": False,
        "explicit_deny": True,
        "junction": False,
        "junction_child": False,
        "removed": True,
        "missing_root": False,
    }
