"""In-app updates from the touchscreen.

Two paths:

* Full update, when this user has passwordless sudo (the Raspberry Pi OS
  default for the first user): run scripts/update.sh, exactly what an SSH
  update runs (git pull, then the installer), in a transient systemd unit.
  The unit sits outside this app's service, so the installer's own
  "systemctl restart" of the app does not kill the update halfway. update.sh
  pulling a new copy of itself is safe: git replaces changed files with new
  files rather than rewriting them in place, and bash keeps reading the copy
  it opened.
* User-owned update, otherwise: fast-forward, move the pinned KlipperScreen
  checkout if klipperscreen.ref changed, and reinstall requirements.txt into
  the user-owned venv. Installer changes are left for scripts/update.sh over
  SSH, because the touchscreen cannot answer a sudo password prompt.

No path grants new privileges: sudo is only used when it already works
without a password (sudo -n never prompts).
"""
import getpass
import hashlib
import os
import re
import subprocess

# git fetch goes over the Internet; pip may compile wheels on a Pi. Both are
# bounded so a hung network cannot leave the update screen spinning forever.
GIT_TIMEOUT_S = 60
PIP_TIMEOUT_S = 15 * 60
# Changes to these files need root-level setup that only the installer does.
INSTALLER_FILES = ("scripts/install.sh",)
# Transient unit for full updates; its log stays readable with
# journalctl -u kdj-update after the unit is collected.
UPDATE_UNIT = "kdj-update"
# Written by scripts/install.sh into the app data directory when it finishes.
SETUP_STAMP = "setup-stamp"
# Output of the last in-app update or installer run, in the app data directory.
UPDATE_LOG = "last-update.log"
# Present when a failed run restarted the app to load newly pulled code; the
# next start opens the Update screen so the failure is shown, then removes it.
SHOW_RESULT_FLAG = "show-update-result"
# sudo -n and systemctl is-active answer at once; this only guards a wedge.
QUICK_TIMEOUT_S = 10


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


def _refuse_local_changes(status):
    if status["dirty"]:
        raise UpdateError("This checkout has local edits. Update over SSH with scripts/update.sh.")
    if status["ahead"]:
        raise UpdateError("This checkout has local commits. Update over SSH with git pull.")


def _fast_forward(source):
    old = _git(source, "rev-parse", "HEAD")
    _git(source, "merge", "--ff-only", "@{u}")
    return _git(source, "diff", "--name-only", old, "HEAD").splitlines()


def setup_state(source, data_dir):
    """'current', 'missing' or 'stale' for the installer's last completed run.

    install.sh writes the SHA-256 of itself to data_dir/setup-stamp at the end.
    'missing': it has never finished since the stamp was introduced, or every
    run since has failed. 'stale': it finished, but has changed since (an
    update added system packages without the installer running, which happens
    when sudo needs a password or an older version did the update).
    """
    try:
        with open(os.path.join(str(source), "scripts", "install.sh"), "rb") as script:
            digest = hashlib.sha256(script.read()).hexdigest()
    except OSError:
        return "missing"
    try:
        stamp = open(os.path.join(data_dir, SETUP_STAMP), encoding="ascii").read().strip()
    except (OSError, UnicodeDecodeError):
        return "missing"
    return "current" if stamp == digest else "stale"


def setup_current(source, data_dir):
    return setup_state(source, data_dir) == "current"


def head(source):
    """The checked-out commit, or '' if it cannot be read."""
    try:
        return _git(source, "rev-parse", "HEAD", timeout=QUICK_TIMEOUT_S)
    except UpdateError:
        return ""


def request_result_screen(data_dir):
    """Ask the next app start to open the Update screen (after a failed run)."""
    try:
        open(os.path.join(data_dir, SHOW_RESULT_FLAG), "w").close()
    except OSError:
        pass


def take_result_screen_request(data_dir):
    """True once if a previous run asked for the Update screen at start."""
    try:
        os.remove(os.path.join(data_dir, SHOW_RESULT_FLAG))
        return True
    except OSError:
        return False


def read_update_log(data_dir, lines=12):
    """The last lines the most recent in-app update or installer run printed."""
    try:
        with open(os.path.join(data_dir, UPDATE_LOG), encoding="utf8", errors="replace") as log:
            text = log.read()
    except OSError:
        return []
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "\n")
    return [line.rstrip() for line in text.splitlines() if line.strip()][-lines:]


def can_sudo():
    """True when sudo works without a password. Never prompts."""
    try:
        _run(["sudo", "-n", "true"], cwd="/", timeout=QUICK_TIMEOUT_S)
        return True
    except UpdateError:
        return False


def start_system_update(source, data_dir, installer_only=False):
    """Run scripts/update.sh (or install.sh alone) as this user, outside the app's service.

    Returns once the unit has started. On success the installer restarts the
    app's service, which ends this process; use system_update_running() to
    notice a run that stopped without restarting the app, and
    read_update_log() for what it printed. An update refuses local edits or
    commits before anything runs rather than leaving git pull to fail; the
    installer alone does not pull, so it runs regardless.
    """
    if not installer_only:
        _refuse_local_changes(check(source))
    source = os.path.abspath(str(source))
    user = getpass.getuser()
    script = "install.sh" if installer_only else "update.sh"
    log = os.path.join(os.path.abspath(str(data_dir)), UPDATE_LOG)
    _run([
        "sudo", "-n", "systemd-run", "--unit", UPDATE_UNIT, "--collect", "--quiet",
        # Run as this user, like an SSH session: install.sh refuses root and
        # calls sudo itself for the system steps.
        "--uid", user, "--gid", str(os.getgid()),
        "--setenv", f"HOME={os.path.expanduser('~')}", "--setenv", f"USER={user}",
        "--working-directory", source,
        # Keep the output where the app (and the user) can read it without
        # journal permissions, and across a reboot.
        "-p", f"StandardOutput=truncate:{log}", "-p", "StandardError=inherit",
        "/bin/bash", os.path.join(source, "scripts", script),
    ], cwd=source)


def system_update_running():
    """Whether the transient update unit is still active."""
    try:
        state = _run(["systemctl", "is-active", UPDATE_UNIT], cwd="/", timeout=QUICK_TIMEOUT_S)
    except UpdateError:
        return False  # is-active exits non-zero for inactive, failed and unknown
    return state in ("active", "activating", "reloading")


def apply(source, klipperscreen_dir, python):
    """Fast-forward and refresh user-owned dependencies. Returns what changed."""
    status = check(source)
    _refuse_local_changes(status)
    if not status["behind"]:
        return {"updated": False, "needs_installer": False}
    changed = _fast_forward(source)

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
