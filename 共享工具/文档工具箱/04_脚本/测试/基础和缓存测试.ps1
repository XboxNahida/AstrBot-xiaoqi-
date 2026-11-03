$ErrorActionPreference = 'Stop'

Import-Module "$PSScriptRoot\..\模块\通用工具.psm1" -Force
$config = Get-ToolConfig "$PSScriptRoot\..\工具配置.json"
if (-not (Test-Path -LiteralPath $config.WorkspaceRoot)) { throw "未解析工作区" }

$createdOutputFiles = @()
try {
    New-Item -ItemType Directory -Force -Path $config.OutputRoot | Out-Null
    $first = New-UniqueOutputPath $config.OutputRoot "测试.txt"
    Set-Content -LiteralPath $first -Value "x" -Encoding UTF8
    $createdOutputFiles += $first
    $second = New-UniqueOutputPath $config.OutputRoot "测试.txt"
    if ($first -eq $second) { throw "同名文件未生成唯一路径" }
    Set-Content -LiteralPath $second -Value "y" -Encoding UTF8
    $createdOutputFiles += $second
    $third = New-UniqueOutputPath $config.OutputRoot "测试.txt"
    if ($second -eq $third) { throw "时间戳路径已存在时未继续生成唯一路径" }
}
finally {
    foreach ($createdFile in $createdOutputFiles) {
        if (Test-Path -LiteralPath $createdFile) {
            Remove-Item -LiteralPath $createdFile -Force
        }
    }
}

Import-Module "$PSScriptRoot\..\模块\缓存工具.psm1" -Force

$cacheRootCreatedByTest = -not (Test-Path -LiteralPath $config.CacheRoot)
try {
    New-Item -ItemType Directory -Force -Path $config.CacheRoot | Out-Null

    try {
        Clear-ToolCache $config -Target $config.WorkspaceRoot
        throw "未拒绝越界删除"
    }
    catch {
        if ($_.Exception.Message -notmatch "超出缓存根目录") { throw }
    }

    $cacheFixture = Join-Path $config.CacheRoot ("缓存测试_{0}" -f [guid]::NewGuid().ToString('N'))
    $cacheFile = Join-Path $cacheFixture '临时.txt'
    $nestedOutsideFixture = Join-Path $config.OutputRoot ("嵌套联接外部测试_{0}" -f [guid]::NewGuid().ToString('N'))
    $targetOutsideFixture = Join-Path $config.OutputRoot ("目标联接外部测试_{0}" -f [guid]::NewGuid().ToString('N'))
    $nestedDirectory = Join-Path $cacheFixture '第一层\第二层'
    $nestedJunction = Join-Path $nestedDirectory '嵌套外部联接'
    $targetJunction = Join-Path $config.CacheRoot ("目标联接_{0}" -f [guid]::NewGuid().ToString('N'))
    $targetBelowJunction = Join-Path $targetJunction '普通子目录'
    try {
        New-Item -ItemType Directory -Force -Path $cacheFixture | Out-Null
        Set-Content -LiteralPath $cacheFile -Value 'cache' -Encoding UTF8

        $summary = Get-CacheSummary $config
        if ($summary.Count -lt 1 -or $summary.Sum -lt 1) { throw "缓存统计结果无效" }

        Clear-ToolCache $config -Target $cacheFixture -WhatIf
        if (-not (Test-Path -LiteralPath $cacheFile)) { throw "WhatIf 意外删除缓存文件" }

        Clear-ToolCache $config -Target $cacheFixture
        if (Test-Path -LiteralPath $cacheFile) { throw "缓存文件未删除" }

        New-Item -ItemType Directory -Force -Path $nestedOutsideFixture | Out-Null
        New-Item -ItemType Directory -Force -Path $targetOutsideFixture | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $targetOutsideFixture '普通子目录') | Out-Null
        New-Item -ItemType Directory -Force -Path $nestedDirectory | Out-Null
        $nestedMarker = Join-Path $nestedOutsideFixture '嵌套外部文件不得删除.txt'
        $targetMarker = Join-Path $targetOutsideFixture '目标外部文件不得删除.txt'
        $belowJunctionMarker = Join-Path $targetOutsideFixture '普通子目录\联接下方文件不得删除.txt'
        Set-Content -LiteralPath $nestedMarker -Value 'source' -Encoding UTF8
        Set-Content -LiteralPath $targetMarker -Value 'source' -Encoding UTF8
        Set-Content -LiteralPath $belowJunctionMarker -Value 'source' -Encoding UTF8
        New-Item -ItemType Junction -Path $nestedJunction -Target $nestedOutsideFixture | Out-Null
        New-Item -ItemType Junction -Path $targetJunction -Target $targetOutsideFixture | Out-Null

        $reparseFailures = @()
        $linkedSummary = Get-CacheSummary $config
        if ($linkedSummary.Count -ne 0 -or $linkedSummary.Sum -ne 0) {
            $reparseFailures += "缓存统计读取了联接外文件（Count=$($linkedSummary.Count)，Sum=$($linkedSummary.Sum)）"
        }

        $nestedJunctionRejected = $false
        try {
            Clear-ToolCache $config -Target $cacheFixture
        }
        catch {
            if ($_.Exception.Message -notmatch '重新分析点') { throw }
            $nestedJunctionRejected = $true
        }
        if (-not $nestedJunctionRejected) {
            $reparseFailures += '未拒绝嵌套目录联接'
        }
        if (-not (Test-Path -LiteralPath $nestedMarker)) {
            $reparseFailures += '嵌套联接指向的缓存外文件被删除'
        }

        $targetJunctionRejected = $false
        try {
            Clear-ToolCache $config -Target $targetJunction
        }
        catch {
            if ($_.Exception.Message -notmatch '重新分析点') { throw }
            $targetJunctionRejected = $true
        }
        if (-not $targetJunctionRejected) {
            $reparseFailures += '未拒绝作为 Target 的目录联接'
        }
        if (-not (Test-Path -LiteralPath $targetMarker)) {
            $reparseFailures += 'Target 联接指向的缓存外文件被删除'
        }

        $belowJunctionRejected = $false
        try {
            Clear-ToolCache $config -Target $targetBelowJunction
        }
        catch {
            if ($_.Exception.Message -notmatch '重新分析点') { throw }
            $belowJunctionRejected = $true
        }
        if (-not $belowJunctionRejected) {
            $reparseFailures += '未拒绝位于目录联接下方的 Target'
        }
        if (-not (Test-Path -LiteralPath $belowJunctionMarker)) {
            $reparseFailures += '目录联接下方 Target 的缓存外文件被删除'
        }
        if ($reparseFailures.Count -ne 0) {
            throw ($reparseFailures -join '；')
        }

        $status = Get-ToolStatus $config
        foreach ($propertyName in 'PowerShell','Workspace','Cache','Python','Node','LibreOffice','Poppler','Spreadsheet') {
            if ($status.PSObject.Properties.Name -notcontains $propertyName) {
                throw "状态缺少字段：$propertyName"
            }
        }
    }
    finally {
        if (Test-Path -LiteralPath $targetJunction) {
            [IO.Directory]::Delete($targetJunction)
        }
        if (Test-Path -LiteralPath $nestedJunction) {
            [IO.Directory]::Delete($nestedJunction)
        }
        if (Test-Path -LiteralPath $cacheFixture) {
            Remove-Item -LiteralPath $cacheFixture -Recurse -Force
        }
        if (Test-Path -LiteralPath $nestedOutsideFixture) {
            Remove-Item -LiteralPath $nestedOutsideFixture -Recurse -Force
        }
        if (Test-Path -LiteralPath $targetOutsideFixture) {
            Remove-Item -LiteralPath $targetOutsideFixture -Recurse -Force
        }
    }
}
finally {
    if ($cacheRootCreatedByTest -and (Test-Path -LiteralPath $config.CacheRoot)) {
        $remainingCacheItems = @(Get-ChildItem -LiteralPath $config.CacheRoot -Force)
        if ($remainingCacheItems.Count -eq 0) {
            Remove-Item -LiteralPath $config.CacheRoot -Force
        }
    }
}

$entryScript = Join-Path $PSScriptRoot '..\文档工具.ps1'
$hostExecutable = (Get-Process -Id $PID -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $hostExecutable -PathType Leaf)) {
    throw "无法发现当前 PowerShell 宿主：$hostExecutable"
}
$statusOutput = & $hostExecutable -NoProfile -ExecutionPolicy Bypass -File $entryScript '状态' 2>&1
if ($LASTEXITCODE -ne 0) { throw "状态命令退出码错误：$LASTEXITCODE`n$($statusOutput -join [Environment]::NewLine)" }
foreach ($label in '工作区','Python','Node.js','LibreOffice','Poppler','表格依赖') {
    if (($statusOutput -join [Environment]::NewLine) -notmatch [regex]::Escape($label)) {
        throw "状态输出缺少：$label"
    }
}
$statusText = $statusOutput -join [Environment]::NewLine
$dependencyChecks = @(
    [pscustomobject]@{ Label = 'Python'; Available = $status.Python },
    [pscustomobject]@{ Label = 'Node.js'; Available = $status.Node },
    [pscustomobject]@{ Label = 'LibreOffice'; Available = $status.LibreOffice },
    [pscustomobject]@{ Label = 'Poppler'; Available = $status.Poppler },
    [pscustomobject]@{ Label = '表格依赖'; Available = $status.Spreadsheet }
)
foreach ($dependency in $dependencyChecks) {
    if ($dependency.Available) {
        if ($statusText -notmatch [regex]::Escape("$($dependency.Label)：可用")) {
            throw "可用依赖状态错误：$($dependency.Label)"
        }
    }
    else {
        if ($statusText -notmatch [regex]::Escape("$($dependency.Label)：缺失") -or
            $statusText -notmatch '不自动下载') {
            throw "缺失依赖诊断不完整：$($dependency.Label)"
        }
    }
}

Write-Output "基础和缓存测试：通过"
