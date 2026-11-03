function Test-SameFullPath {
    param([string]$Left,[string]$Right)
    $leftFull = [IO.Path]::GetFullPath($Left).TrimEnd('\')
    $rightFull = [IO.Path]::GetFullPath($Right).TrimEnd('\')
    return $leftFull.Equals($rightFull, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-DirectoryIsNotReparsePoint {
    param([string]$Path,[string]$Description)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) { throw "$Description 不是目录：$Path" }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description 不允许是 Junction、符号链接或其他重新分析点：$Path"
    }
}

function Assert-SafeNodeModulesParent {
    param([string]$CacheRoot,[string]$LinkParent,[switch]$CreateMissing)

    Assert-DirectoryIsNotReparsePoint -Path $CacheRoot -Description '缓存根目录'
    $relative = $LinkParent.Substring($CacheRoot.TrimEnd('\').Length).TrimStart('\')
    $current = $CacheRoot.TrimEnd('\')
    foreach ($component in @($relative -split '\\' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $current = Join-Path $current $component
        if (-not (Test-Path -LiteralPath $current)) {
            if (-not $CreateMissing) { throw "node_modules 父目录不存在：$current" }
            New-Item -ItemType Directory -Path $current -ErrorAction Stop | Out-Null
        }
        Assert-DirectoryIsNotReparsePoint -Path $current -Description 'node_modules 父目录路径组件'
    }
}

function Ensure-NodeModulesLink {
    param([Parameter(Mandatory)]$Config)

    if (-not (Test-Path -LiteralPath $Config.BundledNodeModules -PathType Container)) {
        throw "Codex 捆绑表格依赖不存在：$($Config.BundledNodeModules)。工具不自动下载依赖。"
    }
    $cacheRoot = [IO.Path]::GetFullPath([string]$Config.CacheRoot).TrimEnd('\')
    $nodeModulesLink = [IO.Path]::GetFullPath([string]$Config.NodeModulesLink)
    $cachePrefix = $cacheRoot + '\'
    if (-not $nodeModulesLink.StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "NodeModulesLink 路径越界，必须位于缓存根目录内：$nodeModulesLink"
    }
    if (-not (Test-Path -LiteralPath $cacheRoot -PathType Container)) {
        throw "缓存根目录不存在：$cacheRoot"
    }

    $linkParent = [IO.Path]::GetFullPath((Split-Path -Parent $nodeModulesLink))
    Assert-SafeNodeModulesParent -CacheRoot $cacheRoot -LinkParent $linkParent -CreateMissing
    Assert-SafeNodeModulesParent -CacheRoot $cacheRoot -LinkParent $linkParent

    if (Test-Path -LiteralPath $nodeModulesLink) {
        $item = Get-Item -LiteralPath $nodeModulesLink -Force
        $target = @($item.Target) | Select-Object -First 1
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -or
            $item.LinkType -ne 'Junction' -or
            [string]::IsNullOrWhiteSpace([string]$target) -or
            -not (Test-SameFullPath -Left $target -Right $Config.BundledNodeModules)) {
            throw "node_modules 最终项必须是指向配置中捆绑依赖目录的 Junction：$nodeModulesLink"
        }
        return
    }

    New-Item -ItemType Junction -Path $nodeModulesLink -Target $Config.BundledNodeModules -ErrorAction Stop | Out-Null
    $created = Get-Item -LiteralPath $nodeModulesLink -Force -ErrorAction Stop
    $createdTarget = @($created.Target) | Select-Object -First 1
    if (($created.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -or
        $created.LinkType -ne 'Junction' -or
        -not (Test-SameFullPath -Left $createdTarget -Right $Config.BundledNodeModules)) {
        throw "node_modules Junction 创建后验证失败：$nodeModulesLink"
    }
}

function Invoke-SpreadsheetCommand {
    param(
        [Parameter(Mandatory)][string]$Action,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Config
    )

    $nodeAction = switch ($Action) {
        '检查' { 'inspect' }
        '预览' { 'render' }
        default { throw "不支持的表格操作：$Action" }
    }
    if (-not (Test-Path -LiteralPath $Config.Node -PathType Leaf)) {
        throw "Codex 捆绑 Node.js 缺失：$($Config.Node)。请检查工具配置；工具不自动下载依赖。"
    }
    Ensure-NodeModulesLink -Config $Config

    $sourceHelper = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\辅助程序\表格处理.mjs'))
    if (-not (Test-Path -LiteralPath $sourceHelper -PathType Leaf)) {
        throw "表格处理辅助程序不存在：$sourceHelper"
    }
    $runtimeRoot = Split-Path -Parent $Config.NodeModulesLink
    $runtimeHelper = Join-Path $runtimeRoot ("表格处理_{0}.mjs" -f [guid]::NewGuid().ToString('N'))

    if ($nodeAction -eq 'inspect') {
        New-Item -ItemType Directory -Force -Path $Config.OutputRoot | Out-Null
        $target = New-UniqueOutputPath $Config.OutputRoot ("{0}_表格检查.json" -f [IO.Path]::GetFileNameWithoutExtension($Path))
    }
    else {
        $target = Join-Path $Config.CacheRoot ("表格预览\{0}" -f [guid]::NewGuid().ToString('N'))
    }

    try {
        Copy-Item -LiteralPath $sourceHelper -Destination $runtimeHelper
        $nodeOutput = @(Invoke-ExternalTool $Config.Node @($runtimeHelper,$nodeAction,$Path,$target))
        $json = $nodeOutput -join [Environment]::NewLine
        try { $null = $json | ConvertFrom-Json -ErrorAction Stop }
        catch { throw '表格处理协议错误：Node.js 标准输出不是有效 JSON' }
        return $json
    }
    finally {
        if (Test-Path -LiteralPath $runtimeHelper -PathType Leaf) {
            Remove-Item -LiteralPath $runtimeHelper -Force
        }
    }
}

Export-ModuleMember -Function Invoke-SpreadsheetCommand,Ensure-NodeModulesLink
