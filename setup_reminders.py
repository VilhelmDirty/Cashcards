"""Install, update or remove the Windows scheduled task that runs remind.py.

Normally you don't need this file directly: the app's Settings page installs the
schedule and updates it whenever you change the reminder times. From a terminal:

    .\\.venv\\Scripts\\python setup_reminders.py            install / update
    .\\.venv\\Scripts\\python setup_reminders.py --status   show next and last run
    .\\.venv\\Scripts\\python setup_reminders.py --remove   remove it

The task:
  - runs remind.py daily at the times chosen on the Settings page (remind.py itself
    skips days that aren't ticked as study days, and days with nothing due);
  - runs as me, only while I'm logged in (notifications need a desktop);
  - catches up if the computer was off at a reminder time ("start when available");
  - is listed in Task Scheduler under the name below. No administrator rights needed.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import config
import db

TASK_NAME = f"{config.APP_NAME} reminder"
NO_WINDOW = 0x08000000  # don't flash a console window when called from the app

INSTALL = r"""
$action = New-ScheduledTaskAction -Execute $env:FC_EXE -Argument ('"' + $env:FC_SCRIPT + '"') -WorkingDirectory $env:FC_DIR
$triggers = $env:FC_TIMES.Split(',') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $env:FC_TASK -Action $action -Trigger $triggers -Settings $settings -Description ('Shows a notification when ' + $env:FC_APP + ' has cards due.') -Force | Out-Null
"""

STATUS = r"""
$task = Get-ScheduledTask -TaskName $env:FC_TASK -ErrorAction SilentlyContinue
if (-not $task) { '{"installed": false}'; exit 0 }
$info = $task | Get-ScheduledTaskInfo
$ran = $info.LastRunTime.Year -ge 2000
[ordered]@{
    installed   = $true
    times       = @($task.Triggers | ForEach-Object { ([datetime]$_.StartBoundary).ToString('HH:mm') })
    next_run    = $(if ($info.NextRunTime) { $info.NextRunTime.ToString('ddd d MMM HH:mm') } else { $null })
    last_run    = $(if ($ran) { $info.LastRunTime.ToString('ddd d MMM HH:mm') } else { $null })
    last_ok     = $(if ($ran) { $info.LastTaskResult -eq 0 } else { $null })
} | ConvertTo-Json -Compress
"""

REMOVE = r"""
if (Get-ScheduledTask -TaskName $env:FC_TASK -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $env:FC_TASK -Confirm:$false
}
"""


class ScheduleError(Exception):
    pass


def _run(script, **env_vars):
    env = {**os.environ, "FC_TASK": TASK_NAME, "FC_APP": config.APP_NAME, **env_vars}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode != 0:
        raise ScheduleError(result.stderr.strip() or "PowerShell reported an error")
    return result.stdout.strip()


def install(times):
    """Create the task, or replace it with the given "HH:MM" times."""
    if not times:
        raise ScheduleError("Choose at least one reminder time.")
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # Python without a console window
    _run(INSTALL,
         FC_EXE=str(pythonw if pythonw.exists() else sys.executable),
         FC_SCRIPT=str(config.PROJECT_DIR / "remind.py"),
         FC_DIR=str(config.PROJECT_DIR),
         FC_TIMES=",".join(times))


def status():
    """{"installed": bool, "times": [...], "next_run": str, "last_run": str, "last_ok": bool}"""
    return json.loads(_run(STATUS) or '{"installed": false}')


def remove():
    _run(REMOVE)


def main():
    try:
        if "--remove" in sys.argv:
            remove()
            print("Removed (or wasn't installed).")
            return
        if "--status" not in sys.argv:
            conn = db.connect()
            times = db.reminder_times(conn)
            conn.close()
            install(times)
            print(f"Installed '{TASK_NAME}' at {', '.join(times)}.")
        info = status()
        if not info["installed"]:
            print("Not installed.")
            return
        print(f"Times:     {', '.join(info['times'])}")
        print(f"Next run:  {info['next_run']}")
        print(f"Last run:  {info['last_run'] or 'never'}"
              + ("" if info["last_ok"] is None else f" ({'OK' if info['last_ok'] else 'error'})"))
    except ScheduleError as err:
        sys.exit(f"Problem with the Windows schedule:\n{err}")


if __name__ == "__main__":
    main()
