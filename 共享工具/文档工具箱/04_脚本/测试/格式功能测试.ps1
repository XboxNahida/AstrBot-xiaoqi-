param(
    [ValidateSet('WordPdf','Spreadsheet','All')]
    [string]$Group = 'WordPdf'
)

$ErrorActionPreference = 'Stop'

if ($Group -eq 'Spreadsheet') {
    $spreadsheetModule = Join-Path $PSScriptRoot '..\模块\表格工具.psm1'
    if (-not (Test-Path -LiteralPath $spreadsheetModule -PathType Leaf)) {
        throw '表格工具.psm1 不存在，无法执行 Spreadsheet 测试'
    }

    Import-Module "$PSScriptRoot\..\模块\通用工具.psm1" -Force
    Import-Module $spreadsheetModule -Force -DisableNameChecking
    $config = Get-ToolConfig "$PSScriptRoot\..\工具配置.json"
    $entry = Join-Path $PSScriptRoot '..\文档工具.ps1'
    $hostExecutable = (Get-Process -Id $PID -ErrorAction Stop).Path
    $fixtureRoot = Join-Path $config.CacheRoot ("Spreadsheet测试_{0}" -f [guid]::NewGuid().ToString('N'))
    $fixturePath = Join-Path $fixtureRoot '中文 空格（多工作表）.xlsx'
    $errorFixturePath = Join-Path $fixtureRoot '公式错误.xlsx'
    $badFixturePath = Join-Path $fixtureRoot '损坏 表格（测试）.xlsx'
    $builderPath = Join-Path (Split-Path -Parent $config.NodeModulesLink) ("生成表格夹具_{0}.mjs" -f [guid]::NewGuid().ToString('N'))
    $createdOutputs = @()
    $createdPreviewRoot = $null
    $securityId = [guid]::NewGuid().ToString('N')
    $securityCacheRoot = Join-Path $config.CacheRoot ("表格链接安全测试_{0}" -f $securityId)
    $securityExternalRoot = Join-Path $config.OutputRoot ("表格链接外部标记_{0}" -f $securityId)
    $securityMarker = Join-Path $securityExternalRoot '禁止修改.txt'
    $securityLinks = New-Object Collections.Generic.List[string]

    function Invoke-SpreadsheetToolCommand {
        param([string]$Command,[string]$InputPath)
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $output = @(& $hostExecutable -NoProfile -ExecutionPolicy Bypass -File $entry $Command $InputPath 2>&1)
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        return [pscustomobject]@{ ExitCode = $exitCode; Text = ($output -join [Environment]::NewLine) }
    }

    function ConvertFrom-SpreadsheetJson {
        param($Result)
        if ($Result.ExitCode -ne 0) { throw "命令失败，退出码 $($Result.ExitCode)：$($Result.Text)" }
        try { return $Result.Text | ConvertFrom-Json -ErrorAction Stop }
        catch { throw "标准输出不是纯 JSON：$($Result.Text)" }
    }

    function Assert-SpreadsheetPathUnderRoot {
        param([string]$Path,[string]$Root,[string]$Message)
        $fullPath = [IO.Path]::GetFullPath($Path)
        $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        if (-not $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "$Message：$fullPath"
        }
    }

    function Copy-SpreadsheetConfig {
        param($Source)
        return ($Source | ConvertTo-Json -Depth 6 | ConvertFrom-Json)
    }

    function Assert-NodeModulesConfigRejected {
        param($TestConfig,[string]$Scenario,[string]$MarkerHash)
        $rejected = $false
        $message = ''
        try { Ensure-NodeModulesLink -Config $TestConfig }
        catch { $rejected = $true; $message = $_.Exception.Message }
        if (-not $rejected) { throw "$Scenario 未拒绝不安全的 node_modules 配置" }
        if ($message -notmatch '缓存|路径|越界|Junction|重新分析|链接') {
            throw "$Scenario 的中文诊断不明确：$message"
        }
        if ((Get-FileHash -LiteralPath $securityMarker -Algorithm SHA256).Hash -ne $MarkerHash) {
            throw "$Scenario 修改了缓存外标记文件"
        }
        $helperCopies = @(Get-ChildItem -LiteralPath $securityExternalRoot -Filter '表格处理_*.mjs' -File -Recurse -ErrorAction SilentlyContinue)
        if ($helperCopies.Count -ne 0) { throw "$Scenario 在缓存外复制了 helper" }
    }

    try {
        New-Item -ItemType Directory -Force -Path $fixtureRoot,$config.OutputRoot | Out-Null
        Ensure-NodeModulesLink -Config $config
        if (-not (Test-Path -LiteralPath $config.Node -PathType Leaf)) { throw "Codex 捆绑 Node.js 不存在：$($config.Node)" }

        $fixtureBuilder = @'
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [normalPath, errorPath] = process.argv.slice(2);
const normal = Workbook.create();
const summary = normal.worksheets.add("汇总 表");
summary.getRange("A1:C4").values = [
  ["项目", "值", "说明"],
  ["收入", 100, "这是一段用于验证长文本检查边界的中文内容。".repeat(20)],
  ["成本", 40, "正常"],
  ["利润", null, "公式单元格"],
];
summary.getRange("B4").formulas = [["=B2-B3"]];
const detail = normal.worksheets.add("明细（2026）");
detail.getRange("A1:B3").values = [["名称", "数量"], ["甲", 2], ["乙", 3]];
detail.getRange("B4").formulas = [["=SUM(B2:B3)"]];
await (await SpreadsheetFile.exportXlsx(normal)).save(normalPath);

const broken = Workbook.create();
const brokenSheet = broken.worksheets.add("错误公式");
brokenSheet.getRange("A1:B2").values = [["分子", "分母"], [1, 0]];
brokenSheet.getRange("C2").formulas = [["=A2/B2"]];
await (await SpreadsheetFile.exportXlsx(broken)).save(errorPath);
'@
        [IO.File]::WriteAllText($builderPath, $fixtureBuilder, (New-Object Text.UTF8Encoding($false)))
        & $config.Node $builderPath $fixturePath $errorFixturePath
        if ($LASTEXITCODE -ne 0) { throw '无法使用 Codex 捆绑 Node.js 和 artifact-tool 生成 XLSX 测试夹具' }
        Set-Content -LiteralPath $badFixturePath -Value '不是有效的 XLSX' -Encoding UTF8

        New-Item -ItemType Directory -Force -Path $securityCacheRoot,$securityExternalRoot | Out-Null
        Set-Content -LiteralPath $securityMarker -Value '缓存外标记不得改变' -Encoding UTF8
        $markerHash = (Get-FileHash -LiteralPath $securityMarker -Algorithm SHA256).Hash

        $outsideConfig = Copy-SpreadsheetConfig $config
        $outsideConfig.CacheRoot = $securityCacheRoot
        $outsideConfig.NodeModulesLink = Join-Path $securityExternalRoot '越界配置\node_modules'
        $securityLinks.Add([string]$outsideConfig.NodeModulesLink)
        Assert-NodeModulesConfigRejected $outsideConfig 'NodeModulesLink 词法越界' $markerHash

        $rootTarget = Join-Path $securityExternalRoot '缓存根目标'
        $rootLink = Join-Path $config.CacheRoot ("缓存根链接_{0}" -f $securityId)
        New-Item -ItemType Directory -Force -Path $rootTarget | Out-Null
        New-Item -ItemType Junction -Path $rootLink -Target $rootTarget | Out-Null
        $securityLinks.Add($rootLink)
        $rootLinkConfig = Copy-SpreadsheetConfig $config
        $rootLinkConfig.CacheRoot = $rootLink
        $rootLinkConfig.NodeModulesLink = Join-Path $rootLink '依赖缓存\node_modules'
        $securityLinks.Add([string]$rootLinkConfig.NodeModulesLink)
        Assert-NodeModulesConfigRejected $rootLinkConfig 'CacheRoot 自身 Junction' $markerHash

        $parentTarget = Join-Path $securityExternalRoot '父目录目标'
        $parentLink = Join-Path $securityCacheRoot '父目录链接'
        New-Item -ItemType Directory -Force -Path $parentTarget | Out-Null
        New-Item -ItemType Junction -Path $parentLink -Target $parentTarget | Out-Null
        $securityLinks.Add($parentLink)
        $parentConfig = Copy-SpreadsheetConfig $config
        $parentConfig.CacheRoot = $securityCacheRoot
        $parentConfig.NodeModulesLink = Join-Path $parentLink '依赖缓存\node_modules'
        $securityLinks.Add([string]$parentConfig.NodeModulesLink)
        Assert-NodeModulesConfigRejected $parentConfig '父目录 Junction' $markerHash

        $regularConfig = Copy-SpreadsheetConfig $config
        $regularConfig.CacheRoot = $securityCacheRoot
        $regularConfig.NodeModulesLink = Join-Path $securityCacheRoot '普通目录\node_modules'
        New-Item -ItemType Directory -Force -Path $regularConfig.NodeModulesLink | Out-Null
        Assert-NodeModulesConfigRejected $regularConfig '最终项普通目录' $markerHash

        $wrongTarget = Join-Path $securityExternalRoot '错误依赖目标'
        $wrongConfig = Copy-SpreadsheetConfig $config
        $wrongConfig.CacheRoot = $securityCacheRoot
        $wrongConfig.NodeModulesLink = Join-Path $securityCacheRoot '错误目标\node_modules'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $wrongConfig.NodeModulesLink),$wrongTarget | Out-Null
        New-Item -ItemType Junction -Path $wrongConfig.NodeModulesLink -Target $wrongTarget | Out-Null
        $securityLinks.Add($wrongConfig.NodeModulesLink)
        Assert-NodeModulesConfigRejected $wrongConfig '最终 Junction 错误目标' $markerHash

        $sourceHashes = @{}
        foreach ($source in $fixturePath,$errorFixturePath,$badFixturePath) {
            $sourceHashes[$source] = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
        }

        $inspection = ConvertFrom-SpreadsheetJson (Invoke-SpreadsheetToolCommand '检查' $fixturePath)
        if ($inspection.type -ne 'xlsx' -or $inspection.action -ne 'inspect') { throw 'XLSX 检查响应字段错误' }
        Assert-SpreadsheetPathUnderRoot $inspection.report $config.OutputRoot 'XLSX 检查报告未写入成品目录'
        $createdOutputs += [string]$inspection.report
        $report = Get-Content -LiteralPath $inspection.report -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace([string]$report.summary)) { throw 'XLSX 检查报告缺少摘要 NDJSON' }
        if ($report.summary -notmatch '汇总 表' -or $report.summary -notmatch '明细（2026）') { throw 'XLSX 检查报告未列出所有工作表' }
        if ($report.summary.Length -gt 20000) { throw 'XLSX 检查报告未限制超大内容' }
        if ([int]$report.errorCount -ne 0) { throw "正常 XLSX 被误报公式错误：$($report.errors)" }

        $errorInspection = ConvertFrom-SpreadsheetJson (Invoke-SpreadsheetToolCommand '检查' $errorFixturePath)
        $createdOutputs += [string]$errorInspection.report
        $errorReport = Get-Content -LiteralPath $errorInspection.report -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        if ([int]$errorReport.errorCount -lt 1 -or $errorReport.errors -notmatch '#DIV/0!') {
            throw '未扫描到公式错误 #DIV/0!'
        }

        $preview = ConvertFrom-SpreadsheetJson (Invoke-SpreadsheetToolCommand '预览' $fixturePath)
        if ($preview.type -ne 'xlsx' -or $preview.action -ne 'render') { throw 'XLSX 预览响应字段错误' }
        if (@($preview.outputs).Count -gt 0) {
            $createdPreviewRoot = Split-Path -Parent ([string]@($preview.outputs)[0])
        }
        $expectedSheetNames = @('汇总 表','明细（2026）')
        if (@($preview.previews).Count -ne $expectedSheetNames.Count -or
            @($preview.outputs).Count -ne $expectedSheetNames.Count) {
            throw '预览数量未与工作表数量一一对应'
        }
        for ($previewIndex = 0; $previewIndex -lt $expectedSheetNames.Count; $previewIndex++) {
            $mapping = @($preview.previews)[$previewIndex]
            if ($mapping.sheetName -ne $expectedSheetNames[$previewIndex] -or
                $mapping.path -ne @($preview.outputs)[$previewIndex]) {
                throw "工作表预览映射错误：索引 $previewIndex"
            }
        }
        foreach ($previewPath in @($preview.outputs)) {
            Assert-SpreadsheetPathUnderRoot $previewPath $config.CacheRoot 'XLSX 预览未写入缓存目录'
            if ([IO.Path]::GetExtension($previewPath) -ne '.png' -or (Get-Item -LiteralPath $previewPath).Length -le 0) {
                throw "XLSX 预览无效：$previewPath"
            }
        }

        $badResult = Invoke-SpreadsheetToolCommand '检查' $badFixturePath
        if ($badResult.ExitCode -eq 0) { throw '损坏 XLSX 未返回失败' }
        if ($badResult.Text -notmatch 'XLSX|表格|损坏|导入') { throw "损坏 XLSX 诊断不明确：$($badResult.Text)" }

        foreach ($source in $fixturePath,$errorFixturePath,$badFixturePath) {
            if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $sourceHashes[$source]) {
                throw "XLSX 输入文件被修改：$source"
            }
        }
        Write-Output '格式功能测试（Spreadsheet）：通过'
    }
    finally {
        foreach ($outputPath in $createdOutputs) {
            if (Test-Path -LiteralPath $outputPath -PathType Leaf) { Remove-Item -LiteralPath $outputPath -Force }
        }
        if (Test-Path -LiteralPath $fixtureRoot) { Remove-Item -LiteralPath $fixtureRoot -Recurse -Force }
        if (Test-Path -LiteralPath $builderPath -PathType Leaf) { Remove-Item -LiteralPath $builderPath -Force }
        if (-not [string]::IsNullOrWhiteSpace($createdPreviewRoot) -and
            (Test-Path -LiteralPath $createdPreviewRoot -PathType Container)) {
            Remove-Item -LiteralPath $createdPreviewRoot -Recurse -Force
        }
        foreach ($link in @($securityLinks | Sort-Object Length -Descending)) {
            if (Test-Path -LiteralPath $link) {
                $linkItem = Get-Item -LiteralPath $link -Force
                if (($linkItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    [IO.Directory]::Delete([string]$linkItem.FullName, $false)
                }
            }
        }
        if (Test-Path -LiteralPath $securityCacheRoot -PathType Container) {
            Remove-Item -LiteralPath $securityCacheRoot -Recurse -Force
        }
        if (Test-Path -LiteralPath $securityExternalRoot -PathType Container) {
            Remove-Item -LiteralPath $securityExternalRoot -Recurse -Force
        }
    }
    exit 0
}

Import-Module "$PSScriptRoot\..\模块\通用工具.psm1" -Force
$config = Get-ToolConfig "$PSScriptRoot\..\工具配置.json"
$entry = Join-Path $PSScriptRoot '..\文档工具.ps1'
$wordModule = Join-Path $PSScriptRoot '..\模块\Word工具.psm1'
$pdfModule = Join-Path $PSScriptRoot '..\模块\PDF工具.psm1'
$hostExecutable = (Get-Process -Id $PID -ErrorAction Stop).Path

function Invoke-ToolCommand {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][string]$InputPath
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $hostExecutable -NoProfile -ExecutionPolicy Bypass -File $entry $Command $InputPath 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
        Text = $output -join [Environment]::NewLine
    }
}

function ConvertFrom-ToolJson {
    param([Parameter(Mandatory)]$Result)

    if ($Result.ExitCode -ne 0) {
        throw "命令未成功，退出码 $($Result.ExitCode)：$($Result.Text)"
    }
    try {
        return $Result.Text | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "标准输出不是纯 JSON：$($Result.Text)"
    }
}

function Assert-PathUnderRoot {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Message
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Message：$fullPath"
    }
}

function Get-UsableToolCommand {
    param([Parameter(Mandatory)][string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command -or
        [string]::IsNullOrWhiteSpace([string]$command.Source) -or
        -not (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
        return $null
    }
    return $command
}

if (-not (Test-Path -LiteralPath $wordModule -PathType Leaf)) {
    throw 'Word 模块不存在，无法执行格式功能测试'
}
if (-not (Test-Path -LiteralPath $pdfModule -PathType Leaf)) {
    throw 'PDF 模块不存在，无法执行格式功能测试'
}
if (-not (Test-Path -LiteralPath $config.Python -PathType Leaf)) {
    throw "Codex 捆绑 Python 不存在：$($config.Python)"
}

$fixtureRoot = Join-Path $config.CacheRoot ("WordPdf测试_{0}" -f [guid]::NewGuid().ToString('N'))
$docxPath = Join-Path $fixtureRoot '中文 空格（夹具）.docx'
$docPath = [IO.Path]::ChangeExtension($docxPath, '.doc')
$pdfPath = Join-Path $fixtureRoot 'PDF fixture (sample).pdf'
$badDocxPath = Join-Path $fixtureRoot '损坏.docx'
$createdOutputs = @()

try {
    New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $config.OutputRoot | Out-Null

    $fixtureBuilder = @'
from pathlib import Path
import sys

from docx import Document
from reportlab.pdfgen import canvas

docx_path = Path(sys.argv[1])
pdf_path = Path(sys.argv[2])

document = Document()
document.add_paragraph('\u7b2c\u4e00\u6bb5\uff1aDOCX fixture text')
document.add_paragraph('\u7b2c\u4e8c\u6bb5\uff1a\u7528\u4e8e\u6587\u672c\u63d0\u53d6')
table = document.add_table(rows=2, cols=2)
table.cell(0, 0).text = '\u9879\u76ee'
table.cell(0, 1).text = '\u503c'
table.cell(1, 0).text = '\u72b6\u6001'
table.cell(1, 1).text = '\u6b63\u5e38'
document.save(docx_path)

pdf = canvas.Canvas(str(pdf_path))
pdf.setTitle('PDF fixture')
pdf.drawString(72, 720, 'PDF fixture text')
pdf.save()
'@
    & $config.Python -c $fixtureBuilder $docxPath $pdfPath
    if ($LASTEXITCODE -ne 0) { throw '无法用 Codex 捆绑 Python 生成 DOCX/PDF 测试夹具' }
    Set-Content -LiteralPath $badDocxPath -Value '非OOXML文件' -Encoding UTF8

    $soffice = Get-UsableToolCommand -Name 'soffice.exe'
    $pdftoppm = Get-UsableToolCommand -Name 'pdftoppm.exe'
    if ($null -ne $soffice) {
        & $soffice.Source --headless --convert-to doc --outdir $fixtureRoot $docxPath | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $docPath -PathType Leaf)) {
            throw 'LibreOffice 可用但未能生成真实 DOC 测试夹具'
        }
    }
    else {
        Set-Content -LiteralPath $docPath -Value 'LibreOffice 缺失分支的 DOC 占位输入' -Encoding UTF8
    }

    $atomicOutputRoot = Join-Path $fixtureRoot '原子输出测试'
    $pythonHelper = Join-Path $PSScriptRoot '..\辅助程序\文档处理.py'
    $atomicCreationTest = @'
import importlib.util
from pathlib import Path
import sys

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('document_tool', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
source = Path(sys.argv[2])
output_root = Path(sys.argv[3])
output_root.mkdir(parents=True, exist_ok=True)
reserved = output_root / (source.stem + '_\u63d0\u53d6\u6587\u672c.txt')
reserved.write_text('reserved', encoding='utf-8')
first = module.write_unique_text(output_root, source, 'first')
second = module.write_unique_text(output_root, source, 'second')
assert reserved.read_text(encoding='utf-8') == 'reserved'
assert first != second
assert first.read_text(encoding='utf-8') == 'first'
assert second.read_text(encoding='utf-8') == 'second'
'@
    & $config.Python -c $atomicCreationTest $pythonHelper $docxPath $atomicOutputRoot
    if ($LASTEXITCODE -ne 0) { throw 'Python 唯一文本输出未使用原子 CreateNew 语义' }

    $sourceHashes = @{
        Docx = (Get-FileHash -LiteralPath $docxPath -Algorithm SHA256).Hash
        Doc = (Get-FileHash -LiteralPath $docPath -Algorithm SHA256).Hash
        Pdf = (Get-FileHash -LiteralPath $pdfPath -Algorithm SHA256).Hash
        BadDocx = (Get-FileHash -LiteralPath $badDocxPath -Algorithm SHA256).Hash
    }

    $badResult = Invoke-ToolCommand -Command '检查' -InputPath $badDocxPath
    if ($badResult.ExitCode -eq 0) { throw '损坏 DOCX 未返回失败' }
    if ($badResult.Text -notmatch 'DOCX|OOXML|压缩包|损坏') {
        throw "损坏 DOCX 诊断不明确：$($badResult.Text)"
    }

    $docxInspection = ConvertFrom-ToolJson (Invoke-ToolCommand -Command '检查' -InputPath $docxPath)
    if ($docxInspection.type -ne 'docx') { throw 'DOCX 检查类型错误' }
    if ($docxInspection.paragraphs -ne 2) { throw "DOCX 段落数错误：$($docxInspection.paragraphs)" }
    if ($docxInspection.tables -ne 1) { throw "DOCX 表格数错误：$($docxInspection.tables)" }
    if ($docxInspection.images -ne 0) { throw "DOCX 图片数错误：$($docxInspection.images)" }

    $pdfInspection = ConvertFrom-ToolJson (Invoke-ToolCommand -Command '检查' -InputPath $pdfPath)
    if ($pdfInspection.type -ne 'pdf') { throw 'PDF 检查类型错误' }
    if ($pdfInspection.pages -ne 1) { throw "PDF 页数错误：$($pdfInspection.pages)" }

    $docxExtraction = ConvertFrom-ToolJson (Invoke-ToolCommand -Command '提取文本' -InputPath $docxPath)
    if ($docxExtraction.type -ne 'docx' -or $docxExtraction.action -ne 'extract') {
        throw 'DOCX 提取响应字段错误'
    }
    Assert-PathUnderRoot -Path $docxExtraction.output -Root $config.OutputRoot -Message 'DOCX 文本未写入成品目录'
    $createdOutputs += [string]$docxExtraction.output
    $docxText = Get-Content -LiteralPath $docxExtraction.output -Raw -Encoding UTF8
    if ($docxText -notmatch '第一段' -or $docxText -notmatch '状态' -or $docxText -notmatch '正常') {
        throw 'DOCX 文本提取缺少段落或表格内容'
    }

    $pdfExtraction = ConvertFrom-ToolJson (Invoke-ToolCommand -Command '提取文本' -InputPath $pdfPath)
    if ($pdfExtraction.type -ne 'pdf' -or $pdfExtraction.action -ne 'extract') {
        throw 'PDF 提取响应字段错误'
    }
    Assert-PathUnderRoot -Path $pdfExtraction.output -Root $config.OutputRoot -Message 'PDF 文本未写入成品目录'
    $createdOutputs += [string]$pdfExtraction.output
    $pdfText = Get-Content -LiteralPath $pdfExtraction.output -Raw -Encoding UTF8
    if ($pdfText -notmatch 'PDF fixture text') { throw 'PDF 文本提取内容错误' }

    $orchestrationPdf = Join-Path $fixtureRoot ("转换后PDF_{0}.pdf" -f [guid]::NewGuid().ToString('N'))
    $orchestrationPng1 = Join-Path $fixtureRoot 'orchestration-page-1.png'
    $orchestrationPng2 = Join-Path $fixtureRoot 'orchestration-page-2.png'
    Copy-Item -LiteralPath $pdfPath -Destination $orchestrationPdf
    [IO.File]::WriteAllBytes($orchestrationPng1, [byte[]](1,2,3))
    [IO.File]::WriteAllBytes($orchestrationPng2, [byte[]](4,5,6))
    $wordModuleInstance = Import-Module $wordModule -Force -PassThru
    $orchestration = & $wordModuleInstance {
        param($TestConfig,$SourcePath,$ConvertedPdf,$PreviewOne,$PreviewTwo)

        $script:TestConvertedPdf = $ConvertedPdf
        $script:TestPreviewOutputs = @($PreviewOne,$PreviewTwo)
        $script:InspectedPdfPath = $null
        $script:PreviewedPdfPath = $null

        function Invoke-LibreOfficeConversion {
            param([string]$Path,$Config,[string]$Format = 'pdf')
            return Get-Item -LiteralPath $script:TestConvertedPdf
        }
        function Invoke-DocumentPython {
            param([string]$Mode,[string]$Path,$Config)
            $script:InspectedPdfPath = $Path
            return '{"type":"pdf","pages":1,"metadata":{}}'
        }
        function Invoke-PdfPreview {
            param([string]$Path,$Config)
            $script:PreviewedPdfPath = $Path
            return ([ordered]@{
                    type = 'pdf'
                    action = 'preview'
                    outputs = @($script:TestPreviewOutputs)
                } | ConvertTo-Json -Compress)
        }

        $before = @(Get-ChildItem -LiteralPath $TestConfig.OutputRoot -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        $mismatchRejected = $false
        try {
            $null = Invoke-WordCommand -Action '转PDF' -Path $SourcePath -Config $TestConfig
        }
        catch {
            $mismatchRejected = $true
        }
        $after = @(Get-ChildItem -LiteralPath $TestConfig.OutputRoot -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        $unexpectedOutputs = @(Compare-Object $before $after -PassThru | Where-Object SideIndicator -eq '=>')
        foreach ($unexpectedOutput in $unexpectedOutputs) {
            Remove-Item -LiteralPath $unexpectedOutput -Force
        }
        if (-not $mismatchRejected) { throw '转 PDF 未拒绝页数与预览 PNG 数量不一致' }
        if ($unexpectedOutputs.Count -ne 0) { throw '转 PDF 验证失败前已产生新成品' }

        $script:TestPreviewOutputs = @($PreviewOne)
        $json = Invoke-WordCommand -Action '转PDF' -Path $SourcePath -Config $TestConfig
        return [pscustomobject]@{
            Json = $json
            InspectedPath = $script:InspectedPdfPath
            PreviewedPath = $script:PreviewedPdfPath
        }
    } $config $docxPath $orchestrationPdf $orchestrationPng1 $orchestrationPng2
    $orchestrationJson = $orchestration.Json | ConvertFrom-Json -ErrorAction Stop
    if ($orchestration.InspectedPath -ne $orchestrationPdf -or $orchestration.PreviewedPath -ne $orchestrationPdf) {
        throw '转 PDF 未检查并预览 LibreOffice 生成的缓存 PDF'
    }
    if ($orchestrationJson.type -ne 'pdf' -or
        $orchestrationJson.sourceType -ne 'docx' -or
        $orchestrationJson.convertedType -ne 'pdf' -or
        $orchestrationJson.pages -ne 1 -or
        @($orchestrationJson.previewOutputs).Count -ne 1) {
        throw '转 PDF 成功响应缺少格式或逐页验证字段'
    }
    $createdOutputs += [string]$orchestrationJson.output

    $docOutputSnapshot = @(Get-ChildItem -LiteralPath $config.OutputRoot -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    if ($null -eq $soffice) {
        foreach ($docAction in '检查','提取文本','预览','转PDF') {
            $docResult = Invoke-ToolCommand -Command $docAction -InputPath $docPath
            if ($docResult.ExitCode -eq 0) { throw "LibreOffice 缺失时 DOC $docAction 伪造成功" }
            if ($docResult.Text -notmatch 'LibreOffice' -or $docResult.Text -notmatch '缺失|不存在|安装') {
                throw "DOC $docAction 未返回 LibreOffice 针对性诊断：$($docResult.Text)"
            }
        }
        $docOutputAfter = @(Get-ChildItem -LiteralPath $config.OutputRoot -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        if (@(Compare-Object $docOutputSnapshot $docOutputAfter).Count -ne 0) {
            throw 'LibreOffice 缺失的 DOC 操作产生了新成品'
        }
    }
    else {
        $docInspection = ConvertFrom-ToolJson (Invoke-ToolCommand -Command '检查' -InputPath $docPath)
        if ($docInspection.sourceType -ne 'doc' -or $docInspection.convertedType -ne 'docx') {
            throw 'DOC 检查未区分源格式与转换格式'
        }
        $docExtraction = ConvertFrom-ToolJson (Invoke-ToolCommand -Command '提取文本' -InputPath $docPath)
        if ($docExtraction.sourceType -ne 'doc' -or $docExtraction.convertedType -ne 'docx') {
            throw 'DOC 提取未区分源格式与转换格式'
        }
        Assert-PathUnderRoot -Path $docExtraction.output -Root $config.OutputRoot -Message 'DOC 提取文本未写入成品目录'
        $createdOutputs += [string]$docExtraction.output

        $docPreview = Invoke-ToolCommand -Command '预览' -InputPath $docPath
        $docConversion = Invoke-ToolCommand -Command '转PDF' -InputPath $docPath
        if ($null -eq $pdftoppm) {
            foreach ($result in $docPreview,$docConversion) {
                if ($result.ExitCode -eq 0 -or $result.Text -notmatch 'Poppler|pdftoppm') {
                    throw 'Poppler 缺失时 DOC 预览/转 PDF 未明确失败'
                }
            }
        }
        else {
            $docPreviewJson = ConvertFrom-ToolJson $docPreview
            if (@($docPreviewJson.outputs).Count -eq 0) { throw 'DOC 预览未生成逐页 PNG' }
            $docConversionJson = ConvertFrom-ToolJson $docConversion
            if ($docConversionJson.sourceType -ne 'doc' -or $docConversionJson.convertedType -ne 'pdf') {
                throw 'DOC 转 PDF 未区分源格式与转换格式'
            }
            $createdOutputs += [string]$docConversionJson.output
        }
    }

    $conversionSnapshot = @(Get-ChildItem -LiteralPath $config.OutputRoot -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    $wordConversion = Invoke-ToolCommand -Command '转PDF' -InputPath $docxPath
    if ($null -eq $soffice) {
        if ($wordConversion.ExitCode -eq 0) { throw 'LibreOffice 缺失时转 PDF 伪造成功' }
        if ($wordConversion.Text -notmatch 'LibreOffice' -or $wordConversion.Text -notmatch '缺失|不存在|安装') {
            throw "LibreOffice 缺失诊断不明确：$($wordConversion.Text)"
        }
    }
    elseif ($null -eq $pdftoppm) {
        if ($wordConversion.ExitCode -eq 0) { throw 'Poppler 缺失时转 PDF 伪造成功' }
        if ($wordConversion.Text -notmatch 'Poppler|pdftoppm' -or $wordConversion.Text -notmatch '缺失|不存在|安装') {
            throw "转 PDF 的 Poppler 缺失诊断不明确：$($wordConversion.Text)"
        }
        $conversionAfter = @(Get-ChildItem -LiteralPath $config.OutputRoot -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        if (@(Compare-Object $conversionSnapshot $conversionAfter).Count -ne 0) {
            throw 'Poppler 缺失的转 PDF 操作产生了新成品'
        }
    }
    else {
        if ($wordConversion.ExitCode -ne 0) { throw "LibreOffice 转换失败：$($wordConversion.Text)" }
        $conversionJson = ConvertFrom-ToolJson $wordConversion
        Assert-PathUnderRoot -Path $conversionJson.output -Root $config.OutputRoot -Message 'PDF 成品未写入成品目录'
        $createdOutputs += [string]$conversionJson.output
        if ((Get-Item -LiteralPath $conversionJson.output).Length -le 0) { throw 'PDF 成品为空' }
        $convertedInspection = ConvertFrom-ToolJson (Invoke-ToolCommand -Command '检查' -InputPath $conversionJson.output)
        if ($conversionJson.sourceType -ne 'docx' -or
            $conversionJson.convertedType -ne 'pdf' -or
            $conversionJson.pages -ne $convertedInspection.pages -or
            @($conversionJson.previewOutputs).Count -ne $convertedInspection.pages) {
            throw '转换后 PDF 的检查页数与逐页 PNG 不一致'
        }
        foreach ($previewPath in @($conversionJson.previewOutputs)) {
            Assert-PathUnderRoot -Path $previewPath -Root $config.CacheRoot -Message '转换后 PDF 预览未写入缓存目录'
            if ((Get-Item -LiteralPath $previewPath).Length -le 0) { throw "转换后 PDF 预览为空：$previewPath" }
        }
    }

    $pdfPreview = Invoke-ToolCommand -Command '预览' -InputPath $pdfPath
    if ($null -eq $pdftoppm) {
        if ($pdfPreview.ExitCode -eq 0) { throw 'Poppler 缺失时 PDF 预览伪造成功' }
        if ($pdfPreview.Text -notmatch 'Poppler|pdftoppm' -or $pdfPreview.Text -notmatch '缺失|不存在|安装') {
            throw "Poppler 缺失诊断不明确：$($pdfPreview.Text)"
        }
    }
    else {
        if ($pdfPreview.ExitCode -ne 0) { throw "PDF 预览失败：$($pdfPreview.Text)" }
        $previewJson = ConvertFrom-ToolJson $pdfPreview
        foreach ($previewPath in @($previewJson.outputs)) {
            Assert-PathUnderRoot -Path $previewPath -Root $config.CacheRoot -Message '预览图片未写入缓存目录'
            if ((Get-Item -LiteralPath $previewPath).Length -le 0) { throw "预览图片为空：$previewPath" }
        }
        if (@($previewJson.outputs).Count -ne $pdfInspection.pages) {
            throw 'PDF 预览页数与检查页数不一致'
        }
    }

    if ((Get-FileHash -LiteralPath $docxPath -Algorithm SHA256).Hash -ne $sourceHashes.Docx) {
        throw 'DOCX 源夹具被修改'
    }
    if ((Get-FileHash -LiteralPath $docPath -Algorithm SHA256).Hash -ne $sourceHashes.Doc) {
        throw 'DOC 源夹具被修改'
    }
    if ((Get-FileHash -LiteralPath $pdfPath -Algorithm SHA256).Hash -ne $sourceHashes.Pdf) {
        throw 'PDF 源夹具被修改'
    }
    if ((Get-FileHash -LiteralPath $badDocxPath -Algorithm SHA256).Hash -ne $sourceHashes.BadDocx) {
        throw '损坏 DOCX 源夹具被修改'
    }

    Write-Output '格式功能测试（WordPdf）：通过'
}
finally {
    foreach ($outputPath in $createdOutputs) {
        if (Test-Path -LiteralPath $outputPath -PathType Leaf) {
            Remove-Item -LiteralPath $outputPath -Force
        }
    }
    if (Test-Path -LiteralPath $fixtureRoot) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
}
