r"""Make the application come up on its own, so the night runs itself.

The daily policy fires at 02:00 Oslo, and the thing that fires it -- the
``DailyScheduler`` -- lives inside ``scripts/serve.py``, beside the ``JobWorker``
that actually executes the stages. Neither exists outside that process. So
"produce a video every night without being asked" reduces to one requirement:
**that process has to be running at 02:00**.

On the night of 2026-08-30 it was not. The launcher had stopped with an error
two days earlier and was sitting at a "press Enter" prompt; nothing was
listening, no log line was written after 03:36 on the 29th, and the recording
made at 21:43 that evening was never even discovered. Nothing failed, because
nothing started.

This registers a Windows scheduled task that starts the application:

* **at log on**, so a normal day brings it up, and
* **at 01:45 every night**, waking the machine if it is asleep, as the safety
  net for the day it was not.

``--keep-existing`` means a healthy instance is left alone: the task can fire
twice without the second copy stopping the first.

    python scripts/autostart.py status
    python scripts/autostart.py install
    python scripts/autostart.py remove

Two honest limits, neither of them fixable from here:

* The task runs **only while this user is logged on**. Analysis needs the GPU
  and Ollama, both of which belong to an interactive session; a task running as
  SYSTEM with nobody logged in would fail at the first vision batch. Logging
  out at night therefore stops the night.
* Registering a task is a change to the machine, so this script asks Windows to
  do it and reports exactly what Windows said. It is undone by ``remove``.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "VAI daily production"

#: Before 02:00, with room for the machine to wake and the app to come up.
NIGHTLY_AT = "01:45:00"


def _account() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def _interpreter() -> Path:
    """The windowless interpreter of this project's own environment.

    ``pythonw`` rather than ``python`` so a nightly start does not throw a
    console window onto the screen at 01:45; the application writes to
    ``logs/`` either way, so nothing is lost by having no console.
    """
    windowless = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    return windowless if windowless.is_file() else Path(sys.executable)


def _xml() -> str:
    """The task definition.

    Written as XML rather than assembled from ``schtasks`` flags because the
    flags cannot express half of it -- waking the machine, restarting after a
    failure, or refusing to start a second copy.
    """
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the VAI application, which carries the daily
    scheduler (02:00 Europe/Oslo) and the job worker that runs the stages.
    Registered by scripts/autostart.py.</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_account()}</UserId>
    </LogonTrigger>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{NIGHTLY_AT}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_account()}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_interpreter()}</Command>
      <Arguments>{ROOT / "scripts" / "serve.py"} --keep-existing</Arguments>
      <WorkingDirectory>{ROOT}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(arguments: list[str]) -> tuple[int, str]:
    finished = subprocess.run(
        arguments, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return finished.returncode, (finished.stdout + finished.stderr).strip()


def install() -> int:
    document = Path(tempfile.gettempdir()) / "vai-autostart.xml"
    document.write_text(_xml(), encoding="utf-16")
    try:
        code, said = _run(
            ["schtasks", "/create", "/tn", TASK_NAME, "/xml", str(document), "/f"]
        )
    finally:
        with contextlib.suppress(OSError):
            document.unlink()
    print(said or "(schtasks said nothing)")
    if code != 0:
        print(
            "\nThe task was not registered. If Windows refused for permission, "
            "run this\nfrom a terminal started with 'Run as administrator' and "
            "try again."
        )
        return code
    print(f"\nRegistered '{TASK_NAME}':")
    print(f"  starts   {_interpreter()} scripts/serve.py --keep-existing")
    print(f"  at       log on, and every night at {NIGHTLY_AT[:5]} (waking the machine)")
    print("  policy   production 02:00 Europe/Oslo, publication 10:00, from config/daily.yaml")
    print("\nIt does nothing while you are logged out: analysis needs the GPU and")
    print("Ollama, which belong to an interactive session.")
    print(f"\nStart it now without waiting:   schtasks /run /tn \"{TASK_NAME}\"")
    print("Undo:                           python scripts/autostart.py remove")
    return 0


def remove() -> int:
    code, said = _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    print(said or "(schtasks said nothing)")
    return code


def status() -> int:
    code, said = _run(["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"])
    if code != 0:
        print(f"'{TASK_NAME}' is not registered: the night depends on the")
        print("application already being up, which is what it was not.")
        print("\n  python scripts/autostart.py install")
        return 1
    keep = (
        "TaskName", "Status", "Next Run Time", "Last Run Time", "Last Result",
        "Task To Run", "Start In", "Schedule Type", "Start Time", "Repeat",
    )
    for line in said.splitlines():
        if any(line.startswith(name) for name in keep):
            print("  " + line.strip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("status", "install", "remove"), nargs="?", default="status"
    )
    arguments = parser.parse_args()
    if sys.platform != "win32":
        print("This registers a Windows scheduled task and only runs on Windows.")
        return 2
    return {"status": status, "install": install, "remove": remove}[arguments.command]()


if __name__ == "__main__":
    raise SystemExit(main())
