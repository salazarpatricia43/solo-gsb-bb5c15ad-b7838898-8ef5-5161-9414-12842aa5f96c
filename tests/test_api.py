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


# ---------------------------------------------------------------------------
# POST /plans/{id}/simulate — constraint-change simulation (never persisted)
# ---------------------------------------------------------------------------


def _create(client, payload):
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


SIM_BASE = {
    "services": ["a", "b", "c", "d"],
    "dependencies": [],
    "max_concurrency": 2,
}


def test_simulate_missing_plan_is_404(client):
    r = client.post("/plans/9999/simulate", json={})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_simulate_noop_reproduces_stored_plan(client):
    plan = _create(client, SIM_BASE)
    pid = plan["id"]
    r = client.post(f"/plans/{pid}/simulate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [w["services"] for w in body["waves"]] == [
        w["services"] for w in plan["waves"]
    ]
    assert body["wave_count"] == plan["wave_count"]
    assert body["original_wave_count"] == plan["wave_count"]
    assert body["waves_delta"] == 0
    assert body["waves_added"] == 0
    assert body["waves_removed"] == 0
    assert body["changed_services"] == []
    assert body["changed_service_count"] == 0
    assert body["affected_services"] == []
    assert body["max_concurrency"] == 2
    assert body["optimal"] is True


def test_simulate_does_not_persist(client):
    plan = _create(client, SIM_BASE)
    pid = plan["id"]
    r = client.post(f"/plans/{pid}/simulate", json={"max_concurrency": 1})
    assert r.status_code == 200
    assert r.json()["wave_count"] == 4
    # Stored plan is untouched.
    stored = client.get(f"/plans/{pid}").json()
    assert stored["wave_count"] == plan["wave_count"]
    assert stored["max_concurrency"] == 2


def test_simulate_concurrency_cap_counts_added_and_removed_waves(client):
    plan = _create(client, SIM_BASE)  # 2 waves of 2
    pid = plan["id"]

    tighter = client.post(f"/plans/{pid}/simulate", json={"max_concurrency": 1}).json()
    assert tighter["wave_count"] == 4
    assert tighter["waves_delta"] == 2
    assert tighter["waves_added"] == 2
    assert tighter["waves_removed"] == 0
    assert all(len(w["services"]) <= 1 for w in tighter["waves"])
    # A cap change names no services as directly constrained.
    assert tighter["affected_services"] == []

    looser = client.post(f"/plans/{pid}/simulate", json={"max_concurrency": 4}).json()
    assert looser["wave_count"] == 1
    assert looser["waves_delta"] == -1
    assert looser["waves_added"] == 0
    assert looser["waves_removed"] == 1


def test_simulate_added_dependency_replans_and_reports_changed_services(client):
    # Base canonical placement: wave1 [a,b], wave2 [c,d], cap 2.
    plan = _create(client, SIM_BASE)
    pid = plan["id"]
    r = client.post(
        f"/plans/{pid}/simulate",
        json={"add_dependencies": [{"service": "d", "depends_on": "c"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Minimum stays 2 waves; c moves to wave 1, d stays wave 2.
    assert body["wave_count"] == 2
    placement = {s: w["index"] for w in body["waves"] for s in w["services"]}
    assert placement["c"] < placement["d"]
    changed = {x["service"]: (x["original_wave"], x["new_wave"]) for x in body["changed_services"]}
    assert changed["c"] == (2, 1)
    assert "d" not in changed
    # Both endpoints of the violated new edge are directly affected.
    assert body["affected_services"] == ["c", "d"]


def test_simulate_minimizes_moved_services_among_optimum_plans(client):
    # cap 2 => [a,b] | [c,d]. Adding a->d forces a later than d. In 2 waves
    # both a and d must move; the solver must pick a 2-wave plan (never a
    # 3-wave plan that might move fewer services).
    plan = _create(client, SIM_BASE)
    pid = plan["id"]
    body = client.post(
        f"/plans/{pid}/simulate",
        json={"add_dependencies": [{"service": "a", "depends_on": "d"}]},
    ).json()
    assert body["wave_count"] == 2
    assert body["changed_service_count"] == 2
    moved = {x["service"] for x in body["changed_services"]}
    assert moved == {"a", "d"}


def test_simulate_satisfied_added_edge_has_no_effect(client):
    plan = _create(client, SIM_BASE)  # a in wave 1, d in wave 2
    pid = plan["id"]
    body = client.post(
        f"/plans/{pid}/simulate",
        json={"add_dependencies": [{"service": "d", "depends_on": "a"}]},
    ).json()
    assert body["wave_count"] == 2
    assert body["changed_service_count"] == 0
    assert body["affected_services"] == []


def test_simulate_remove_dependencies_shrinks_waves(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "b", "depends_on": "a"},
            {"service": "c", "depends_on": "b"},
        ],
        "max_concurrency": 3,
    }
    plan = _create(client, payload)
    assert plan["wave_count"] == 3
    body = client.post(
        f"/plans/{plan['id']}/simulate",
        json={
            "remove_dependencies": [
                {"service": "b", "depends_on": "a"},
                {"service": "c", "depends_on": "b"},
            ]
        },
    ).json()
    assert body["wave_count"] == 1
    assert body["waves_removed"] == 2
    assert body["waves_added"] == 0
    assert body["changed_service_count"] == 2
    # Removing constraints never directly violates anything in the old plan.
    assert body["affected_services"] == []


def test_simulate_fault_domain_add_update_remove(client):
    payload = {
        "services": ["a", "b", "c", "d"],
        "dependencies": [],
        "max_concurrency": 4,
        "fault_domains": [
            {"domain": "z1", "services": ["a", "b"]},
            {"domain": "z2", "services": ["c"]},
        ],
    }
    plan = _create(client, payload)
    pid = plan["id"]
    # Base: [a] | [b,c,d]. Updating z1 to [a,b,c] splits b and c.
    updated = client.post(
        f"/plans/{pid}/simulate",
        json={"update_fault_domains": [{"domain": "z1", "services": ["a", "b", "c"]}]},
    ).json()
    assert updated["wave_count"] == 3
    assert set(updated["affected_services"]) == {"b", "c"}

    # Removing z1 collapses everything back into one wave.
    removed = client.post(
        f"/plans/{pid}/simulate",
        json={"remove_fault_domains": ["z1"]},
    ).json()
    assert removed["wave_count"] == 1
    assert removed["affected_services"] == []

    # Adding a fresh domain that splits the original wave.
    added = client.post(
        f"/plans/{pid}/simulate",
        json={"add_fault_domains": [{"domain": "z3", "services": ["c", "d"]}]},
    ).json()
    assert added["wave_count"] == 2
    assert set(added["affected_services"]) == {"c", "d"}


def test_simulate_is_deterministic(client):
    plan = _create(client, SIM_BASE)
    pid = plan["id"]
    patch = {"add_dependencies": [{"service": "a", "depends_on": "d"}]}
    responses = [
        client.post(f"/plans/{pid}/simulate", json=patch).json() for _ in range(3)
    ]
    waves = [b["waves"] for b in responses]
    changed = [b["changed_services"] for b in responses]
    assert waves[0] == waves[1] == waves[2]
    assert changed[0] == changed[1] == changed[2]


def test_simulate_added_edge_creating_cycle_is_localized(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "b", "depends_on": "a"},
            {"service": "c", "depends_on": "b"},
        ],
        "max_concurrency": 3,
    }
    plan = _create(client, payload)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_dependencies": [{"service": "a", "depends_on": "c"}]},
    )
    assert r.status_code == 422
    body = r.json()
    issue = body["issues"][0]
    assert issue["code"] == "dependency_cycle"
    assert issue["constraint"] == "add_dependencies"
    assert ["a", "c"] in issue["context"]["added_edges_in_cycle"]


def test_simulate_patch_references_unknown_service(client):
    plan = _create(client, SIM_BASE)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_dependencies": [{"service": "a", "depends_on": "ghost"}]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "simulation_unknown_dependency_target"
    assert issue["service"] == "ghost"
    assert issue["related_services"] == ["a"]


def test_simulate_patch_missing_and_conflicting_ops(client):
    payload = {
        "services": ["a", "b"],
        "dependencies": [{"service": "b", "depends_on": "a"}],
        "max_concurrency": 2,
    }
    plan = _create(client, payload)
    pid = plan["id"]

    r = client.post(
        f"/plans/{pid}/simulate",
        json={"add_dependencies": [{"service": "b", "depends_on": "a"}]},
    )
    assert r.status_code == 422
    assert r.json()["issues"][0]["code"] == "simulation_dependency_exists"

    r = client.post(
        f"/plans/{pid}/simulate",
        json={"remove_dependencies": [{"service": "a", "depends_on": "b"}]},
    )
    assert r.status_code == 422
    assert r.json()["issues"][0]["code"] == "simulation_dependency_not_found"

    r = client.post(
        f"/plans/{pid}/simulate",
        json={
            "add_dependencies": [{"service": "a", "depends_on": "b"}],
            "remove_dependencies": [{"service": "a", "depends_on": "b"}],
        },
    )
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "simulation_dependency_conflicting_ops" in codes

    # Fault-domain op targets a domain that does not exist.
    r = client.post(
        f"/plans/{pid}/simulate",
        json={"remove_fault_domains": ["nope"]},
    )
    assert r.status_code == 422
    assert r.json()["issues"][0]["code"] == "simulation_fault_domain_not_found"


def test_simulate_invalid_concurrency_is_validation_error(client):
    plan = _create(client, SIM_BASE)
    r = client.post(f"/plans/{plan['id']}/simulate", json={"max_concurrency": 0})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"
    assert body["issues"][0]["constraint"] == "max_concurrency"
