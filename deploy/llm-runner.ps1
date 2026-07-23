# Запускает службу llm в ИНТЕРАКТИВНОЙ пользовательской сессии (этап
# LLM, живая находка 2026-07-23): WSL2 не даёт вызывать wsl.exe из-под
# Windows-службы sa-home-node (Session 0, LocalSystem) — попытка кода
# завершения даёт -1 (4294967295), сама WSL-VM просто не поднимается.
# Обходной путь — тот же принцип, что уже применяется для автообновления
# (win-auto-update.ps1: стоп/pipx/старт вместо протокольного update): вынести
# то, чему нужна интерактивная сессия, из-под службы в отдельную задачу
# планировщика.
#
# Регистрируется НЕ install-node.ps1 (это делается вручную/по месту, только
# на нодах с GPU и реальной службой llm — большинство Windows-нод её не
# запускают вовсе), пример регистрации:
#
#   $action = New-ScheduledTaskAction -Execute "powershell.exe" `
#       -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\ProgramData\sa-home-bot\llm-runner.ps1"'
#   $trigger = New-ScheduledTaskTrigger -AtLogOn -User "User"
#   $principal = New-ScheduledTaskPrincipal -UserId "User" -LogonType Interactive -RunLevel Limited
#   $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
#       -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
#   Register-ScheduledTask -TaskName "sa-home-llm" -Action $action -Trigger $trigger `
#       -Principal $principal -Settings $settings -Force
#
# "llm" остаётся в [node].assignments конфига (нужно для маршрутизации,
# node/app.py::build_router), но сознательно убрана из ASSIGNMENT_ARGS
# супервизора (node/supervisor.py::EXTERNALLY_MANAGED_ASSIGNMENTS) — sa-home-node
# её не спавнит и не следит за ней, весь жизненный цикл — здесь.
#
# Перезапуск при падении — свой цикл (не полагаемся на Restart Count задачи
# планировщика, у него есть верхний предел попыток).
param(
    [string]$ExePath = "C:\ProgramData\sa-home-bot\pipx\venvs\sa-home-bot\Scripts\sa-home-bot.exe",
    [string]$ConfigPath = "C:\ProgramData\sa-home-bot\config.toml",
    [int]$RestartDelaySec = 5
)

function Log($msg) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

Log "llm-runner: старт (exe=$ExePath, config=$ConfigPath)"
while ($true) {
    & $ExePath --service llm --config $ConfigPath
    Log "llm завершился (код $LASTEXITCODE) — перезапуск через ${RestartDelaySec}с"
    Start-Sleep -Seconds $RestartDelaySec
}
