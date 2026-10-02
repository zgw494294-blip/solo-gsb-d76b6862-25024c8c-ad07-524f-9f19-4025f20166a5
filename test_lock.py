"""End-to-end checks for the session lock (read-only baseline) feature."""

import os
import sqlite3
import tempfile
import threading

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


def new_session():
    r = client.post("/v1/sessions", json=P)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run(sid, tid, ver):
    return client.post(f"/v1/sessions/{sid}/transitions",
                       json={"transition_id": tid, "expected_version": ver})


def lock(sid, ver, locked):
    return client.put(f"/v1/sessions/{sid}/lock",
                      json={"expected_version": ver, "locked": locked})


# 1. every session snapshot carries locked, defaulting to false
sid = new_session()
r = client.get(f"/v1/sessions/{sid}")
check("get 200", r.status_code == 200, r.status_code)
check("snapshot has locked=false", r.json().get("locked") is False, r.json())
r = run(sid, "t1", 0)
check("execute snapshot has locked=false", r.json().get("locked") is False, r.json())

# 2. non-boolean locked -> 422, nothing changes
for bad in ("true", "false", 1, 0, None, ["true"], {"locked": True}):
    r = client.put(f"/v1/sessions/{sid}/lock",
                   json={"expected_version": 1, "locked": bad})
    check(f"non-bool locked {bad!r} 422", r.status_code == 422, (r.status_code, r.text))
r = client.put(f"/v1/sessions/{sid}/lock", json={"expected_version": 1})
check("missing locked 422", r.status_code == 422, r.status_code)
r = client.put(f"/v1/sessions/{sid}/lock",
               json={"expected_version": 1, "locked": True, "extra": 1})
check("extra field 422", r.status_code == 422, r.status_code)
check("failed locks keep version",
      client.get(f"/v1/sessions/{sid}").json()["version"] == 1)

# 3. unknown session -> 404; stale version -> 409 with current_version
r = lock("deadbeef", 0, True)
check("unknown lock 404", r.status_code == 404, r.status_code)
r = lock(sid, 0, True)
check("stale lock 409", r.status_code == 409, r.status_code)
check("stale lock current_version",
      r.json().get("detail", {}).get("current_version") == 1, r.json())

# 4. successful lock: only the flag changes, version +1, full snapshot returned
before = client.get(f"/v1/sessions/{sid}").json()
r = lock(sid, 1, True)
check("lock 200", r.status_code == 200, (r.status_code, r.text))
b = r.json()
check("lock bumps version", b["version"] == 2, b)
check("lock sets flag", b["locked"] is True, b)
check("lock keeps protocol", b["protocol"] == before["protocol"])
check("lock keeps trace",
      [s["transition_id"] for s in b["trace"]] == ["t1"], b["trace"])
check("lock keeps configuration", b["configuration"] == before["configuration"])
check("lock keeps enabled ids",
      b["enabled_transition_ids"] == before["enabled_transition_ids"])

# 5. locked session rejects writes with 409 and stays untouched
r = run(sid, "t4", 2)
check("locked execute 409", r.status_code == 409, r.status_code)
check("locked execute error",
      r.json().get("detail", {}).get("error") == "session_locked", r.json())
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P, "expected_version": 2})
check("locked replace 409", r.status_code == 409, r.status_code)
check("locked replace error",
      r.json().get("detail", {}).get("error") == "session_locked", r.json())

# locked target rejects adoption
src = new_session()
run(src, "t1", 0)
run(src, "t4", 1)
r = client.post(f"/v1/sessions/{sid}/adoptions",
                json={"source_session_id": src,
                      "target_expected_version": 2,
                      "source_expected_version": 2})
check("locked target adopt 409", r.status_code == 409, (r.status_code, r.text))
check("locked target adopt error",
      r.json().get("detail", {}).get("error") == "session_locked", r.json())
check("locked target adopt names target",
      r.json().get("detail", {}).get("session") == "target", r.json())

after = client.get(f"/v1/sessions/{sid}").json()
check("locked session untouched",
      after["version"] == 2 and after["locked"] is True
      and [s["transition_id"] for s in after["trace"]] == ["t1"], after)

# 6. locked session still readable: query, compare, branch, adoption source
r = client.get(f"/v1/sessions/{sid}")
check("locked get 200", r.status_code == 200 and r.json()["locked"] is True)
r = client.post("/v1/session-comparisons",
                json={"left_session_id": sid, "right_session_id": src,
                      "left_expected_version": 2, "right_expected_version": 2})
check("locked compare 200", r.status_code == 200, (r.status_code, r.text))
r = client.post(f"/v1/sessions/{sid}/branches",
                json={"expected_version": 2, "prefix_length": 1})
check("locked branch 201", r.status_code == 201, (r.status_code, r.text))
branch = r.json()
check("branch starts unlocked", branch["locked"] is False, branch)
check("branch version 0", branch["version"] == 0)
# the branch is an ordinary session: it can execute immediately
r = run(branch["id"], "t4", 0)
check("branch executes", r.status_code == 200, (r.status_code, r.text))
# locked session as adoption source (read-only use)
tgt = new_session()
r = client.post(f"/v1/sessions/{tgt}/adoptions",
                json={"source_session_id": src,
                      "target_expected_version": 0,
                      "source_expected_version": 2})
check("adopt from unlocked source 200", r.status_code == 200, (r.status_code, r.text))
lock(src, 2, True)
tgt2 = new_session()
r = client.post(f"/v1/sessions/{tgt2}/adoptions",
                json={"source_session_id": src,
                      "target_expected_version": 0,
                      "source_expected_version": 3})
check("locked source adopt 200", r.status_code == 200, (r.status_code, r.text))
check("locked source trace adopted",
      [s["transition_id"] for s in r.json()["trace"]] == ["t1", "t4"], r.json())

# 7. re-locking an already locked session still bumps only the version
r = lock(sid, 2, True)
check("relock 200", r.status_code == 200, (r.status_code, r.text))
check("relock bumps version", r.json()["version"] == 3 and r.json()["locked"] is True)

# 8. unlock, then continue the drill with the new version
r = lock(sid, 3, False)
check("unlock 200", r.status_code == 200, (r.status_code, r.text))
check("unlock clears flag", r.json()["locked"] is False and r.json()["version"] == 4)
r = run(sid, "t4", 3)
check("stale version after unlock 409", r.status_code == 409, r.status_code)
r = run(sid, "t4", 4)
check("execute after unlock 200", r.status_code == 200, (r.status_code, r.text))
check("drill completed after unlock",
      r.json()["version"] == 5 and r.json()["enabled_transition_ids"] == [], r.json())

# 9. same version: lock vs execute race — exactly one commits
race = new_session()
results = []
barrier = threading.Barrier(6)


def lock_worker():
    barrier.wait()
    results.append(("lock", lock(race, 0, True).status_code))


def exec_worker():
    barrier.wait()
    results.append(("exec", run(race, "t1", 0).status_code))


threads = [threading.Thread(target=lock_worker) for _ in range(3)]
threads += [threading.Thread(target=exec_worker) for _ in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()
codes = [code for _, code in results]
check("lock/exec race exactly one 200", codes.count(200) == 1, results)
check("lock/exec race rest 409", codes.count(409) == 5, results)
final = client.get(f"/v1/sessions/{race}").json()
check("race version advanced once", final["version"] == 1, final)
if any(kind == "lock" and code == 200 for kind, code in results):
    check("race: lock won -> locked", final["locked"] is True, final)
    check("race: lock won -> empty trace", final["trace"] == [], final)
else:
    check("race: exec won -> unlocked", final["locked"] is False, final)
    check("race: exec won -> trace [t1]",
          [s["transition_id"] for s in final["trace"]] == ["t1"], final)

# 10. same version: lock vs adoption-target race — exactly one commits
asrc = new_session()
run(asrc, "t1", 0)
atgt = new_session()
results = []
barrier = threading.Barrier(4)


def adopt_worker():
    barrier.wait()
    rr = client.post(f"/v1/sessions/{atgt}/adoptions",
                     json={"source_session_id": asrc,
                           "target_expected_version": 0,
                           "source_expected_version": 1})
    results.append(("adopt", rr.status_code))


def lock_worker2():
    barrier.wait()
    results.append(("lock", lock(atgt, 0, True).status_code))


threads = [threading.Thread(target=adopt_worker) for _ in range(2)]
threads += [threading.Thread(target=lock_worker2) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
codes = [code for _, code in results]
check("lock/adopt race exactly one 200", codes.count(200) == 1, results)
check("lock/adopt race rest 409", codes.count(409) == 3, results)
check("lock/adopt race version 1",
      client.get(f"/v1/sessions/{atgt}").json()["version"] == 1)

# 11. startup upgrade: a pre-lock database gains the column, data untouched
import app.sessions as sessions_module

old_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
old_db.close()
connection = sqlite3.connect(old_db.name)
connection.execute(
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, version INTEGER NOT NULL,"
    " protocol TEXT NOT NULL, trace_ids TEXT NOT NULL)"
)
import json as _json
protocol_text = _json.dumps(P)
connection.execute(
    "INSERT INTO sessions (id, version, protocol, trace_ids) VALUES (?, ?, ?, ?)",
    ("legacy", 7, protocol_text, _json.dumps(["t1"])),
)
connection.commit()
connection.close()

original_path = sessions_module._DB_PATH
original_initialized = sessions_module._initialized
try:
    sessions_module._DB_PATH = old_db.name
    sessions_module._initialized = False
    sessions_module.init_storage()
    connection = sqlite3.connect(old_db.name)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    check("upgrade adds locked column", "locked" in columns, columns)
    row = connection.execute(
        "SELECT version, protocol, trace_ids, locked FROM sessions WHERE id = 'legacy'"
    ).fetchone()
    connection.close()
    check("upgrade keeps version", row[0] == 7, row)
    check("upgrade keeps protocol", row[1] == protocol_text)
    check("upgrade keeps trace", row[2] == '["t1"]', row)
    check("upgrade defaults unlocked", row[3] == 0, row)
    # the upgraded row is served as an ordinary unlocked session
    view = sessions_module.get_session_view("legacy")
    check("legacy session view unlocked", view["locked"] is False, view)
    check("legacy session version kept", view["version"] == 7, view)
    check("legacy session trace kept",
          [s["transition_id"] for s in view["trace"]] == ["t1"], view)
finally:
    sessions_module._DB_PATH = original_path
    sessions_module._initialized = original_initialized
    os.unlink(old_db.name)

# 12. old endpoints still work
r = client.get("/healthz")
check("healthz intact", r.status_code == 200)
r = client.post("/v1/deadlocks", json=P)
check("deadlocks intact", r.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL LOCK CHECKS PASSED")
