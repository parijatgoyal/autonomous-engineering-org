import time

from fastapi.testclient import TestClient

from aeeo import api
from aeeo.config import Settings
from aeeo.orchestrator import Orchestrator


def _client(tmp_path):
    api.configure(Orchestrator(settings=Settings(data_dir=tmp_path / "data", provider="mock")))
    return TestClient(api.app)


def _login(c, user):
    r = c.post("/api/login", json={"username": user, "password": f"{user}-demo"})
    assert r.status_code == 200
    return {"authorization": f"Bearer {r.json()['token']}"}


def _wait(c, rid, h):
    for _ in range(200):
        d = c.get(f"/api/runs/{rid}", headers=h).json()
        if d["status"] != "RUNNING":
            return d
        time.sleep(0.1)
    raise AssertionError("run did not settle")


def test_requires_sign_in(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/runs").status_code == 401
    assert c.post("/api/login", json={"username": "priya", "password": "wrong"}).status_code == 401
    assert c.get("/").status_code == 200  # the page itself shows the sign-in form


def test_full_journey_by_role(tmp_path):
    c = _client(tmp_path)
    asha, sam, priya, dev = (_login(c, u) for u in ("asha", "sam", "priya", "dev"))
    meta = c.get("/api/meta", headers=asha).json()

    # Approvers cannot submit; arbitrary server paths are refused.
    assert c.post("/api/runs", headers=priya, json={"requirement": meta["sample_requirement"]}).status_code == 403
    assert c.post("/api/runs", headers=asha, json={"requirement": meta["sample_requirement"],
                                                    "repo": "/etc"}).status_code == 400

    rid = c.post("/api/runs", headers=asha, json={"requirement": meta["sample_requirement"],
                                                  "faults": ["buggy_first_impl"]}).json()["run_id"]
    d = _wait(c, rid, asha)
    assert d["status"] == "PLAN_REVIEW" and d["my_actions"] == ["confirm_plan", "revise_plan", "reject"]
    assert c.get(f"/api/runs/{rid}", headers=dev).json()["my_actions"] == []
    c.post(f"/api/runs/{rid}/confirm-plan", headers=asha, json={"comment": "codes are case-insensitive"})

    d = _wait(c, rid, asha)
    assert d["status"] == "AWAITING_APPROVAL"
    inbox = {r["id"]: r for r in c.get("/api/runs", headers=sam).json()}
    assert inbox[rid]["my_actions"] == ["signoff", "request_changes"]
    assert c.post(f"/api/runs/{rid}/approve", headers=priya, json={}).status_code == 409  # sign-off first
    assert c.post(f"/api/runs/{rid}/signoff", headers=sam, json={"decision": "security"}).status_code == 200
    assert c.post(f"/api/runs/{rid}/approve", headers=asha, json={}).status_code == 403
    assert c.post(f"/api/runs/{rid}/approve", headers=priya, json={"comment": "ship"}).status_code == 200
    assert c.get(f"/api/runs/{rid}", headers=asha).json()["status"] == "DEPLOYED"
    assert "Subject:" in c.get(f"/api/runs/{rid}/patch", headers=dev).text
    assert c.get("/api/audit", headers=asha).status_code == 403
    assert c.get("/api/audit", headers=priya).json()["decisions"]
