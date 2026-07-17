# Автообновление Windows-ноды (этап 19, п. 4). Регистрируется
# install-node.ps1 как ежедневная задача планировщика.
#
# Переиспользует УЖЕ существующий протокольный механизм ноды
# (node/update.py, node/service.py) через nodectl — не дублирует
# pipx/git-логику здесь. Три шага:
#   1. nodectl update    — если есть новый тег, запускает pipx install
#      --force в фоне на стороне ноды и сразу возвращается (не ждёт).
#   2. nodectl events     — слушаем живой поток событий, ждём
#      update_finished (успех/неудача) с общим таймаутом.
#   3. nodectl restart_node — только если обновление реально прошло
#      успешно; сам процесс не заменяется (Windows), нода выходит с
#      RESTART_EXIT_CODE=10 — перезапуск подхватывает WinSW
#      (<onfailure action="restart"/> в sa-home-node.xml).
#
# Ничего не делает, если версия уже последняя — безопасно гонять хоть
# каждый час.

param(
    [string]$ConfigPath = "$env:USERPROFILE\sa-home-bot\config.toml",
    [string]$NodectlExe = "$env:USERPROFILE\AppData\Local\pipx\pipx\venvs\sa-home-bot\Scripts\nodectl.exe",
    [int]$TimeoutSec = 360   # запас над внутренним таймаутом pipx (300s) в node/update.py
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

function Log($msg) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

$updateOutput = & $NodectlExe -c $ConfigPath update 2>&1
Log "nodectl update: $updateOutput"

if ($updateOutput -match "Уже последняя версия") {
    Log "Обновление не требуется."
    exit 0
}

# "Запущено обновление ... в фоне" — слушаем events до update_finished.
Log "Обновление запущено, жду update_finished (до ${TimeoutSec}s)..."

$job = Start-Job -ScriptBlock {
    param($exe, $cfg)
    & $exe -c $cfg events
} -ArgumentList $NodectlExe, $ConfigPath

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$finishedLine = $null
while ((Get-Date) -lt $deadline) {
    $lines = Receive-Job -Job $job
    foreach ($line in $lines) {
        Log "event: $line"
        if ($line -match "update_finished") {
            $finishedLine = $line
            break
        }
    }
    if ($finishedLine) { break }
    Start-Sleep -Seconds 3
}
Stop-Job -Job $job -ErrorAction SilentlyContinue | Out-Null
Remove-Job -Job $job -Force -ErrorAction SilentlyContinue | Out-Null

if (-not $finishedLine) {
    Log "ОШИБКА: update_finished не пришёл за ${TimeoutSec}s — рестарт НЕ выполняется, проверьте вручную."
    exit 1
}
if ($finishedLine -notmatch "ok=True") {
    Log "ОШИБКА: обновление завершилось неудачей — рестарт НЕ выполняется: $finishedLine"
    exit 1
}

Log "Обновление успешно установлено на диск — перезапуск ноды..."
$restartOutput = & $NodectlExe -c $ConfigPath restart_node 2>&1
Log "nodectl restart_node: $restartOutput"
exit 0
