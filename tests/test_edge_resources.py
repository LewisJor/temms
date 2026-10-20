import asyncio
import errno
import io
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from examples.edge import resources


@pytest.fixture
def system(tmp_path, monkeypatch):
    group, proc = tmp_path / "cgroup", tmp_path / "proc"
    group.mkdir()
    (proc / "self").mkdir(parents=True)
    (proc / "net").mkdir()
    (proc / "self/cgroup").write_text("0::/\n")
    (proc / "net/route").write_text("Iface\tDestination\tGateway\n")
    files = {
        "cpu.max": "100000 100000", "memory.max": "536870912", "memory.swap.max": "0",
        "cpu.stat": "usage_usec 100\nnr_throttled 1\nthrottled_usec 10\n",
        "memory.current": "104857600", "memory.peak": "209715200",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n",
    }
    for name, value in files.items():
        (group / name).write_text(value)
    monkeypatch.setattr(resources, "CGROUP", group)
    monkeypatch.setattr(resources, "PROC", proc)
    monkeypatch.setattr(resources.os, "sysconf", lambda name: 100)
    return group, proc


def _stat(proc, pid, user, kernel):
    # Include spaces and ')' inside comm, rather than depending on split-by-space parsing.
    fields = ["R", *(["0"] * 10), str(user), str(kernel), "0"]
    (proc / str(pid) / "stat").write_text(f"{pid} (cpu (pressure) peer) " + " ".join(fields))


@pytest.mark.parametrize("name,value", [
    ("cpu.max", "max 100000"), ("memory.max", "max"), ("memory.swap.max", "max"),
    ("memory.current", "-1"), ("cpu.stat", "usage_usec 100\n"),
    ("memory.events", "oom 0\n"), ("memory.peak", None),
])
def test_snapshot_rejects_unavailable_unlimited_and_invalid_accounting(system, name, value):
    path = system[0] / name
    if value is None:
        path.unlink()
    else:
        path.write_text(value)
    with pytest.raises((ValueError, OSError)):
        resources.snapshot()


@pytest.mark.parametrize("field,value", [
    ("memory_max", 1024 * 1024 * 1024), ("swap_max", 512 * 1024 * 1024),
    ("cpu_quota", 50000), ("cpu_period", 0), ("oom", 1), ("oom_kill", 1),
    ("oom_group_kill", 1),
])
def test_profile_rejects_different_limits_and_oom_events(system, field, value):
    snapshot = resources.snapshot()
    resources.validate_profile(snapshot)
    target = snapshot["memory_events"] if field.startswith("oom") else snapshot
    target[field] = value
    with pytest.raises(ValueError):
        resources.validate_profile(snapshot)


@pytest.mark.parametrize("interface,route,error", [
    ("lo", "", errno.ENETUNREACH), ("eth0", "", errno.ENETUNREACH),
    ("lo", "lo\t00000000\t00000000\n", errno.ENETUNREACH),
    ("lo", "", errno.ECONNREFUSED), ("lo", "", None),
])
def test_network_probe_requires_loopback_no_routes_and_unreachable_ip(
    system, monkeypatch, interface, route, error,
):
    (system[1] / "net/route").write_text("Iface\tDestination\tGateway\n" + route)
    monkeypatch.setattr(resources.socket, "if_nameindex", lambda: [(1, interface)])

    def connect(address):
        assert address == ("192.0.2.1", 443)
        if error is not None:
            raise OSError(error, "probe result")

    connection = SimpleNamespace(settimeout=lambda timeout: None, connect=connect)
    monkeypatch.setattr(resources.socket, "socket", lambda: nullcontext(connection))
    if interface == "lo" and not route and error == errno.ENETUNREACH:
        assert resources.network_probe()["connect_errno"] == errno.ENETUNREACH
    else:
        with pytest.raises(ValueError, match="isolation was not established"):
            resources.network_probe()


class _Worker:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None
        self.stdout = io.BytesIO(b"ready\n")
        self.stubborn, self.killed = False, False
        self.waits = []

    def poll(self):
        return self.returncode

    def terminate(self):
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.killed, self.returncode = True, -9

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("CPU peer", timeout)
        return self.returncode


@pytest.fixture
def workers(system, monkeypatch):
    peers = [_Worker(101), _Worker(102)]
    for peer in peers:
        (system[1] / str(peer.pid)).mkdir()
        (system[1] / str(peer.pid) / "cgroup").write_text("0::/\n")
        _stat(system[1], peer.pid, 100, 20)
    pending = iter(peers)
    monkeypatch.setattr(resources.subprocess, "Popen", lambda *args, **kwargs: next(pending))
    monkeypatch.setattr(resources.select, "select", lambda readers, *args: (readers, [], []))
    return peers


def _advance(system, workers):
    (system[0] / "cpu.stat").write_text("usage_usec 200\nnr_throttled 3\nthrottled_usec 40\n")
    for worker in workers:
        _stat(system[1], worker.pid, 300, 50)


def _assert_reaped(workers):
    assert all(worker.returncode is not None and worker.stdout.closed for worker in workers)
    assert all(worker.waits and all(timeout == 2 for timeout in worker.waits) for worker in workers)


def test_pressure_records_cpu_deltas_during_the_block_and_reaps_both_workers(system, workers):
    with resources.cpu_pressure() as pressure:
        assert pressure["pids"] == [101, 102]
        assert pressure["worker_cpu_before_seconds"] == [1.2, 1.2]
        assert "after" not in pressure
        _advance(system, workers)
    assert pressure["worker_cpu_after_seconds"] == [3.5, 3.5]
    assert pressure["worker_cpu_seconds"] == pytest.approx([2.3, 2.3])
    assert pressure["after"]["cpu_stat"]["nr_throttled"] == 3
    _assert_reaped(workers)


@pytest.mark.parametrize("failure", ["no-work", "exited", "other-group", "no-throttle", "rollback"])
def test_pressure_rejects_missing_or_invalid_phase_witnesses(system, workers, failure):
    mutations = {
        "no-work": lambda: _stat(system[1], 101, 100, 20),
        "exited": lambda: setattr(workers[0], "returncode", 1),
        "other-group": lambda: (system[1] / "101/cgroup").write_text("0::/different\n"),
        "no-throttle": lambda: (system[0] / "cpu.stat").write_text(
            "usage_usec 200\nnr_throttled 1\nthrottled_usec 10\n"),
        "rollback": lambda: (system[0] / "cpu.stat").write_text(
            "usage_usec 50\nnr_throttled 3\nthrottled_usec 40\n"),
    }
    with pytest.raises(RuntimeError), resources.cpu_pressure():
        _advance(system, workers)
        mutations[failure]()
    _assert_reaped(workers)


def test_pressure_requires_ready_workers_before_entering_block(system, workers, monkeypatch):
    monkeypatch.setattr(resources.select, "select", lambda *args: ([], [], []))
    with pytest.raises(RuntimeError, match="did not become ready"), resources.cpu_pressure():
        pytest.fail("serving block ran without a CPU peer readiness witness")
    _assert_reaped(workers)


@pytest.mark.parametrize("failure", [RuntimeError("inference failed"), asyncio.CancelledError()])
def test_pressure_preserves_body_failure_and_kills_a_stubborn_peer(system, workers, failure):
    workers[0].stubborn = True
    with pytest.raises(type(failure)) as caught, resources.cpu_pressure():
        raise failure
    assert caught.value is failure and workers[0].killed
    assert workers[0].waits == [2, 2]
    _assert_reaped(workers)
