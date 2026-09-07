"""Repository-level tests for SavedPatternRepository.

Tests only the database layer (engine/repository.py + engine/db.py).
No API endpoint, no frontend, no backtest logic — just the INSERT and SELECT
behaviour of the saved_patterns table.
"""

import pytest

from engine.db import init_db
from engine.repository import SavedPatternRepository


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_FILTERS = {
    "direction": "up",
    "threshold_pct": 19.5,
    "min_vol_ratio": 2.0,
    "exclude_frozen": True,
}

_DEFAULTS = dict(
    name="High Vol Reversal",
    test_start_date="2026-01-01",
    test_end_date="2026-03-31",
    cases=120,
    fell_count=82,
    rose_count=38,
    fall_rate=0.6833,
    filters=_FILTERS,
)


def _repo(tmp_path) -> SavedPatternRepository:
    db = str(tmp_path / "test.db")
    init_db(db)
    return SavedPatternRepository(db)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_returns_integer_id(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.create(**_DEFAULTS)
    assert isinstance(pid, int)
    assert pid >= 1


def test_create_second_pattern_gets_incremented_id(tmp_path):
    repo = _repo(tmp_path)
    id1 = repo.create(**_DEFAULTS)
    id2 = repo.create(**{**_DEFAULTS, "name": "Second Pattern"})
    assert id2 == id1 + 1


# ---------------------------------------------------------------------------
# get — round-trip fidelity
# ---------------------------------------------------------------------------

def test_get_returns_correct_fields(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.create(**_DEFAULTS)
    p = repo.get(pid)

    assert p is not None
    assert p["id"] == pid
    assert p["name"] == "High Vol Reversal"
    assert p["test_start_date"] == "2026-01-01"
    assert p["test_end_date"] == "2026-03-31"
    assert p["cases"] == 120
    assert p["fell_count"] == 82
    assert p["rose_count"] == 38
    assert p["fall_rate"] == pytest.approx(0.6833)
    assert p["status"] == "discovered"   # default


def test_get_deserialises_filters_back_to_dict(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.create(**_DEFAULTS)
    p = repo.get(pid)
    assert p["filters"] == _FILTERS


def test_get_nonexistent_returns_none(tmp_path):
    repo = _repo(tmp_path)
    assert repo.get(9999) is None


def test_created_at_is_set_automatically(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.create(**_DEFAULTS)
    p = repo.get(pid)
    # ISO-8601 UTC timestamp — must be non-empty and contain a 'T'
    assert p["created_at"]
    assert "T" in p["created_at"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_default_status_is_discovered(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.create(**_DEFAULTS)
    assert repo.get(pid)["status"] == "discovered"


def test_explicit_status_is_stored(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.create(**{**_DEFAULTS, "status": "confirmed"})
    assert repo.get(pid)["status"] == "confirmed"


# ---------------------------------------------------------------------------
# filters — edge cases
# ---------------------------------------------------------------------------

def test_empty_filters_dict_is_stored_and_retrieved(tmp_path):
    repo = _repo(tmp_path)
    pid = repo.create(**{**_DEFAULTS, "filters": {}})
    assert repo.get(pid)["filters"] == {}


def test_nested_filters_survive_round_trip(tmp_path):
    nested = {"range": {"low": 19.0, "high": 20.0}, "tags": ["swing", "large-cap"]}
    repo = _repo(tmp_path)
    pid = repo.create(**{**_DEFAULTS, "filters": nested})
    assert repo.get(pid)["filters"] == nested


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------

def test_list_all_returns_all_patterns(tmp_path):
    repo = _repo(tmp_path)
    repo.create(**_DEFAULTS)
    repo.create(**{**_DEFAULTS, "name": "Pattern B"})
    repo.create(**{**_DEFAULTS, "name": "Pattern C"})
    assert len(repo.list_all()) == 3


def test_list_all_empty_returns_empty_list(tmp_path):
    repo = _repo(tmp_path)
    assert repo.list_all() == []


def test_list_all_newest_first(tmp_path):
    """list_all() orders by created_at DESC — newest pattern comes first."""
    import time
    repo = _repo(tmp_path)
    repo.create(**{**_DEFAULTS, "name": "First"})
    time.sleep(0.01)   # ensure distinct timestamps
    repo.create(**{**_DEFAULTS, "name": "Second"})
    names = [p["name"] for p in repo.list_all()]
    assert names[0] == "Second"
    assert names[1] == "First"


def test_list_all_limit_respected(tmp_path):
    repo = _repo(tmp_path)
    for i in range(5):
        repo.create(**{**_DEFAULTS, "name": f"Pattern {i}"})
    assert len(repo.list_all(limit=3)) == 3
