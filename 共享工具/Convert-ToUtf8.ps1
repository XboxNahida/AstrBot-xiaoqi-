#requires -Version 5.1
<#
.SYNOPSIS
批量将 .c、.h、.md、.txt 转换成 UTF-8，可输出副本或备份后覆盖原文件。
.EXAMPLE
.\Convert-ToUtf8.ps1 -SourceFolder 'D:\项目' -Recurse -TargetEncoding UTF8BOM
#>
[CmdletBinding()]
param(
    [string]$SourceFolder,
    [switch]$Recurse,
    [switch]$Overwrite,
    [ValidateSet('UTF8', 'UTF8BOM')][string]$TargetEncoding = 'UTF8'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-Link([string]$Path) {
    return (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Read-StrictText([byte[]]$Bytes) {
    $name = 'UTF-8'
    $offset = 0
    $encoding = New-Object Text.UTF8Encoding($false, $true)
    # 四字节 BOM 必须先于 UTF-16 判断。
    if ($Bytes.Length -ge 4 -and $Bytes[0] -eq 255 -and $Bytes[1] -eq 254 -and $Bytes[2] -eq 0 -and $Bytes[3] -eq 0) {
        $name = 'UTF-32 LE BOM'; $offset = 4
        $encoding = New-Object Text.UTF32Encoding($false, $true, $true)
    } elseif ($Bytes.Length -ge 4 -and $Bytes[0] -eq 0 -and $Bytes[1] -eq 0 -and $Bytes[2] -eq 254 -and $Bytes[3] -eq 255) {
        $name = 'UTF-32 BE BOM'; $offset = 4
        $encoding = New-Object Text.UTF32Encoding($true, $true, $true)
    } elseif ($Bytes.Length -ge 3 -and $Bytes[0] -eq 239 -and $Bytes[1] -eq 187 -and $Bytes[2] -eq 191) {
        $name = 'UTF-8 BOM'; $offset = 3
    } elseif ($Bytes.Length -ge 2 -and $Bytes[0] -eq 255 -and $Bytes[1] -eq 254) {
        $name = 'UTF-16 LE BOM'; $offset = 2
        $encoding = New-Object Text.UnicodeEncoding($false, $true, $true)
    } elseif ($Bytes.Length -ge 2 -and $Bytes[0] -eq 254 -and $Bytes[1] -eq 255) {
        $name = 'UTF-16 BE BOM'; $offset = 2
        $encoding = New-Object Text.UnicodeEncoding($true, $true, $true)
    }
    try {
        $content = $encoding.GetString($Bytes, $offset, $Bytes.Length - $offset)
    } catch {
        if ($offset -gt 0) { throw [IO.InvalidDataException]::new('文件有 BOM，但内容不符合声明的编码，已跳过。') }
        try {
            $encoding = [Text.Encoding]::GetEncoding(54936, [Text.EncoderFallback]::ExceptionFallback, [Text.DecoderFallback]::ExceptionFallback)
            $content = $encoding.GetString($Bytes)
            $name = 'GB18030（推定）'
        } catch {
            throw [IO.InvalidDataException]::new('无法按严格 UTF-8 或 GB18030 解码，已跳过。')
        }
    }
    if ($content -match '[\x00-\x08\x0B\x0E-\x1F\x7F]') {
        throw [IO.InvalidDataException]::new('包含非文本控制字符，可能是二进制文件，已跳过。')
    }
    return [pscustomobject]@{ Text = $content; Encoding = $name }
}

function Write-AtomicText([string]$Path, [string]$Content, [Text.Encoding]$Encoding, [string]$BackupPath = '') {
    $directory = [IO.Path]::GetDirectoryName($Path)
    $directories = @($directory)
    if ($BackupPath) {
        if ([IO.File]::Exists($BackupPath)) { throw "备份文件已存在，不能覆盖：$BackupPath" }
        $directories += [IO.Path]::GetDirectoryName($BackupPath)
    }
    # 目标和备份路径均不得经由目录链接重定向。
    foreach ($folder in $directories) {
        $current = $folder
        while ($current) {
            if ([IO.Directory]::Exists($current) -and (Test-Link $current)) {
                throw "输出目录不能是符号链接或目录联接：$current"
            }
            $current = [IO.Path]::GetDirectoryName($current)
        }
        [void][IO.Directory]::CreateDirectory($folder)
    }
    if ([IO.File]::Exists($Path) -and (Test-Link $Path)) { throw "输出文件不能是符号链接：$Path" }
    $temporary = Join-Path $directory ('.utf8-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Content, $Encoding)
        if ([IO.File]::Exists($Path)) {
            # 原子替换目录项；覆盖模式保留被替换文件的原始字节。
            $backup = if ($BackupPath) { $BackupPath } else { $temporary + '.bak' }
            [IO.File]::Replace($temporary, $Path, $backup)
            if (-not $BackupPath) { [IO.File]::Delete($backup) }
        } else {
            if ($BackupPath) { throw '源文件已不存在，取消覆盖。' }
            [IO.File]::Move($temporary, $Path)
        }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

if ([string]::IsNullOrWhiteSpace($SourceFolder)) {
    Write-Host '批量 UTF-8 转换工具（可选备份后覆盖原文件）' -ForegroundColor Cyan
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object Windows.Forms.FolderBrowserDialog
    $dialog.Description = '选择需要批量转换的文件夹'
    $dialog.ShowNewFolderButton = $false
    try {
        if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) {
            Write-Host '已取消。'
            return
        }
        $SourceFolder = $dialog.SelectedPath
    } finally { $dialog.Dispose() }
    do {
        $choice = Read-Host '输出编码：1 = UTF-8；2 = UTF-8 with BOM（默认 1）'
    } while ($choice -notin @('', '1', '2'))
    if ($choice -eq '2') { $TargetEncoding = 'UTF8BOM' } else { $TargetEncoding = 'UTF8' }
    do {
        $choice = Read-Host '是否处理所有子文件夹？Y/N（默认 Y）'
    } while ($choice -notin @('', 'y', 'n'))
    $Recurse = ($choice -ne 'n')
    do {
        $choice = Read-Host '是否覆盖原文件？Y = 备份后覆盖；N = 输出到 converted（默认 N）'
    } while ($choice -notin @('', 'y', 'n'))
    $Overwrite = ($choice -eq 'y')
}
$source = Get-Item -LiteralPath $SourceFolder -Force
if (-not $source.PSIsContainer -or $source.PSProvider.Name -ne 'FileSystem') { throw '源路径必须是文件系统中的文件夹。' }
if (Test-Link $source.FullName) { throw '请选择真实文件夹，不能以符号链接或目录联接作为源目录。' }
$root = $source.FullName.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$output = Join-Path $root 'converted'
if ([IO.Directory]::Exists($output) -and (Test-Link $output)) { throw 'converted 是目录链接，已停止以保护源文件。' }
$backupRoot = ''
if ($Overwrite) {
    $backupRoot = Join-Path $output ('backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))
}
$extensions = @('.c', '.h', '.md', '.txt')
$report = New-Object 'Collections.Generic.List[object]'
$files = New-Object 'Collections.Generic.List[IO.FileInfo]'
$pending = New-Object 'Collections.Generic.Stack[string]'
$pending.Push($root)
Write-Host "源目录：$root"
if ($Overwrite) {
    Write-Host '写入模式：备份后覆盖原文件' -ForegroundColor Yellow
    Write-Host "备份目录：$backupRoot"
} else {
    Write-Host "输出目录：$output（源文件保持不变）"
}
Write-Host "输出编码：$TargetEncoding；递归：$([bool]$Recurse)"
while ($pending.Count -gt 0) {
    $directory = $pending.Pop()
    try { $entries = @(Get-ChildItem -LiteralPath $directory -Force) }
    catch {
        $report.Add([pscustomobject][ordered]@{ 源文件=$directory; 目标文件=''; 备份文件=''; 源编码=''; 状态='失败'; 说明=$_.Exception.Message })
        continue
    }
    foreach ($entry in $entries) {
        $isLink = (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        if ($entry.PSIsContainer) {
            if ($Recurse -and -not $isLink -and $entry.FullName -ine $output) { $pending.Push($entry.FullName) }
        } elseif ($extensions -contains $entry.Extension) {
            if ($isLink) {
                $report.Add([pscustomobject][ordered]@{ 源文件=$entry.FullName; 目标文件=''; 备份文件=''; 源编码=''; 状态='跳过'; 说明='符号链接文件不处理。' })
            } else { $files.Add($entry) }
        }
    }
}
$targetEncoder = New-Object Text.UTF8Encoding(($TargetEncoding -eq 'UTF8BOM'), $true)
$index = 0
foreach ($file in $files) {
    $index++
    Write-Progress -Activity '转换文本编码' -Status "$index / $($files.Count)：$($file.Name)" -PercentComplete ([int](100 * $index / $files.Count))
    $relative = $file.FullName.Substring($root.Length)
    $destination = Join-Path $output $relative
    $backupPath = ''
    if ($Overwrite) {
        $destination = $file.FullName
        $backupPath = Join-Path $backupRoot $relative
    }
    $row = [pscustomobject][ordered]@{ 源文件=$file.FullName; 目标文件=$destination; 备份文件=''; 源编码=''; 状态='失败'; 说明='' }
    try {
        $decoded = Read-StrictText ([IO.File]::ReadAllBytes($file.FullName))
        $row.源编码 = $decoded.Encoding
        Write-AtomicText $destination $decoded.Text $targetEncoder $backupPath
        $row.备份文件 = $backupPath
        $row.状态 = '成功'
        if ($decoded.Encoding -eq 'GB18030（推定）') { $row.说明 = '无 BOM 编码不能唯一确定，请核对中文内容。' }
    } catch {
        if ($_.Exception -is [IO.InvalidDataException]) { $row.状态 = '跳过' }
        $row.说明 = $_.Exception.Message
    }
    $report.Add($row)
}
Write-Progress -Activity '转换文本编码' -Completed
$csv = '"源文件","目标文件","备份文件","源编码","状态","说明"' + "`r`n"
if ($report.Count -gt 0) { $csv = (@($report | ConvertTo-Csv -NoTypeInformation) -join "`r`n") + "`r`n" }
$reportPath = Join-Path $output 'conversion-report.csv'
Write-AtomicText $reportPath $csv (New-Object Text.UTF8Encoding($true, $true))
$success = @($report | Where-Object { $_.状态 -eq '成功' }).Count
$skipped = @($report | Where-Object { $_.状态 -eq '跳过' }).Count
$failed = @($report | Where-Object { $_.状态 -eq '失败' }).Count
Write-Host "处理结束：成功 $success，跳过 $skipped，失败 $failed。" -ForegroundColor Cyan
Write-Host "报告：$reportPath"
if ($files.Count -eq 0) { Write-Host '未找到可转换的 .c、.h、.md、.txt 普通文件。' }
if ($skipped -gt 0 -or $failed -gt 0) {
    Write-Host '部分项目未转换，请查看报告。' -ForegroundColor Yellow
    if ($Overwrite) { Write-Host '成功覆盖的文件已有备份，位置见报告。' -ForegroundColor Yellow }
}
