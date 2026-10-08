# Registers a Windows scheduled task that runs the trading bot every weekday.
# The task starts before 9:30 AM ET in both EDT and EST; the bot itself checks the
# Alpaca market calendar, skips holidays, and waits for the exact opening bell.
#
# 9:30 AM ET = 7:00 PM Sri Lanka time (EDT, Mar-Nov) / 8:00 PM (EST, Nov-Mar).
# Run once:  powershell -ExecutionPolicy Bypass -File D:\Alpaca\setup_schedule.ps1

$TaskName = "Alpaca MA Crossover Bot"
$ProjectDir = $PSScriptRoot
$Python = Join-Path $ProjectDir "venv\Scripts\python.exe"
$Script = Join-Path $ProjectDir "trading_bot.py"
$StartTime = "18:55"

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`" --wait-for-open" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $StartTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Runs the moving average crossover bot (symbols in config.py) at the 9:30 AM ET market open" -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
