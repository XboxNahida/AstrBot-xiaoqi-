function Invoke-DocumentPython {
    param(
        [Parameter(Mandatory)][string]$Mode,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Config
    )

    $pythonMode = switch ($Mode) {
        '检查' { 'inspect' }
        '提取文本' { 'extract' }
        default { throw "不支持的文档 Python 操作：$Mode" }
    }
    if (-not (Test-Path -LiteralPath $Config.Python -PathType Leaf)) {
        throw "Codex 捆绑 Python 缺失：$($Config.Python)。请检查工具配置；工具不自动下载依赖。"
    }

    $helper = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\辅助程序\文档处理.py'))
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw "文档处理辅助程序不存在：$helper"
    }

    $arguments = @($helper, $pythonMode, $Path, '--output-root', $Config.OutputRoot)
    $utf8 = New-Object Text.UTF8Encoding($false)
    $previousConsoleOutputEncoding = [Console]::OutputEncoding
    $previousOutputEncoding = $OutputEncoding
    $previousPythonIoEncoding = $env:PYTHONIOENCODING
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        [Console]::OutputEncoding = $utf8
        $OutputEncoding = $utf8
        $env:PYTHONIOENCODING = 'utf-8'
        $ErrorActionPreference = 'Continue'
        $pythonOutput = @(& $Config.Python @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        [Console]::OutputEncoding = $previousConsoleOutputEncoding
        $OutputEncoding = $previousOutputEncoding
        $env:PYTHONIOENCODING = $previousPythonIoEncoding
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        $diagnostic = $pythonOutput -join [Environment]::NewLine
        throw "文档处理失败，Python 退出码 $exitCode：$diagnostic"
    }

    $json = $pythonOutput -join [Environment]::NewLine
    try {
        $null = $json | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "文档处理协议错误：Python 标准输出不是有效 JSON"
    }
    return $json
}

function Invoke-LibreOfficeConversion {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Config,
        [ValidateSet('pdf','docx')][string]$Format = 'pdf'
    )

    $command = Get-Command soffice.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command -or -not (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
        throw 'LibreOffice 缺失，无法转换 Word 文档。请手动安装或配置；工具不自动下载依赖。'
    }

    $target = Join-Path $Config.CacheRoot ("Word与PDF转换\{0}" -f [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    & $command.Source --headless --convert-to $Format --outdir $target $Path | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "LibreOffice 转换失败，退出码：$LASTEXITCODE"
    }

    $expectedName = "{0}.{1}" -f [IO.Path]::GetFileNameWithoutExtension($Path), $Format
    $convertedFile = Get-ChildItem -LiteralPath $target -Filter $expectedName -File | Select-Object -First 1
    if ($null -eq $convertedFile -or $convertedFile.Length -le 0) {
        throw "LibreOffice 未生成有效 $Format 文件"
    }
    return $convertedFile
}

function Add-ConversionMetadata {
    param(
        [Parameter(Mandatory)][string]$Json,
        [Parameter(Mandatory)][string]$SourceType,
        [Parameter(Mandatory)][string]$ConvertedType
    )

    $data = $Json | ConvertFrom-Json -ErrorAction Stop
    $result = [ordered]@{
        sourceType = $SourceType
        convertedType = $ConvertedType
    }
    foreach ($property in $data.PSObject.Properties) {
        $result[$property.Name] = $property.Value
    }
    return ($result | ConvertTo-Json -Compress -Depth 6)
}

function Invoke-WordCommand {
    param(
        [Parameter(Mandatory)][string]$Action,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Config
    )

    $sourceType = [IO.Path]::GetExtension($Path).TrimStart('.').ToLowerInvariant()
    if ($sourceType -eq 'doc' -and ($Action -eq '检查' -or $Action -eq '提取文本')) {
        $docx = Invoke-LibreOfficeConversion -Path $Path -Config $Config -Format 'docx'
        $json = Invoke-DocumentPython -Mode $Action -Path $docx.FullName -Config $Config
        return Add-ConversionMetadata -Json $json -SourceType 'doc' -ConvertedType 'docx'
    }

    if ($Action -eq '转PDF' -or $Action -eq '预览') {
        $pdf = Invoke-LibreOfficeConversion -Path $Path -Config $Config -Format 'pdf'
        if ($Action -eq '转PDF') {
            $inspectionJson = Invoke-DocumentPython -Mode '检查' -Path $pdf.FullName -Config $Config
            $inspection = $inspectionJson | ConvertFrom-Json -ErrorAction Stop
            if ($inspection.type -ne 'pdf' -or [int]$inspection.pages -le 0) {
                throw '转换后的 PDF 检查结果无效'
            }

            $previewJson = Invoke-PdfPreview -Path $pdf.FullName -Config $Config
            $preview = $previewJson | ConvertFrom-Json -ErrorAction Stop
            $previewOutputs = @($preview.outputs)
            if ($previewOutputs.Count -ne [int]$inspection.pages) {
                throw ("转换后的 PDF 共 {0} 页，但生成了 {1} 张预览 PNG" -f $inspection.pages, $previewOutputs.Count)
            }
            foreach ($previewOutput in $previewOutputs) {
                if (-not (Test-Path -LiteralPath $previewOutput -PathType Leaf) -or
                    (Get-Item -LiteralPath $previewOutput).Length -le 0) {
                    throw "转换后的 PDF 预览图片无效：$previewOutput"
                }
            }

            New-Item -ItemType Directory -Force -Path $Config.OutputRoot | Out-Null
            $output = New-UniqueOutputPath $Config.OutputRoot $pdf.Name
            Copy-Item -LiteralPath $pdf.FullName -Destination $output
            if (-not (Test-Path -LiteralPath $output -PathType Leaf) -or (Get-Item -LiteralPath $output).Length -le 0) {
                throw 'PDF 成品复制失败或文件为空'
            }
            return ([ordered]@{
                    type = 'pdf'
                    sourceType = $sourceType
                    convertedType = 'pdf'
                    action = 'convert_pdf'
                    output = [IO.Path]::GetFullPath($output)
                    pages = [int]$inspection.pages
                    previewOutputs = @($previewOutputs)
                } | ConvertTo-Json -Compress -Depth 4)
        }
        return Invoke-PdfPreview -Path $pdf.FullName -Config $Config
    }

    return Invoke-DocumentPython -Mode $Action -Path $Path -Config $Config
}

Export-ModuleMember -Function Invoke-WordCommand,Invoke-DocumentPython,Invoke-LibreOfficeConversion
