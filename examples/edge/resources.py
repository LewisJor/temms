"""Enforced Linux limits and witnessed CPU contention for the edge qualification."""

import errno
import os
import select
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")
PROC = Path("/proc")
BURN = """import time
print('ready', flush=True)
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    pass
"""


def _number(value):
    number = int(value)
    if number < 0:
        raise ValueError("resource counters and limits must be nonnegative")
    return number


def _counters(name):
    pairs = [line.split() for line in (CGROUP / name).read_text().splitlines()]
    return {key: _number(value) for key, value in pairs}


def snapshot():
    """Read this private cgroup namespace; missing/unlimited limits fail closed."""
    quota, period = (CGROUP / "cpu.max").read_text().split()
    result = {
        "memory_max": _number((CGROUP / "memory.max").read_text()),
        "swap_max": _number((CGROUP / "memory.swap.max").read_text()),
        "cpu_quota": _number(quota), "cpu_period": _number(period),
        "cpu_stat": _counters("cpu.stat"),
        "memory_current": _number((CGROUP / "memory.current").read_text()),
        "memory_peak": _number((CGROUP / "memory.peak").read_text()),
        "memory_events": _counters("memory.events"),
    }
    if not {"usage_usec", "nr_throttled", "throttled_usec"} <= result["cpu_stat"].keys():
        raise ValueError("CPU usage/throttling counters are unavailable")
    if not {"oom", "oom_kill"} <= result["memory_events"].keys():
        raise ValueError("memory OOM counters are unavailable")
    return result


def validate_profile(value):
    if value["memory_max"] != 512 * 1024 * 1024 or value["swap_max"] != 0:
        raise ValueError("qualification requires 512 MiB memory and no swap")
    if not value["cpu_quota"] == value["cpu_period"] > 0:
        raise ValueError("qualification requires exactly one CPU of quota")
    events = value["memory_events"]
    if any(events.get(key) != 0 for key in ("oom", "oom_kill")) or events.get("oom_group_kill", 0):
        raise ValueError("qualification observed an OOM event")


def network_probe():
    interfaces = socket.if_nameindex()
    routes = (PROC / "net/route").read_text()
    connect_errno = None
    with socket.socket() as connection:
        connection.settimeout(2)
        try:
            connection.connect(("192.0.2.1", 443))
        except OSError as error:
            connect_errno = error.errno
    result = {"interfaces": interfaces, "routes": routes, "connect_errno": connect_errno}
    if ([name for _, name in interfaces] != ["lo"] or not routes.startswith("Iface")
            or any(line.strip() for line in routes.splitlines()[1:])
            or connect_errno != errno.ENETUNREACH):
        raise ValueError(f"external network isolation was not established: {result}")
    return result


def _cpu_seconds(pid):
    # comm may contain spaces and parentheses; fields after its final ')' start at state.
    fields = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return sum(_number(fields[index]) for index in (11, 12)) / os.sysconf("SC_CLK_TCK")


def _worker_cpu(workers):
    own_group = (PROC / "self/cgroup").read_text()
    values = []
    for worker in workers:
        if worker.poll() is not None:
            raise RuntimeError("CPU pressure worker exited before the serving block finished")
        if (PROC / str(worker.pid) / "cgroup").read_text() != own_group:
            raise RuntimeError("CPU pressure worker is outside the inference cgroup")
        values.append(_cpu_seconds(worker.pid))
    return values


def _reap(worker):
    try:
        try:
            worker.wait(timeout=2)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=2)
    finally:
        worker.stdout.close()


def _stop(workers):
    errors = []
    for worker in workers:
        try:
            if worker.poll() is None:
                worker.terminate()
        except OSError as error:
            errors.append(error)
    for worker in workers:
        try:
            _reap(worker)
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(error)
    if errors:
        raise RuntimeError("CPU pressure workers could not be reaped") from errors[0]


def _finish_pressure(state, workers):
    state["after"] = snapshot()
    final_cpu = _worker_cpu(workers)
    state["worker_cpu_after_seconds"] = final_cpu
    state["worker_cpu_seconds"] = [after - before for before, after in zip(
        state["worker_cpu_before_seconds"], final_cpu, strict=True,
    )]
    validate_profile(state["after"])
    if not all(value > 0 for value in state["worker_cpu_seconds"]):
        raise RuntimeError("CPU pressure worker did no CPU work during the serving block")
    before, after = state["before"]["cpu_stat"], state["after"]["cpu_stat"]
    if not all(after[key] > before[key]
               for key in ("usage_usec", "nr_throttled", "throttled_usec")):
        raise RuntimeError("no CPU work/throttling was observed during the serving block")


@contextmanager
def cpu_pressure():
    """Witness two CPU peers sharing the capped inference cgroup during the block."""
    workers = []
    failure = None
    try:
        for _ in range(2):
            workers.append(subprocess.Popen(
                [sys.executable, "-c", BURN], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            ))
        for worker in workers:
            ready, _, _ = select.select([worker.stdout], [], [], 5)
            if not ready or worker.stdout.readline() != b"ready\n":
                raise RuntimeError("CPU pressure worker did not become ready")
        initial_cpu = _worker_cpu(workers)
        state = {"before": snapshot(), "pids": [worker.pid for worker in workers],
                 "worker_cpu_before_seconds": initial_cpu}
        validate_profile(state["before"])
        yield state
        _finish_pressure(state, workers)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            _stop(workers)
        except Exception as error:
            if failure is None:
                raise
            failure.add_note(f"CPU pressure cleanup also failed: {error}")
