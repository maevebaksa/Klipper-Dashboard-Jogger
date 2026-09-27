import os
import shutil
import subprocess

import pytest

from jogger import updater

real_run = updater._run


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo, path, text, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repos(tmp_path):
    """origin (bare), the controller's clone, and a developer clone that pushes."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    dev = tmp_path / "dev"
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    commit(dev, "README.md", "v1\n", "Initial")
    commit(dev, "requirements.txt", "requests\n", "Requirements")
    git(dev, "push", "-q", "origin", "HEAD:main")
    pi = tmp_path / "pi"
    git(tmp_path, "clone", "-q", "--branch", "main", str(origin), str(pi))
    return dev, pi


def test_up_to_date(repos):
    _dev, pi = repos
    status = updater.check(pi)
    assert status == {"behind": 0, "ahead": 0, "dirty": False, "changes": [], "needs_installer": False}
    assert updater.apply(pi, pi, "python") == {"updated": False, "needs_installer": False}


def test_check_lists_updates_and_apply_fast_forwards(repos, monkeypatch):
    dev, pi = repos
    commit(dev, "README.md", "v2\n", "Second change")
    commit(dev, "jogger/x.py", "x = 1\n", "Third change")
    git(dev, "push", "-q", "origin", "HEAD:main")

    status = updater.check(pi)
    assert status["behind"] == 2
    assert status["changes"] == ["Third change", "Second change"]
    assert not status["needs_installer"]

    calls = []
    monkeypatch.setattr(updater, "_run", lambda args, **kw: calls.append(args) or real_run(args, **kw))
    result = updater.apply(pi, pi, "python")
    assert result == {"updated": True, "needs_installer": False}
    assert (pi / "README.md").read_text() == "v2\n"
    assert not any("pip" in args for args in calls)  # requirements unchanged


def test_requirements_change_runs_pip_and_installer_change_is_flagged(repos, monkeypatch):
    dev, pi = repos
    commit(dev, "requirements.txt", "requests\nsegno\n", "Add segno")
    commit(dev, "scripts/install.sh", "echo hi\n", "Installer change")
    git(dev, "push", "-q", "origin", "HEAD:main")

    pip_calls = []

    def fake_run(args, **kw):
        if "pip" in args:
            pip_calls.append(args)
            return ""
        return real_run(args, **kw)
    monkeypatch.setattr(updater, "_run", fake_run)
    assert updater.check(pi)["needs_installer"]
    result = updater.apply(pi, pi, "/venv/python")
    assert result == {"updated": True, "needs_installer": True}
    assert pip_calls and pip_calls[0][0] == "/venv/python"
    assert pip_calls[0][-1].endswith("requirements.txt")


def test_local_edits_or_commits_are_never_overwritten(repos):
    dev, pi = repos
    commit(dev, "README.md", "v2\n", "Upstream change")
    git(dev, "push", "-q", "origin", "HEAD:main")

    (pi / "README.md").write_text("my edit\n")
    assert updater.check(pi)["dirty"]
    with pytest.raises(updater.UpdateError, match="local edits"):
        updater.apply(pi, pi, "python")
    assert (pi / "README.md").read_text() == "my edit\n"

    git(pi, "checkout", "-q", "--", "README.md")
    commit(pi, "notes.txt", "mine\n", "Local commit")
    with pytest.raises(updater.UpdateError, match="local commits"):
        updater.apply(pi, pi, "python")


def fake_system(monkeypatch, fail=()):
    """Record sudo/systemctl calls instead of running them; git runs for real."""
    calls = []

    def run(args, **kw):
        if args[0] in ("sudo", "systemctl"):
            calls.append(args)
            if args[0] in fail or (len(args) > 1 and args[1] in fail):
                raise updater.UpdateError(f"{args[0]} failed")
            return "active" if args[0] == "systemctl" else ""
        return real_run(args, **kw)
    monkeypatch.setattr(updater, "_run", run)
    monkeypatch.setattr(updater.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(updater.getpass, "getuser", lambda: "pi")
    return calls


def test_can_sudo_never_prompts(monkeypatch):
    calls = fake_system(monkeypatch)
    assert updater.can_sudo()
    assert calls == [["sudo", "-n", "true"]]
    fake_system(monkeypatch, fail=("sudo",))
    assert not updater.can_sudo()


def test_full_update_runs_update_sh_outside_the_app(repos, monkeypatch):
    dev, pi = repos
    commit(dev, "scripts/install.sh", "echo new\n", "Installer change")
    git(dev, "push", "-q", "origin", "HEAD:main")
    calls = fake_system(monkeypatch)

    updater.start_system_update(pi, pi.parent)

    (run,) = calls
    assert run[:4] == ["sudo", "-n", "systemd-run", "--unit"]
    assert run[run.index("--unit") + 1] == "kdj-update"
    assert run[run.index("--uid") + 1] == "pi"  # as the user, never as root
    assert "USER=pi" in run
    assert run[-2:] == ["/bin/bash", os.path.abspath(str(pi / "scripts" / "update.sh"))]
    # The script does the pull, exactly as over SSH; nothing was pulled here.
    assert not (pi / "scripts" / "install.sh").exists()


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(not shutil.which("bash") or os.name == "nt", reason="runs the real update.sh")
def test_button_command_runs_the_real_update_script(repos, monkeypatch, tmp_path):
    """The unit's command, minus sudo/systemd-run, against the repo's update.sh."""
    dev, pi = repos
    real_update = open(os.path.join(REPO, "scripts", "update.sh"), encoding="utf8").read()
    marker = tmp_path / "installer-ran"
    commit(dev, "scripts/update.sh", real_update, "Add update.sh")
    commit(dev, "scripts/install.sh", f'echo "$PWD" > "{marker}"\n', "Fake installer")
    git(dev, "push", "-q", "origin", "HEAD:main")
    calls = fake_system(monkeypatch)
    # update.sh must exist in the checkout the button runs; bring pi to the
    # commit that has it, then add one more upstream change for it to pull.
    git(pi, "pull", "-q", "--ff-only")
    commit(dev, "README.md", "v2\n", "Upstream change")
    git(dev, "push", "-q", "origin", "HEAD:main")

    updater.start_system_update(pi, pi.parent)
    (run,) = calls
    command = run[-2:]  # bash and the script; the rest is sudo and systemd-run
    subprocess.run(command, cwd=pi, check=True, capture_output=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    assert (pi / "README.md").read_text() == "v2\n"   # git pull ran
    assert marker.exists()                           # then the installer


def test_full_update_refuses_local_changes(repos, monkeypatch):
    dev, pi = repos
    commit(dev, "README.md", "v2\n", "Upstream change")
    git(dev, "push", "-q", "origin", "HEAD:main")
    (pi / "README.md").write_text("my edit\n")
    calls = fake_system(monkeypatch)
    with pytest.raises(updater.UpdateError, match="local edits"):
        updater.start_system_update(pi, pi.parent)
    assert calls == []


def test_system_update_running(monkeypatch):
    fake_system(monkeypatch)
    assert updater.system_update_running()
    fake_system(monkeypatch, fail=("systemctl",))
    assert not updater.system_update_running()


def test_installer_only_runs_install_sh_even_with_local_edits(repos, monkeypatch):
    _dev, pi = repos
    (pi / "README.md").write_text("my edit\n")
    calls = fake_system(monkeypatch)
    updater.start_system_update(pi, pi.parent, installer_only=True)
    (run,) = calls
    assert run[-2:] == ["/bin/bash", os.path.abspath(str(pi / "scripts" / "install.sh"))]
    log = os.path.join(os.path.abspath(str(pi.parent)), updater.UPDATE_LOG)
    assert f"StandardOutput=truncate:{log}" in run and "StandardError=inherit" in run
    assert (pi / "README.md").read_text() == "my edit\n"


def test_setup_state_and_update_log(tmp_path):
    import hashlib
    source, data = tmp_path / "src", tmp_path / "data"
    (source / "scripts").mkdir(parents=True)
    data.mkdir()
    (source / "scripts" / "install.sh").write_bytes(b"echo v1\n")
    assert updater.setup_state(source, data) == "missing"
    (data / updater.SETUP_STAMP).write_text(hashlib.sha256(b"old").hexdigest())
    assert updater.setup_state(source, data) == "stale"
    (data / updater.SETUP_STAMP).write_text(hashlib.sha256(b"echo v1\n").hexdigest())
    assert updater.setup_state(source, data) == "current"

    assert updater.read_update_log(data) == []
    (data / updater.UPDATE_LOG).write_text(
        "".join(f"line {i}\n" for i in range(30)) + "\x1b[1mE: Unable to locate package\x1b[0m\r\n\n")
    tail = updater.read_update_log(data, lines=3)
    assert tail == ["line 28", "line 29", "E: Unable to locate package"]


def test_setup_current_tracks_the_installer_that_last_ran(tmp_path):
    import hashlib
    source, data = tmp_path / "src", tmp_path / "data"
    (source / "scripts").mkdir(parents=True)
    data.mkdir()
    installer = source / "scripts" / "install.sh"
    installer.write_bytes(b"echo v1\n")
    assert not updater.setup_current(source, data)  # never ran: no stamp

    # What install.sh writes: sha256sum of itself.
    (data / updater.SETUP_STAMP).write_text(hashlib.sha256(b"echo v1\n").hexdigest() + "\n")
    assert updater.setup_current(source, data)

    installer.write_bytes(b"echo v2 adds a package\n")  # an update changed setup
    assert not updater.setup_current(source, data)


@pytest.mark.skipif(not shutil.which("sha256sum"), reason="uses coreutils like install.sh")
def test_stamp_format_matches_install_sh(tmp_path):
    source, data = tmp_path / "src", tmp_path / "data"
    (source / "scripts").mkdir(parents=True)
    data.mkdir()
    (source / "scripts" / "install.sh").write_text("echo setup\n")
    # The exact line from scripts/install.sh.
    subprocess.run(
        ["bash", "-c", 'sha256sum "$SOURCE/scripts/install.sh" | cut -d\' \' -f1 > "$KDJ_DATA/setup-stamp"'],
        env={**os.environ, "SOURCE": str(source), "KDJ_DATA": str(data)}, check=True,
    )
    assert updater.setup_current(source, data)


def test_no_upstream_is_a_clear_error(tmp_path):
    git(tmp_path, "init", "-q")
    with pytest.raises(updater.UpdateError, match="does not track"):
        updater.check(tmp_path)
