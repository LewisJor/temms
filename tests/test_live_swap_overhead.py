import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from temms import InferenceResult, ModelRef


@pytest.fixture
def overhead():
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    from examples.live_swap import overhead

    return overhead


@pytest.fixture
def probe(overhead, tmp_path, monkeypatch):
    sessions, hooks = [], {}
    input_value = overhead.np.array([1.0])
    names = ("small", "large")
    manifest = {
        "files": [{"name": name + ".onnx", "sha256": name * 8} for name in names],
        "models": [{"id": name, "file": name + ".onnx"} for name in names],
    }
    for name in names:
        (tmp_path / (name + ".onnx")).write_bytes(b"controlled session fixture")

    class Session:
        def __init__(self, name):
            self.name, self.calls = name, 0
            self.scores = overhead.np.zeros((1, 1000), dtype=overhead.np.float32)
            self.scores[0, names.index(name)] = 1

        def run(self, output_names, feed):
            assert output_names is None and list(feed) == ["data"]
            assert feed["data"] is input_value
            self.calls += 1
            hooks.get("run", lambda session: None)(self)
            return hooks.get("output", lambda session: [session.scores])(self)

    def factory(path, providers):
        assert providers == ("CPUExecutionProvider",)
        session = Session(Path(path).stem)
        sessions.append(session)
        return session

    monkeypatch.setattr(overhead, "cpu_session", factory)
    return SimpleNamespace(module=overhead, directory=tmp_path, manifest=manifest,
                           feed={"data": input_value},
                           sessions=sessions, hooks=hooks)


async def one_model(probe, emit):
    manifest = {**probe.manifest, "models": probe.manifest["models"][:1]}
    await probe.module.run(probe.directory, manifest, probe.feed, 24, emit)


@pytest.mark.asyncio
async def test_all_orders_balance_positions_and_preserve_negative_layer_deltas(probe, monkeypatch):
    overhead = probe.module
    durations = {"native": 100, "threaded": 70, "adapter": 40, "temms": 10}
    stamps, now = [], 0
    for order in overhead.ORDERS:
        for path in order:
            stamps.extend((now, now + durations[path]))
            now += durations[path] + 7
    timestamps = iter(stamps)
    monkeypatch.setattr(overhead, "time", SimpleNamespace(perf_counter_ns=lambda: next(timestamps)))
    records = []
    await one_model(probe, records.append)
    reference, *blocks, summary = records
    assert reference["event"] == "reference" and len(blocks) == 24
    orders = [tuple(row["order"]) for row in blocks]
    assert len(set(orders)) == 24
    for position in range(4):
        assert Counter(order[position] for order in orders) == dict.fromkeys(overhead.PATHS, 6)
    for left in overhead.PATHS:
        for right in set(overhead.PATHS) - {left}:
            assert sum(order.index(left) < order.index(right) for order in orders) == 12
    expected = {"threaded-native": -30, "adapter-threaded": -30, "temms-adapter": -30}
    for index, row in enumerate(blocks):
        assert row["block"] == index and row["ns"] == durations and row["delta_ns"] == expected
        assert row["digest"] == reference["digest"] == "sha256:" + "small" * 8
        for path, output in row["outputs"].items():
            assert output["sha256"] == row["reference_sha256"] == reference["sha256"]
            identity = {"id": row["model"], "digest": row["digest"]}
            assert output["serving"] == (identity if path in ("adapter", "temms") else None)
    assert summary["blocks"] == 24 and summary["event"] == "summary"
    assert summary["delta_ns"] == {name: dict(mean=-30, p50=-30, p95=-30) for name in expected}
    assert summary["ns"] == {name: dict.fromkeys(("mean", "p50", "p95"), ns)
                             for name, ns in durations.items()}
    assert list(timestamps) == []  # Untimed warmups and validation never read the timing clock.


@pytest.mark.asyncio
async def test_each_path_uses_the_single_captured_session_and_all_warmups_are_counted(
    probe, monkeypatch,
):
    overhead, calls = probe.module, []
    invoke = overhead.invoke

    async def observed(path, session, runtime, temms, feed, *, timed=False):
        calls.append((path, session, timed))
        return await invoke(path, session, runtime, temms, feed, timed=timed)

    monkeypatch.setattr(overhead, "invoke", observed)
    records = []
    await one_model(probe, records.append)
    session, = probe.sessions
    assert {id(item[1]) for item in calls} == {id(session)}
    assert Counter((path, timed) for path, _, timed in calls) == {
        (path, timed): count for path in overhead.PATHS for timed, count in ((False, 8), (True, 24))
    }
    assert session.calls == 1 + 1 + 4 * 8 + 4 * 24
    reference = records[0]
    assert reference["shape"] == [1, 1000] and reference["dtype"] == session.scores.dtype.str
    assert reference["scores"] == session.scores.tolist()
    assert reference["sha256"] == hashlib.sha256(session.scores.tobytes()).hexdigest()


def test_reference_copies_and_freezes_returned_buffer(overhead):
    original = overhead.np.ones((1, 1000), dtype=overhead.np.float32)
    copied = overhead.freeze([original])
    original[0, 1] = 9
    assert copied[0, 1] == 1 and not copied.flags.writeable
    with pytest.raises(ValueError):
        copied[0, 0] = 3


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "empty", "empty-vector", "count", "shape", "nan", "inf", "dtype", "value",
])
@pytest.mark.parametrize("call", [2, 35], ids=["reference", "first-measured-block"])
async def test_invalid_reference_or_measured_output_never_emits_a_block(probe, bad, call):
    np = probe.module.np

    def output(session):
        if session.calls != call:
            return [session.scores]
        if bad == "empty":
            return []
        if bad == "empty-vector":
            return [np.empty((1, 0))]
        if bad == "count":
            return [session.scores, session.scores]
        if bad == "shape":
            return [session.scores.reshape(1000)]
        if bad == "dtype":
            return [session.scores.astype(np.float64)]
        scores = session.scores.copy()
        scores[0, 0] = {"nan": np.nan, "inf": np.inf, "value": 2}[bad]
        return [scores]

    probe.hooks["output"] = output
    records = []
    # A finite reference with a different dtype/value is permitted initially;
    # its disagreement with the first following output must still fail.
    with pytest.raises(ValueError):
        await one_model(probe, records.append)
    assert not any(row["event"] in ("block", "summary") for row in records)
    expected_calls = 3 if call == 2 and bad in ("dtype", "value") else call
    assert probe.sessions[0].calls == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path_index", range(4), ids=["native", "threaded", "adapter", "temms"])
async def test_last_untimed_call_of_every_path_is_validated(probe, path_index):
    fail_at = 2 + 7 * 4 + path_index + 1

    def output(session):
        if session.calls == fail_at:
            session.scores[0, 0] += 1  # Reused buffer must not mutate the copied reference.
        return [session.scores]

    probe.hooks["output"] = output
    records = []
    with pytest.raises(ValueError, match="frozen reference"):
        await one_model(probe, records.append)
    assert records == [] and probe.sessions[0].calls == fail_at


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["adapter", "temms"])
@pytest.mark.parametrize("field", ["id", "digest"])
async def test_wrong_full_identity_prevents_measured_block(probe, monkeypatch, path, field):
    cls = probe.module.OnnxRuntime if path == "adapter" else probe.module.TEMMS
    infer, calls = cls.infer, 0

    async def wrong(self, slot, feed):
        nonlocal calls
        result = await infer(self, slot, feed)
        calls += 1
        if calls == (17 if path == "adapter" else 9):
            values = {"id": result.model.id, "digest": result.model.digest, field: "wrong"}
            return InferenceResult(ModelRef(**values), result.output)
        return result

    monkeypatch.setattr(cls, "infer", wrong)
    records = []
    with pytest.raises(ValueError, match="attribution"):
        await one_model(probe, records.append)
    assert [row["event"] for row in records] == ["reference"]


class MeteredFeed(Mapping):
    def __init__(self, values, clock):
        self.values, self.clock = values, clock

    def __iter__(self):
        self.clock.ns += 2
        return iter(self.values)

    def __getitem__(self, key):
        self.clock.ns += 3
        return self.values[key]

    def __len__(self):
        return len(self.values)


@pytest.mark.asyncio
async def test_timers_include_mapping_dispatch_and_wrapping_but_exclude_checks_and_emission(
    probe, monkeypatch,
):
    overhead, clock = probe.module, SimpleNamespace(ns=0)
    probe.feed = MeteredFeed(probe.feed, clock)

    def charge(amount):
        clock.ns += amount

    probe.hooks["run"] = lambda session: charge(7)
    threaded, adapter = overhead.asyncio.to_thread, overhead.OnnxRuntime.infer
    temms = overhead.TEMMS.infer

    async def dispatch(function, *args, **kwargs):
        charge(11)
        return await threaded(function, *args, **kwargs)

    async def adapt(self, *args):
        result = await adapter(self, *args)
        charge(13)
        return result

    async def wrap(self, *args):
        charge(17)
        return await temms(self, *args)

    check = overhead.check

    def checked(*args):
        charge(5000)
        return check(*args)

    monkeypatch.setattr(overhead, "time", SimpleNamespace(perf_counter_ns=lambda: clock.ns))
    monkeypatch.setattr(overhead.asyncio, "to_thread", dispatch)
    monkeypatch.setattr(overhead.OnnxRuntime, "infer", adapt)
    monkeypatch.setattr(overhead.TEMMS, "infer", wrap)
    monkeypatch.setattr(overhead, "check", checked)
    records = []

    def emit(record):
        charge(10000)
        records.append(record)

    await one_model(probe, emit)
    assert all(row["ns"] == {"native": 12, "threaded": 23, "adapter": 36, "temms": 53}
               for row in records if row["event"] == "block")


@pytest.mark.parametrize("blocks", ["0", "-24", "1", "25", "1.5", "not-an-integer"])
def test_invalid_counts_fail_before_assets_sessions_or_record_creation(overhead, tmp_path,
                                                                     monkeypatch, blocks):
    def forbidden(*args):
        pytest.fail("invalid block count reached expensive work")

    monkeypatch.setattr(overhead, "verify_assets", forbidden)
    monkeypatch.setattr(overhead, "cpu_session", forbidden)
    records = tmp_path / "records.jsonl"
    monkeypatch.setattr(sys, "argv", ["overhead.py", "--blocks", blocks, "--records", str(records)])
    with pytest.raises(SystemExit) as error:
        overhead.main()
    assert error.value.code == 2 and not records.exists()


@pytest.mark.parametrize("reverse,blocks", [(False, "24"), (True, "24"), (False, None)])
def test_cli_records_protocol_and_model_order(probe, monkeypatch, reverse, blocks):
    overhead = probe.module
    monkeypatch.setattr(overhead, "verify_assets", lambda directory: probe.manifest)
    monkeypatch.setattr(overhead, "preprocess", lambda path: probe.feed)
    monkeypatch.setattr(overhead, "version", lambda name: "test-" + name)
    output = probe.directory / "records.jsonl"
    arguments = ["overhead.py", "--assets", str(probe.directory), "--records", str(output)]
    monkeypatch.setattr(sys, "argv", arguments + (["--reverse-models"] if reverse else [])
                        + (["--blocks", blocks] if blocks else []))
    overhead.main()
    records = [json.loads(line) for line in output.read_text().splitlines()]
    header = records[0]
    names = ["large", "small"] if reverse else ["small", "large"]
    count = int(blocks) if blocks else 240
    assert header["schema"] == "temms-overhead" and header["schema_version"] == 2
    assert header["model_order"] == names == [session.name for session in probe.sessions]
    assert header["manifest"] == probe.manifest and header["versions"]["temms"] == "test-temms"
    assert header["blocks_per_model"] == count and len(header["orders"]) == 24
    assert header["clock"]["name"] == "perf_counter_ns" and header["clock"]["monotonic"] is True
    assert header["warmup"] == {"activation_calls_per_model": 1, "activation_output_checked": False,
                                "reference_calls_per_model": 1, "checked_calls_per_path": 8,
                                "total_untimed_calls_per_model": 34}
    assert header["intra_op_threads"] == header["inter_op_threads"] == 1
    assert all(session.calls == 34 + 4 * count for session in probe.sessions)
    assert [row["model"] for row in records if row["event"] == "summary"] == names
    assert not probe.feed["data"].flags.writeable
