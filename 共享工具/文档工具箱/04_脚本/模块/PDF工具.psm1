function Invoke-PdfPreview {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Config
    )

    $command = Get-Command pdftoppm.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command -or -not (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
        throw 'Poppler（pdftoppm）缺失，无法生成 PDF 逐页预览。请手动安装或配置；工具不自动下载依赖。'
    }

    $previewDir = Join-Path $Config.CacheRoot ("图片预览\{0}_{1}" -f [IO.Path]::GetFileNameWithoutExtension($Path), [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $previewDir | Out-Null
    $prefix = Join-Path $previewDir 'page'
    & $command.Source -png $Path $prefix | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Poppler 预览失败，退出码：$LASTEXITCODE"
    }

    $images = @(Get-ChildItem -LiteralPath $previewDir -Filter 'page-*.png' -File | Sort-Object Name)
    if ($images.Count -eq 0) {
        throw 'Poppler 未生成任何 PDF 预览图片'
    }
    foreach ($image in $images) {
        if ($image.Length -le 0) { throw "PDF 预览图片为空：$($image.FullName)" }
    }

    return ([ordered]@{
            type = 'pdf'
            action = 'preview'
            outputs = @($images.FullName)
        } | ConvertTo-Json -Compress)
}

function Invoke-PdfCommand {
    param(
        [Parameter(Mandatory)][string]$Action,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Config
    )

    if ($Action -eq '预览') {
        return Invoke-PdfPreview -Path $Path -Config $Config
    }
    if ($Action -eq '转PDF') {
        throw '输入文件已经是 PDF，无需再次转换。'
    }
    return Invoke-DocumentPython -Mode $Action -Path $Path -Config $Config
}

Export-ModuleMember -Function Invoke-PdfCommand,Invoke-PdfPreview
