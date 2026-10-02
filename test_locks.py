"""End-to-end checks for locking a drill session as a read-only baseline."""

import os
import tempfile
import threading

_tmp = tempfile.NamedTemporaryFile(prefix="sessions-lock-", suffix=".db", delete=False)
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


def new_session():
    r = client.post("/v1/sessions", json=P)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run(sid, tid, ver):
    return client.post(f"/v1/sessions/{sid}/transitions",
                       json={"transition_id": tid, "expected_version": ver})


def lock(sid, ver, value):
    return client.put(f"/v1/sessions/{sid}/lock",
                      json={"expected_version": ver, "locked": value})


def adopt(tid, src, tver, sver):
    return client.post(f"/v1/sessions/{tid}/adoptions", json={
        "source_session_id": src,
        "target_expected_version": tver,
        "source_expected_version": sver,
    })


# 1. every snapshot carries locked=false by default
r = client.post("/v1/sessions", json=P)
sid = r.json()["id"]
check("create has locked=false", r.status_code == 201 and r.json()["locked"] is False)
r = client.get(f"/v1/sessions/{sid}")
check("get has locked=false", r.json()["locked"] is False)
r = run(sid, "t1", 0)
check("execute snapshot has locked=false", r.json()["locked"] is False)

# 2. lock: only the flag changes, version +1, full snapshot returned
r = lock(sid, 1, True)
check("lock 200", r.status_code == 200, r.status_code)
b = r.json()
check("locked true in response", b["locked"] is True)
check("lock bumps version", b["version"] == 2)
check("lock keeps trace", [s["transition_id"] for s in b["trace"]] == ["t1"],
      b["trace"])
check("lock keeps protocol", b["protocol"]["transitions"][0]["id"] == "t1")
check("lock keeps config", b["configuration"] == {
    "state": {"A": 1, "B": 0}, "queues": {"A_to_B": "m", "B_to_A": None}},
    b["configuration"])
check("lock keeps enabled ids", b["enabled_transition_ids"] == ["t4"],
    b["enabled_transition_ids"])
# full snapshot shape: all standard fields present
for field in ("id", "version", "locked", "protocol", "configuration",
              "trace", "enabled_transition_ids"):
    check(f"lock snapshot has {field}", field in b)

# 3. the flag is persisted and visible on plain reads
check("persisted locked on GET", client.get(f"/v1/sessions/{sid}").json()["locked"] is True)

# 4. locked session refuses execution / protocol replacement / adoption target
r = run(sid, "t4", 2)
check("locked execute 409", r.status_code == 409, (r.status_code, r.text))
check("locked execute error body", r.json()["detail"] == {
    "error": "session_locked", "session_id": sid}, r.json())

r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P, "expected_version": 2})
check("locked replace 409", r.status_code == 409, r.text)
check("locked replace error", r.json()["detail"]["error"] == "session_locked")

src = new_session()
run(src, "t1", 0)
run(src, "t4", 1)  # completed source version 2
# locked sid at v2 with trace [t1] would otherwise be able to adopt from src
r = adopt(sid, src, 2, 2)
check("locked adoption target 409", r.status_code == 409, (r.status_code, r.text))
check("locked adoption error", r.json()["detail"] == {
    "error": "session_locked", "session_id": sid}, r.json())

# 5. failed writes change nothing: still version 2, still locked, trace [t1]
b = client.get(f"/v1/sessions/{sid}").json()
check("refused writes keep version", b["version"] == 2)
check("refused writes keep locked", b["locked"] is True)
check("refused writes keep trace",
      [s["transition_id"] for s in b["trace"]] == ["t1"])

# 6. locked session remains queryable / comparable / adoption source / branchable
check("locked GET 200", client.get(f"/v1/sessions/{sid}").status_code == 200)
r = client.post("/v1/session-comparisons", json={
    "left_session_id": sid, "right_session_id": src,
    "left_expected_version": 2, "right_expected_version": 2})
check("locked comparable 200", r.status_code == 200, (r.status_code, r.text))
check("locked comparison A_to_B prefix",
      r.json()["A_to_B"]["relation"] == "left_is_strict_prefix")

# locked sid (trace [t1] v2) is an adoption SOURCE for an empty target
empty = new_session()
r = adopt(empty, sid, 0, 2)
check("locked adoption source 200", r.status_code == 200, (r.status_code, r.text))
check("source adopted suffix", [s["transition_id"] for s in r.json()["trace"]] == ["t1"])
check("locked source untouched", client.get(f"/v1/sessions/{sid}").json()["version"] == 2)

# branching off a locked baseline is allowed; the branch starts unlocked
r = client.post(f"/v1/sessions/{sid}/branches",
                json={"expected_version": 2, "prefix_length": 1})
check("branch off locked 201", r.status_code == 201, (r.status_code, r.text))
branch = r.json()
check("branch unlocked", branch["locked"] is False)
check("branch copied trace", [s["transition_id"] for s in branch["trace"]] == ["t1"])
# the branch can be drilled normally
r = run(branch["id"], "t4", 0)
check("branch continues 200", r.status_code == 200, r.text)
check("branch version 1 unlocked",
      r.json()["version"] == 1 and r.json()["locked"] is False)
# full-length and empty branches also unlocked
for plen in (0, 1):
    rr = client.post(f"/v1/sessions/{sid}/branches",
                     json={"expected_version": 2, "prefix_length": plen})
    check(f"branch prefix {plen} unlocked",
          rr.status_code == 201 and rr.json()["locked"] is False, rr.text)

# 7. stale expected_version on lock -> 409 with current_version, data unchanged
r = lock(sid, 1, True)
check("stale lock 409", r.status_code == 409)
check("stale lock body",
      r.json()["detail"] == {"error": "version_conflict", "current_version": 2},
      r.json())
b = client.get(f"/v1/sessions/{sid}").json()
check("stale lock unchanged", b["version"] == 2 and b["locked"] is True)

# 8. unknown session on the lock route -> 404
r = lock("deadbeef", 0, True)
check("lock unknown 404", r.status_code == 404, r.status_code)

# 9. non-boolean locked -> 422 (and nothing is written)
for bad_value, name in [
    ("true", "string true"), ("false", "string false"),
    (1, "int 1"), (0, "int 0"), (None, "null"),
    (1.0, "float"), ([], "list"),
]:
    r = client.put(f"/v1/sessions/{sid}/lock",
                   json={"expected_version": 2, "locked": bad_value})
    check(f"non-boolean locked ({name}) 422", r.status_code == 422,
          (r.status_code, r.text[:120]))
for bad_body, name in [
    ({"locked": True}, "missing expected_version"),
    ({"expected_version": 2}, "missing locked"),
    ({"expected_version": 2, "locked": True, "extra": 1}, "extra field"),
    ({"expected_version": -1, "locked": True}, "negative version"),
]:
    r = client.put(f"/v1/sessions/{sid}/lock", json=bad_body)
    check(f"malformed lock body ({name}) 422", r.status_code == 422,
          (r.status_code, r.text[:120]))
b = client.get(f"/v1/sessions/{sid}").json()
check("422s changed nothing", b["version"] == 2 and b["locked"] is True)

# 10. locking an already locked session bumps the version again (flag-only write)
r = lock(sid, 2, True)
check("re-lock same value 200", r.status_code == 200, r.text)
check("re-lock version +1", r.json()["version"] == 3 and r.json()["locked"] is True)
b = client.get(f"/v1/sessions/{sid}").json()
check("re-lock keeps trace",
      [s["transition_id"] for s in b["trace"]] == ["t1"])

# 11. unlock: version +1, locked=false; afterwards the drill resumes under the
#     new version, and stale versions from before unlocking are refused.
r = lock(sid, 3, False)
check("unlock 200", r.status_code == 200, r.text)
check("unlock snapshot", r.json()["version"] == 4 and r.json()["locked"] is False)
# stale (locked-era) version cannot execute
r = run(sid, "t4", 2)
check("old version after unlock 409", r.status_code == 409)
check("old version current_version",
      r.json()["detail"]["current_version"] == 4, r.json())
# drill continues with the fresh version
r = run(sid, "t4", 4)
check("resume after unlock 200", r.status_code == 200, (r.status_code, r.text))
b = r.json()
check("resume version 5", b["version"] == 5 and b["locked"] is False)
check("resume trace", [s["transition_id"] for s in b["trace"]] == ["t1", "t4"])
check("resume completed", b["enabled_transition_ids"] == [])

# 12. lock check and write target the same committed state: same-version
#     lock-setting vs execute/replace/adoption — at most one succeeds.
def race_lock_vs(write_kind):
    target = new_session()
    source = new_session()
    run(source, "t1", 0)
    run(source, "t4", 1)  # source [t1,t4] v2
    results = []
    barrier = threading.Barrier(10)

    def lock_worker():
        barrier.wait()
        rr = lock(target, 0, True)
        results.append(("lock", rr.status_code))

    def write_worker():
        barrier.wait()
        if write_kind == "execute":
            rr = run(target, "t1", 0)
        elif write_kind == "replace":
            rr = client.put(f"/v1/sessions/{target}/protocol",
                            json={"protocol": P, "expected_version": 0})
        else:
            rr = adopt(target, source, 0, 2)
        results.append((write_kind, rr.status_code))

    threads = [threading.Thread(target=lock_worker) for _ in range(5)]
    threads += [threading.Thread(target=write_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [name for name, code in results if code in (200, 201)]
    check(f"lock vs {write_kind}: exactly one winner",
          len(winners) == 1, results)
    check(f"lock vs {write_kind}: all others 409",
          all(code == 409 for name, code in results if name != winners[0]),
          results)
    final = client.get(f"/v1/sessions/{target}").json()
    check(f"lock vs {write_kind}: version advanced once", final["version"] == 1,
          final["version"])
    if winners[0] == "lock":
        check(f"lock vs {write_kind}: winner locked", final["locked"] is True)
        check(f"lock vs {write_kind}: trace empty",
              [s["transition_id"] for s in final["trace"]] == [])
    else:
        check(f"lock vs {write_kind}: winner unlocked", final["locked"] is False)
        check(f"lock vs {write_kind}: drill moved",
              len(final["trace"]) >= 1, final["trace"])


for kind in ("execute", "replace", "adopt"):
    race_lock_vs(kind)

# 13. while locked, a failed semantic 422 path is never reached: e.g. executing
#     a bogus transition on a locked session answers 409, not the semantic 409
#     either — locked always wins as session_locked.
locked_sid = new_session()
lock(locked_sid, 0, True)
r = run(locked_sid, "nope", 1)
check("locked bogus transition 409 locked",
      r.status_code == 409 and r.json()["detail"]["error"] == "session_locked",
      (r.status_code, r.text))

# 14. locked target + stale expected version at once: version conflict wins,
#     and current_version is reported (failure changes nothing).
r = run(locked_sid, "t1", 0)
check("locked+stale version conflict",
      r.status_code == 409 and r.json()["detail"]["error"] == "version_conflict"
      and r.json()["detail"]["current_version"] == 1,
      (r.status_code, r.text))
# locked with the right version on the other hand is session_locked
r = run(locked_sid, "t1", 1)
check("locked current version locked conflict",
      r.status_code == 409 and r.json()["detail"]["error"] == "session_locked",
      (r.status_code, r.text))
check("locked sid stays at v1",
      client.get(f"/v1/sessions/{locked_sid}").json()["version"] == 1)

# 15. unlock of a locked session allows protocol replacement and adoption too
unlockable = new_session()
run(unlockable, "t1", 0)  # v1, trace [t1]
lock(unlockable, 1, True)  # v2
# replace while locked refused
r = client.put(f"/v1/sessions/{unlockable}/protocol",
               json={"protocol": P, "expected_version": 2})
check("replace locked refused",
      r.status_code == 409 and r.json()["detail"]["error"] == "session_locked")
lock(unlockable, 2, False)  # v3
r = client.put(f"/v1/sessions/{unlockable}/protocol",
               json={"protocol": P, "expected_version": 3})
check("replace after unlock 200", r.status_code == 200, (r.status_code, r.text))
check("replace after unlock v4 unlocked",
      r.json()["version"] == 4 and r.json()["locked"] is False)

# adoption into a previously locked session after unlock, using new version
adopt_tgt = new_session()
run(adopt_tgt, "t1", 0)  # v1
lock(adopt_tgt, 1, True)  # v2
r = adopt(adopt_tgt, src, 2, 2)
check("adopt locked refused",
      r.status_code == 409 and r.json()["detail"]["error"] == "session_locked")
lock(adopt_tgt, 2, False)  # v3
r = adopt(adopt_tgt, src, 3, 2)
check("adopt after unlock 200", r.status_code == 200, (r.status_code, r.text))
check("adopt after unlock trace",
      [s["transition_id"] for s in r.json()["trace"]] == ["t1", "t4"])

# 16. flag persists across "restarts": a second TestClient/app lifespan against
#     the same DB file still sees the locked row
sid2 = new_session()
lock(sid2, 0, True)
client2 = TestClient(app)
b = client2.get(f"/v1/sessions/{sid2}").json()
check("lock persists across fresh client", b["locked"] is True and b["version"] == 1)
r = client2.put(f"/v1/sessions/{sid2}/lock",
                json={"expected_version": 1, "locked": False})
check("unlock via fresh client",
      r.status_code == 200 and r.json()["locked"] is False and r.json()["version"] == 2)

# 17. lock route conflicts with no other route: /lock is distinct from
#     /protocol /branches /adoptions and works through the same id namespace
check("GET locked snapshot intact",
      client2.get(f"/v1/sessions/{sid2}").json()["locked"] is False)

# 18. offline endpoints untouched
r = client.post("/v1/deadlocks", json=P)
check("deadlocks intact", r.status_code == 200)
r = client.get("/healthz")
check("healthz intact", r.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL LOCK CHECKS PASSED")
