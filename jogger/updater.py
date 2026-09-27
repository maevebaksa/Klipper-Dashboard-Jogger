"""In-app updates for the parts that do not need root.

scripts/update.sh reruns the installer, which uses sudo (apt packages, udev,
polkit, the systemd unit) and cannot prompt from the touchscreen. This module
does the user-owned steps only: fast-forward this checkout, move the pinned
KlipperScreen checkout if klipperscreen.ref changed, and reinstall Python
requirements into the user-owned venv if requirements.txt changed. When an
update changes the installer itself, the caller tells the user to run
scripts/update.sh over SSH.
"""
import os
import subprocess

# git fetch goes over the Internet; pip may compile wheels on a Pi. Both are
# bounded so a hung network cannot leave the update screen spinning forever.
GIT_TIMEOUT_S = 60
PIP_TIMEOUT_S = 15 * 60
# Changes to these files need root-level setup that only the installer does.
INSTALLER_FILES = ("scripts/install.sh",)


class UpdateError(RuntimeError):
    pass


def _run(args, cwd, timeout=GIT_TIMEOUT_S):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UpdateError("Timed out. Check the controller's Internet connection.") from None
    except OSError as exc:
        raise UpdateError(f"Could not run {args[0]}: {exc.strerror}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise UpdateError(detail[-1] if detail else f"{args[0]} failed.")
    return result.stdout.strip()


def _git(source, *args, timeout=GIT_TIMEOUT_S):
    return _run(["git", "-C", str(source), *args], cwd=str(source), timeout=timeout)


def check(source):
    """Fetch and describe available updates. Raises UpdateError."""
    try:
        _git(source, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except UpdateError:
        raise UpdateError("This checkout does not track a remote branch.") from None
    _git(source, "fetch", "--quiet")
    behind = int(_git(source, "rev-list", "--count", "HEAD..@{u}") or 0)
    ahead = int(_git(source, "rev-list", "--count", "@{u}..HEAD") or 0)
    dirty = bool(_git(source, "status", "--porcelain", "--untracked-files=no"))
    changed = _git(source, "diff", "--name-only", "HEAD", "@{u}").splitlines() if behind else []
    log = _git(source, "log", "--format=%s", "-n", "8", "HEAD..@{u}").splitlines() if behind else []
    return {
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "changes": log,
        "needs_installer": any(f in changed for f in INSTALLER_FILES),
    }


def apply(source, klipperscreen_dir, python):
    """Fast-forward and refresh user-owned dependencies. Returns what changed."""
    status = check(source)
    if status["dirty"]:
        raise UpdateError("This checkout has local edits. Update over SSH with scripts/update.sh.")
    if status["ahead"]:
        raise UpdateError("This checkout has local commits. Update over SSH with git pull.")
    if not status["behind"]:
        return {"updated": False, "needs_installer": False}

    old = _git(source, "rev-parse", "HEAD")
    _git(source, "merge", "--ff-only", "@{u}")
    changed = _git(source, "diff", "--name-only", old, "HEAD").splitlines()

    if "klipperscreen.ref" in changed:
        ref = open(os.path.join(source, "klipperscreen.ref"), encoding="utf8").read().strip()
        if _git(klipperscreen_dir, "status", "--porcelain"):
            raise UpdateError("The KlipperScreen checkout has local edits. Run scripts/update.sh over SSH.")
        _git(klipperscreen_dir, "fetch", "origin", ref)
        _git(klipperscreen_dir, "checkout", "--detach", ref)

    if "requirements.txt" in changed:
        _run([str(python), "-m", "pip", "install", "--only-binary=sdbus", "-r",
              os.path.join(str(source), "requirements.txt")],
             cwd=str(source), timeout=PIP_TIMEOUT_S)

    return {
        "updated": True,
        "needs_installer": any(f in changed for f in INSTALLER_FILES),
    }
