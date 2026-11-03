# 调用方先导入通用工具模块，以提供 Resolve-SafePath。

function Get-NormalizedPath {
    param([Parameter(Mandatory)][string]$Path)

    return [IO.Path]::GetFullPath($Path).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-PathWithinRoot {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )

    $fullPath = Get-NormalizedPath $Path
    $fullRoot = Get-NormalizedPath $Root
    $prefix = $fullRoot + [IO.Path]::DirectorySeparatorChar
    return $fullPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-SafeCacheTarget {
    param(
        [Parameter(Mandatory)]$Config,
        [Parameter(Mandatory)][string]$Target
    )

    if ([string]::IsNullOrWhiteSpace($Target)) {
        throw '缓存清理目标不能为空'
    }

    $cacheRoot = Resolve-SafePath ([string]$Config.CacheRoot)
    $targetPath = Resolve-SafePath $Target
    $normalizedRoot = Get-NormalizedPath $cacheRoot
    $normalizedTarget = Get-NormalizedPath $targetPath
    $volumeRoot = Get-NormalizedPath ([IO.Path]::GetPathRoot($normalizedRoot))

    if ($normalizedRoot -eq $volumeRoot) {
        throw '缓存根目录不能是磁盘根目录'
    }
    if ($normalizedTarget -ne $normalizedRoot -and -not (Test-PathWithinRoot $normalizedTarget $normalizedRoot)) {
        throw '目标超出缓存根目录'
    }

    return [pscustomobject]@{
        CacheRoot = $normalizedRoot
        Target = $normalizedTarget
    }
}

function Assert-NoReparsePointInPath {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Target
    )

    $normalizedRoot = Get-NormalizedPath $Root
    $normalizedTarget = Get-NormalizedPath $Target
    $pathsToCheck = @($normalizedRoot)

    if ($normalizedTarget -ne $normalizedRoot) {
        $relativePath = $normalizedTarget.Substring($normalizedRoot.Length).TrimStart('\', '/')
        $currentPath = $normalizedRoot
        foreach ($component in ($relativePath -split '[\\/]+')) {
            $currentPath = Join-Path $currentPath $component
            $pathsToCheck += $currentPath
        }
    }

    foreach ($pathToCheck in $pathsToCheck) {
        $item = Get-Item -LiteralPath $pathToCheck -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "缓存路径包含重新分析点，已拒绝删除：$pathToCheck"
        }
    }
}

function Get-SafeCacheTree {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$CacheRoot,
        [switch]$RejectReparsePoint
    )

    $items = New-Object System.Collections.ArrayList
    $topLevelItems = New-Object System.Collections.ArrayList
    $rootItem = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        if ($RejectReparsePoint) {
            throw '缓存清理目标是重新分析点，已拒绝删除'
        }
        return [pscustomobject]@{ Items = @(); TopLevelItems = @() }
    }

    $pendingDirectories = New-Object System.Collections.Stack
    $pendingDirectories.Push($rootItem)
    $normalizedRoot = Get-NormalizedPath $Root

    while ($pendingDirectories.Count -gt 0) {
        $directory = $pendingDirectories.Pop()
        $children = @(Get-ChildItem -LiteralPath $directory.FullName -Force -ErrorAction Stop)
        foreach ($child in $children) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                if ($RejectReparsePoint) {
                    throw '缓存内容包含重新分析点，已拒绝删除'
                }
                continue
            }

            $resolvedChild = Resolve-SafePath $child.FullName
            if (-not (Test-PathWithinRoot $resolvedChild $CacheRoot)) {
                throw '缓存内容解析后超出缓存根目录，已拒绝访问'
            }
            $items.Add($child) | Out-Null
            if ((Get-NormalizedPath $directory.FullName) -eq $normalizedRoot) {
                $topLevelItems.Add($child) | Out-Null
            }
            if ($child.PSIsContainer) {
                $pendingDirectories.Push($child)
            }
        }
    }

    return [pscustomobject]@{
        Items = @($items.ToArray())
        TopLevelItems = @($topLevelItems.ToArray())
    }
}

function Clear-ToolCache {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Config,
        [string]$Target = $Config.CacheRoot,
        [switch]$WhatIf
    )

    $safePaths = Assert-SafeCacheTarget -Config $Config -Target $Target
    Assert-NoReparsePointInPath -Root $safePaths.CacheRoot -Target $safePaths.Target
    $tree = Get-SafeCacheTree -Root $safePaths.Target -CacheRoot $safePaths.CacheRoot -RejectReparsePoint

    foreach ($item in $tree.TopLevelItems) {
        Remove-Item -LiteralPath $item.FullName -Recurse -Force -WhatIf:$WhatIf
    }
}

function Get-CacheSummary {
    param([Parameter(Mandatory)]$Config)

    if (-not (Test-Path -LiteralPath $Config.CacheRoot -PathType Container)) {
        return [pscustomobject]@{ Count = 0; Sum = 0 }
    }

    $cacheRoot = Resolve-SafePath ([string]$Config.CacheRoot)
    $tree = Get-SafeCacheTree -Root $cacheRoot -CacheRoot $cacheRoot
    $files = @($tree.Items | Where-Object { -not $_.PSIsContainer })
    $measurement = $files | Measure-Object -Property Length -Sum
    return [pscustomobject]@{
        Count = [int64]$measurement.Count
        Sum = if ($null -eq $measurement.Sum) { [int64]0 } else { [int64]$measurement.Sum }
    }
}

function Get-ToolStatus {
    param([Parameter(Mandatory)]$Config)

    $libreOffice = Get-Command soffice.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $poppler = Get-Command pdftoppm.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $spreadsheetPath = Join-Path $Config.BundledNodeModules '@oai\artifact-tool'

    return [pscustomobject]@{
        PowerShell = $PSVersionTable.PSVersion.ToString()
        Workspace = Test-Path -LiteralPath $Config.WorkspaceRoot -PathType Container
        Cache = Test-Path -LiteralPath $Config.CacheRoot -PathType Container
        Python = Test-Path -LiteralPath $Config.Python -PathType Leaf
        Node = Test-Path -LiteralPath $Config.Node -PathType Leaf
        LibreOffice = $null -ne $libreOffice
        Poppler = $null -ne $poppler
        Spreadsheet = Test-Path -LiteralPath $spreadsheetPath -PathType Container
        LibreOfficePath = if ($null -eq $libreOffice) { $null } else { $libreOffice.Source }
        PopplerPath = if ($null -eq $poppler) { $null } else { $poppler.Source }
        SpreadsheetPath = $spreadsheetPath
    }
}

Export-ModuleMember -Function Get-ToolStatus,Get-CacheSummary,Clear-ToolCache
