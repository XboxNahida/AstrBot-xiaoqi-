param(
    [Parameter(Position = 0)][string]$Command = '帮助',
    [Parameter(Position = 1)][string]$Path
)

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot '模块\通用工具.psm1') -Force
Import-Module (Join-Path $PSScriptRoot '模块\缓存工具.psm1') -Force
Import-Module (Join-Path $PSScriptRoot '模块\Word工具.psm1') -Force
Import-Module (Join-Path $PSScriptRoot '模块\PDF工具.psm1') -Force
Import-Module (Join-Path $PSScriptRoot '模块\表格工具.psm1') -Force -DisableNameChecking

$config = Get-ToolConfig (Join-Path $PSScriptRoot '工具配置.json')

function Write-DependencyStatus {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][bool]$Available,
        [string]$Location
    )

    if ($Available) {
        if ([string]::IsNullOrWhiteSpace($Location)) {
            Write-Output ("{0}：可用" -f $Name)
        }
        else {
            Write-Output ("{0}：可用（{1}）" -f $Name, $Location)
        }
        return
    }

    if ([string]::IsNullOrWhiteSpace($Location)) {
        Write-Output ("{0}：缺失，请手动安装或配置；工具不自动下载依赖。" -f $Name)
    }
    else {
        Write-Output ("{0}：缺失（{1}），请手动安装或配置；工具不自动下载依赖。" -f $Name, $Location)
    }
}

function Invoke-FormatCommand {
    param(
        [Parameter(Mandatory)][string]$Action,
        [string]$InputPath
    )

    if ([string]::IsNullOrWhiteSpace($InputPath)) {
        throw ('命令“{0}”需要提供文件路径。' -f $Action)
    }
    $resolvedPath = Resolve-SafePath $InputPath
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        throw "输入路径不是文件：$resolvedPath"
    }

    $extension = [IO.Path]::GetExtension($resolvedPath).ToLowerInvariant()
    switch ($extension) {
        '.doc' { return Invoke-WordCommand -Action $Action -Path $resolvedPath -Config $config }
        '.docx' { return Invoke-WordCommand -Action $Action -Path $resolvedPath -Config $config }
        '.pdf' { return Invoke-PdfCommand -Action $Action -Path $resolvedPath -Config $config }
        '.xlsx' { return Invoke-SpreadsheetCommand -Action $Action -Path $resolvedPath -Config $config }
        default { throw "暂不支持此文件格式：$extension" }
    }
}

switch ($Command.ToLowerInvariant()) {
    '状态' {
        $status = Get-ToolStatus $config
        Write-Output ("PowerShell：可用（{0}）" -f $status.PowerShell)
        Write-DependencyStatus '工作区' $status.Workspace $config.WorkspaceRoot
        Write-DependencyStatus '缓存目录' $status.Cache $config.CacheRoot
        Write-DependencyStatus 'Python' $status.Python $config.Python
        Write-DependencyStatus 'Node.js' $status.Node $config.Node
        Write-DependencyStatus 'LibreOffice' $status.LibreOffice $status.LibreOfficePath
        Write-DependencyStatus 'Poppler' $status.Poppler $status.PopplerPath
        Write-DependencyStatus '表格依赖' $status.Spreadsheet $status.SpreadsheetPath
        exit 0
    }
    '清理缓存' {
        $before = Get-CacheSummary $config
        Clear-ToolCache $config
        Write-Output ("缓存清理完成：删除前共 {0} 个文件，{1} 字节。" -f $before.Count, $before.Sum)
        exit 0
    }
    '整理' {
        try {
            if ([string]::IsNullOrWhiteSpace($Path)) { throw '命令“整理”需要提供目录路径。' }
            $result = Export-DirectoryInventory -Path $Path -Config $config
            Write-Output ($result | ConvertTo-Json -Compress)
            exit 0
        }
        catch {
            [Console]::Error.WriteLine("错误：{0}" -f $_.Exception.Message)
            exit 1
        }
    }
    '检查' {
        try {
            Write-Output (Invoke-FormatCommand -Action '检查' -InputPath $Path)
            exit 0
        }
        catch {
            [Console]::Error.WriteLine("错误：{0}" -f $_.Exception.Message)
            exit 1
        }
    }
    '提取文本' {
        try {
            Write-Output (Invoke-FormatCommand -Action '提取文本' -InputPath $Path)
            exit 0
        }
        catch {
            [Console]::Error.WriteLine("错误：{0}" -f $_.Exception.Message)
            exit 1
        }
    }
    '预览' {
        try {
            Write-Output (Invoke-FormatCommand -Action '预览' -InputPath $Path)
            exit 0
        }
        catch {
            [Console]::Error.WriteLine("错误：{0}" -f $_.Exception.Message)
            exit 1
        }
    }
    '转pdf' {
        try {
            Write-Output (Invoke-FormatCommand -Action '转PDF' -InputPath $Path)
            exit 0
        }
        catch {
            [Console]::Error.WriteLine("错误：{0}" -f $_.Exception.Message)
            exit 1
        }
    }
    default {
        Write-Output '命令：状态、整理、检查、预览、提取文本、转PDF、清理缓存'
        exit 2
    }
}
