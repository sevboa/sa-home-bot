# Автообновление Windows-ноды (этап 19, п. 4). Регистрируется
# install-node.ps1 как ежедневная задача планировщика (от SYSTEM).
#
# НЕ использует протокольное умение nodectl update (node/update.py) — это
# сознательный отход от Linux-модели. Живая находка 2026-07-17: на
# Windows "горячий" pipx install --force поверх РАБОТАЮЩЕЙ ноды падает с
# WinError 5 (Access Denied) — Windows не даёt перезаписать DLL
# (в частности ClrLoader.dll у pythonnet/LHM), пока её держит открытой
# работающий процесс. На Linux так можно (там разрешено перезаписывать
# открytый файл, старый процесс держит старый inode) — отсюда и модель
# node/update.py "обнови файлы на диске сейчас, рестарт — когда сможет
# человек/WinSW". На Windows с активным LHM это в принципе не работает,
# пока служба жива, — nodectl update на такой ноде будет надёжно
# проваливаться (pipx откатится, ничего не сломает, но и не обновит).
#
# Поэтому здесь — честный цикл СТОП → pipx install --force → СТАРТ,
# без обращения к самой ноде вообще (работает, даже если нода уже упала).
# target-тег определяется напрямую через git ls-remote — та же логика,
# что node/update.py:latest_tag(), но без сети/протокола к ноде.

# GitExe/PipxExe — АБСОЛЮТНЫЕ пути, не полагаемся на PATH: задача идёт от
# SYSTEM, у которого PATH может не включать то, что winget/pipx прописали
# только в User- или даже "текущий процесс"-scope (та же ловушка, что
# была со smartctl — см. install-node.ps1, живая находка 2026-07-17).
#
# PipxHome — ЕЩЁ важнее: pipx хранит venv'ы в каталоге, который сам
# вычисляет из %LOCALAPPDATA% ВЫЗЫВАЮЩЕГО аккаунта. У SYSTEM свой
# %LOCALAPPDATA% (системный профиль), отличный от того, что реально
# использует служба (User). Без явного PIPX_HOME задача вроде бы
# успешно отрабатывает pipx install --force, но ставит пакет в venv
# ПОД SYSTEM — призрак, никак не связанный с реально работающей службой
# (живой баг 2026-07-17: nodectl update рапортовал "успех", но
# installed_version() после этого честно продолжал видеть старую версию —
# см. node/update.py:_pipx_home). Задаём переменную окружения явно.
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl,
    [Parameter(Mandatory = $true)]
    [string]$GitExe,
    [Parameter(Mandatory = $true)]
    [string]$PipxExe,
    [Parameter(Mandatory = $true)]
    [string]$PipxHome,
    [string]$ServiceName = "sa-home-node",
    [int]$ServiceStopTimeoutSec = 120
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PIPX_HOME = $PipxHome

function Log($msg) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

# --- 1. Последний тег в репозитории ---
$tagRefs = & $GitExe ls-remote --tags --refs $RepoUrl 2>$null
if (-not $tagRefs) {
    Log "git ls-remote не вернул тегов (сеть/репозиторий?) — пропускаю."
    exit 0
}
$tags = $tagRefs | ForEach-Object { ($_ -split "refs/tags/")[-1] } | Where-Object { $_ -match '^v\d+(\.\d+)*$' }
if (-not $tags) {
    Log "Не нашёл ни одного тега формата vX.Y.Z — пропускаю."
    exit 0
}
$latestTag = $tags | Sort-Object { [version]($_ -replace '^v', '') } | Select-Object -Last 1
$latestVersion = $latestTag.TrimStart('v')

# --- 2. Что установлено сейчас (pipx, не то, что в памяти процесса) ---
$pipxJson = & $PipxExe list --json | ConvertFrom-Json
$installed = $pipxJson.venvs.'sa-home-bot'.metadata.main_package.package_version
if (-not $installed) {
    Log "ОШИБКА: sa-home-bot не найден в 'pipx list' — пропускаю."
    exit 1
}

Log "Установлено: $installed / В репозитории: $latestVersion"
if ($installed -eq $latestVersion) {
    Log "Уже последняя версия — обновление не требуется."
    exit 0
}

# --- 3. Стоп → pipx install --force → старт ---
Log "Останавливаю службу $ServiceName..."
Stop-Service -Name $ServiceName -ErrorAction Stop
$deadline = (Get-Date).AddSeconds($ServiceStopTimeoutSec)
while ((Get-Service $ServiceName).Status -ne 'Stopped' -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
}
if ((Get-Service $ServiceName).Status -ne 'Stopped') {
    Log "ОШИБКА: служба не остановилась за ${ServiceStopTimeoutSec}s — прерываю, обновление НЕ выполнено."
    exit 1
}

Log "pipx install --force до $latestTag..."
# Живая находка 2026-07-19: pipx/pip пишут в stderr даже безобидный прогресс,
# а при $ErrorActionPreference = "Stop" (см. верх файла) PowerShell превращает
# КАЖДУЮ строку stderr нативного процесса в завершающую ошибку — сюда, к
# гарантированному Start-Service, скрипт тогда не добирался вовсе, и упавшее
# обновление роняло службу НАСОВСЕМ (до ручного вмешательства), а не просто
# откатывалось на старую версию, как задумано. Поэтому: EAP=Continue именно
# на время вызова pipx, плюс try/catch снаружи — до Start-Service ниже
# доходим при любом исходе (исключение, ненулевой код возврата — не важно).
$installOk = $false
$installOutput = $null
try {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $installOutput = & $PipxExe install --force "sa-home-bot[windows] @ git+$RepoUrl@$latestTag" 2>&1
        $installOk = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $prevEap
    }
} catch {
    $installOutput = $_
}
Log ($installOutput -join "`n")

Log "Запускаю службу $ServiceName..."
Start-Service -Name $ServiceName

if (-not $installOk) {
    Log "ОШИБКА: pipx install завершился с кодом $LASTEXITCODE — служба перезапущена на СТАРОЙ версии (venv при неудаче не трогается, см. вывод выше)."
    exit 1
}
Log "Обновление до $latestTag установлено, служба перезапущена."
exit 0
