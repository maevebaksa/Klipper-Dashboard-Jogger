import os
import shutil
import subprocess
import sys

import pytest

from jogger.terminal import host_for, ssh_argv

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash"), reason="runs the real prompt script with bash")


@pytest.fixture
def fake_ssh(tmp_path):
    """A stand-in ssh on PATH that prints the arguments it received."""
    script = tmp_path / "ssh"
    script.write_text('#!/bin/sh\nfor a in "$@"; do printf "[%s]" "$a"; done\n')
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


def run(env, default_host, typed):
    argv = ssh_argv(default_host)
    result = subprocess.run(argv, input=typed, env=env, capture_output=True, text=True, timeout=10)
    return result.returncode, result.stdout


@needs_bash
def test_enter_accepts_the_printer_host(fake_ssh):
    code, out = run(fake_ssh, "voron24.local", "\npi\n")
    assert code == 0
    assert out.endswith("[-o][ConnectTimeout=10][-l][pi][--][voron24.local]")


@needs_bash
def test_typed_host_overrides_default(fake_ssh):
    code, out = run(fake_ssh, "voron24.local", "192.168.1.30\nbiqu\n")
    assert out.endswith("[-l][biqu][--][192.168.1.30]")


@needs_bash
@pytest.mark.parametrize("typed", [
    "-oProxyCommand=touch /tmp/x\npi\n",   # option injection as host
    "host;reboot\npi\n",                   # shell metacharacters
    "voron.local\n-oProxyCommand=x\n",     # option injection as user
    "voron.local\n\n",                     # empty user
])
def test_hostile_or_empty_input_never_reaches_ssh(fake_ssh, typed):
    code, out = run(fake_ssh, "", typed)
    assert code == 2 and "[-l]" not in out


@needs_bash
def test_default_host_is_data_not_code(fake_ssh, tmp_path):
    marker = tmp_path / "pwned"
    code, out = run(fake_ssh, f"$(touch {marker})", "\npi\n")
    assert not marker.exists()
    assert code == 2


def test_host_for_uses_lan_addresses_only():
    assert host_for({"url": "http://voron24.local:7125"}) == "voron24.local"
    assert host_for({"url": "http://192.168.1.20:7125"}) == "192.168.1.20"
    assert host_for({"url": "https://example.com:443/moonraker", "remote": True}) == ""
    assert host_for({"url": "https://example.com:443", "remote": True,
                     "lan_fallback_url": "http://10.0.0.5:7125"}) == "10.0.0.5"
    assert host_for({"url": "http://8.8.8.8:7125"}) == ""
    assert host_for(None) == ""
