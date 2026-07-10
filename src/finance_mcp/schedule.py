"""Daily auto-refresh via a macOS launchd LaunchAgent.

The manual refresh is ``finance-mcp sync``. This module installs a per-user
launchd agent that runs that same sync once a day, so the archive stays fresh
without the user remembering to pull. It is entirely opt-in and reversible:

* ``finance-mcp schedule --install [--at HH:MM] [--days N]`` writes a LaunchAgent
  plist under ``~/Library/LaunchAgents`` and loads it.
* ``finance-mcp schedule --uninstall`` unloads and removes it.
* ``finance-mcp schedule --status`` reports whether it is installed and loaded.

Design notes:

* The agent invokes ``<python> -m finance_mcp sync`` with the **absolute**
  interpreter path captured at install time. A LaunchAgent runs with a minimal
  ``PATH`` that need not contain the venv's ``bin`` directory, so relying on the
  ``finance-mcp`` console script being on ``PATH`` would be fragile; the module
  entry point sidesteps that.
* If ``FINANCE_MCP_HOME`` is set in the installing shell, it is baked into the
  agent's environment so the scheduled sync reads the same private storage (the
  credential + archive) as the interactive CLI.
* launchctl invocation is injected (``runner``) so the command construction is
  unit-testable on any platform without touching a real launchd.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from . import config

#: Reverse-DNS label identifying the agent to launchd. Also the plist filename.
LABEL = "com.finance-mcp.autosync"

#: Default daily run time (local wall-clock), chosen to be after most banks post
#: overnight batches but early enough that the morning's data is fresh.
DEFAULT_HOUR = 6
DEFAULT_MINUTE = 30

#: Default look-back for the scheduled sync. A daily agent only needs a few days
#: to cover weekends/holidays and any late-posting corrections, but a wider
#: default cheaply heals gaps if the machine was asleep for a while.
DEFAULT_SYNC_DAYS = 30

#: A callable with the shape of ``subprocess.run`` used to invoke ``launchctl``.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ScheduleError(RuntimeError):
    """A schedule install/uninstall/status operation could not be completed."""


def launch_agents_dir() -> Path:
    """Return ``~/Library/LaunchAgents``, creating it 0700 if absent.

    ``mkdir(mode=...)`` only sets the mode when it *creates* the directory, so an
    already-present ``~/Library/LaunchAgents`` (commonly 0755) is explicitly
    tightened to 0700 as well — the plist below is per-user state that no other
    local account should be able to read or replace.
    """
    d = Path.home() / "Library" / "LaunchAgents"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        # A directory we don't own (unusual for ~/Library) — leave it as-is
        # rather than crash; the plist itself is still written 0600 below.
        pass
    return d


def plist_path(label: str = LABEL) -> Path:
    """Path to this agent's LaunchAgent plist."""
    return launch_agents_dir() / f"{label}.plist"


def log_dir() -> Path:
    """Return the private log directory for the scheduled sync's stdout/stderr."""
    d = config.home_dir() / "logs"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def default_program_args(days: int = DEFAULT_SYNC_DAYS) -> list[str]:
    """Command the agent runs: the current interpreter, the package module, sync.

    Uses ``sys.executable`` (absolute) so the agent does not depend on ``PATH``.
    """
    return [sys.executable, "-m", "finance_mcp", "sync", "--days", str(int(days))]


def render_plist(
    *,
    label: str,
    program_args: Sequence[str],
    hour: int,
    minute: int,
    stdout_path: str,
    stderr_path: str,
    env: dict[str, str] | None = None,
    run_at_load: bool = True,
) -> bytes:
    """Build the LaunchAgent plist XML (bytes) for a daily calendar run.

    Pure and side-effect-free so it can be asserted on directly. ``hour`` and
    ``minute`` are validated to real wall-clock ranges because launchd silently
    ignores an out-of-range ``StartCalendarInterval`` (the agent would then never
    fire, a silent failure we refuse to emit).
    """
    if not program_args:
        raise ValueError("program_args must not be empty")
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be in 0..23, got {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"minute must be in 0..59, got {minute}")

    spec: dict[str, object] = {
        "Label": label,
        "ProgramArguments": list(program_args),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": bool(run_at_load),
        "StandardOutPath": stdout_path,
        "StandardErrorPath": stderr_path,
        "ProcessType": "Background",
    }
    if env:
        spec["EnvironmentVariables"] = dict(env)
    return plistlib.dumps(spec, sort_keys=True)


def _agent_env() -> dict[str, str]:
    """Environment to bake into the agent so it reads the same private storage.

    Only ``FINANCE_MCP_HOME`` (a path, not a secret) is propagated, and only when
    it is explicitly set in the installing shell. It is resolved to an *absolute*
    path before being baked into the plist: launchd runs the agent with its own
    working directory, so a relative ``FINANCE_MCP_HOME`` (which the install-time
    credential guard would have resolved against the installer's cwd) would
    otherwise point the scheduled job at a different directory and it would fail
    to find the credential every run. The SimpleFIN access credential is
    deliberately NOT written into the plist: the scheduled ``sync`` reads it from
    the 0600 ``access_url`` file via :func:`config.load_access_url`, so baking it
    into a LaunchAgent plist would only create a second, world-readable-until-chmod
    on-disk copy of a bank credential (and one that backup/sync tooling would
    capture) for no benefit.
    """
    env: dict[str, str] = {}
    home = os.environ.get("FINANCE_MCP_HOME")
    if home:
        env["FINANCE_MCP_HOME"] = str(Path(home).expanduser().resolve())
    return env


def _require_macos(check: bool) -> None:
    if check and sys.platform != "darwin":
        raise ScheduleError(
            "Automatic scheduling uses macOS launchd and is only available on "
            f"macOS; this platform is {sys.platform!r}. Run `finance-mcp sync` "
            "from your own cron/systemd timer instead."
        )


def _run(runner: Runner, args: list[str]) -> "subprocess.CompletedProcess[str]":
    return runner(args, capture_output=True, text=True, check=False)


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so it is 0600 for the whole time it exists.

    The file is created with ``O_CREAT`` at mode 0600 directly, so there is no
    window in which it exists at the process umask (commonly 0644) before a
    follow-up ``chmod`` — a plain ``write_bytes`` then ``chmod`` would leave that
    gap. ``O_TRUNC`` makes a reinstall overwrite an existing plist in place.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    # If the plist already existed at looser perms from a prior version, tighten
    # it — O_CREAT does not change the mode of a pre-existing file.
    os.chmod(path, 0o600)


def install(
    *,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    days: int = DEFAULT_SYNC_DAYS,
    program_args: Sequence[str] | None = None,
    runner: Runner = subprocess.run,
    check_platform: bool = True,
    uid: int | None = None,
) -> dict:
    """Write the LaunchAgent plist and load it into the user's launchd domain.

    Idempotent: an already-loaded agent is booted out first so the new plist
    takes effect. Returns a summary dict. Raises :class:`ScheduleError` if
    launchd rejects the load (surfacing its stderr) so a failed install never
    looks like success.
    """
    _require_macos(check_platform)
    # The scheduled sync runs under a minimal launchd environment, so it can only
    # read the SimpleFIN credential from the saved 0600 access_url file — NOT from
    # a SIMPLEFIN_ACCESS_URL variable in the installing shell (which is not, and
    # for security must not be, propagated into the plist). Refuse to install a
    # scheduler that would authenticate-fail on every run; fail loudly here where
    # the user can act, instead of silently the next morning in a log file.
    if config.load_access_url_file() is None:
        raise ScheduleError(
            "No saved SimpleFIN credential at "
            f"{config.access_url_path()}. The daily scheduled sync runs with a "
            "minimal launchd environment and can only read the credential from "
            "that 0600 file, not from a SIMPLEFIN_ACCESS_URL variable in your "
            "shell. Run `finance-mcp claim <setup-token>` to save it before "
            "enabling the schedule."
        )
    args = list(program_args) if program_args is not None else default_program_args(days)
    out_path = str(log_dir() / "autosync.out.log")
    err_path = str(log_dir() / "autosync.err.log")
    data = render_plist(
        label=LABEL,
        program_args=args,
        hour=hour,
        minute=minute,
        stdout_path=out_path,
        stderr_path=err_path,
        env=_agent_env(),
    )
    path = plist_path()
    _write_private(path, data)

    resolved_uid = os.getuid() if uid is None else uid
    domain = f"gui/{resolved_uid}"
    # Best-effort remove any prior instance so bootstrap doesn't fail on a
    # duplicate label; ignore its result (it fails harmlessly when not loaded).
    _run(runner, ["launchctl", "bootout", f"{domain}/{LABEL}"])
    boot = _run(runner, ["launchctl", "bootstrap", domain, str(path)])
    if boot.returncode != 0:
        raise ScheduleError(
            f"launchctl bootstrap failed (exit {boot.returncode}): "
            f"{(boot.stderr or boot.stdout or '').strip()}"
        )
    return {
        "installed": True,
        "plist": str(path),
        "label": LABEL,
        "hour": hour,
        "minute": minute,
        "days": days,
        "program_args": args,
        "stdout": out_path,
        "stderr": err_path,
    }


def uninstall(
    *,
    runner: Runner = subprocess.run,
    check_platform: bool = True,
    uid: int | None = None,
) -> dict:
    """Unload the agent and delete its plist. Safe to call when not installed."""
    _require_macos(check_platform)
    resolved_uid = os.getuid() if uid is None else uid
    domain = f"gui/{resolved_uid}"
    boot = _run(runner, ["launchctl", "bootout", f"{domain}/{LABEL}"])
    if boot.returncode != 0:
        # bootout returns nonzero both when the agent was never loaded (the
        # normal idempotent case) and when an unload genuinely failed. They are
        # not interchangeable: deleting the plist while the agent is still loaded
        # would orphan a running job with no plist left to reason about or remove
        # it. Verify against the SAME bootstrap domain bootout targeted (a bare
        # `launchctl list LABEL` queries a different context and could miss it):
        # `launchctl print gui/$uid/LABEL` exits 0 only when the service is still
        # present in that domain. Treat a still-present service as a hard failure
        # and leave the plist in place; any other result is the benign
        # already-absent case and proceeds to cleanup.
        still = _run(runner, ["launchctl", "print", f"{domain}/{LABEL}"])
        if still.returncode == 0:
            raise ScheduleError(
                f"launchctl bootout failed (exit {boot.returncode}) and agent "
                f"{domain}/{LABEL} is still loaded; leaving its plist in place: "
                f"{(boot.stderr or boot.stdout or '').strip()}"
            )
    path = plist_path()
    existed = path.exists()
    if existed:
        path.unlink()
    return {
        "installed": False,
        "plist_removed": existed,
        "label": LABEL,
        "bootout_exit": boot.returncode,
    }


def status(
    *,
    runner: Runner = subprocess.run,
    check_platform: bool = True,
    uid: int | None = None,
) -> dict:
    """Report whether the agent's plist exists and whether launchd has it loaded."""
    _require_macos(check_platform)
    path = plist_path()
    resolved_uid = os.getuid() if uid is None else uid
    listed = _run(runner, ["launchctl", "list", LABEL])
    loaded = listed.returncode == 0
    return {
        "plist_present": path.exists(),
        "plist": str(path),
        "loaded": loaded,
        "label": LABEL,
        "detail": (listed.stdout or "").strip() if loaded else None,
    }
