# Runs the llm service in an INTERACTIVE user session (live finding
# 2026-07-23): WSL2 refuses to start when invoked from the sa-home-node
# Windows service (Session 0, LocalSystem) - wsl.exe exits immediately
# with code -1 (4294967295), the WSL VM never comes up at all.
#
# Same workaround already used for auto-update (win-auto-update.ps1: stop/
# pipx/start instead of the protocol update RPC): move whatever needs an
# interactive session out of the service into its own scheduled task.
#
# Registered manually (not by install-node.ps1 - only needed on GPU nodes
# actually running llm), example registration:
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
# NOTE (live finding 2026-07-23): this file must stay plain ASCII. Windows
# PowerShell 5.1 auto-detects source encoding for .ps1 files without a BOM
# using the system codepage, not UTF-8 - non-ASCII (Cyrillic) comments here
# caused a real parser error ("TerminatorExpectedAtEndOfString") specifically
# when launched via Task Scheduler (works fine run directly from an
# interactive shell, which is why this was hard to spot at first).
#
# "llm" stays in [node].assignments (routing needs it - node/app.py's
# build_router()) but is intentionally excluded from the supervisor's
# ASSIGNMENT_ARGS (node/supervisor.py::EXTERNALLY_MANAGED_ASSIGNMENTS) -
# sa-home-node does not spawn or watch it, this script owns its whole
# lifecycle instead.
#
# Restart-on-crash is a plain loop here, not the scheduled task's own
# Restart Count (Task Scheduler caps total restart attempts).
param(
    [string]$ExePath = "C:\ProgramData\sa-home-bot\pipx\venvs\sa-home-bot\Scripts\sa-home-bot.exe",
    [string]$ConfigPath = "C:\ProgramData\sa-home-bot\config.toml",
    [int]$RestartDelaySec = 5
)

function Log($msg) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

Log "llm-runner: starting (exe=$ExePath, config=$ConfigPath)"
while ($true) {
    & $ExePath --service llm --config $ConfigPath
    Log "llm exited (code $LASTEXITCODE) - restarting in ${RestartDelaySec}s"
    Start-Sleep -Seconds $RestartDelaySec
}
