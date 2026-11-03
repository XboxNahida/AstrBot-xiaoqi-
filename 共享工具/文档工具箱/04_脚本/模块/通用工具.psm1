function Get-ToolConfig {
    param([Parameter(Mandatory)][string]$ConfigPath)

    $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $configDirectory = Split-Path -Parent (Resolve-Path -LiteralPath $ConfigPath).Path
    foreach ($name in 'WorkspaceRoot','OutputRoot','CacheRoot','Python','Node','BundledNodeModules','NodeModulesLink') {
        $value = [Environment]::ExpandEnvironmentVariables([string]$raw.$name)
        if (-not [IO.Path]::IsPathRooted($value)) { $value = Join-Path $configDirectory $value }
        $raw.$name = [IO.Path]::GetFullPath($value)
    }
    return $raw
}

function Resolve-SafePath {
    param([Parameter(Mandatory)][string]$Path)

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    return [IO.Path]::GetFullPath($resolved.Path)
}

function New-UniqueOutputPath {
    param([string]$Directory,[string]$FileName)

    $candidate = Join-Path $Directory $FileName
    if (-not (Test-Path -LiteralPath $candidate)) { return $candidate }
    $stem = [IO.Path]::GetFileNameWithoutExtension($FileName)
    $ext = [IO.Path]::GetExtension($FileName)
    $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $candidate = Join-Path $Directory ("{0}_{1}{2}" -f $stem,$timestamp,$ext)
    $suffix = 2
    while (Test-Path -LiteralPath $candidate) {
        $candidate = Join-Path $Directory ("{0}_{1}_{2}{3}" -f $stem,$timestamp,$suffix,$ext)
        $suffix++
    }
    return $candidate
}

function Invoke-ExternalTool {
    param([Parameter(Mandatory)][string]$Executable,[Parameter(Mandatory)][string[]]$Arguments)

    if (-not (Test-Path -LiteralPath $Executable)) { throw "依赖不存在：$Executable" }
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "外部工具执行失败，退出码：$LASTEXITCODE" }
}

function ConvertTo-MarkdownCell {
    param([AllowNull()][string]$Value)

    if ($null -eq $Value) { return '' }
    return $Value.Replace('\','\\').Replace('|','\|').Replace("`r`n",'<br>').Replace("`r",'<br>').Replace("`n",'<br>')
}

function Export-DirectoryInventory {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Config
    )

    $sourcePath = Resolve-SafePath $Path
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
        throw "输入路径不是目录：$sourcePath"
    }

    $rows = @(Get-ChildItem -LiteralPath $sourcePath -File -Recurse -ErrorAction Stop |
        Sort-Object FullName |
        ForEach-Object {
            [pscustomobject][ordered]@{
                '文件名' = $_.Name
                '类型' = $_.Extension.ToLowerInvariant()
                '大小字节' = $_.Length
                '修改时间' = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                '完整路径' = $_.FullName
            }
        })

    New-Item -ItemType Directory -Force -Path $Config.OutputRoot | Out-Null
    $csvPath = New-UniqueOutputPath $Config.OutputRoot '文件清单.csv'
    $markdownPath = New-UniqueOutputPath $Config.OutputRoot '文件清单.md'
    $utf8 = New-Object Text.UTF8Encoding($true)

    $csvLines = @($rows | ConvertTo-Csv -NoTypeInformation)
    if ($csvLines.Count -eq 0) {
        $csvLines = @('"文件名","类型","大小字节","修改时间","完整路径"')
    }
    [IO.File]::WriteAllLines($csvPath, [string[]]$csvLines, $utf8)

    $markdownLines = New-Object 'System.Collections.Generic.List[string]'
    $markdownLines.Add('# 目录文件清单')
    $markdownLines.Add('')
    $markdownLines.Add(('扫描目录：`{0}`' -f $sourcePath))
    $markdownLines.Add(("文件数量：{0}" -f $rows.Count))
    $markdownLines.Add('')
    $markdownLines.Add('| 文件名 | 类型 | 大小字节 | 修改时间 | 完整路径 |')
    $markdownLines.Add('|---|---|---:|---|---|')
    foreach ($row in $rows) {
        $values = @($row.'文件名',$row.'类型',$row.'大小字节',$row.'修改时间',$row.'完整路径') | ForEach-Object { ConvertTo-MarkdownCell ([string]$_) }
        $markdownLines.Add(("| {0} | {1} | {2} | {3} | {4} |" -f $values))
    }
    [IO.File]::WriteAllLines($markdownPath, $markdownLines.ToArray(), $utf8)

    return [pscustomobject][ordered]@{
        command = '整理'
        source = $sourcePath
        count = $rows.Count
        csv = $csvPath
        markdown = $markdownPath
    }
}

Export-ModuleMember -Function Get-ToolConfig,Resolve-SafePath,New-UniqueOutputPath,Invoke-ExternalTool,Export-DirectoryInventory
