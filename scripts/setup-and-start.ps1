#requires -Version 5.1
param(
    [string]$ConfigPath = "filemover.yaml",
    [switch]$Admin
)

$ErrorActionPreference = 'Stop'

# 1) 解析配置路径（相对路径转绝对路径）
if (-not (Split-Path -IsAbsolute $ConfigPath)) {
    $ConfigPath = Join-Path -Path (Get-Location) -ChildPath $ConfigPath
}

# 2) 设置环境变量（仅当未设置或不同）
$current = [Environment]::GetEnvironmentVariable('FILEMOVER2_CONFIG', 'Process')
if (-not $current -or ($current -ne $ConfigPath)) {
    $env:FILEMOVER2_CONFIG = $ConfigPath
}

# 3) 打印提示
Write-Host "使用配置: $ConfigPath" -ForegroundColor Cyan
if ($Admin) {
    Write-Host "管理员模式: 已请求" -ForegroundColor Yellow
}

# 4) 启动 filemover2
$adminFlag = if ($Admin) { '--admin' } else { '' }

# 直接调用 Python 模块入口（或安装后可用 filemover2）
try {
    python -m src.filemover2.cli $adminFlag start | Out-Host
}
catch {
    Write-Host "未能调用 python -m src.filemover2.cli，尝试使用 filemover2 命令..." -ForegroundColor Yellow
    filemover2 $adminFlag start | Out-Host
}
