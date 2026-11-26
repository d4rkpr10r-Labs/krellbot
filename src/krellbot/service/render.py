"""Render scheduler units for `krellbot tick`.

The render functions are pure: each takes the absolute executable path and
the home directory, and returns bytes. They never call `Path.home()` and
never write files. Callers (the `service install` command, or a test) decide
when and where to write.

The bodies match the contract in `.omo/briefs/phase6-implement.md`:

- macOS launchd plist with label `dev.krellbot.tick`. `StartCalendarInterval`
  fires at minute 1 of every hour (no `Hour` key). `ProgramArguments` is
  exactly `[executable, "tick"]`. `RunAtLoad` is false. Logs go to
  `<home>/logs/tick.{out,err}.log`.
- Linux systemd user units `krellbot-tick.service` and `krellbot-tick.timer`.
  The timer has `OnCalendar=*-*-* *:01:00` and `Persistent=true`. The
  service `ExecStart` is the executable plus `tick`. No `User=` line — this
  is a user unit.
- Windows task XML with `<StartWhenAvailable>true</StartWhenAvailable>` and a
  calendar trigger at minute 1 of every hour. The Task XML schema (1.2) has
  no per-task environment-variable element, so the ``Exec`` block runs
  ``cmd.exe`` to set ``KRELLBOT_HOME`` for exactly the child tick:

      <Command>%SystemRoot%\\System32\\cmd.exe</Command>
      <Arguments>/d /c set "KRELLBOT_HOME=&lt;home&gt;" &amp;&amp; "&lt;executable&gt;" tick</Arguments>
      <WorkingDirectory>&lt;home&gt;</WorkingDirectory>

The ``render_heartbeat_*`` functions render a second, independent schedule
for ``krellbot service heartbeat`` (label ``dev.krellbot.heartbeat``, units
``krellbot-heartbeat.{timer,service}``, task ``\\krellbot\\heartbeat``). It
fires every 15 minutes and checks whether the tick ran; see
``supervise.check_heartbeat``. A check missed while the computer slept is
not caught up on wake where the platform allows it, so the tick, which is
caught up, runs first.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

# ``"`` cannot be safely nested inside cmd's double-quoted set command.
# ``%`` triggers cmd variable expansion; ``^`` is the cmd escape character.
# ``\n``/``\r`` would split the command or inject a literal CR.
_CMD_METACHARS = ('"', "%", "^", "\n", "\r")


def render_launchd(executable: str, home: Path) -> bytes:
    """Render the macOS launchd plist body for label `dev.krellbot.tick`."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        "    <string>dev.krellbot.tick</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{executable}</string>\n"
        "        <string>tick</string>\n"
        "    </array>\n"
        "    <key>StartCalendarInterval</key>\n"
        "    <dict>\n"
        "        <key>Minute</key>\n"
        "        <integer>1</integer>\n"
        "    </dict>\n"
        f"    <key>StandardOutPath</key>\n"
        f"    <string>{home}/logs/tick.out.log</string>\n"
        f"    <key>StandardErrorPath</key>\n"
        f"    <string>{home}/logs/tick.err.log</string>\n"
        "    <key>RunAtLoad</key>\n"
        "    <false/>\n"
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        "        <key>KRELLBOT_HOME</key>\n"
        f"        <string>{home}</string>\n"
        "    </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )
    return body.encode("utf-8")


def render_systemd(executable: str, home: Path) -> dict[str, bytes]:
    """Render the Linux systemd user units. Returns {filename: bytes}.

    The pair is `krellbot-tick.timer` and `krellbot-tick.service`. The timer
    is persistent, firing at minute 1 of every hour. The service has no
    `User=` line because user units run as the caller.
    """
    timer = (
        "[Unit]\n"
        "Description=krellbot tick timer\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*-*-* *:01:00\n"
        "Persistent=true\n"
        "Unit=krellbot-tick.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    service = (
        "[Unit]\n"
        "Description=krellbot tick\n\n"
        "[Service]\n"
        f"ExecStart={executable} tick\n"
        f"Environment=KRELLBOT_HOME={home}\n"
    )
    return {
        "krellbot-tick.timer": timer.encode("utf-8"),
        "krellbot-tick.service": service.encode("utf-8"),
    }


def render_windows(executable: str, home: Path) -> bytes:
    """Render the Windows task XML for the tick schedule.

    `<StartWhenAvailable>true</StartWhenAvailable>` lets the task catch up
    after the laptop wakes from sleep. The trigger fires at minute 1 of every
    hour.

    The Task Scheduler XML schema (1.2) has no per-task environment-variable
    element, so the task would otherwise inherit the user's environment and
    the tick would fall back to ``~/.krellbot`` while the operator installed
    against a different ``KRELLBOT_HOME``. The ``Exec`` block therefore runs
    through ``cmd.exe`` so the environment variable is set for exactly the
    child tick:

        cmd.exe /d /c set "KRELLBOT_HOME=<home>" && "<executable>" tick

    ``home`` and ``executable`` are XML-escaped before interpolation so a
    path containing ``&``, ``<`` or ``>`` cannot break the XML. A path or
    executable containing ``"``, ``%``, ``^``, newline or carriage return
    is refused with ``ValueError`` because cmd cannot quote it safely.
    """
    home_s = str(home)
    for ch in _CMD_METACHARS:
        if ch in home_s:
            raise ValueError(f"home contains cmd/XML metacharacter {ch!r}: {home_s!r}")
    for ch in _CMD_METACHARS:
        if ch in executable:
            raise ValueError(f"executable contains cmd/XML metacharacter {ch!r}: {executable!r}")

    home_x = escape(home_s)
    exe_x = escape(executable)
    arguments = f'/d /c set "KRELLBOT_HOME={home_x}" &amp;&amp; "{exe_x}" tick'

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Author>krellbot</Author>\n"
        "    <URI>\\krellbot\\tick</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <CalendarTrigger>\n"
        "      <StartBoundary>2026-01-01T00:01:00</StartBoundary>\n"
        "      <Repetition>\n"
        "        <Interval>PT1H</Interval>\n"
        "      </Repetition>\n"
        "    </CalendarTrigger>\n"
        "  </Triggers>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>%SystemRoot%\\System32\\cmd.exe</Command>\n"
        f"      <Arguments>{arguments}</Arguments>\n"
        f"      <WorkingDirectory>{home_x}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# The heartbeat check: ``<executable> service heartbeat`` every 15 minutes
# ---------------------------------------------------------------------------

HEARTBEAT_SECONDS = 15 * 60


def render_heartbeat_launchd(executable: str, home: Path) -> bytes:
    """Render the macOS launchd plist body for label `dev.krellbot.heartbeat`.

    `StartInterval` fires the check every 900 seconds. `ProgramArguments` is
    exactly `[executable, "service", "heartbeat"]`; logs go to
    `<home>/logs/heartbeat.{out,err}.log`. launchd runs a missed interval on
    wake; the check itself skips what it missed (`supervise.check_heartbeat`).
    """
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        "    <string>dev.krellbot.heartbeat</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{executable}</string>\n"
        "        <string>service</string>\n"
        "        <string>heartbeat</string>\n"
        "    </array>\n"
        "    <key>StartInterval</key>\n"
        f"    <integer>{HEARTBEAT_SECONDS}</integer>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{home}/logs/heartbeat.out.log</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{home}/logs/heartbeat.err.log</string>\n"
        "    <key>RunAtLoad</key>\n"
        "    <false/>\n"
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        "        <key>KRELLBOT_HOME</key>\n"
        f"        <string>{home}</string>\n"
        "    </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )
    return body.encode("utf-8")


def render_heartbeat_systemd(executable: str, home: Path) -> dict[str, bytes]:
    """Render the Linux user units `krellbot-heartbeat.{timer,service}`.

    The timer fires at minutes 0, 15, 30 and 45. It is not persistent: a
    check missed while the computer slept is skipped, not run on wake.
    """
    timer = (
        "[Unit]\n"
        "Description=krellbot heartbeat timer\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*-*-* *:00/15:00\n"
        "Persistent=false\n"
        "Unit=krellbot-heartbeat.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    service = (
        "[Unit]\n"
        "Description=krellbot heartbeat\n\n"
        "[Service]\n"
        f"ExecStart={executable} service heartbeat\n"
        f"Environment=KRELLBOT_HOME={home}\n"
    )
    return {
        "krellbot-heartbeat.timer": timer.encode("utf-8"),
        "krellbot-heartbeat.service": service.encode("utf-8"),
    }


def render_heartbeat_windows(executable: str, home: Path) -> bytes:
    """Render the Windows task XML for the heartbeat check.

    The calendar trigger repeats every 15 minutes (`PT15M`).
    `<StartWhenAvailable>` is false, so a check missed while the computer
    slept is skipped, not run on wake. `KRELLBOT_HOME` is set through
    `cmd.exe` exactly as in `render_windows`, with the same XML escaping and
    the same refusal of `"`, `%`, `^`, newline and carriage return.
    """
    home_s = str(home)
    for ch in _CMD_METACHARS:
        if ch in home_s:
            raise ValueError(f"home contains cmd/XML metacharacter {ch!r}: {home_s!r}")
    for ch in _CMD_METACHARS:
        if ch in executable:
            raise ValueError(f"executable contains cmd/XML metacharacter {ch!r}: {executable!r}")

    home_x = escape(home_s)
    exe_x = escape(executable)
    arguments = f'/d /c set "KRELLBOT_HOME={home_x}" &amp;&amp; "{exe_x}" service heartbeat'

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Author>krellbot</Author>\n"
        "    <URI>\\krellbot\\heartbeat</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <CalendarTrigger>\n"
        "      <StartBoundary>2026-01-01T00:00:00</StartBoundary>\n"
        "      <Repetition>\n"
        "        <Interval>PT15M</Interval>\n"
        "      </Repetition>\n"
        "    </CalendarTrigger>\n"
        "  </Triggers>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>%SystemRoot%\\System32\\cmd.exe</Command>\n"
        f"      <Arguments>{arguments}</Arguments>\n"
        f"      <WorkingDirectory>{home_x}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )
    return body.encode("utf-8")
