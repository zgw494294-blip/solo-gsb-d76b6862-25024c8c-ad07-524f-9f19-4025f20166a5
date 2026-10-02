"""End-to-end checks for the stateful session drill feature."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

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

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# 1. create session
r = client.post("/v1/sessions", json=P)
check("create 201", r.status_code == 201, r.status_code)
body = r.json()
sid = body.get("id", "")
check("create returns id", bool(sid))
check("create version 0", body.get("version") == 0)
check("trace empty", body.get("trace") == [])
check("enabled ids sorted", body.get("enabled_transition_ids") == ["t1"],
      body.get("enabled_transition_ids"))
check("configuration initial", body.get("configuration") == {
    "state": {"A": 0, "B": 0}, "queues": {"A_to_B": None, "B_to_A": None}})
check("protocol echoed with aliases", body["protocol"]["transitions"][0]["from"] == "A")

# 2. unknown session 404
r = client.get("/v1/sessions/deadbeef")
check("unknown session 404", r.status_code == 404)
r = client.post("/v1/sessions/deadbeef/transitions",
                json={"transition_id": "t1", "expected_version": 0})
check("unknown execute 404", r.status_code == 404)
r = client.put("/v1/sessions/deadbeef/protocol",
               json={"protocol": P, "expected_version": 0})
check("unknown replace 404", r.status_code == 404)

# 3. invalid protocol reuses validation -> 422
bad = dict(P)
bad = {**P, "transitions": P["transitions"] + [
    {"id": "t1", "from": "B", "to": "A", "action": "send", "message": "q",
     "source": 0, "target": 1}]}
r = client.post("/v1/sessions", json=bad)
check("duplicate ids 422", r.status_code == 422, r.status_code)
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": bad, "expected_version": 0})
check("replace invalid protocol 422", r.status_code == 422, r.status_code)

# 4. execute first transition
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1", "expected_version": 0})
check("execute t1 200", r.status_code == 200, r.status_code)
b = r.json()
check("version 1", b["version"] == 1)
check("trace len 1", len(b["trace"]) == 1)
step = b["trace"][0]
check("step fields", {k: step[k] for k in
                      ("index", "transition_id", "from", "to", "action", "message")} == {
    "index": 1, "transition_id": "t1", "from": "A", "to": "B",
    "action": "send", "message": "m"})
check("step post-state", step["state"] == {"A": 1, "B": 0})
check("step post-queue", step["queues"] == {"A_to_B": "m", "B_to_A": None})
check("current config after t1", b["configuration"]["state"] == {"A": 1, "B": 0}
      and b["configuration"]["queues"] == {"A_to_B": "m", "B_to_A": None})
check("enabled after t1 = t4", b["enabled_transition_ids"] == ["t4"],
      b["enabled_transition_ids"])

# 5. stale version -> 409, session untouched
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t4", "expected_version": 0})
check("stale execute 409", r.status_code == 409, r.status_code)
check("stale keeps version", client.get(f"/v1/sessions/{sid}").json()["version"] == 1)

# 6. non-enabled transition -> 409
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1", "expected_version": 1})
check("disabled transition 409", r.status_code == 409, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "nope", "expected_version": 1})
check("unknown transition execute 409", r.status_code == 409, r.status_code)

# 7. complete the protocol (t4) -> version 2, completed, no enabled transitions
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t4", "expected_version": 1})
check("execute t4 200", r.status_code == 200, r.status_code)
b = r.json()
check("version 2", b["version"] == 2)
check("trace len 2", len(b["trace"]) == 2)
check("completed empty enabled", b["enabled_transition_ids"] == [],
      b["enabled_transition_ids"])
check("completed config", b["configuration"]["state"] == {"A": 1, "B": 1}
      and b["configuration"]["queues"] == {"A_to_B": None, "B_to_A": None})

# 8. completion cannot continue -> 409
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t4", "expected_version": 2})
check("completed no execute 409", r.status_code == 409, r.status_code)
check("after refused still v2", client.get(f"/v1/sessions/{sid}").json()["version"] == 2)

# 9. replace protocol at completion: same trace ids must replay against new defs.
#    New protocol: t1 sends m (A 0->0 instead of 0->1), t2 receives m on B...
#    Trace is [t1, t4]; make replay fail at step 2 by removing enablement.
#    First, a successful replacement changing definitions:
P2 = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        # t1 still sends m from A0; now loops A 0->0
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        # t4 receives m as B 0->1 (owner of a receive is the `to` endpoint!)
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P2, "expected_version": 2})
check("replace 200", r.status_code == 200, r.status_code)
b = r.json()
check("replace version 3", b["version"] == 3)
check("replace replayed trace len", len(b["trace"]) == 2)
check("replayed step1 owner-state", b["trace"][0]["state"] == {"A": 0, "B": 0})
check("replayed step2 final", b["trace"][1]["state"] == {"A": 0, "B": 1})
check("current config recomputed", b["configuration"]["state"] == {"A": 0, "B": 1},
      b["configuration"])
check("protocol definition changed",
      b["protocol"]["transitions"][0]["target"] == 0)
# A=0 not final -> not completed -> execution can continue with the new model
check("continuable after replace", b["enabled_transition_ids"] == ["t1"],
      b["enabled_transition_ids"])

# 10. failed replay -> 422 with first_failed_step, old session retained
#     P3: t1 now A 0->1 sending m (same as original), t4 receive m A-owned
#     1->0? make step 2 not enabled by requiring different message.
P3 = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "ZZ",
         "source": 0, "target": 1},
    ],
}
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P3, "expected_version": 3})
check("replay fail 422", r.status_code == 422, (r.status_code, r.text))
check("first failed step = 2",
      r.json().get("detail", {}).get("first_failed_step") == 2,
      r.json())
b = client.get(f"/v1/sessions/{sid}").json()
check("old session retained after failed replay",
      b["version"] == 3 and b["protocol"]["transitions"][1]["message"] == "m")

# missing id in new protocol -> first failed step 1
P4 = dict(P2)
P4 = {**P2, "transitions": [P2["transitions"][0]]}  # drop t4
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P4, "expected_version": 3})
check("missing id replay 422 step 2",
      r.status_code == 422 and r.json()["detail"]["first_failed_step"] == 2,
      (r.status_code, r.text))

# 11. replay cannot complete before trace end
# fresh session: execute t1,t4 to completion in original protocol, then
# replace with a protocol whose t1 already lands in a completion config.
r = client.post("/v1/sessions", json=P)
sid2 = r.json()["id"]
for tid, ver in (("t1", 0), ("t4", 1)):
    client.post(f"/v1/sessions/{sid2}/transitions",
                json={"transition_id": tid, "expected_version": ver})
# t1 sends m with A 0->1 (not completion, queue has m), t4 consumes -> completion.
# New t1: A already starts in a final state... build protocol where after step1
# (the send) BOTH are final and queues empty — impossible after a send (queue
# occupied). Instead use trace [t4? no]. Achieve "completion before trace end"
# with a 2-step trace where after step 1 queues are empty & both final: t1 would
# need to be a receive that empties... but first step starts with empty queues.
# Simpler: replace into protocol where step 1 (t1, the send) yields completion:
# a send always fills a queue, so completion is impossible immediately after a
# send. Use original 4-transition protocol with trace t1,t2,t4,t3? Instead make
# trace of length 2 where step1 is t1 send, and new t1 loops while new
# *initial* configuration is already a completion configuration (both final).
P_completed_initial = {
    "A": {"states": [0], "initial": 0, "finals": [0]},
    "B": {"states": [0], "initial": 0, "finals": [0]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 0},
    ],
}
r = client.put(f"/v1/sessions/{sid2}/protocol",
               json={"protocol": P_completed_initial, "expected_version": 2})
check("initial completed -> replay 422 step1",
      r.status_code == 422 and r.json()["detail"]["first_failed_step"] == 1,
      (r.status_code, r.text))
check("session2 retained", client.get(f"/v1/sessions/{sid2}").json()["version"] == 2)

# 12. stale version on replace -> 409
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P2, "expected_version": 0})
check("stale replace 409", r.status_code == 409, r.status_code)

# 13. concurrent same-version modifications: exactly one succeeds
import threading
results = []
barrier = threading.Barrier(5)


def worker():
    barrier.wait()
    rr = client.post(f"/v1/sessions/{sid}/transitions",
                     json={"transition_id": "t1", "expected_version": 3})
    results.append(rr.status_code)


threads = [threading.Thread(target=worker) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent: exactly one 200", results.count(200) == 1, results)
check("concurrent: rest 409", results.count(409) == 4, results)
check("version incremented once",
      client.get(f"/v1/sessions/{sid}").json()["version"] == 4)

# 14. concurrent replaces same expected_version: exactly one succeeds
b = client.get(f"/v1/sessions/{sid}").json()
ver = b["version"]
results = []
barrier = threading.Barrier(5)


def worker_replace():
    barrier.wait()
    rr = client.put(f"/v1/sessions/{sid}/protocol",
                    json={"protocol": P2, "expected_version": ver})
    results.append(rr.status_code)


threads = [threading.Thread(target=worker_replace) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent replace: exactly one 200", results.count(200) == 1, results)
check("concurrent replace version +1",
      client.get(f"/v1/sessions/{sid}").json()["version"] == ver + 1)

# 15. malformed bodies -> 422
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1", "expected_version": -1})
check("negative version 422", r.status_code == 422)
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1"})
check("missing expected_version 422", r.status_code == 422)
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 4})
check("missing protocol 422", r.status_code == 422)

# 16. branching: copy a trace prefix into an independent session
# fresh source: execute t1 only -> version 1, trace [t1]
r = client.post("/v1/sessions", json=P)
sid3 = r.json()["id"]
client.post(f"/v1/sessions/{sid3}/transitions",
            json={"transition_id": "t1", "expected_version": 0})

r = client.post(f"/v1/sessions/{sid3}/branches",
                json={"expected_version": 1, "prefix_length": 1})
check("branch 201", r.status_code == 201, r.status_code)
b = r.json()
bid = b["id"]
check("branch new id", bool(bid) and bid != sid3)
check("branch version 0", b["version"] == 0)
check("branch trace is prefix", [s["transition_id"] for s in b["trace"]] == ["t1"])
check("branch trace reindexed", b["trace"][0]["index"] == 1)
check("branch config recomputed", b["configuration"] == {
    "state": {"A": 1, "B": 0}, "queues": {"A_to_B": "m", "B_to_A": None}},
    b["configuration"])
check("branch enabled recomputed", b["enabled_transition_ids"] == ["t4"],
      b["enabled_transition_ids"])

# source untouched by the branch: version not consumed, trace unchanged
src = client.get(f"/v1/sessions/{sid3}").json()
check("source version not consumed", src["version"] == 1)
check("source trace unchanged", [s["transition_id"] for s in src["trace"]] == ["t1"])

# the same source version branches again: empty prefix
r = client.post(f"/v1/sessions/{sid3}/branches",
                json={"expected_version": 1, "prefix_length": 0})
check("branch empty prefix 201", r.status_code == 201, r.status_code)
b2 = r.json()
bid2 = b2["id"]
check("branch empty prefix new id", bid2 not in (sid3, bid))
check("branch empty trace", b2["trace"] == [])
check("branch initial config", b2["configuration"] == {
    "state": {"A": 0, "B": 0}, "queues": {"A_to_B": None, "B_to_A": None}})
check("branch initial enabled", b2["enabled_transition_ids"] == ["t1"])

# the branch continues on its own: execute t4 to completion
r = client.post(f"/v1/sessions/{bid}/transitions",
                json={"transition_id": "t4", "expected_version": 0})
check("branch execute 200", r.status_code == 200, r.status_code)
check("branch version 1", r.json()["version"] == 1)
check("branch completed", r.json()["enabled_transition_ids"] == [])
# ... and the source is unaffected
src = client.get(f"/v1/sessions/{sid3}").json()
check("source unaffected by branch execute", src["version"] == 1
      and src["configuration"]["queues"] == {"A_to_B": "m", "B_to_A": None})

# the source also continues on its own
r = client.post(f"/v1/sessions/{sid3}/transitions",
                json={"transition_id": "t4", "expected_version": 1})
check("source execute after branch 200", r.status_code == 200, r.status_code)
check("source version 2", r.json()["version"] == 2)

# full-length prefix copies the whole trace (branch is completed too)
r = client.post(f"/v1/sessions/{sid3}/branches",
                json={"expected_version": 2, "prefix_length": 2})
check("branch full prefix 201", r.status_code == 201, r.status_code)
b = r.json()
check("full prefix trace", [s["transition_id"] for s in b["trace"]] == ["t1", "t4"])
check("full prefix completed", b["enabled_transition_ids"] == []
      and b["configuration"]["state"] == {"A": 1, "B": 1})

# the branch supports protocol replacement; the source protocol is unchanged
r = client.put(f"/v1/sessions/{bid2}/protocol",
               json={"protocol": P2, "expected_version": 0})
check("branch replace 200", r.status_code == 200, r.status_code)
check("branch replace version 1", r.json()["version"] == 1)
check("source protocol unchanged",
      client.get(f"/v1/sessions/{sid3}").json()["protocol"]["transitions"][0]["target"] == 1)

# a branch of a replaced protocol uses the replaced definitions:
# sid is at version 5 with protocol P2 (t1 loops A 0->0) and trace [t1, t4, t1]
r = client.post(f"/v1/sessions/{sid}/branches",
                json={"expected_version": 5, "prefix_length": 2})
check("branch of replaced 201", r.status_code == 201, (r.status_code, r.text))
b = r.json()
check("replaced protocol copied", b["protocol"]["transitions"][0]["target"] == 0)
check("recomputed under replaced defs", b["configuration"] == {
    "state": {"A": 0, "B": 1}, "queues": {"A_to_B": None, "B_to_A": None}},
    b["configuration"])
check("recomputed enabled under replaced defs", b["enabled_transition_ids"] == ["t1"],
      b["enabled_transition_ids"])

# stale version -> 409 with current_version; no branch created
r = client.post(f"/v1/sessions/{sid3}/branches",
                json={"expected_version": 1, "prefix_length": 1})
check("branch stale 409", r.status_code == 409, r.status_code)
check("branch stale current_version",
      r.json()["detail"] == {"error": "version_conflict", "current_version": 2},
      r.json())

# unknown source -> 404
r = client.post("/v1/sessions/deadbeef/branches",
                json={"expected_version": 0, "prefix_length": 0})
check("branch unknown 404", r.status_code == 404)

# invalid prefix_length -> 422 (source unchanged, no branch created)
for bad_body, name in [
    ({"expected_version": 2, "prefix_length": 3}, "prefix beyond trace"),
    ({"expected_version": 2, "prefix_length": -1}, "negative prefix"),
    ({"expected_version": 2, "prefix_length": True}, "boolean prefix"),
    ({"expected_version": 2, "prefix_length": False}, "false prefix"),
    ({"expected_version": 2, "prefix_length": "1"}, "string prefix"),
    ({"expected_version": 2, "prefix_length": 1.0}, "float prefix"),
    ({"expected_version": 2}, "missing prefix"),
]:
    r = client.post(f"/v1/sessions/{sid3}/branches", json=bad_body)
    check(f"{name} 422", r.status_code == 422, (r.status_code, r.text))
check("source still version 2",
      client.get(f"/v1/sessions/{sid3}").json()["version"] == 2)

# concurrent branch vs source modification: every branch either copies the
# expected version's whole snapshot or is rejected as stale; the source
# modification succeeds exactly once and the source ends at version 1.
r = client.post("/v1/sessions", json=P)
sid4 = r.json()["id"]
results = {}
barrier = threading.Barrier(5)


def worker_branch(tag):
    barrier.wait()
    rr = client.post(f"/v1/sessions/{sid4}/branches",
                     json={"expected_version": 0, "prefix_length": 0})
    results[tag] = rr.status_code


def worker_execute():
    barrier.wait()
    rr = client.post(f"/v1/sessions/{sid4}/transitions",
                     json={"transition_id": "t1", "expected_version": 0})
    results["execute"] = rr.status_code


threads = [threading.Thread(target=worker_branch, args=(f"branch{i}",))
           for i in range(4)]
threads.append(threading.Thread(target=worker_execute))
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent execute succeeds", results["execute"] == 200, results)
check("concurrent branches copied or stale",
      all(results[f"branch{i}"] in (201, 409) for i in range(4)), results)
check("concurrent source version 1",
      client.get(f"/v1/sessions/{sid4}").json()["version"] == 1)

# 17. existing offline endpoints still work
r = client.get("/healthz")
check("healthz", r.status_code == 200 and r.json() == {"status": "ok"})
r = client.post("/v1/deadlocks", json=P)
check("deadlocks endpoint intact", r.status_code == 200)
r = client.post("/v1/completion-plans",
                json={**P, "required_transition_ids": ["t1"]})
check("completion-plans endpoint intact", r.status_code == 200
      and r.json()["completable"] is True)
r = client.post("/v1/deactivation-plans", json={**P, "removable_transition_ids": []})
check("deactivation-plans endpoint intact", r.status_code == 200)
r = client.post("/v1/completion-guarantees", json=P)
check("completion-guarantees endpoint intact", r.status_code == 200)

# 18. trace adoption: splice the remaining trace of another drill session
#     into the target without replaying the shared prefix step by step.

def new_session(protocol=P):
    return client.post("/v1/sessions", json=protocol).json()["id"]


def run(sid, tid, ver):
    return client.post(f"/v1/sessions/{sid}/transitions",
                       json={"transition_id": tid, "expected_version": ver})


def adopt(tid, src, tver, sver):
    return client.post(f"/v1/sessions/{tid}/adoptions", json={
        "source_session_id": src,
        "target_expected_version": tver,
        "source_expected_version": sver,
    })


# target walked t1 (v1); source walked t1,t4 to completion (v2)
atgt = new_session()
asrc = new_session()
check("setup target t1", run(atgt, "t1", 0).status_code == 200)
check("setup source t1", run(asrc, "t1", 0).status_code == 200)
check("setup source t4", run(asrc, "t4", 1).status_code == 200)

r = adopt(atgt, asrc, 1, 2)
check("adopt 200", r.status_code == 200, (r.status_code, r.text))
b = r.json()
check("adopt returns target id", b["id"] == atgt)
check("adopt version +1", b["version"] == 2)
check("adopt full trace", [s["transition_id"] for s in b["trace"]] == ["t1", "t4"])
check("adopt trace reindexed", [s["index"] for s in b["trace"]] == [1, 2])
check("adopt completed config", b["configuration"]["state"] == {"A": 1, "B": 1}
      and b["configuration"]["queues"] == {"A_to_B": None, "B_to_A": None},
      b["configuration"])
check("adopt recomputed enabled", b["enabled_transition_ids"] == [],
      b["enabled_transition_ids"])
check("adopt snapshot step2 state", b["trace"][1]["state"] == {"A": 1, "B": 1})

# source is never written by an adoption
src = client.get(f"/v1/sessions/{asrc}").json()
check("source untouched version", src["version"] == 2)
check("source untouched trace", [s["transition_id"] for s in src["trace"]] == ["t1", "t4"])

# the target is a normal session afterwards: completed -> execute refused
r = run(atgt, "t4", 2)
check("adopted session completed 409", r.status_code == 409)

# adopting again from the identical source now fails: not a strict prefix
r = adopt(atgt, asrc, 2, 2)
check("re-adopt equal traces 422", r.status_code == 422, (r.status_code, r.text))
check("re-adopt error", r.json()["detail"]["error"] == "trace_not_strict_prefix")
check("target still v2 after refused",
      client.get(f"/v1/sessions/{atgt}").json()["version"] == 2)

# an empty target adopts the source's whole remaining trace
aempty = new_session()
r = adopt(aempty, asrc, 0, 2)
check("empty target adopt 200", r.status_code == 200, r.status_code)
check("empty target version 1", r.json()["version"] == 1)
check("empty target full trace",
      [s["transition_id"] for s in r.json()["trace"]] == ["t1", "t4"])

# same session id -> 422 (before any database lookup)
r = adopt(asrc, asrc, 2, 2)
check("same session 422", r.status_code == 422, r.status_code)
check("same session error", r.json()["detail"]["error"] == "same_session")

# target longer than source -> 422 strict prefix
along = new_session()
run(along, "t1", 0)
run(along, "t4", 1)
ashort = new_session()
run(ashort, "t1", 0)
r = adopt(along, ashort, 2, 1)
check("target longer 422", r.status_code == 422, r.text)
d = r.json()["detail"]
check("target longer error", d["error"] == "trace_not_strict_prefix")
check("target longer lengths", d["target_trace_length"] == 2
      and d["source_trace_length"] == 1 and d["first_divergent_step"] == 2, d)
check("longer target unchanged",
      client.get(f"/v1/sessions/{along}").json()["version"] == 2)

# two empty traces -> 422 (the source must actually continue the target)
e1, e2 = new_session(), new_session()
r = adopt(e1, e2, 0, 0)
check("both empty 422", r.status_code == 422, r.text)
check("both empty error", r.json()["detail"]["error"] == "trace_not_strict_prefix")
check("both empty no divergent step",
      "first_divergent_step" not in r.json()["detail"], r.json())

# protocol with two independent paths from the initial configuration:
# [t1, t4] finishes via A's message, [t2, t3] via B's message.
P2PATHS = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "t2", "from": "B", "to": "A", "action": "send", "message": "n",
         "source": 0, "target": 1},
        {"id": "t3", "from": "B", "to": "A", "action": "receive", "message": "n",
         "source": 0, "target": 1},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}
check("two-path protocol valid", client.post("/v1/deadlocks", json=P2PATHS).status_code == 200)
dtgt = new_session(P2PATHS)
dsrc = new_session(P2PATHS)
run(dtgt, "t1", 0)
run(dsrc, "t2", 0)
run(dsrc, "t3", 1)
# target [t1] is shorter than source [t2, t3] but is not its prefix
r = adopt(dtgt, dsrc, 1, 2)
check("divergent trace 422", r.status_code == 422, (r.status_code, r.text))
d = r.json()["detail"]
check("divergent error", d["error"] == "trace_not_strict_prefix")
check("divergent step 1", d.get("first_divergent_step") == 1, d)
check("divergent target untouched",
      [s["transition_id"] for s in client.get(f"/v1/sessions/{dtgt}").json()["trace"]]
      == ["t1"])
check("divergent source untouched",
      client.get(f"/v1/sessions/{dsrc}").json()["version"] == 2)

# same ids but a different field value -> protocol mismatch 422
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
mtgt = new_session()
msrc = new_session(POTHER)
run(mtgt, "t1", 0)
run(msrc, "t1", 0)
r = adopt(mtgt, msrc, 1, 1)
check("field mismatch 422", r.status_code == 422, r.text)
check("field mismatch error", r.json()["detail"]["error"] == "protocol_mismatch")

# same field values, transition list in a different order -> 422
PREORDER = {**P, "transitions": [P["transitions"][1], P["transitions"][0]]}
otgt = new_session()
osrc = new_session(PREORDER)
run(otgt, "t1", 0)
run(osrc, "t1", 0)
r = adopt(otgt, osrc, 1, 1)
check("list order mismatch 422", r.status_code == 422, r.text)
check("list order error", r.json()["detail"]["error"] == "protocol_mismatch")

# version conflicts: name the stale session and its current version
r = adopt(atgt, asrc, 0, 2)
check("stale target 409", r.status_code == 409, r.text)
d = r.json()["detail"]
check("stale target detail", d == {"error": "version_conflict", "current_version": 2,
                                   "session": "target", "session_id": atgt}, d)
r = adopt(aempty, asrc, 1, 0)
check("stale source 409", r.status_code == 409, r.text)
d = r.json()["detail"]
check("stale source detail", d == {"error": "version_conflict", "current_version": 2,
                                   "session": "source", "session_id": asrc}, d)
# nothing moved on a conflict
check("conflict target unchanged", client.get(f"/v1/sessions/{atgt}").json()["version"] == 2)
check("conflict source unchanged", client.get(f"/v1/sessions/{asrc}").json()["version"] == 2)

# unknown sessions -> 404
r = adopt("deadbeef", asrc, 0, 2)
check("unknown target adopt 404", r.status_code == 404)
r = adopt(aempty, "deadbeef", 1, 2)
check("unknown source adopt 404", r.status_code == 404)

# malformed bodies -> 422
for bad_body, name in [
    ({"source_session_id": asrc, "source_expected_version": 2}, "missing target version"),
    ({"source_session_id": asrc, "target_expected_version": 0}, "missing source version"),
    ({"target_expected_version": 0, "source_expected_version": 2}, "missing source id"),
    ({"source_session_id": "", "target_expected_version": 0,
      "source_expected_version": 2}, "empty source id"),
    ({"source_session_id": asrc, "target_expected_version": -1,
      "source_expected_version": 2}, "negative target version"),
    ({"source_session_id": asrc, "target_expected_version": 0,
      "source_expected_version": -1}, "negative source version"),
    ({"source_session_id": asrc, "target_expected_version": 0,
      "source_expected_version": 2, "extra": 1}, "extra field"),
]:
    r = client.post(f"/v1/sessions/{aempty}/adoptions", json=bad_body)
    check(f"{name} 422", r.status_code == 422, (r.status_code, r.text))

# concurrency: acceptance vs execution on one target version — exactly one wins
ctgt = new_session()
csrc = new_session()
run(csrc, "t1", 0)
run(csrc, "t4", 1)  # source [t1,t4] v2; target empty v0
results = []
barrier = threading.Barrier(10)


def adopt_worker():
    barrier.wait()
    rr = adopt(ctgt, csrc, 0, 2)
    results.append(("adopt", rr.status_code))


def execute_worker():
    barrier.wait()
    rr = run(ctgt, "t1", 0)
    results.append(("execute", rr.status_code))


threads = [threading.Thread(target=adopt_worker) for _ in range(5)]
threads += [threading.Thread(target=execute_worker) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("adopt vs execute one 200", sum(1 for _, code in results if code == 200) == 1, results)
check("adopt vs execute rest 409", sum(1 for _, code in results if code == 409) == 9, results)
final = client.get(f"/v1/sessions/{ctgt}").json()
check("adopt vs execute version 1", final["version"] == 1)
check("adopt vs execute one outcome",
      [s["transition_id"] for s in final["trace"]] in (["t1"], ["t1", "t4"]),
      [s["transition_id"] for s in final["trace"]])

# concurrency: acceptance vs protocol replacement on one target version
rtgt = new_session()
results = []
barrier = threading.Barrier(10)


def adopt_worker2():
    barrier.wait()
    rr = adopt(rtgt, csrc, 0, 2)
    results.append(("adopt", rr.status_code))


def replace_worker():
    barrier.wait()
    rr = client.put(f"/v1/sessions/{rtgt}/protocol",
                    json={"protocol": P, "expected_version": 0})
    results.append(("replace", rr.status_code))


threads = [threading.Thread(target=adopt_worker2) for _ in range(5)]
threads += [threading.Thread(target=replace_worker) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("adopt vs replace one 200", sum(1 for _, code in results if code == 200) == 1, results)
check("adopt vs replace rest 409", sum(1 for _, code in results if code == 409) == 9, results)
check("adopt vs replace version 1",
      client.get(f"/v1/sessions/{rtgt}").json()["version"] == 1)

# concurrency: two adoptions with the same expected versions — one wins
ttgt = new_session()
results = []
barrier = threading.Barrier(5)


def adopt_worker3():
    barrier.wait()
    rr = adopt(ttgt, csrc, 0, 2)
    results.append(rr.status_code)


threads = [threading.Thread(target=adopt_worker3) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("parallel adopts one 200", results.count(200) == 1, results)
check("parallel adopts rest 409", results.count(409) == 4, results)
check("parallel adopts version 1",
      client.get(f"/v1/sessions/{ttgt}").json()["version"] == 1)

# concurrency: adoption vs source modification — accept the named complete
# source snapshot or refuse as stale; the source modification wins exactly once.
# Each adoption uses its own fresh target, so target-side races do not mask the
# source snapshot that was accepted.
ssrc = new_session()
run(ssrc, "t1", 0)  # source [t1] v1; it can continue with t4
results = {}
barrier = threading.Barrier(5)


def source_adopt(tag):
    barrier.wait()
    target = new_session()
    rr = adopt(target, ssrc, 0, 1)
    accepted_trace = None
    if rr.status_code == 200:
        accepted_trace = [s["transition_id"] for s in rr.json()["trace"]]
    results[tag] = (rr.status_code, accepted_trace)


def source_continue():
    barrier.wait()
    rr = run(ssrc, "t4", 1)
    results["continue"] = rr.status_code


threads = [threading.Thread(target=source_adopt, args=(f"adopt{i}",)) for i in range(4)]
threads.append(threading.Thread(target=source_continue))
for t in threads:
    t.start()
for t in threads:
    t.join()
check("source continue 200", results["continue"] == 200, results)
for i in range(4):
    code, accepted_trace = results[f"adopt{i}"]
    check(f"source race adopt{i} 200/409", code in (200, 409), results)
    # a 200 means the whole expected source snapshot (trace [t1]) was accepted;
    # the post-commit suffix [t4] must never leak into an accepted response
    check(f"source race adopt{i} whole snapshot",
          code == 409 or accepted_trace == ["t1"], results)
src = client.get(f"/v1/sessions/{ssrc}").json()
check("source ends v2", src["version"] == 2 and
      [s["transition_id"] for s in src["trace"]] == ["t1", "t4"])

# adopted sessions persist like ordinary sessions (same table, same file)
r = client.get(f"/v1/sessions/{atgt}")
check("adopted session queryable", r.status_code == 200
      and [s["transition_id"] for s in r.json()["trace"]] == ["t1", "t4"])

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
