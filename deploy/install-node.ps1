# Установка/обновление Windows-ноды sa-home-bot (этап 19, п. 4).
# Запускать из PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА:
#
#   .\install-node.ps1 -Tag v0.24.1 -SwarmToken "..." -JoinEndpoint "tcp://alfred.tailXXXXXX.ts.net:8710"
#
# Повторный запуск того же скрипта = обновление: шаги идемпотентны
# (пропускают уже сделанное), pipx install --force всегда подтягивает
# указанный тег заново. config.toml, если уже существует, НЕ трогается —
# опечатка в разово скопированном конфиге не должна лечиться молчаливой
# перезаписью (урок арх-t480, IMPLEMENTATION_PLAN §19 п.2).
#
# Что делает:
#   1. Python/git через winget, если их нет; pipx через pip --user.
#   2. pipx install --force "sa-home-bot[windows] @ git+<repo>@<Tag>".
#   3. Генерирует config.toml из шаблона (только если файла ещё нет).
#   4. Скачивает LibreHardwareMonitor (полный релизный zip — dll одна не
#      раздаётся, нужны соседние dll) в <InstallDir>\lhm, если его ещё нет.
#   5. smartmontools через winget (best-effort) + гарантирует, что его
#      bin-каталог в СИСТЕМНОМ (Machine) PATH — служба идёт от LocalSystem,
#      у которого нет пользовательских PATH-добавок (живая находка
#      2026-07-17: smartctl был не виден именно поэтому).
#   6. WinSW: скачивает (если нет), пишет sa-home-node.xml, ставит и
#      стартует службу sa-home-node (LocalSystem — работает и до логина,
#      после /wake; LHM живым тестом подтверждён под LocalSystem, права
#      админа для датчиков не нужны отдельно).
#   7. Кладёт win-auto-update.ps1 в <InstallDir> и регистрирует ежедневную
#      задачу планировщика от SYSTEM с АБСОЛЮТНЫМИ путями к git/pipx (НЕ
#      полагаемся на $env:USERPROFILE/PATH — у SYSTEM всё своё, живая
#      ошибка 2026-07-17). Задача сама делает стоп→pipx install --force→
#      старт службы (не протокольное nodectl update — на Windows с LHM
#      "горячий" pipx install --force поверх РАБОТАЮЩЕЙ ноды падает с
#      WinError 5 Access Denied, ClrLoader.dll занят живым процессом;
#      подробности — в шапке win-auto-update.ps1).
#
# Живая проверка (2026-07-17, desktop-ie953ua): служба под LocalSystem
# работает, датчики LHM читаются без дополнительных прав, smartctl виден
# после переноса PATH в Machine-scope, задача автообновления под SYSTEM
# гоняна вручную (Start-ScheduledTask) — стоп/pipx/старт отработали,
# нода поднялась, пиры переподключились. Сам этот сводный скрипт целиком
# (одним запуском, с нуля, на новой машине) живьём ЕЩЁ НЕ прогнан — при
# следующей Windows-ноде стоит проверить именно так и поправить, если
# несостыковки вылезут.

param(
    [Parameter(Mandatory = $true)]
    [string]$Tag,

    [string]$InstallDir = "C:\ProgramData\sa-home-bot",
    [string]$NodeId = $env:COMPUTERNAME,
    [string]$SwarmToken = "",
    [string]$JoinEndpoint = "",
    [string]$ListenAddress = "",  # tcp://<адрес>:8710, анонсируется пирам — НЕ 0.0.0.0 (own_endpoint, node/app.py); пусто = автоопределение по tailscale ip
    [string]$RepoUrl = "https://github.com/sevboa/sa-home-bot",
    [string]$LhmVersion = "v0.9.6",
    [string]$WinSwVersion = "v2.12.0",
    [string]$AutoUpdateTime = "4:00AM"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Запустите PowerShell от имени администратора — нужны права на службу и системный PATH."
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# PIPX_HOME — ЖЁСТКО закреплён на account-independent путь под ProgramData
# (не $env:USERPROFILE): pipx иначе вычисляет каталог venv'ов из
# %LOCALAPPDATA% ВЫЗЫВАЮЩЕГО аккаунта — служба идёт от LocalSystem/задача
# планировщика от SYSTEM, у каждого свой профиль, отдельный от вашего.
# Живой баг 2026-07-17: pipx install --force от LocalSystem "успешно"
# ставил пакет в venv ПОД LocalSystem — призрак, никак не связанный с
# реально запущенной службой (installed_version() после такого
# "обновления" честно продолжал видеть старую версию). Установка venv'а
# в ProgramData ОДИН РАЗ и для всех — то же место видят и вы, и служба,
# и задача автообновления, независимо от того, кто сейчас зовёт pipx.
$pipxHomeTarget = Join-Path $InstallDir "pipx"
$machineHome = [Environment]::GetEnvironmentVariable("PIPX_HOME", "Machine")
if ($machineHome -ne $pipxHomeTarget) {
    [Environment]::SetEnvironmentVariable("PIPX_HOME", $pipxHomeTarget, "Machine")
}
$env:PIPX_HOME = $pipxHomeTarget  # подхватить в этом же процессе, не дожидаясь нового логона

# --- 1. Python / git / pipx ---
Step "Python, git, pipx"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    winget install -e --id Python.Python.3.13 --accept-source-agreements --accept-package-agreements
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
}
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    python -m pip install --user pipx
    python -m pipx ensurepath
}
$gitExe = (Get-Command git).Source
$pipxToolExe = (Get-Command pipx).Source

# Задача автообновления идёт от SYSTEM — у него ДРУГОЙ PATH, не факт что
# видит git/pipx (живая находка 2026-07-17). Дублируем их каталоги в
# системный (Machine) PATH, как уже делаем для smartmontools ниже —
# надёжнее, чем передавать голые имена и надеяться на PATH.
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
foreach ($dir in @((Split-Path $gitExe), (Split-Path $pipxToolExe))) {
    if ($machinePath -notlike "*$dir*") {
        $machinePath = "$machinePath;$dir"
        [Environment]::SetEnvironmentVariable("Path", $machinePath, "Machine")
    }
}

# --- 2. pipx install/upgrade ---
Step "pipx install sa-home-bot[windows] @ $Tag"
# Если служба уже стоит и работает — на Windows нельзя перезаписать её
# открытые DLL "на лету" (живая находка 2026-07-17: pipx install --force
# падает с WinError 5 Access Denied на ClrLoader.dll у pythonnet/LHM,
# пока процесс жив; см. подробности в шапке win-auto-update.ps1). Гасим
# перед переустановкой, шаг 6 поднимет обратно.
$runningService = Get-Service -Name "sa-home-node" -ErrorAction SilentlyContinue
if ($runningService -and $runningService.Status -eq "Running") {
    Write-Host "Останавливаю службу sa-home-node на время переустановки..."
    Stop-Service -Name "sa-home-node"
}
& $pipxToolExe install --force "sa-home-bot[windows] @ git+$RepoUrl@$Tag"

$pipxVenvs = (& $pipxToolExe environment --value PIPX_LOCAL_VENVS 2>$null)
if (-not $pipxVenvs) { $pipxVenvs = Join-Path $pipxHomeTarget "venvs" }  # запасной путь на старых pipx
$pipxHome = Split-Path $pipxVenvs -Parent  # venvs всегда прямо под PIPX_HOME
$saHomeBotExe = Join-Path $pipxVenvs "sa-home-bot\Scripts\sa-home-bot.exe"
$nodectlExe = Join-Path $pipxVenvs "sa-home-bot\Scripts\nodectl.exe"
if (-not (Test-Path $saHomeBotExe)) { throw "pipx install прошёл, но $saHomeBotExe не найден — проверьте вывод выше." }

# --- 3. config.toml (только если ещё нет) ---
Step "config.toml"
$configPath = Join-Path $InstallDir "config.toml"
$dataDir = Join-Path $InstallDir "data"
$lhmDir = Join-Path $InstallDir "lhm"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

if (Test-Path $configPath) {
    Write-Host "config.toml уже существует — не трогаю (правьте руками, если нужно)."
}
else {
    if (-not $SwarmToken) {
        throw "Первая установка: нужен -SwarmToken (общий секрет роя, см. [swarm].token на alfred)."
    }
    if (-not $ListenAddress) {
        $tsIp = & tailscale ip -4 2>$null
        if ($tsIp) { $ListenAddress = "tcp://${tsIp}:8710" }
    }
    if (-not $ListenAddress) {
        throw "Не удалось определить tailscale-адрес автоматически — передайте -ListenAddress `"tcp://<адрес>:8710`" явно (это то, что нода анонсирует пирам, должно быть реально достижимо с других нод роя)."
    }
    $joinLine = if ($JoinEndpoint) { "join = `"$JoinEndpoint`"" } else { '#join = ""' }
    @"
[node]
id = "$NodeId"
socket = "tcp://127.0.0.1:8710"
listen = "$ListenAddress"
assignments = ["monitor"]
state_path = '$dataDir\node-state.json'
restart_delay_s = 5.0
stop_timeout_s = 90.0

[monitor]
socket = "tcp://127.0.0.1:8711"
db_path = '$dataDir\monitor.sqlite'

[sensors.cpu]
enabled = true
warn_c = 85.0
crit_c = 95.0

[sensors.disks]
enabled = true
warn_c = 55.0
crit_c = 65.0

[sensors.lhm]
dll_path = '$lhmDir\LibreHardwareMonitorLib.dll'

[swarm]
token = "$SwarmToken"
$joinLine

[logging]
level = "INFO"
format = "plain"
"@ | Set-Content -Path $configPath -Encoding UTF8
    Write-Host "Сгенерирован $configPath — проверьте listen/id при необходимости."
}

# --- 4. LibreHardwareMonitor ---
Step "LibreHardwareMonitor"
$lhmDll = Join-Path $lhmDir "LibreHardwareMonitorLib.dll"
if (Test-Path $lhmDll) {
    Write-Host "LHM уже распакован в $lhmDir."
}
else {
    $lhmZip = Join-Path $env:TEMP "lhm.zip"
    Invoke-WebRequest "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/$LhmVersion/LibreHardwareMonitor.zip" -OutFile $lhmZip
    Expand-Archive $lhmZip $lhmDir -Force
    Get-ChildItem $lhmDir -Recurse | Unblock-File
    Remove-Item $lhmZip -Force
}

# --- 5. smartmontools + системный PATH ---
Step "smartmontools"
$smartctl = Get-Command smartctl -ErrorAction SilentlyContinue
if (-not $smartctl) {
    try {
        winget install -e --id smartmontools.smartmontools --accept-source-agreements --accept-package-agreements
    }
    catch {
        Write-Warning "winget не поставил smartmontools — поставьте вручную, SMART-здоровье дисков не будет работать."
    }
}
$smartBin = "C:\Program Files\smartmontools\bin"
if (Test-Path $smartBin) {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($machinePath -notlike "*$smartBin*") {
        # LocalSystem (служба) не видит User-PATH — только Machine-PATH.
        [Environment]::SetEnvironmentVariable("Path", "$machinePath;$smartBin", "Machine")
        Write-Host "Добавлено в системный PATH: $smartBin (нужен рестарт службы, будет ниже)."
    }
}

# --- 6. Служба WinSW ---
Step "Служба sa-home-node (WinSW)"
$serviceDir = Join-Path $InstallDir "service"
New-Item -ItemType Directory -Force -Path $serviceDir | Out-Null
$winswExe = Join-Path $serviceDir "sa-home-node.exe"
if (-not (Test-Path $winswExe)) {
    Invoke-WebRequest "https://github.com/winsw/winsw/releases/download/$WinSwVersion/WinSW-x64.exe" -OutFile $winswExe
    Unblock-File $winswExe
}

$xmlTemplate = Join-Path $PSScriptRoot "sa-home-node.xml"
$xmlTarget = Join-Path $serviceDir "sa-home-node.xml"
(Get-Content $xmlTemplate -Raw) `
    -replace [regex]::Escape("%PIPX_EXE%"), $saHomeBotExe `
    -replace [regex]::Escape("%CONFIG_PATH%"), $configPath `
    -replace [regex]::Escape("%INSTALL_DIR%"), $InstallDir `
    | Set-Content -Path $xmlTarget -Encoding UTF8

$existing = Get-Service -Name "sa-home-node" -ErrorAction SilentlyContinue
if (-not $existing) {
    & $winswExe install
    & $winswExe start
}
else {
    Write-Host "Служба уже установлена — перезапускаю, чтобы подхватить конфиг/PATH."
    Restart-Service -Name "sa-home-node"
}

# --- 7. Автообновление (задача планировщика) ---
Step "Задача автообновления"
$autoUpdateScript = Join-Path $InstallDir "win-auto-update.ps1"
Copy-Item (Join-Path $PSScriptRoot "win-auto-update.ps1") $autoUpdateScript -Force

$argStr = "-ExecutionPolicy Bypass -NoProfile -File `"$autoUpdateScript`" -RepoUrl `"$RepoUrl`" -GitExe `"$gitExe`" -PipxExe `"$pipxToolExe`" -PipxHome `"$pipxHome`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argStr
$trigger = New-ScheduledTaskTrigger -Daily -At $AutoUpdateTime
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "sa-home-node-autoupdate" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Step "Готово"
Write-Host "Служба: sa-home-node (LocalSystem, автостарт)"
Write-Host "Автообновление: задача 'sa-home-node-autoupdate', ежедневно в $AutoUpdateTime"
Write-Host "Конфиг: $configPath"
Write-Host "Проверить: sc query sa-home-node ; $nodectlExe -c `"$configPath`" status"
