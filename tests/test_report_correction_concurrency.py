"""The double-correction scenario: two parallel transaction bundles, each with
a DiagnosticReport PUT guarded by the same If-Match version, must not both
succeed — not even when two API instances share one SQLite database file.

Covered guarantees:

* the same report, based on the same version, is corrected at most once;
* the loser gets 412 OperationOutcome and the bundle leaves no trace: report
  body, observation links and version history are all unchanged;
* the losing clerk may resubmit with the same accession (Idempotency-Key) and
  a fresh If-Match version;
* the winning bundle, retried with its original content, replays the first
  receipt;
* corrections to *other* reports are not blocked by an in-flight bundle.
"""
from __future__ import annotations

import concurrent.futures
import threading

from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    skip_unless_shared_database,
    urn,
)


def _submit(client, key, bundle, expect=200):
    resp = client.post("/fhir", json=bundle, headers={"Idempotency-Key": key})
    assert resp.status_code == expect, resp.text
    return resp


def _seed_report(client, value="CORR-SEED"):
    """Create patient + two observations + report(version 1, links obs A)."""
    p_urn, o1_urn, o2_urn, r_urn = urn(), urn(), urn(), urn()
    p_entry, _ = patient_entry(p_urn, value=value)
    o1 = observation_entry(o1_urn, p_urn, code="GLU", value=5.5)
    o2 = observation_entry(o2_urn, p_urn, code="GLU", value=6.1)
    r = report_entry(r_urn, p_urn, [o1_urn])
    receipt = _submit(client, f"{value}-SETUP", make_bundle([r, o1, o2, p_entry])).json()
    ids = {m["id"]: m for m in receipt["resourceMap"]}
    by_type = {}
    for m in receipt["resourceMap"]:
        by_type.setdefault(m["resourceType"], []).append(m["id"])
    return {
        "pid": by_type["Patient"][0],
        "rid": by_type["DiagnosticReport"][0],
        "obs": by_type["Observation"],
    }


def _correction(rid, pid, oid, tag, version=1):
    return make_bundle([
        {
            "resource": {
                "resourceType": "DiagnosticReport",
                "id": rid,
                "status": "final",
                "code": {"coding": [{"code": "24323-8"}]},
                "subject": {"reference": f"Patient/{pid}"},
                "result": [{"reference": f"Observation/{oid}"}],
                "conclusion": f"corrected-by-{tag}",
            },
            "request": {
                "method": "PUT",
                "url": f"DiagnosticReport/{rid}",
                "ifMatch": f'W/"{version}"',
            },
        }
    ])


def _report_state(client, rid):
    report = client.get(f"/diagnostic-reports/{rid}").json()
    history = client.get(f"/diagnostic-reports/{rid}/_history").json()
    return report, history


def test_parallel_corrections_same_version_exactly_one_wins(client):
    skip_unless_shared_database()
    ids = _seed_report(client)
    rid, pid = ids["rid"], ids["pid"]
    obs_a, obs_b = ids["obs"]

    barrier = threading.Barrier(2)

    def correct(tag, oid, key):
        barrier.wait(timeout=15)
        return client.post(
            "/fhir",
            json=_correction(rid, pid, oid, tag),
            headers={"Idempotency-Key": key},
        )

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        fut_a = pool.submit(correct, "A", obs_a, "CORR-A")
        fut_b = pool.submit(correct, "B", obs_b, "CORR-B")
        resp_a, resp_b = fut_a.result(), fut_b.result()

    outcomes = {resp_a.status_code: resp_a, resp_b.status_code: resp_b}
    assert sorted(outcomes) == [200, 412], (
        f"expected one 200 and one 412, got {[resp_a.status_code, resp_b.status_code]}"
    )
    loser = outcomes[412].json()
    assert loser["resourceType"] == "OperationOutcome"
    assert "stale" in loser["issue"][0]["diagnostics"]

    winner_resp = outcomes[200]
    winner_key = "CORR-A" if resp_a.status_code == 200 else "CORR-B"
    winner_tag = "A" if resp_a.status_code == 200 else "B"
    winner_obs = obs_a if resp_a.status_code == 200 else obs_b
    loser_key = "CORR-B" if resp_a.status_code == 200 else "CORR-A"
    loser_oid = obs_b if resp_a.status_code == 200 else obs_a

    # Exactly one correction applied: version 2, winner's body, winner's links,
    # and a single version-2 row in history. The loser's bundle left no trace.
    report, history = _report_state(client, rid)
    assert report["version"] == 2
    assert report["resource"]["conclusion"] == f"corrected-by-{winner_tag}"
    assert report["observationIds"] == [winner_obs]
    assert [e["version"] for e in history["entry"]] == [1, 2]
    assert history["entry"][1]["resource"]["conclusion"] == f"corrected-by-{winner_tag}"

    # The winner's accession key replays the first receipt on identical retry.
    replay = client.post(
        "/fhir",
        json=_correction(rid, pid, winner_obs, winner_tag),
        headers={"Idempotency-Key": winner_key},
    )
    assert replay.status_code == 200
    assert replay.json() == winner_resp.json()
    report_after_replay, history_after_replay = _report_state(client, rid)
    assert report_after_replay["version"] == 2
    assert [e["version"] for e in history_after_replay["entry"]] == [1, 2]

    # The losing clerk resubmits with the same accession key and the fresh
    # version: accepted, and now the report carries the loser's correction.
    resubmit = _submit(
        client, loser_key, _correction(rid, pid, loser_oid, "B-retry", version=2)
    )
    assert resubmit.json()["resourceMap"][0]["version"] == 3
    report, history = _report_state(client, rid)
    assert report["version"] == 3
    assert report["observationIds"] == [loser_oid]
    assert [e["version"] for e in history["entry"]] == [1, 2, 3]


def test_stale_correction_leaves_no_trace(client):
    ids = _seed_report(client, value="CORR-TRACE")
    rid, pid = ids["rid"], ids["pid"]
    obs_a, obs_b = ids["obs"]

    before_report, before_history = _report_state(client, rid)

    # First correction succeeds (version 1 -> 2).
    _submit(client, "TRACE-1", _correction(rid, pid, obs_a, "first"))

    # A late bundle still based on version 1 must be rejected and must not
    # touch body, links or history.
    stale = client.post(
        "/fhir",
        json=_correction(rid, pid, obs_b, "stale", version=1),
        headers={"Idempotency-Key": "TRACE-2"},
    )
    assert stale.status_code == 412
    assert stale.json()["resourceType"] == "OperationOutcome"

    report, history = _report_state(client, rid)
    assert report["version"] == 2
    assert report["resource"]["conclusion"] == "corrected-by-first"
    assert report["observationIds"] == [obs_a]
    assert [e["version"] for e in history["entry"]] == [1, 2]
    # The failed bundle did not resurrect or alter the pre-correction state.
    assert before_report["version"] == 1
    assert [e["version"] for e in before_history["entry"]] == [1]

    # The clerk retries with the same accession key and the current version.
    ok = _submit(client, "TRACE-2", _correction(rid, pid, obs_b, "stale", version=2))
    assert ok.json()["resourceMap"][0]["version"] == 3


def test_parallel_corrections_on_different_reports_both_commit(client):
    skip_unless_shared_database()
    first = _seed_report(client, value="CORR-MULTI-1")
    second = _seed_report(client, value="CORR-MULTI-2")

    barrier = threading.Barrier(2)

    def correct(ids, key):
        barrier.wait(timeout=15)
        return client.post(
            "/fhir",
            json=_correction(ids["rid"], ids["pid"], ids["obs"][0], key),
            headers={"Idempotency-Key": key},
        )

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        f1 = pool.submit(correct, first, "MULTI-1")
        f2 = pool.submit(correct, second, "MULTI-2")
        r1, r2 = f1.result(), f2.result()

    # Unrelated reports are independent: neither is rejected, neither blocks
    # the other beyond the (short) write critical section.
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    for ids in (first, second):
        report, history = _report_state(client, ids["rid"])
        assert report["version"] == 2
        assert [e["version"] for e in history["entry"]] == [1, 2]


def test_high_contention_same_report_single_winner(client):
    """8 parallel corrections on the same report/version: exactly one wins."""
    skip_unless_shared_database()
    ids = _seed_report(client, value="CORR-HOT")
    rid, pid = ids["rid"], ids["pid"]
    obs_a = ids["obs"][0]

    def correct(i):
        return client.post(
            "/fhir",
            json=_correction(rid, pid, obs_a, f"hot-{i}"),
            headers={"Idempotency-Key": f"HOT-{i}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(correct, range(8)))

    ok = [r for r in responses if r.status_code == 200]
    stale = [r for r in responses if r.status_code == 412]
    other = [r for r in responses if r.status_code not in (200, 412)]
    assert not other, [r.text for r in other]
    assert len(ok) == 1 and len(stale) == 7

    report, history = _report_state(client, rid)
    assert report["version"] == 2
    assert [e["version"] for e in history["entry"]] == [1, 2]


def test_same_bundle_cannot_correct_same_report_twice(client):
    ids = _seed_report(client, value="CORR-DOUBLE")
    rid, pid = ids["rid"], ids["pid"]
    obs_a, obs_b = ids["obs"]

    double = make_bundle([
        _correction(rid, pid, obs_a, "one")["entry"][0],
        _correction(rid, pid, obs_b, "two")["entry"][0],
    ])
    resp = client.post("/fhir", json=double, headers={"Idempotency-Key": "DOUBLE-1"})
    assert resp.status_code == 412
    report, history = _report_state(client, rid)
    assert report["version"] == 1
    assert [e["version"] for e in history["entry"]] == [1]
