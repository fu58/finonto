# FinOnto：一条命令跑起来（Windows PowerShell）
#   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Port 8080 -Reload
#
# 选 Python 的顺序（前一个不可用就看下一个）：
#   1) server\.venv（存在且依赖齐全）
#   2) 系统 python（依赖齐全）
#   3) 尝试把 requirements.txt 装进 .venv（再用系统 pip 兜底）→ 重试 1、2
#   4) 仍失败：打印可手工执行的命令并退出
param(
    [int]$Port = 8000,
    [switch]$Reload,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$server = Join-Path $root "server"
$probe = "import fastapi, sqlalchemy, jinja2, numpy"

function Test-Python($exe) {
    if (-not $exe) { return $false }
    try {
        & $exe -c $probe 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-Python {
    $venv = Join-Path $server ".venv\Scripts\python.exe"
    if ((Test-Path $venv) -and (Test-Python $venv)) { return @{exe = $venv; kind = "venv"} }
    if (Test-Python "python") { return @{exe = "python"; kind = "system"} }
    return $null
}

Write-Host "FinOnto · 启动中" -ForegroundColor Cyan
Write-Host "  仓库：$root"

Push-Location $server
try {
    $resolved = Resolve-Python

    if (-not $resolved -and -not $SkipInstall) {
        $venv = Join-Path $server ".venv\Scripts\python.exe"
        if (-not (Test-Path $venv)) {
            Write-Host "  创建虚拟环境 server\.venv ..." -ForegroundColor DarkGray
            try { & python -m venv .venv 2>$null | Out-Null } catch { }
        }
        if (Test-Path $venv) {
            Write-Host "  安装依赖到 .venv ..." -ForegroundColor DarkGray
            & python -m pip --disable-pip-version-check --python $venv install -r requirements.txt
        } else {
            Write-Host "  安装依赖到当前 Python ..." -ForegroundColor DarkGray
            & python -m pip --disable-pip-version-check install -r requirements.txt
        }
        $resolved = Resolve-Python
    }

    if (-not $resolved) {
        Write-Host ""
        Write-Host "  依赖未就绪。请手工执行：" -ForegroundColor Yellow
        Write-Host "    cd server"
        Write-Host "    python -m venv .venv"
        Write-Host "    .venv\Scripts\python.exe -m pip install -r requirements.txt"
        exit 1
    }

    Write-Host "  使用 Python：$($resolved.exe)（$($resolved.kind)）" -ForegroundColor DarkGray
    $env:PYTHONPATH = (Get-Location).Path

    $arguments = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port")
    if ($Reload) { $arguments += "--reload" }

    Write-Host ""
    Write-Host "  打开 http://127.0.0.1:$Port" -ForegroundColor Green
    Write-Host "  首次启动会自动装入 5 个领域包（金融 / 设备维保 / 人工智能 / 网络 / 通用）" -ForegroundColor DarkGray
    Write-Host "  按 Ctrl+C 停止" -ForegroundColor DarkGray
    Write-Host ""
    & $resolved.exe @arguments
} finally {
    Pop-Location
}
