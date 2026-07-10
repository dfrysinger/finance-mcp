import plistlib
import subprocess

import pytest

from finance_mcp import config, schedule


def _seed_credential(tmp_path, monkeypatch):
    """Point FINANCE_MCP_HOME at a private dir holding a saved access URL.

    The installer refuses to schedule a sync with no persisted credential, so
    install-path tests must set one up (mirroring a real `finance-mcp claim`).
    """
    home = tmp_path / "priv"
    monkeypatch.setenv("FINANCE_MCP_HOME", str(home))
    config.save_access_url("https://user:pw@example.com/simplefin")


class _FakeRunner:
    """Records launchctl invocations and returns scripted results."""

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        # Map the launchctl subcommand (calls[1]) to a return code.
        self.results = results or {}

    def __call__(self, args, capture_output=True, text=True, check=False):
        self.calls.append(list(args))
        sub = args[1] if len(args) > 1 else ""
        rc, out, err = self.results.get(sub, (0, "", ""))
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=err)


def test_render_plist_has_calendar_interval_and_program():
    data = schedule.render_plist(
        label="com.finance-mcp.autosync",
        program_args=["/py", "-m", "finance_mcp", "sync", "--days", "30"],
        hour=6,
        minute=30,
        stdout_path="/o.log",
        stderr_path="/e.log",
        env={"FINANCE_MCP_HOME": "/home"},
    )
    spec = plistlib.loads(data)
    assert spec["Label"] == "com.finance-mcp.autosync"
    assert spec["ProgramArguments"][0] == "/py"
    assert spec["ProgramArguments"][-2:] == ["--days", "30"]
    assert spec["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
    assert spec["RunAtLoad"] is True
    assert spec["StandardOutPath"] == "/o.log"
    assert spec["EnvironmentVariables"] == {"FINANCE_MCP_HOME": "/home"}


@pytest.mark.parametrize("hour,minute", [(-1, 0), (24, 0), (0, -1), (0, 60)])
def test_render_plist_rejects_out_of_range_time(hour, minute):
    with pytest.raises(ValueError):
        schedule.render_plist(
            label="l",
            program_args=["x"],
            hour=hour,
            minute=minute,
            stdout_path="/o",
            stderr_path="/e",
        )


def test_render_plist_rejects_empty_program():
    with pytest.raises(ValueError):
        schedule.render_plist(
            label="l", program_args=[], hour=1, minute=1,
            stdout_path="/o", stderr_path="/e",
        )


def test_default_program_args_uses_absolute_interpreter():
    args = schedule.default_program_args(days=14)
    assert args[1:] == ["-m", "finance_mcp", "sync", "--days", "14"]
    assert args[0].startswith("/")  # sys.executable is absolute


def test_install_writes_plist_and_bootstraps(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_credential(tmp_path, monkeypatch)
    monkeypatch.delenv("SIMPLEFIN_ACCESS_URL", raising=False)
    runner = _FakeRunner()
    result = schedule.install(
        hour=7, minute=15, days=21, runner=runner,
        check_platform=False, uid=501,
    )
    path = tmp_path / "Library" / "LaunchAgents" / f"{schedule.LABEL}.plist"
    assert path.exists()
    spec = plistlib.loads(path.read_bytes())
    assert spec["StartCalendarInterval"] == {"Hour": 7, "Minute": 15}
    assert spec["ProgramArguments"][-2:] == ["--days", "21"]
    assert spec["EnvironmentVariables"]["FINANCE_MCP_HOME"] == str(tmp_path / "priv")
    # bootout (idempotent cleanup) precedes bootstrap.
    assert runner.calls[0][:2] == ["launchctl", "bootout"]
    assert runner.calls[1][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert result["installed"] is True


def test_install_never_bakes_credential_into_plist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_credential(tmp_path, monkeypatch)
    # Even when the SimpleFIN credential is present in the environment, it must
    # NOT be written into the plist — the scheduled sync reads it from the 0600
    # access_url file, so baking it in would only create a second on-disk copy.
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", "https://user:secret@example.com/simplefin")
    schedule.install(runner=_FakeRunner(), check_platform=False, uid=501)
    path = schedule.plist_path()
    raw = path.read_bytes()
    assert b"secret" not in raw
    spec = plistlib.loads(raw)
    assert "SIMPLEFIN_ACCESS_URL" not in spec.get("EnvironmentVariables", {})
    # ...and the plist is owner-only from the moment it exists.
    assert (path.stat().st_mode & 0o777) == 0o600


def test_install_bakes_absolute_home_into_plist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # A RELATIVE FINANCE_MCP_HOME: the install-time credential guard resolves it
    # against the installer's cwd, but launchd runs the agent from its own
    # working directory. The plist must therefore bake the ABSOLUTE resolved
    # path, or the scheduled job would look for the credential in the wrong place
    # and authenticate-fail every run.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FINANCE_MCP_HOME", "relhome")
    config.save_access_url("******example.com/simplefin")
    schedule.install(runner=_FakeRunner(), check_platform=False, uid=501)
    spec = plistlib.loads(schedule.plist_path().read_bytes())
    baked = spec.get("EnvironmentVariables", {}).get("FINANCE_MCP_HOME")
    assert baked is not None
    from pathlib import Path

    assert Path(baked).is_absolute()
    assert Path(baked) == (tmp_path / "relhome").resolve()


def test_install_refuses_without_saved_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # A private home with NO access_url file — the credential lives only in the
    # shell env. The scheduled job runs with a minimal launchd environment and
    # would not see that env var, so install must fail loudly rather than
    # schedule a job that authenticate-fails every morning.
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path / "priv"))
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL", "https://user:pw@example.com/simplefin")
    runner = _FakeRunner()
    with pytest.raises(schedule.ScheduleError) as exc:
        schedule.install(runner=runner, check_platform=False, uid=501)
    assert "credential" in str(exc.value).lower()
    assert not schedule.plist_path().exists()
    assert runner.calls == []


def test_install_raises_when_bootstrap_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_credential(tmp_path, monkeypatch)
    runner = _FakeRunner(results={"bootstrap": (5, "", "Load failed: 5: input/output error")})
    with pytest.raises(schedule.ScheduleError) as exc:
        schedule.install(runner=runner, check_platform=False, uid=501)
    assert "bootstrap failed" in str(exc.value)
    assert "input/output error" in str(exc.value)


def test_uninstall_boots_out_and_removes_plist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_credential(tmp_path, monkeypatch)
    runner = _FakeRunner()
    schedule.install(runner=runner, check_platform=False, uid=501)
    path = schedule.plist_path()
    assert path.exists()
    result = schedule.uninstall(runner=runner, check_platform=False, uid=501)
    assert not path.exists()
    assert result["plist_removed"] is True
    assert any(c[:2] == ["launchctl", "bootout"] for c in runner.calls)


def test_uninstall_is_safe_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # bootout fails because nothing was loaded, and a domain-qualified `print` confirms
    # the label is absent — the normal idempotent case, not a real failure.
    runner = _FakeRunner(
        results={
            "bootout": (3, "", "not found"),
            "print": (113, "", "Could not find service"),
        }
    )
    result = schedule.uninstall(runner=runner, check_platform=False, uid=501)
    assert result["plist_removed"] is False
    assert result["installed"] is False


def test_uninstall_raises_and_keeps_plist_when_agent_stays_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_credential(tmp_path, monkeypatch)
    schedule.install(runner=_FakeRunner(), check_platform=False, uid=501)
    path = schedule.plist_path()
    assert path.exists()
    # bootout fails AND the agent is still loaded (`print` returns 0): a genuine
    # unload failure. The plist must be left in place and the error surfaced.
    runner = _FakeRunner(
        results={
            "bootout": (1, "", "Operation not permitted"),
            "print": (0, '{ "PID" = 123; };', ""),
        }
    )
    with pytest.raises(schedule.ScheduleError) as exc:
        schedule.uninstall(runner=runner, check_platform=False, uid=501)
    assert "still loaded" in str(exc.value)
    assert path.exists()


def test_status_reports_loaded_when_launchctl_lists_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_credential(tmp_path, monkeypatch)
    installer = _FakeRunner()
    schedule.install(runner=installer, check_platform=False, uid=501)
    runner = _FakeRunner(results={"list": (0, '{ "PID" = 123; };', "")})
    st = schedule.status(runner=runner, check_platform=False, uid=501)
    assert st["plist_present"] is True
    assert st["loaded"] is True


def test_status_not_loaded_when_launchctl_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = _FakeRunner(results={"list": (113, "", "Could not find service")})
    st = schedule.status(runner=runner, check_platform=False, uid=501)
    assert st["plist_present"] is False
    assert st["loaded"] is False


def test_operations_reject_non_macos(monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    for op in (schedule.install, schedule.uninstall, schedule.status):
        with pytest.raises(schedule.ScheduleError):
            op(runner=_FakeRunner(), check_platform=True)
