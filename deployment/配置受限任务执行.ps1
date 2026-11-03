[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Provision', 'Verify')][string]$Mode = 'Plan',
    [string]$TaskRoot = 'C:\AstrBotTasks',
    [string]$BotRoot = 'C:\Test_Program\AstrBot',
    [string]$BrokerRoot = 'C:\ProgramData\AstrBotTaskBroker',
    [string]$BotId,
    [string]$GroupId = '',
    [string]$UserId,
    [string]$Python,
    [switch]$ResumeProvision,
    [string]$ExpectedAccountSid,
    [switch]$FinalizeVerifiedDeployment
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-HexHash([string]$Text) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Assert-PlainPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    if ($current.StartsWith('\\') -or $current.Substring(2).Contains(':')) { throw ('路径必须是本机磁盘上的普通绝对路径：' + $current) }
    while ($current) {
        if ((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw ('检测到 reparse point，停止部署。实际条目：' + $current + '；原始检查路径：' + $Path) }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
}

function Set-PrivateDirectory([string]$Path, [string]$TaskSid = '') {
    if (-not (Test-Path -LiteralPath $Path)) { [void](New-Item -ItemType Directory -Path $Path) }
    Assert-PlainPath $Path
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'))) {
        $sid = New-Object Security.Principal.SecurityIdentifier($entry[0])
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    if ($TaskSid) {
        $sid = New-Object Security.Principal.SecurityIdentifier($TaskSid)
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Initialize-BrokerRoot([string]$Path) {
    Assert-PlainPath $Path
    if (-not (Test-Path -LiteralPath $Path)) {
        Set-PrivateDirectory $Path
        return
    }
    # Never reset an existing broker ACL: earlier actors require their explicit
    # Deny entries to remain effective after another account is provisioned.
    $trusted = @('S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $Path
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) {
        throw '已有 broker 目录所有者不可信；停止而不重置权限。'
    }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $trusted) {
            throw '已有 broker 目录允许非 SYSTEM/Administrators 访问；停止而不重置权限。'
        }
    }
}

function Set-TaskDenyIfMissing($Item, [Security.Principal.SecurityIdentifier]$TaskSid) {
    $acl = Get-Acl -LiteralPath $Item.FullName
    if (-not $acl.AreAccessRulesCanonical) { throw ('ACL顺序非规范，拒绝跳过或重排：' + $Item.FullName) }
    $explicitDeny = $false
    $inheritedDeny = $false
    $explicitUntrustedAllow = $false
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq 'Allow' -and -not $rule.IsInherited -and $rule.IdentityReference.Value -notin @('S-1-5-18', 'S-1-5-32-544')) { $explicitUntrustedAllow = $true }
        if ($rule.AccessControlType -eq 'Deny' -and $rule.IdentityReference.Value -eq $TaskSid.Value -and ([int]$rule.FileSystemRights -band 2032127) -eq 2032127 -and -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) {
            if ($rule.IsInherited) { $inheritedDeny = $true } else { $explicitDeny = $true }
        }
    }
    if ($explicitDeny -or ($inheritedDeny -and -not $explicitUntrustedAllow)) { return $false }
    if ($Item.PSIsContainer) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($TaskSid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Deny')
    } else {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($TaskSid, 'FullControl', 'Deny')
    }
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Item.FullName -AclObject $acl
    return $true
}

function Invoke-RuntimeTree([string]$Python, [string]$Workspace, [string]$Source, [bool]$Resume, [string]$Operation) {
    $runtimeCode = @'
import base64, hashlib, json, os, shutil, stat, sys
from pathlib import Path
q=json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
def extended(value):
    text=str(Path(value).absolute())
    prefix=chr(92)*2+'?'+chr(92)
    if os.name=='nt' and not text.startswith(prefix):
        text=prefix+text
    return Path(text)
def plain(path):
    for item in (path,*path.parents):
        if item.exists() and (item.is_symlink() or getattr(item.lstat(),'st_file_attributes',0)&1024):
            raise RuntimeError('Reparse entry rejected: '+str(item))
def entries(root):
    plain(root)
    for folder,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            path=Path(folder)/name
            plain(path)
            if not path.is_dir() and not stat.S_ISREG(path.lstat().st_mode): raise RuntimeError('Nonregular runtime entry: '+str(path))
            yield path
def digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').digest()
root=extended(q['workspace'])
plain(root)
if q['mode']=='validate':
    for item in root.iterdir():
        plain(item)
        if item.name not in ('.runtime','.tmp') or not item.is_dir(): raise RuntimeError('Business entry blocks resume: '+str(item))
        for child in entries(item): pass
        if item.name=='.tmp' and any(item.iterdir()): raise RuntimeError('Nonempty temp blocks resume')
else:
    source=extended(q['source'])
    plain(source)
    if not (source/'python.exe').is_file(): raise RuntimeError('Runtime interpreter is missing')
    destination=root/'.runtime'
    for existing in entries(destination): pass
    source_entries=list(entries(source))
    for entry in source_entries:
        target=destination/entry.relative_to(source)
        plain(target)
        if entry.is_dir():
            target.mkdir(parents=True,exist_ok=True)
        elif target.exists():
            if not q['resume'] or not target.is_file() or digest(entry)!=digest(target): raise RuntimeError('Runtime mismatch; refusing overwrite: '+str(target))
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            with entry.open('rb') as src,target.open('xb') as dst: shutil.copyfileobj(src,dst)
print('Runtime tree operation passed: '+q['mode'])
'@
    $request = @{mode=$Operation; workspace=$Workspace; source=$Source; resume=$Resume} | ConvertTo-Json -Compress
    $payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($request))
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($runtimeCode))
    & $Python -I -c ('import base64;exec(base64.b64decode(''' + $encoded + '''))') $payload
    if ($LASTEXITCODE -ne 0) { throw ('Python 长路径运行时检查/复制失败：' + $Operation) }
}

function Assert-ResumeProvisionState([string]$Account, [string]$ExpectedSid, [string]$Workspace, [string[]]$Artifacts, [string]$RuntimePython = '') {
    if (-not $ExpectedSid) { throw '恢复准备必须指定已只读核对的 ExpectedAccountSid。' }
    $existing = Get-LocalUser -Name $Account -ErrorAction Stop
    if ($existing.SID.Value -ne $ExpectedSid -or $existing.Enabled) { throw '恢复仅允许相同 SID 且当前禁用的任务账号。' }
    foreach ($artifact in $Artifacts) {
        if (Test-Path -LiteralPath $artifact) { throw ('已有凭据/部署证明/ACL备份，拒绝覆盖恢复：' + $artifact) }
    }
    Assert-PlainPath $Workspace
    if ($RuntimePython) {
        [void](Invoke-RuntimeTree $RuntimePython $Workspace '' $true 'validate')
        return $existing
    }
    foreach ($item in Get-ChildItem -LiteralPath $Workspace -Force -ErrorAction Stop) {
        if (-not $item.PSIsContainer -or $item.Name -notin @('.runtime', '.tmp')) { throw ('工作目录包含非本次准备文件，拒绝恢复：' + $item.FullName) }
        Assert-PlainPath $item.FullName
        foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force -Recurse -ErrorAction Stop) { Assert-PlainPath $child.FullName }
        if ($item.Name -eq '.tmp' -and @(Get-ChildItem -LiteralPath $item.FullName -Force).Count) { throw '临时目录已有文件，不能证明没有业务执行，拒绝恢复。' }
    }
    return $existing
}

function Set-TaskLogonRights([string]$Sid) {
    if (-not ('AstrBotTaskLogon' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AstrBotTaskLogon {
 [StructLayout(LayoutKind.Sequential)] struct OA { public uint Length; public IntPtr Root; public IntPtr Name; public uint Attributes; public IntPtr Descriptor; public IntPtr Qos; }
 [StructLayout(LayoutKind.Sequential)] struct US { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
 [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system, ref OA attributes, uint access, out IntPtr policy);
 [DllImport("advapi32.dll")] static extern uint LsaAddAccountRights(IntPtr policy, IntPtr sid, US[] rights, uint count);
 [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr policy);
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool ConvertStringSidToSid(string sid, out IntPtr pointer);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr pointer);
 public static void Apply(string sid) {
  var attributes = new OA(); attributes.Length=(uint)Marshal.SizeOf(attributes);
  IntPtr policy=IntPtr.Zero, pointer=IntPtr.Zero;
  var names=new[]{"SeBatchLogonRight","SeDenyInteractiveLogonRight","SeDenyRemoteInteractiveLogonRight","SeDenyNetworkLogonRight"};
  var rights=new US[names.Length];
  try {
   if(!ConvertStringSidToSid(sid,out pointer) || LsaOpenPolicy(IntPtr.Zero,ref attributes,0x810,out policy)!=0) throw new InvalidOperationException("Task logon policy unavailable");
   for(int i=0;i<names.Length;i++) { rights[i].Buffer=Marshal.StringToHGlobalUni(names[i]); rights[i].Length=(ushort)(names[i].Length*2); rights[i].MaximumLength=(ushort)(rights[i].Length+2); }
   if(LsaAddAccountRights(policy,pointer,rights,(uint)rights.Length)!=0) throw new InvalidOperationException("Task logon rights rejected");
  } finally { foreach(var right in rights) if(right.Buffer!=IntPtr.Zero) Marshal.FreeHGlobal(right.Buffer); if(policy!=IntPtr.Zero)LsaClose(policy); if(pointer!=IntPtr.Zero)LocalFree(pointer); }
 }
}
'@
    }
    [AstrBotTaskLogon]::Apply($Sid)
}

if ($Mode -eq 'Plan') {
    Write-Output '默认关闭：不会创建账户、目录或更改 ACL。'
    Write-Output '每个 bot/group/真实账号使用独立标准用户和 SHA256 目录。'
    Write-Output 'Provision 必须由云端 SYSTEM 显式运行；Verify 默认不生成 ready，实际探针通过且显式 Finalize 才启用。'
    Write-Output '标准账户仍可读取 Windows 公共文件；本方案不是完整文件系统命名空间。'
    Write-Output '若要求只能看到任务目录，应保持关闭，采用容器/虚拟机/独立主机后另行验证。'
    return
}

if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne 'S-1-5-18') { throw '仅允许 SYSTEM 进行准备或实际账号验证。' }
if (-not $BotId -or -not $UserId) { throw 'BotId 与真实平台 UserId 必填，不能使用昵称。' }
foreach ($path in @($TaskRoot, $BotRoot, $BrokerRoot)) { Assert-PlainPath $path }
$TaskRoot = [IO.Path]::GetFullPath($TaskRoot).TrimEnd('\')
$BotRoot = [IO.Path]::GetFullPath($BotRoot).TrimEnd('\')
$BrokerRoot = [IO.Path]::GetFullPath($BrokerRoot).TrimEnd('\')
foreach ($pair in @(@($TaskRoot, $BotRoot), @($TaskRoot, $BrokerRoot))) {
    if ($pair[0].StartsWith($pair[1] + '\', [StringComparison]::OrdinalIgnoreCase) -or $pair[1].StartsWith($pair[0] + '\', [StringComparison]::OrdinalIgnoreCase) -or $pair[0] -eq $pair[1]) { throw '任务、机器人和 broker 目录必须互不包含。' }
}
$botHash = Get-HexHash $BotId
$groupHash = Get-HexHash $GroupId
$userHash = Get-HexHash $UserId
$workspace = Join-Path (Join-Path (Join-Path $TaskRoot $botHash) $groupHash) $userHash
$account = 'abt_' + (Get-HexHash ($BotId + "`0" + $GroupId + "`0" + $UserId)).Substring(0, 16)
$recordPath = Join-Path $BrokerRoot ($account + '.credential.dpapi')
$candidatePath = Join-Path $BrokerRoot ($account + '.candidate.json')

if ($Mode -eq 'Provision') {
    if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Provision 必须指定宿主 Python，以复制独立任务运行时。' }
    $resumeAccount = $null
    if ($ResumeProvision) {
        $resumeAccount = Assert-ResumeProvisionState $account $ExpectedAccountSid $workspace @($recordPath, $candidatePath, (Join-Path $BrokerRoot ($account + '.ready.json')), (Join-Path $BrokerRoot ($account + '.acl-backup.json'))) $Python
    } else {
        if (Get-LocalUser -Name $account -ErrorAction SilentlyContinue) { throw '账户已存在；停止而不覆盖旧账户、密码或 ACL。' }
        if (Test-Path -LiteralPath $workspace) { throw '工作目录已存在；停止而不覆盖任务文件。' }
    }
    if (Test-Path -LiteralPath $candidatePath) { throw '宿主部署记录已存在；必须先审核旧状态。' }
    Initialize-BrokerRoot $BrokerRoot
    # Protect each parent without granting task users traversal/listing of siblings.
    foreach ($directory in @($TaskRoot, (Join-Path $TaskRoot $botHash), (Join-Path (Join-Path $TaskRoot $botHash) $groupHash))) {
        if (-not (Test-Path -LiteralPath $directory)) { Set-PrivateDirectory $directory }
    }
    $bytes = New-Object byte[] 48
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $password = [Convert]::ToBase64String($bytes) + '!aA1'
    $secure = ConvertTo-SecureString $password -AsPlainText -Force
    try {
        if ($resumeAccount) {
            Set-LocalUser -Name $account -Password $secure
            $local = Get-LocalUser -Name $account
            if ($local.SID.Value -ne $ExpectedAccountSid -or $local.Enabled) { throw '恢复过程账号 SID/禁用状态变化，停止。' }
        } else {
            $local = New-LocalUser -Name $account -Password $secure -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword -Description 'AstrBot task account; batch logon only'
        }
        # Keep disabled throughout provisioning, including failures before ACLs.
        Disable-LocalUser -Name $account
        $sid = $local.SID.Value
        Set-TaskLogonRights $sid
        foreach ($directory in @($TaskRoot, (Join-Path $TaskRoot $botHash), (Join-Path (Join-Path $TaskRoot $botHash) $groupHash))) {
            $acl = Get-Acl -LiteralPath $directory
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($local.SID, 'Traverse', 'None', 'None', 'Allow')))
            Set-Acl -LiteralPath $directory -AclObject $acl
        }
        $sharedTools = Join-Path $TaskRoot 'tools'
        if (-not (Test-Path -LiteralPath $sharedTools)) { Set-PrivateDirectory $sharedTools }
        Assert-PlainPath $sharedTools
        $acl = Get-Acl -LiteralPath $sharedTools
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($local.SID, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        Set-Acl -LiteralPath $sharedTools -AclObject $acl
        Set-PrivateDirectory $workspace $sid
        Set-PrivateDirectory (Join-Path $workspace '.tmp') $sid
        Set-PrivateDirectory (Join-Path $workspace '.runtime') $sid
        $basePython = (& $Python -I -c 'import sys; from pathlib import Path; print(Path(sys.base_prefix).resolve(strict=True))').Trim()
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $basePython 'python.exe'))) { throw '无法确定可复制的 Python 基础运行时。' }
        Assert-PlainPath $basePython
        Invoke-RuntimeTree $Python $workspace $basePython ([bool]$resumeAccount) 'copy'
        # Save the complete tree before root inheritance changes any descendant.
        $aclBackup = New-Object Collections.Generic.List[object]
        $protectedItems = New-Object Collections.Generic.List[object]
        foreach ($protected in @($BotRoot, $BrokerRoot)) {
            Assert-PlainPath $protected
            $longProtected = '\\?\' + $protected
            foreach ($item in @((Get-Item -LiteralPath $longProtected -Force)) + @(Get-ChildItem -LiteralPath $longProtected -Force -Recurse -ErrorAction Stop)) {
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('保护目录存在 reparse，拒绝操作：' + $item.FullName) }
                $aclBackup.Add(@{path=$item.FullName; sddl=(Get-Acl -LiteralPath $item.FullName).Sddl})
                $protectedItems.Add($item)
            }
        }
        [IO.File]::WriteAllText((Join-Path $BrokerRoot ($account + '.acl-backup.json')), ($aclBackup | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
        $aclWritten = 0
        $aclSkipped = 0
        # Parent-first writes let each real inherited Deny propagate. Still read
        # every current ACL, and stamp explicit Allow/protected-inheritance gaps.
        foreach ($item in @($protectedItems | Sort-Object {$_.FullName.Length})) {
            if (Set-TaskDenyIfMissing $item $local.SID) { $aclWritten++ } else { $aclSkipped++ }
        }
        # Existing sibling workspaces are denied explicitly; parent ACLs also omit
        # all task identities, including future accounts. No data is deleted.
        foreach ($botDir in Get-ChildItem -LiteralPath $TaskRoot -Directory) {
            if ($botDir.FullName -eq $sharedTools) { continue }
            foreach ($groupDir in Get-ChildItem -LiteralPath $botDir.FullName -Directory) {
                foreach ($sibling in Get-ChildItem -LiteralPath $groupDir.FullName -Directory) {
                    Assert-PlainPath $sibling.FullName
                    if ($sibling.FullName -ne $workspace) {
                        foreach ($item in @(@($sibling) + @(Get-ChildItem -LiteralPath ('\\?\' + $sibling.FullName) -Force -Recurse -ErrorAction Stop) | Sort-Object {$_.FullName.Length})) {
                            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('其他任务目录存在 reparse，拒绝操作：' + $item.FullName) }
                            if (Set-TaskDenyIfMissing $item $local.SID) { $aclWritten++ } else { $aclSkipped++ }
                        }
                    }
                }
            }
        }
        Write-Output ('ACL逐项检查完成；写入=' + $aclWritten + '，已有有效Deny跳过=' + $aclSkipped)
        $secret = @{domain=$env:COMPUTERNAME; username=$account; password=$password; user_sid=$sid} | ConvertTo-Json -Compress
        Add-Type -AssemblyName System.Security
        $encrypted = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($secret), $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
        [IO.File]::WriteAllBytes($recordPath, $encrypted)
        # Account deliberately disabled until the separate actual-account probes.
        Disable-LocalUser -Name $account
        $machineSid = (Get-LocalUser -Name $account).SID.AccountDomainSid.Value
        $aclHash = Get-HexHash ((Get-Acl -LiteralPath $workspace).Sddl + (Get-Acl -LiteralPath $BotRoot).Sddl + (Get-Acl -LiteralPath $BrokerRoot).Sddl)
        $candidate = @{schema=1; enabled=$false; machine_sid=$machineSid; user_sid=$sid; workspace=$workspace; bot_id=$BotId; group_id=$GroupId; user_id=$UserId; acl_sha256=$aclHash; credential=$recordPath; protected_roots=@($BotRoot,$BrokerRoot); verification='pending'; required_rights=@('SeBatchLogonRight','SeDenyInteractiveLogonRight','SeDenyRemoteInteractiveLogonRight','SeDenyNetworkLogonRight')}
        [IO.File]::WriteAllText($candidatePath, ($candidate | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
        Write-Output '准备完成；账户仍禁用，执行仍关闭。登录权已设置；Verify 将临时启用账号进行探针。'
        Write-Output '密码仅保存在宿主 ACL 保护的 DPAPI LocalMachine 文件，不会输出。'
    } catch {
        if (Get-LocalUser -Name $account -ErrorAction SilentlyContinue) { Disable-LocalUser -Name $account }
        throw
    } finally { $password = $null; $secret = $null; $secure = $null; [Array]::Clear($bytes, 0, $bytes.Length) }
    return
}

if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Verify 需要明确指定宿主 Python 解释器。' }
Assert-PlainPath $Python
Assert-PlainPath $recordPath
Assert-PlainPath $candidatePath
$candidate = Get-Content -LiteralPath $candidatePath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($candidate.workspace -ne $workspace -or $candidate.user_id -ne $UserId -or $candidate.bot_id -ne $BotId -or $candidate.group_id -ne $GroupId) { throw '宿主部署记录与真实账号不匹配。' }
Set-TaskLogonRights $candidate.user_sid
Enable-LocalUser -Name $account
Add-Type -AssemblyName System.Security
$encrypted = [IO.File]::ReadAllBytes($recordPath)
$plain = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine)
$secret = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
$fakeProtected = Join-Path $BrokerRoot ($account + '.fake-protected.txt')
[IO.File]::WriteAllText($fakeProtected, 'fake protected verification data', [Text.Encoding]::UTF8)
$protectedProbes = New-Object Collections.Generic.List[string]
foreach ($dir in @($BotRoot, (Join-Path $BotRoot 'AstrBot-master'), (Join-Path $BotRoot 'AstrBot-master\data'))) {
    if (Test-Path -LiteralPath $dir -PathType Container) {
        $fake = Join-Path $dir ($account + '.fake-protected-' + [Guid]::NewGuid().ToString('N') + '.txt')
        [IO.File]::WriteAllText($fake, 'fake bot data; never a credential', [Text.Encoding]::UTF8)
        $protectedProbes.Add($fake)
    }
}
$sibling = Join-Path $TaskRoot ('verification-sibling-' + [Guid]::NewGuid().ToString('N'))
Set-PrivateDirectory $sibling
$testCode = @'
import asyncio, json, pathlib, sys
request = json.load(sys.stdin)
sys.path.insert(0, request['plugin'])
from task_execution import WindowsNativeBackend, ExecutionIdentity, _check_account_rights, _host_acl_probe
identity = ExecutionIdentity(request['domain'], request['username'], request['user_sid'])
_check_account_rights(identity.user_sid)
backend = WindowsNativeBackend(lambda _: (identity.domain, identity.username, request['password']))
root = pathlib.Path(request['workspace'])
ps = str(pathlib.Path(request['windows']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
env = {'SystemRoot':request['windows'],'WINDIR':request['windows'],'TEMP':str(root/'.tmp'),'TMP':str(root/'.tmp'),'PATH':str(pathlib.Path(request['windows'])/'System32'),'PATHEXT':'.COM;.EXE;.BAT;.CMD','USERPROFILE':str(root),'PIP_CONFIG_FILE':'NUL','PYTHONNOUSERSITE':'1'}
def quote(value): return "'" + value.replace("'", "''") + "'"
protected = quote(request['fake_protected'])
sibling = quote(str(pathlib.Path(request['sibling'])/'write-denied.txt'))
python = quote(str(root/'.runtime/python.exe'))
venv = quote(str(root/'.venv'))
venv_python = quote(str(root/'.venv/Scripts/python.exe'))
native_probe = r"""
function Invoke-NativeDiagnostic([string]$Exe,[string[]]$Arguments) {
 $previous=$ErrorActionPreference
 $ErrorActionPreference='Continue'
 $global:LASTEXITCODE=$null
 $captured=@()
 $exception=$null
 try { $captured=@(& $Exe @Arguments 2>&1 | ForEach-Object { $_.ToString() }) }
 catch { $exception=$_.Exception.Message }
 finally { $nativeExit=$global:LASTEXITCODE; $ErrorActionPreference=$previous }
 return @{exit_code=$nativeExit;output=($captured -join "`n");exception=$exception}
}
"""
code = native_probe + f"; $ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; $r=@{{powershell_version=$PSVersionTable.PSVersion.ToString();venv_exit=$null;install_exit=$null;venv_output='';install_output=''}}; try {{ [void][IO.File]::ReadAllText({protected}); $r.protected_denied=$false }} catch {{ $r.protected_denied=$true }}; try {{ [IO.File]::WriteAllText({sibling},'fake'); $r.sibling_denied=$false }} catch {{ $r.sibling_denied=$true }}; try {{ $r.network=([int](Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 'https://pypi.org/simple/pip/').StatusCode -eq 200) }} catch {{ $r.network=$false }}; $v=Invoke-NativeDiagnostic -Exe {python} -Arguments @('-I','-m','venv',{venv}); $r.venv_exit=$v.exit_code; $r.venv_output=$v.output; $r.venv_exception=$v.exception; $r.venv=($null -ne $v.exit_code -and $v.exit_code -eq 0 -and $null -eq $v.exception); if ($r.venv) {{ $i=Invoke-NativeDiagnostic -Exe {venv_python} -Arguments @('-I','-m','pip','--isolated','install','--no-input','packaging==25.0'); $r.install_exit=$i.exit_code; $r.install_output=$i.output; $r.install_exception=$i.exception; $r.install=($null -ne $i.exit_code -and $i.exit_code -eq 0 -and $null -eq $i.exception) }} else {{ $r.install=$false }}; $r | ConvertTo-Json -Compress; if (-not ($r.protected_denied -and $r.sibling_denied -and $r.network -and $r.venv -and $r.install)) {{ exit 1 }}"
probes = ','.join(quote(p) for p in request['protected_probes'])
code += f"; foreach($p in @({probes})) {{ try {{ [void][IO.File]::ReadAllText($p); exit 2 }} catch {{ }}; try {{ [IO.File]::WriteAllText($p,'fake'); exit 3 }} catch {{ }} }}; exit 0"
async def main():
    process = backend.start(identity, (ps,'-NoLogo','-NoProfile','-NonInteractive','-Command',code), root, env)
    try:
        result = await asyncio.wait_for(process.wait(), 180)
        print(json.dumps({'exit_code':result.exit_code,'stdout':result.stdout,'stderr':result.stderr}))
        if result.exit_code == 0:
            _host_acl_probe(pathlib.Path(request['broker_root']), (pathlib.Path(request['vault']),))
            # Deployment Verify already performed the full SYSTEM ACL tree scan;
            # this post-process attestation only checks the protected roots.
            _host_acl_probe(pathlib.Path(request['broker_root']), (root, *(pathlib.Path(p) for p in request['protected_roots'])), account=identity.username, scan=False)
            started = root/'.tmp/probe-started.txt'
            escaped = root/'.tmp/probe-escaped.txt'
            for path in (started,escaped):
                if path.exists(): path.unlink()
            child = f"[IO.File]::WriteAllText({quote(str(started))},'started'); Start-Sleep 3; [IO.File]::WriteAllText({quote(str(escaped))},'escaped')"
            import base64
            encoded = base64.b64encode(child.encode('utf-16-le')).decode('ascii')
            parent = f"Start-Process -FilePath {quote(ps)} -ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{encoded}'; Start-Sleep 60"
            job = backend.start(identity, (ps,'-NoProfile','-NonInteractive','-Command',parent), root, env)
            waiter = asyncio.create_task(job.wait())
            try:
                for _ in range(50):
                    if started.exists(): break
                    await asyncio.sleep(.1)
                if not started.exists(): raise RuntimeError('Descendant probe did not start')
                waiter.cancel()
                try: await waiter
                except asyncio.CancelledError: pass
                await asyncio.sleep(3.2)
                if escaped.exists(): raise RuntimeError('Descendant survived cancelled job')
            finally:
                job.kill_tree()
                job.close()
        sys.exit(result.exit_code)
    finally:
        process.kill_tree()
        process.close()
asyncio.run(main())
'@
$request = @{domain=$secret.domain; username=$secret.username; password=$secret.password; user_sid=$secret.user_sid; workspace=$workspace; plugin=(Join-Path $BotRoot 'AstrBot-master\data\plugins\astrbot_plugin_private_companion'); windows=$env:SystemRoot; fake_protected=$fakeProtected; sibling=$sibling; broker_root=$BrokerRoot; vault=$recordPath; protected_roots=@($BotRoot,$BrokerRoot); protected_probes=@($protectedProbes)} | ConvertTo-Json -Compress
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = $Python
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($testCode))
$start.Arguments = '-I -c "import base64;exec(base64.b64decode(''' + $encoded + '''))"'
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$process = New-Object Diagnostics.Process
$process.StartInfo = $start
try {
    [void]$process.Start()
    $process.StandardInput.Write($request)
    $process.StandardInput.Close()
    $output = $process.StandardOutput.ReadToEndAsync()
    $errors = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(600000)) {
        $process.Kill()
        throw '验证宿主超时；宿主进程已终止，Job close回收子进程，执行保持关闭。'
    }
    $evidence = @{enabled=$false; exit_code=$process.ExitCode; result=$output.Result; errors=$errors.Result; machine_sid=$candidate.machine_sid; user_sid=$candidate.user_sid; workspace=$workspace; tested_at=[DateTime]::UtcNow.ToString('o')}
    [IO.File]::WriteAllText((Join-Path $BrokerRoot ($account + '.verification.json')), ($evidence | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Write-Output ('实际账户验证退出码：' + $process.ExitCode + '。证据已保存，执行仍关闭。')
    Write-Output '还须验证登录权、组成员、全部 ACL、DPAPI/宿主 marker 防伪、取消/超时子孙进程回收及并发链接替换。'
    if ($process.ExitCode -ne 0) { throw '账户隔离/联网/虚拟环境验证未通过。' }
    if ($FinalizeVerifiedDeployment) {
        $candidate.enabled = $true
        $candidate.verification = 'passed'
        $candidate.acl_sha256 = Get-HexHash ((Get-Acl -LiteralPath $workspace).Sddl + (Get-Acl -LiteralPath $BotRoot).Sddl + (Get-Acl -LiteralPath $BrokerRoot).Sddl)
        [IO.File]::WriteAllText((Join-Path $BrokerRoot ($account + '.ready.json')), ($candidate | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
        Write-Output '真实账号探针通过并生成宿主 ready 证据；运行时仍逐次复核全部部署 invariant。'
    }
} catch { Disable-LocalUser -Name $account; throw }
finally {
    $process.Dispose(); $request=$null; $secret=$null; [Array]::Clear($plain, 0, $plain.Length)
    foreach ($probe in $protectedProbes) { if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force } }
}
