"""DecisionChain as a unit, decoupled from SlotManager (#16/review extraction).

These exercise the chain against a bare Database, proving the tamper-evident
audit log stands on its own — the reason it was extracted from SlotManager.
"""

from __future__ import annotations

from temms.core.database import Database
from temms.slots.decision_chain import DECISION_CHAIN_GENESIS, DecisionChain


class _Db(Database):
    """A minimal Database whose only table is the decision chain's."""

    def _init_tables(self) -> None:  # noqa: D401 - test double
        pass


def _chain(tmp_path) -> DecisionChain:
    chain = DecisionChain(_Db(tmp_path / "chain.db"))
    chain.ensure_schema()
    return chain


def _decision(slot="vision", to="daylight", from_model=None, i=0):
    return {
        "slot": slot,
        "from_model": from_model,
        "to_model": to,
        "trigger_type": "policy",
        "trigger_detail": f"rule-{i}",
        "conditions_snapshot": {"i": i},
        "audit_metadata": {"model_id": to},
        "created_at": f"2026-01-01T00:00:0{i}",
    }


def test_first_entry_links_to_genesis(tmp_path):
    chain = _chain(tmp_path)
    chain.append(_decision(i=1))
    chain._db.conn.commit()

    exported = chain.export()
    assert len(exported) == 1
    assert exported[0]["prev_hash"] == DECISION_CHAIN_GENESIS
    assert chain.head() == exported[0]["entry_hash"]


def test_entries_link_head_to_tail(tmp_path):
    chain = _chain(tmp_path)
    for i in range(3):
        chain.append(_decision(to=f"m{i}", i=i))
    chain._db.conn.commit()

    exported = chain.export()
    assert [e["prev_hash"] for e in exported][1:] == [e["entry_hash"] for e in exported][:-1]
    assert chain.count() == 3
    assert chain.verify()["valid"] is True


def test_verify_detects_content_mutation(tmp_path):
    chain = _chain(tmp_path)
    chain.append(_decision(to="daylight", i=1))
    chain.append(_decision(to="lowlight", i=2))
    chain._db.conn.commit()

    # Tamper with a past decision's content without re-hashing.
    chain._db.execute("UPDATE slot_decisions SET to_model = 'EVIL' WHERE id = 1")
    chain._db.conn.commit()

    result = chain.verify()
    assert result["valid"] is False
    assert result["broken_at"] == 0
    assert "does not match its hash" in result["reason"]


def test_verify_detects_deletion(tmp_path):
    chain = _chain(tmp_path)
    for i in range(3):
        chain.append(_decision(to=f"m{i}", i=i))
    chain._db.conn.commit()

    # Remove the middle entry: the link from the third to the (now gone) second breaks.
    chain._db.execute("DELETE FROM slot_decisions WHERE id = 2")
    chain._db.conn.commit()

    assert chain.verify()["valid"] is False


def test_backfill_links_legacy_rows(tmp_path):
    chain = _chain(tmp_path)
    # Insert a row the old way — no chain columns.
    chain._db.execute(
        "INSERT INTO slot_decisions (slot, to_model, trigger_type, trigger_detail, "
        "conditions_snapshot, audit_metadata, created_at) "
        "VALUES ('vision', 'daylight', 'startup', 'seed', '{}', '{}', '2026-01-01T00:00:00')"
    )
    chain._db.conn.commit()

    assert chain.export()[0]["entry_hash"] is None  # unlinked before backfill
    chain.backfill()

    exported = chain.export()
    assert exported[0]["entry_hash"] is not None
    assert exported[0]["prev_hash"] == DECISION_CHAIN_GENESIS
    assert chain.verify()["valid"] is True


def test_entry_hash_is_pure_and_order_sensitive():
    a = DecisionChain.entry_hash({"slot": "v", "to_model": "x"}, DECISION_CHAIN_GENESIS)
    b = DecisionChain.entry_hash({"slot": "v", "to_model": "x"}, DECISION_CHAIN_GENESIS)
    c = DecisionChain.entry_hash({"slot": "v", "to_model": "y"}, DECISION_CHAIN_GENESIS)
    assert a == b  # deterministic
    assert a != c  # content-sensitive
