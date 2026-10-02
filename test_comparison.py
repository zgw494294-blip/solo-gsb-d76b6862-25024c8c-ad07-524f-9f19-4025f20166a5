"""End-to-end checks for the two-session drill comparison feature."""

import json
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(prefix="sessions-cmp-", suffix=".db", delete=False)
_tmp.close()
os.environ["SESSIONS_DB"] = _tmp.name

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def compare(body):
    return client.post("/v1/session-comparisons", json=body)


def create(protocol):
    r = client.post("/v1/sessions", json=protocol)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run(sid, tid, ver):
    return client.post(
        f"/v1/sessions/{sid}/transitions",
        json={"transition_id": tid, "expected_version": ver},
    )


# Protocols -------------------------------------------------------------------

P = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}

# Two independent directions, every transition loops on state 0 so all
# send/receive interleavings stay enabled.
PINTER = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "sAB", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        {"id": "rAB", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
        {"id": "sBA", "from": "B", "to": "A", "action": "send", "message": "n",
         "source": 0, "target": 0},
        {"id": "rBA", "from": "B", "to": "A", "action": "receive", "message": "n",
         "source": 0, "target": 1},
    ],
}

# Same ids/trace shape as P but the A->B message differs.
POTHER = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "ZZ",
         "source": 0, "target": 1},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "ZZ",
         "source": 0, "target": 1},
    ],
}

# 1. identical traces of the same protocol -> equal in both directions
s1 = create(P)
s2 = create(P)
check("setup s1 t1,t4", run(s1, "t1", 0).status_code == 200
      and run(s1, "t4", 1).status_code == 200)
check("setup s2 t1,t4", run(s2, "t1", 0).status_code == 200
      and run(s2, "t4", 1).status_code == 200)
r = compare({"left_session_id": s1, "right_session_id": s2,
             "left_expected_version": 2, "right_expected_version": 2})
check("equal 200", r.status_code == 200, (r.status_code, r.text))
b = r.json()
check("echoes ids/versions",
      b["left_session_id"] == s1 and b["right_session_id"] == s2
      and b["left_version"] == 2 and b["right_version"] == 2, b)
check("equal A_to_B", b["A_to_B"] == {
    "left_event_count": 2, "right_event_count": 2,
    "relation": "equal", "first_difference": None}, b["A_to_B"])
check("equal B_to_A empty", b["B_to_A"] == {
    "left_event_count": 0, "right_event_count": 0,
    "relation": "equal", "first_difference": None}, b["B_to_A"])

# 2. a session compared with itself (same id, same version) -> equal
r = compare({"left_session_id": s1, "right_session_id": s1,
             "left_expected_version": 2, "right_expected_version": 2})
check("self compare 200 equal", r.status_code == 200
      and r.json()["A_to_B"]["relation"] == "equal", (r.status_code, r.text))

# 3. only the inter-direction interleaving differs -> equal in both directions
left = create(PINTER)
right = create(PINTER)
for tid, ver in (("sAB", 0), ("sBA", 1), ("rAB", 2), ("rBA", 3)):
    check("left setup", run(left, tid, ver).status_code == 200)
for tid, ver in (("sBA", 0), ("sAB", 1), ("rBA", 2), ("rAB", 3)):
    check("right setup", run(right, tid, ver).status_code == 200)
r = compare({"left_session_id": left, "right_session_id": right,
             "left_expected_version": 4, "right_expected_version": 4})
check("interleaving only 200", r.status_code == 200, (r.status_code, r.text))
b = r.json()
check("interleaving equal A_to_B", b["A_to_B"] == {
    "left_event_count": 2, "right_event_count": 2,
    "relation": "equal", "first_difference": None}, b["A_to_B"])
check("interleaving equal B_to_A", b["B_to_A"] == {
    "left_event_count": 2, "right_event_count": 2,
    "relation": "equal", "first_difference": None}, b["B_to_A"])

# 4. strict prefix: right trace continues the left within one direction
p1 = create(P)
check("prefix setup t1", run(p1, "t1", 0).status_code == 200)
r = compare({"left_session_id": p1, "right_session_id": s1,
             "left_expected_version": 1, "right_expected_version": 2})
check("prefix 200", r.status_code == 200, (r.status_code, r.text))
b = r.json()
check("left strict prefix relation",
      b["A_to_B"]["relation"] == "left_is_strict_prefix", b["A_to_B"])
check("prefix counts", b["A_to_B"]["left_event_count"] == 1
      and b["A_to_B"]["right_event_count"] == 2)
check("prefix first_difference",
      b["A_to_B"]["first_difference"] == {
          "position": 2,
          "left": None,
          "right": {"action": "receive", "message": "m",
                    "trace_step": 2, "transition_id": "t4"}},
      b["A_to_B"]["first_difference"])
check("prefix empty other direction equal",
      b["B_to_A"]["relation"] == "equal"
      and b["B_to_A"]["first_difference"] is None, b["B_to_A"])

# reverse the sides -> right_is_strict_prefix
r = compare({"left_session_id": s1, "right_session_id": p1,
             "left_expected_version": 2, "right_expected_version": 1})
b = r.json()
check("right strict prefix relation",
      b["A_to_B"]["relation"] == "right_is_strict_prefix", b["A_to_B"])
check("right prefix first_difference",
      b["A_to_B"]["first_difference"] == {
          "position": 2,
          "left": {"action": "receive", "message": "m",
                   "trace_step": 2, "transition_id": "t4"},
          "right": None},
      b["A_to_B"]["first_difference"])

# 5. divergent: different protocols, same id labels, different message
d1 = create(P)
d2 = create(POTHER)
run(d1, "t1", 0)
run(d2, "t1", 0)
r = compare({"left_session_id": d1, "right_session_id": d2,
             "left_expected_version": 1, "right_expected_version": 1})
check("divergent 200", r.status_code == 200, (r.status_code, r.text))
b = r.json()
check("divergent relation", b["A_to_B"]["relation"] == "divergent", b["A_to_B"])
check("divergent position/events",
      b["A_to_B"]["first_difference"] == {
          "position": 1,
          "left": {"action": "send", "message": "m",
                   "trace_step": 1, "transition_id": "t1"},
          "right": {"action": "send", "message": "ZZ",
                    "trace_step": 1, "transition_id": "t1"}},
      b["A_to_B"]["first_difference"])

# 6. interleaved sessions diverge in B_to_A after sharing the first A->B event
c1 = create(PINTER)
c2 = create(PINTER)
run(c1, "sAB", 0)
run(c1, "sBA", 1)          # A_to_B [send m], B_to_A [send n]
run(c2, "sAB", 0)
run(c2, "sBA", 1)
run(c2, "rBA", 2)          # B_to_A [send n, receive n]
r = compare({"left_session_id": c1, "right_session_id": c2,
             "left_expected_version": 2, "right_expected_version": 3})
b = r.json()
check("mixed: A_to_B equal", b["A_to_B"]["relation"] == "equal", b)
check("mixed: B_to_A prefix",
      b["B_to_A"]["relation"] == "left_is_strict_prefix", b["B_to_A"])
check("mixed prefix event points to original trace step",
      b["B_to_A"]["first_difference"] == {
          "position": 2,
          "left": None,
          "right": {"action": "receive", "message": "n",
                    "trace_step": 3, "transition_id": "rBA"}},
      b["B_to_A"]["first_difference"])

# 7. unknown sessions -> 404 (either side)
r = compare({"left_session_id": "deadbeef", "right_session_id": s1,
             "left_expected_version": 0, "right_expected_version": 2})
check("unknown left 404", r.status_code == 404
      and r.json()["detail"] == "unknown session", (r.status_code, r.text))
r = compare({"left_session_id": s1, "right_session_id": "deadbeef",
             "left_expected_version": 2, "right_expected_version": 0})
check("unknown right 404", r.status_code == 404, (r.status_code, r.text))

# 8. stale expected version -> 409 naming side, session id, current version
r = compare({"left_session_id": s1, "right_session_id": s2,
             "left_expected_version": 1, "right_expected_version": 2})
check("stale left 409", r.status_code == 409, (r.status_code, r.text))
check("stale left detail", r.json()["detail"] == {
    "error": "version_conflict", "current_version": 2,
    "side": "left", "session_id": s1}, r.json())
r = compare({"left_session_id": s1, "right_session_id": s2,
             "left_expected_version": 2, "right_expected_version": 9})
check("stale right 409", r.status_code == 409, (r.status_code, r.text))
check("stale right detail", r.json()["detail"] == {
    "error": "version_conflict", "current_version": 2,
    "side": "right", "session_id": s2}, r.json())

# unknown id reported even when the other side is also stale
r = compare({"left_session_id": "deadbeef", "right_session_id": s2,
             "left_expected_version": 0, "right_expected_version": 9})
check("unknown beats stale 404", r.status_code == 404, r.status_code)

# stale self comparison names the left side first
r = compare({"left_session_id": s1, "right_session_id": s1,
             "left_expected_version": 1, "right_expected_version": 2})
check("stale self names left", r.status_code == 409
      and r.json()["detail"]["side"] == "left", r.json())

# 9. comparison never writes and consumes no version
before = client.get(f"/v1/sessions/{s1}").json()
compare({"left_session_id": s1, "right_session_id": s2,
         "left_expected_version": 2, "right_expected_version": 2})
after = client.get(f"/v1/sessions/{s1}").json()
check("read-only", before["version"] == after["version"]
      and before["trace"] == after["trace"])

# 10. malformed bodies -> 422
for bad_body, name in [
    ({"right_session_id": s1, "left_expected_version": 0,
      "right_expected_version": 0}, "missing left id"),
    ({"left_session_id": s1, "left_expected_version": 0,
      "right_expected_version": 0}, "missing right id"),
    ({"left_session_id": s1, "right_session_id": s1,
      "right_expected_version": 0}, "missing left version"),
    ({"left_session_id": s1, "right_session_id": s1,
      "left_expected_version": 0}, "missing right version"),
    ({"left_session_id": "", "right_session_id": s1,
      "left_expected_version": 0, "right_expected_version": 0}, "empty left id"),
    ({"left_session_id": s1, "right_session_id": s1,
      "left_expected_version": -1, "right_expected_version": 0}, "negative version"),
    ({"left_session_id": s1, "right_session_id": s1,
      "left_expected_version": 0, "right_expected_version": 0,
      "extra": 1}, "extra field"),
]:
    r = compare(bad_body)
    check(f"{name} 422", r.status_code == 422, (r.status_code, r.text))

# 11. compare against a live (GET) snapshot: per-direction projection matches
view = client.get(f"/v1/sessions/{left}").json()
r = compare({"left_session_id": left, "right_session_id": left,
             "left_expected_version": view["version"],
             "right_expected_version": view["version"]})
b = r.json()
ab = [s for s in view["trace"] if (s["from"], s["to"]) == ("A", "B")]
ba = [s for s in view["trace"] if (s["from"], s["to"]) == ("B", "A")]
check("counts mirror trace split",
      b["A_to_B"]["left_event_count"] == len(ab)
      and b["B_to_A"]["left_event_count"] == len(ba), b)

# 12. concurrency: comparison vs execution either sees the named complete
#     version or answers 409; the two sessions' reads share one snapshot.
import threading

target = create(PINTER)
results = {}
barrier = threading.Barrier(6)


def compare_worker(tag):
    barrier.wait()
    rr = compare({"left_session_id": target, "right_session_id": target,
                  "left_expected_version": 0, "right_expected_version": 0})
    body = rr.json() if rr.status_code == 200 else None
    results[tag] = (
        rr.status_code,
        None if body is None else (
            body["A_to_B"]["left_event_count"],
            body["B_to_A"]["left_event_count"],
        ),
    )


def execute_worker():
    barrier.wait()
    results["execute"] = (run(target, "sAB", 0).status_code, None)


threads = [threading.Thread(target=compare_worker, args=(f"cmp{i}",))
           for i in range(5)]
threads.append(threading.Thread(target=execute_worker))
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent execute 200", results["execute"][0] == 200, results)
for i in range(5):
    code, counts = results[f"cmp{i}"]
    check(f"concurrent cmp{i} 200/409", code in (200, 409), results)
    # a 200 is the whole v0 snapshot (empty traces) — the post-commit step can
    # never leak into an accepted comparison
    check(f"concurrent cmp{i} whole snapshot",
          code == 409 or counts == (0, 0), results)
final = client.get(f"/v1/sessions/{target}").json()
check("session advanced exactly once", final["version"] == 1
      and [s["transition_id"] for s in final["trace"]] == ["sAB"],
      (final["version"], final["trace"]))

# a comparison at the fresh current version then sees exactly that one step
r = compare({"left_session_id": target, "right_session_id": target,
             "left_expected_version": 1, "right_expected_version": 1})
check("post-commit comparison sees one event",
      r.status_code == 200
      and r.json()["A_to_B"]["left_event_count"] == 1
      and r.json()["B_to_A"]["left_event_count"] == 0,
      (r.status_code, r.text))

# concurrent compare vs protocol replace on the same version
rtarget = create(P)
results = {}
barrier = threading.Barrier(6)


def compare_worker2(tag):
    barrier.wait()
    rr = compare({"left_session_id": rtarget, "right_session_id": rtarget,
                  "left_expected_version": 0, "right_expected_version": 0})
    results[tag] = rr.status_code


def replace_worker():
    barrier.wait()
    rr = client.put(f"/v1/sessions/{rtarget}/protocol",
                    json={"protocol": P, "expected_version": 0})
    results["replace"] = rr.status_code


threads = [threading.Thread(target=compare_worker2, args=(f"cmp{i}",))
           for i in range(5)]
threads.append(threading.Thread(target=replace_worker))
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent replace 200", results["replace"] == 200, results)
for i in range(5):
    check(f"concurrent replace cmp{i} 200/409",
          results[f"cmp{i}"] in (200, 409), results)

# 13. old endpoints still work
r = client.get("/healthz")
check("healthz intact", r.status_code == 200)
r = client.post("/v1/sessions", json=P)
check("sessions create intact", r.status_code == 201)
r = client.post("/v1/deadlocks", json=P)
check("deadlocks intact", r.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL COMPARISON CHECKS PASSED")
