$ErrorActionPreference = "Stop"

$source = Join-Path $PSScriptRoot "..\KLineMobile.html"
$target = Join-Path $PSScriptRoot "assets\KLineMobile.html"

if (-not (Test-Path $source -PathType Leaf)) {
    throw "Source mobile file not found: $source"
}

$content = [System.IO.File]::ReadAllText($source)
$debugPattern = '(?ms)^[ \t]*// #region debug-point[^\r\n]*\r?\n.*?^[ \t]*// #endregion[^\r\n]*\r?\n'
$cleaned = [System.Text.RegularExpressions.Regex]::Replace(
    $content,
    $debugPattern,
    ""
)

if ($cleaned.Contains("__MOBILE_DEBUG_REPORT__") -or
        $cleaned.Contains("192.168.1.6")) {
    throw "Debug blocks were not completely removed"
}

$encoding = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($target, $cleaned, $encoding)
Write-Output "APK asset generated: $target"

# 安装包只嵌入确认不含调试上报代码的页面，原网页版文件保持不变。
