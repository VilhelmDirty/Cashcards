"""Install, update or remove the Windows scheduled task that runs remind.py.

    .\\.venv\\Scripts\\python setup_reminders.py            install / update
    .\\.venv\\Scripts\\python setup_reminders.py --status   show next and last run
    .\\.venv\\Scripts\\python setup_reminders.py --remove   remove it

Times come from config.REMINDER_TIMES. The task:
  - runs as me, only while I'm logged in (notifications need a desktop);
  - catches up if the computer was off at a reminder time ("start when available");
  - is listed in Task Scheduler under the name below, where it can also be removed.
No administrator rights are needed.
"""
import os
import subprocess
import sys
from pathlib import Path

import config

TASK_NAME = "Finance Flashcards reminder"

INSTALL = r"""
$action = New-ScheduledTaskAction -Execute $env:FC_EXE -Argument ('"' + $env:FC_SCRIPT + '"') -WorkingDirectory $env:FC_DIR
$triggers = $env:FC_TIMES.Split(',') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $env:FC_TASK -Action $action -Trigger $triggers -Settings $settings -Description 'Shows a notification when Finance Flashcards has cards due.' -Force | Out-Null
"""

STATUS = r"""
$task = Get-ScheduledTask -TaskName $env:FC_TASK -ErrorAction SilentlyContinue
if (-not $task) { 'Not installed.'; exit 0 }
$info = $task | Get-ScheduledTaskInfo
'State:       ' + $task.State
'Times:       ' + (($task.Triggers | ForEach-Object { ([datetime]$_.StartBoundary).ToString('HH:mm') }) -join ', ')
'Next run:    ' + $info.NextRunTime
'Last run:    ' + $(if ($info.LastRunTime.Year -lt 2000) { 'never' } else { $info.LastRunTime })
'Last result: ' + $(if ($info.LastRunTime.Year -lt 2000) { '-' } elseif ($info.LastTaskResult -eq 0) { 'OK' } else { 'error code ' + $info.LastTaskResult })
"""

REMOVE = r"""
if (Get-ScheduledTask -TaskName $env:FC_TASK -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $env:FC_TASK -Confirm:$false; 'Removed.'
} else { 'Not installed, nothing to remove.' }
"""


def run_powershell(script, **env_vars):
    env = {**os.environ, "FC_TASK": TASK_NAME, **env_vars}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"PowerShell reported an error:\n{result.stderr.strip()}")
    return result.stdout.strip()


def main():
    if "--remove" in sys.argv:
        print(run_powershell(REMOVE))
        return
    if "--status" not in sys.argv:
        # pythonw.exe runs Python without opening a console window.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        run_powershell(
            INSTALL,
            FC_EXE=str(pythonw if pythonw.exists() else sys.executable),
            FC_SCRIPT=str(config.PROJECT_DIR / "remind.py"),
            FC_DIR=str(config.PROJECT_DIR),
            FC_TIMES=",".join(config.REMINDER_TIMES),
        )
        print(f"Installed '{TASK_NAME}'.")
    print(run_powershell(STATUS))


if __name__ == "__main__":
    main()
