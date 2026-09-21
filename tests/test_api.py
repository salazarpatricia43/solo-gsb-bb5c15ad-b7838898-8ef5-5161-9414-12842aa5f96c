"""End-to-end tests covering planning, error localization and persistence."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("PLANNER_DB_PATH", tmp.name)
    monkeypatch.setenv("PLANNER_SOLVER_TIME_LIMIT", "5")
    from importlib import reload

    from app import config, db, main

    reload(config)
    settings = config.load_settings()
    main.app.state.settings = settings
    db.init_db(settings.database_path)

    with TestClient(main.app) as c:
        yield c

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


BASE_PAYLOAD = {
    "services": ["a", "b", "c", "d"],
    "dependencies": [
        {"service": "b", "depends_on": "a"},
        {"service": "c", "depends_on": "a"},
        {"service": "d", "depends_on": "b"},
        {"service": "d", "depends_on": "c"},
    ],
    "max_concurrency": 4,
}


def test_minimum_waves_with_dependencies(client):
    r = client.post("/plans", json=BASE_PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["wave_count"] == 3
    assert [w["services"] for w in body["waves"]] == [["a"], ["b", "c"], ["d"]]
    assert [w["index"] for w in body["waves"]] == [1, 2, 3]
    assert body["optimal"] is True


def test_concurrency_cap_spreads_services(client):
    payload = {"services": ["a", "b", "c", "d"], "dependencies": [], "max_concurrency": 2}
    r = client.post("/plans", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["wave_count"] == 2
    assert all(len(w["services"]) <= 2 for w in body["waves"])


def test_fault_domain_members_split_across_waves(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [],
        "max_concurrency": 3,
        "fault_domains": [{"domain": "az-1", "services": ["a", "b", "c"]}],
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["wave_count"] == 3
    placement = {s: w["index"] for w in body["waves"] for s in w["services"]}
    assert len({placement["a"], placement["b"], placement["c"]}) == 3


def test_dependency_cycle_is_localized(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "a", "depends_on": "b"},
            {"service": "b", "depends_on": "c"},
            {"service": "c", "depends_on": "a"},
        ],
        "max_concurrency": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "planning_failed"
    issue = body["issues"][0]
    assert issue["code"] == "dependency_cycle"
    assert issue["constraint"] == "dependencies"
    assert set(issue["related_services"]) == {"a", "b", "c"}
    assert issue["context"]["cycle_count"] >= 1


def test_self_dependency_is_a_cycle(client):
    payload = {
        "services": ["a"],
        "dependencies": [{"service": "a", "depends_on": "a"}],
        "max_concurrency": 1,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    assert r.json()["issues"][0]["code"] == "dependency_cycle"


def test_unknown_dependency_reference(client):
    payload = {
        "services": ["a"],
        "dependencies": [{"service": "a", "depends_on": "ghost"}],
        "max_concurrency": 1,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "unknown_dependency_target" in codes
    issue = next(i for i in r.json()["issues"] if i["code"] == "unknown_dependency_target")
    assert issue["service"] == "ghost"
    assert issue["related_services"] == ["a"]


def test_max_waves_dependency_chain_reason(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "b", "depends_on": "a"},
            {"service": "c", "depends_on": "b"},
        ],
        "max_concurrency": 3,
        "max_waves": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "dependency_chain_too_long" in codes
    issue = next(i for i in r.json()["issues"] if i["code"] == "dependency_chain_too_long")
    assert issue["context"]["required_waves"] == 3
    assert set(issue["related_services"]) == {"a", "b", "c"}


def test_max_waves_capacity_reason(client):
    payload = {
        "services": ["a", "b", "c", "d"],
        "dependencies": [],
        "max_concurrency": 1,
        "max_waves": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "concurrency_cap_too_low" in codes


def test_max_waves_fault_domain_reason(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [],
        "max_concurrency": 3,
        "fault_domains": [{"domain": "fd", "services": ["a", "b", "c"]}],
        "max_waves": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "fault_domain_anti_affinity" in codes


def test_plans_are_deterministic(client):
    responses = [client.post("/plans", json=BASE_PAYLOAD).json() for _ in range(3)]
    waves = [body["waves"] for body in responses]
    assert waves[0] == waves[1] == waves[2]


def test_list_get_delete_lifecycle(client):
    created = client.post("/plans", json=BASE_PAYLOAD).json()
    pid = created["id"]

    listing = client.get("/plans").json()
    assert listing["total"] >= 1
    assert any(item["id"] == pid for item in listing["items"])

    fetched = client.get(f"/plans/{pid}")
    assert fetched.status_code == 200
    assert fetched.json()["waves"] == created["waves"]

    missing = client.get("/plans/9999")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    deleted = client.delete(f"/plans/{pid}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    assert client.get(f"/plans/{pid}").status_code == 404
    assert client.delete(f"/plans/{pid}").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_schema_validation_error_envelope(client):
    r = client.post(
        "/plans",
        json={"services": ["a"], "dependencies": [], "max_concurrency": 0},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"
    assert body["issues"]
