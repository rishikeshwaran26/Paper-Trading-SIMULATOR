"""API tests for POST /patterns.

Exercises the endpoint only — the repository layer is already tested in
test_saved_patterns.py. These tests use the full Flask test client with a
throwaway in-memory-adjacent SQLite database (tmp_path fixture).
"""

import pytest

from api import create_app


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    app.config.update(TESTING=True)
    c = app.test_client()
    r = c.post("/auth/register", json={"username": "alice", "password": "supersecret1"})
    c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {r.get_json()['token']}"
    return c


# A minimal valid payload — every required field present, filters is a dict.
_VALID = {
    "name": "High Vol Reversal",
    "test_start_date": "2026-01-01",
    "test_end_date": "2026-03-31",
    "cases": 120,
    "fell_count": 82,
    "rose_count": 38,
    "fall_rate": 0.6833,
    "filters": {"direction": "up", "threshold_pct": 19.5},
}


# ---------------------------------------------------------------------------
# successful creation
# ---------------------------------------------------------------------------

def test_create_pattern_returns_201(client):
    r = client.post("/patterns", json=_VALID)
    assert r.status_code == 201


def test_create_pattern_returns_pattern_object(client):
    r = client.post("/patterns", json=_VALID)
    body = r.get_json()
    assert "pattern" in body


def test_create_pattern_returns_integer_id(client):
    r = client.post("/patterns", json=_VALID)
    pid = r.get_json()["pattern"]["id"]
    assert isinstance(pid, int)
    assert pid >= 1


def test_create_pattern_location_header(client):
    r = client.post("/patterns", json=_VALID)
    pid = r.get_json()["pattern"]["id"]
    assert r.headers.get("Location") == f"/patterns/{pid}"


def test_create_pattern_increments_id(client):
    id1 = client.post("/patterns", json=_VALID).get_json()["pattern"]["id"]
    id2 = client.post("/patterns", json={**_VALID, "name": "Pattern B"}).get_json()["pattern"]["id"]
    assert id2 == id1 + 1


def test_create_pattern_default_status_is_discovered(client):
    r = client.post("/patterns", json=_VALID)
    assert r.get_json()["pattern"]["status"] == "discovered"


def test_create_pattern_fields_round_trip(client):
    r = client.post("/patterns", json=_VALID)
    p = r.get_json()["pattern"]
    assert p["name"] == "High Vol Reversal"
    assert p["test_start_date"] == "2026-01-01"
    assert p["test_end_date"] == "2026-03-31"
    assert p["cases"] == 120
    assert p["fell_count"] == 82
    assert p["rose_count"] == 38
    assert abs(p["fall_rate"] - 0.6833) < 1e-9
    assert p["filters"] == {"direction": "up", "threshold_pct": 19.5}


def test_create_pattern_created_at_is_set(client):
    r = client.post("/patterns", json=_VALID)
    assert "T" in r.get_json()["pattern"]["created_at"]


def test_create_pattern_same_date_allowed(client):
    """A single-date test: test_start_date == test_end_date is valid."""
    payload = {**_VALID, "test_start_date": "2026-06-01", "test_end_date": "2026-06-01"}
    r = client.post("/patterns", json=payload)
    assert r.status_code == 201


def test_create_pattern_empty_filters_dict_allowed(client):
    r = client.post("/patterns", json={**_VALID, "filters": {}})
    assert r.status_code == 201
    assert r.get_json()["pattern"]["filters"] == {}


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------

def test_create_pattern_requires_auth(tmp_path):
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    r = app.test_client().post("/patterns", json=_VALID)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# required-field validation  (each missing field → 400)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "name",
    "test_start_date",
    "test_end_date",
    "cases",
    "fell_count",
    "rose_count",
    "fall_rate",
    "filters",
])
def test_missing_required_field_returns_400(client, field):
    payload = {k: v for k, v in _VALID.items() if k != field}
    r = client.post("/patterns", json=payload)
    assert r.status_code == 400
    body = r.get_json()
    assert "error" in body


# ---------------------------------------------------------------------------
# type validation
# ---------------------------------------------------------------------------

def test_name_must_be_string(client):
    r = client.post("/patterns", json={**_VALID, "name": 123})
    assert r.status_code == 400


def test_cases_must_be_integer_not_float(client):
    r = client.post("/patterns", json={**_VALID, "cases": 10.5})
    assert r.status_code == 400


def test_cases_must_not_be_bool(client):
    r = client.post("/patterns", json={**_VALID, "cases": True})
    assert r.status_code == 400


def test_fall_rate_must_be_number(client):
    r = client.post("/patterns", json={**_VALID, "fall_rate": "high"})
    assert r.status_code == 400


def test_filters_must_be_object_not_list(client):
    r = client.post("/patterns", json={**_VALID, "filters": [1, 2, 3]})
    assert r.status_code == 400


def test_filters_must_be_object_not_string(client):
    r = client.post("/patterns", json={**_VALID, "filters": "direction=up"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# semantic validation
# ---------------------------------------------------------------------------

def test_start_date_after_end_date_returns_400(client):
    payload = {**_VALID, "test_start_date": "2026-06-01", "test_end_date": "2026-01-01"}
    r = client.post("/patterns", json=payload)
    assert r.status_code == 400


def test_invalid_date_format_returns_400(client):
    r = client.post("/patterns", json={**_VALID, "test_start_date": "01-01-2026"})
    assert r.status_code == 400


def test_negative_cases_returns_400(client):
    r = client.post("/patterns", json={**_VALID, "cases": -1})
    assert r.status_code == 400


def test_fell_plus_rose_exceeds_cases_returns_400(client):
    # 80 + 50 = 130 > 120 cases
    r = client.post("/patterns", json={**_VALID, "fell_count": 80, "rose_count": 50})
    assert r.status_code == 400


def test_fall_rate_above_1_returns_400(client):
    r = client.post("/patterns", json={**_VALID, "fall_rate": 1.5})
    assert r.status_code == 400


def test_fall_rate_below_0_returns_400(client):
    r = client.post("/patterns", json={**_VALID, "fall_rate": -0.1})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# error envelope shape
# ---------------------------------------------------------------------------

def test_error_response_has_error_envelope(client):
    r = client.post("/patterns", json={})
    body = r.get_json()
    assert "error" in body
    assert "type" in body["error"]
    assert "message" in body["error"]


def test_non_json_body_returns_400(client):
    r = client.post(
        "/patterns",
        data="not json",
        content_type="text/plain",
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /patterns — list saved patterns
# ---------------------------------------------------------------------------

def test_list_patterns_requires_auth(tmp_path):
    app = create_app(data_root=str(tmp_path), start_worker=False, price_source_mode="manual")
    r = app.test_client().get("/patterns")
    assert r.status_code == 401


def test_list_patterns_empty_returns_200_and_empty_list(client):
    r = client.get("/patterns")
    assert r.status_code == 200
    body = r.get_json()
    assert "patterns" in body
    assert body["patterns"] == []


def test_list_patterns_returns_saved_patterns(client):
    client.post("/patterns", json=_VALID)
    client.post("/patterns", json={**_VALID, "name": "Second Pattern"})
    r = client.get("/patterns")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["patterns"]) == 2


def test_list_patterns_newest_first(client):
    client.post("/patterns", json={**_VALID, "name": "Older Pattern"})
    client.post("/patterns", json={**_VALID, "name": "Newer Pattern"})
    r = client.get("/patterns")
    assert r.status_code == 200
    names = [p["name"] for p in r.get_json()["patterns"]]
    assert names[0] == "Newer Pattern"
    assert names[1] == "Older Pattern"


def test_list_patterns_all_expected_fields(client):
    client.post("/patterns", json=_VALID)
    r = client.get("/patterns")
    assert r.status_code == 200
    p = r.get_json()["patterns"][0]
    assert p["name"] == _VALID["name"]
    assert p["test_start_date"] == _VALID["test_start_date"]
    assert p["test_end_date"] == _VALID["test_end_date"]
    assert p["cases"] == _VALID["cases"]
    assert p["fell_count"] == _VALID["fell_count"]
    assert p["rose_count"] == _VALID["rose_count"]
    assert p["status"] == "discovered"
    assert isinstance(p["id"], int)
    assert "created_at" in p


def test_list_patterns_filters_remain_dict(client):
    nested = {"direction": "up", "tags": ["spike", "fade"], "levels": {"min": 19}}
    client.post("/patterns", json={**_VALID, "filters": nested})
    r = client.get("/patterns")
    assert r.status_code == 200
    p = r.get_json()["patterns"][0]
    assert isinstance(p["filters"], dict)
    assert p["filters"] == nested


def test_list_patterns_fall_rate_remains_decimal(client):
    client.post("/patterns", json={**_VALID, "fall_rate": 0.6833})
    r = client.get("/patterns")
    assert r.status_code == 200
    p = r.get_json()["patterns"][0]
    assert isinstance(p["fall_rate"], float)
    assert p["fall_rate"] == 0.6833

