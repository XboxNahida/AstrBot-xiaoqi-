$ErrorActionPreference = 'Stop'

if ($args -contains '-All') {
    & "$PSScriptRoot\基础和缓存测试.ps1"
    & "$PSScriptRoot\格式功能测试.ps1" -Group WordPdf
    & "$PSScriptRoot\格式功能测试.ps1" -Group Spreadsheet
    & "$PSScriptRoot\整理测试.ps1"
    exit 0
}

& "$PSScriptRoot\基础和缓存测试.ps1"
& "$PSScriptRoot\格式功能测试.ps1" -Group WordPdf
