$ErrorActionPreference = 'Stop'

Import-Module "$PSScriptRoot\..\模块\通用工具.psm1" -Force
$commonModule = Get-Module '通用工具'
$escapedCell = & $commonModule { ConvertTo-MarkdownCell "a\b|c`r`nd" }
if ($escapedCell -cne 'a\\b\|c<br>d') { throw "Markdown 单元格转义顺序错误：$escapedCell" }
$config = Get-ToolConfig "$PSScriptRoot\..\工具配置.json"
$entry = Join-Path $PSScriptRoot '..\文档工具.ps1'
$hostExe = (Get-Process -Id $PID).Path
$fixture = Join-Path ([IO.Path]::GetTempPath()) ("整理 测试(中文)_{0}" -f [guid]::NewGuid().ToString('N'))
$outputs = @()

function Get-Snapshot([string]$Root) {
    @(Get-ChildItem -LiteralPath $Root -File -Recurse | Sort-Object FullName | ForEach-Object {
        [pscustomobject]@{ Name=$_.Name; Path=$_.FullName; Size=$_.Length; Modified=$_.LastWriteTimeUtc.ToString('o'); Hash=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    })
}

function Invoke-Inventory([string]$Path) {
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $text = & $hostExe -NoProfile -ExecutionPolicy Bypass -File $entry '整理' $Path 2>&1
        [pscustomobject]@{ ExitCode=$LASTEXITCODE; Text=($text -join [Environment]::NewLine) }
    }
    finally { $ErrorActionPreference = $oldPreference }
}

try {
    $nested = Join-Path $fixture '子目录 (一)'
    $commonMarkDirectory = Join-Path $nested '(报告)'
    New-Item -ItemType Directory -Force -Path $nested,$commonMarkDirectory,$config.OutputRoot | Out-Null
    Set-Content -LiteralPath (Join-Path $fixture '文档 甲.txt') -Value '中文内容' -Encoding UTF8
    [IO.File]::WriteAllBytes((Join-Path $nested '数据(乙).pdf'), [byte[]](1,2,3,4))
    Set-Content -LiteralPath (Join-Path $nested '表格.csv') -Value 'a,b' -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $commonMarkDirectory '[草稿]#说明.txt') -Value '路径转义测试' -Encoding UTF8
    $before = Get-Snapshot $fixture

    $result = Invoke-Inventory $fixture
    if ($result.ExitCode -ne 0) { throw "目录整理命令失败：$($result.Text)" }
    $json = $result.Text | ConvertFrom-Json
    foreach ($field in 'csv','markdown','count','source') {
        if ($json.PSObject.Properties.Name -notcontains $field) { throw "整理 JSON 缺少字段：$field" }
    }
    if ($json.count -ne 4) { throw "整理文件数量错误：$($json.count)" }
    foreach ($path in @($json.csv,$json.markdown)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "整理成品不存在：$path" }
        $outputs += [string]$path
        $null = [Text.UTF8Encoding]::new($false, $true).GetString([IO.File]::ReadAllBytes($path))
    }
    $rows = @(Import-Csv -LiteralPath $json.csv -Encoding UTF8)
    if ($rows.Count -ne 4) { throw 'CSV 清单行数错误' }
    foreach ($field in '文件名','类型','大小字节','修改时间','完整路径') {
        if ($rows[0].PSObject.Properties.Name -notcontains $field) { throw "CSV 缺少字段：$field" }
    }
    $rawCommonMarkPath = [IO.Path]::GetFullPath((Join-Path $commonMarkDirectory '[草稿]#说明.txt'))
    $csvPathValue = @($rows | Where-Object '文件名' -eq '[草稿]#说明.txt')[0].'完整路径'
    if ($csvPathValue -cne $rawCommonMarkPath) { throw "CSV 完整路径被 Markdown 转义：$csvPathValue" }
    $md = Get-Content -LiteralPath $json.markdown -Raw -Encoding UTF8
    foreach ($value in '文档 甲.txt','数据(乙).pdf','表格.csv','完整路径') {
        if ($md -notmatch [regex]::Escape($value)) { throw "Markdown 清单缺少：$value" }
    }
    $escapedCommonMarkPath = $rawCommonMarkPath.Replace('\','\\')
    if (-not $md.Contains($escapedCommonMarkPath)) { throw "Markdown 完整路径未双写反斜杠：$escapedCommonMarkPath" }
    if ((ConvertTo-Json $before -Compress) -ne (ConvertTo-Json (Get-Snapshot $fixture) -Compress)) {
        throw '整理命令修改了源文件的名称、路径、修改时间或 SHA256'
    }

    $again = Invoke-Inventory $fixture
    if ($again.ExitCode -ne 0) { throw "第二次整理失败：$($again.Text)" }
    $againJson = $again.Text | ConvertFrom-Json
    $outputs += [string]$againJson.csv,[string]$againJson.markdown
    if ($json.csv -eq $againJson.csv -or $json.markdown -eq $againJson.markdown) { throw '同名成品未生成唯一路径' }

    $empty = Join-Path $fixture '空目录'
    New-Item -ItemType Directory -Path $empty | Out-Null
    $emptyResult = Invoke-Inventory $empty
    if ($emptyResult.ExitCode -ne 0) { throw "空目录整理应成功：$($emptyResult.Text)" }
    $emptyJson = $emptyResult.Text | ConvertFrom-Json
    $outputs += [string]$emptyJson.csv,[string]$emptyJson.markdown
    if ($emptyJson.count -ne 0) { throw '空目录计数应为 0' }

    $missing = Invoke-Inventory (Join-Path $fixture '不存在')
    if ($missing.ExitCode -eq 0 -or $missing.Text -notmatch '不存在|路径|目录') { throw '不存在目录未清晰失败' }
    Write-Output '整理测试：通过'
}
finally {
    foreach ($path in $outputs) { if ($path -and (Test-Path -LiteralPath $path)) { Remove-Item -LiteralPath $path -Force } }
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
