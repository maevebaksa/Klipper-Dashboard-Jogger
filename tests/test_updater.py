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


def test_no_upstream_is_a_clear_error(tmp_path):
    git(tmp_path, "init", "-q")
    with pytest.raises(updater.UpdateError, match="does not track"):
        updater.check(tmp_path)
