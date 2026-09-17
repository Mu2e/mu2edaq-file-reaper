"""Start/stop scripts driven for real (argument handling via the FR_DRY_RUN hook,
process discovery against real processes)."""

import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = os.path.join(ROOT, "start-mu2edaq-file-reaper.sh")
STOP = os.path.join(ROOT, "stop-mu2edaq-file-reaper.sh")
PROC = os.path.join(ROOT, "lib", "file-reaper-proc.sh")

pytestmark = pytest.mark.skipif(sys.platform.startswith("win") or shutil.which("bash") is None,
                                reason="POSIX shell scripts")


def run(args, env=None, cwd=ROOT):
    e = dict(os.environ, FR_DRY_RUN="1")
    e.update(env or {})
    return subprocess.run(["bash"] + args, cwd=cwd, env=e, capture_output=True, text=True, timeout=60)


def test_bash_syntax():
    for script in (START, STOP, PROC, os.path.join(ROOT, "bootstrap.sh")):
        assert subprocess.run(["bash", "-n", script]).returncode == 0, script


def test_start_arg_forms(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("reaper: {}\n")
    pid = tmp_path / "x.pid"
    for args in ([str(cfg)], ["-c", str(cfg)], [f"--config={cfg}"]):
        r = run([START] + args + ["--pid-file", str(pid)])
        assert r.returncode == 0, r.stderr
        assert f"config: {cfg}" in r.stdout and "http=5004" in r.stdout
    r = run([START, "-c", str(cfg), "-p", "5010", "--pid-file", str(pid)])
    assert "http=5010" in r.stdout
    r = run([START, "-c", str(cfg), "--pid-file", str(pid)], env={"CRS_PORT_HTTP": "5011"})
    assert "http=5011" in r.stdout
    r = run([START, "-c", str(tmp_path / "missing.yaml"), "--pid-file", str(pid)])
    assert r.returncode == 1 and "not found" in r.stderr
    r = run([START, "-c"])
    assert r.returncode == 2
    assert run([START, "--help"]).returncode == 0


def test_stop_without_running_copy(tmp_path):
    pid = tmp_path / "none.pid"
    r = run([STOP, str(pid)])
    assert r.returncode == 0 and "not running" in r.stdout
    pid.write_text("999999\n")
    r = run([STOP, str(pid)])
    assert r.returncode == 0 and "stale" in r.stdout and not pid.exists()


def test_stale_pid_naming_a_live_stranger_is_not_killed(tmp_path):
    """A pid file left by a SIGKILLed daemon may name an unrelated live process."""
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid = tmp_path / "s.pid"
        pid.write_text(f"{stranger.pid}\n")
        r = run([STOP, str(pid)])
        assert r.returncode == 0
        time.sleep(0.3)
        assert stranger.poll() is None, "stop script killed an unrelated process"
        r = run([START, "--pid-file", str(pid), "-c", str(_cfg(tmp_path))])
        assert r.returncode == 0 and "stopping it first" not in r.stdout
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_running_copy_is_found_and_stopped(tmp_path):
    """A python process whose argv mentions file_reaper.py and whose cwd is the repo is ours."""
    if shutil.which("lsof") is None:
        pytest.skip("lsof required for cwd matching")
    fake = tmp_path / "file_reaper.py"
    fake.write_text("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, str(fake)], cwd=ROOT)
    try:
        time.sleep(0.5)
        pid = tmp_path / "r.pid"
        r = run([START, "--no-replace", "--pid-file", str(pid), "-c", str(_cfg(tmp_path))])
        assert r.returncode == 1 and "already running" in r.stderr
        r = run([STOP, str(pid)], env={"CRS_STOP_TIMEOUT": "5"})
        assert r.returncode == 0 and "Stopping" in r.stdout
        deadline = time.time() + 10
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _cfg(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("reaper: {}\n")
    return cfg
