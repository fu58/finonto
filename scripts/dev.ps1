# 本地开发（Windows PowerShell）：一条命令拉起后端 (8000) 与前端 Vite dev (5173)。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$server = Join-Path $root "server"
$client = Join-Path $root "client"
$py = Join-Path $server ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "[dev] 创建后端虚拟环境…"
    python -m venv (Join-Path $server ".venv")
}
Write-Host "[dev] 安装后端依赖…"
& $py -m pip install -q -r (Join-Path $server "requirements.txt")
Write-Host "[dev] 安装前端依赖…"
Push-Location $client
npm install --silent
Pop-Location

$backend = Start-Process -FilePath $py -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" -WorkingDirectory $server -PassThru
Write-Host "[dev] 后端 http://127.0.0.1:8000 · 前端 http://localhost:5173 （Ctrl+C 退出）"
Push-Location $client
try {
    npm run dev
} finally {
    Pop-Location
    # Ctrl+C 时一并收尾，避免留下占着 8000 端口的孤儿进程
    if ($backend -and -not $backend.HasExited) { Stop-Process -Id $backend.Id -Force }
}
