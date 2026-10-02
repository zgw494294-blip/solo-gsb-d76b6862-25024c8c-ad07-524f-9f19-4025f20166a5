"""SQLite-backed stateful protocol-drill sessions.

A session binds one validated protocol to an execution trace: the ordered list
of transition ids already fired, replayed from the protocol's initial
configuration. Replacing the protocol keeps the same trace ids and replays
them against the new transition definitions, which lets a drill continue after
the protocol is edited.

Sessions live in a local SQLite database (env ``SESSIONS_DB``, default
``sessions.db`` in the working directory) holding the protocol, the trace ids
and the version of every session. All worker processes share the same file, so
a session survives service or container rebuilds: it can be queried by its
original id and drilled further, and its version never rolls back.

Every mutation runs inside one ``BEGIN IMMEDIATE`` transaction: the version
check, the trace replay and the write all observe the same committed state, so
two processes modifying one session with the same expected version cannot both
succeed — the loser re-reads the stored version and answers 409. A failed
mutation raises before commit and leaves the stored session untouched. Reads
observe one committed version at a time, and a successful response is built
from the values its own transaction committed, never from later commits.

A session can also be branched: a new independent session is created from the
source's current protocol and a prefix of its trace. The version check and the
copy run inside one immediate transaction against the same source snapshot, so
a concurrent source modification either commits first (the branch answers 409)
or waits until the expected version has been copied whole. The source row is
never written, so its version is not consumed and the same version can be
branched repeatedly; a failed branch creates nothing.

Finally, the continuation already validated in another drill session can be
adopted back into a target session instead of replaying it step by step. The
target's trace id sequence must be a strict prefix of the source's and both
snapshots must carry exactly the same protocol; only the source's remaining
suffix is appended to the target (the source is never written), and the target
version advances by one. Both version reads, the protocol and prefix checks
and the target write run inside one ``BEGIN IMMEDIATE`` transaction against the
same committed snapshot, so concurrent modification of either session ends in
either accepting that exact complete outcome or answering 409 — acceptance,
execution and protocol replacement on one target version are mutually
exclusive. Any failure raises before commit and leaves both sessions untouched.

Two sessions can also be compared without writing anything. Each stored trace
is replayed against that session's own (possibly different) current protocol,
and every step is projected onto its message direction as an ``(action,
message)`` pair — A→B and B→A each keep their own order, while transition ids,
endpoint states/queues and the interleaving between the two directions are
ignored. Both rows are read inside one read transaction, so the two snapshots
always belong to the same committed state: when either named expected version
is stale the comparison answers 409 instead of mixing revisions; an unknown id
answers 404.

A session can be locked into a read-only baseline. Setting the lock flag is
itself a versioned mutation: it carries an ``expected_version``, changes only
the flag, advances the version by one and runs inside the same kind of
``BEGIN IMMEDIATE`` transaction as every other mutation, so a lock request and
an execution, protocol replacement or adoption submitted for the same version
cannot both succeed. While locked, a session rejects transition execution,
protocol replacement and adoption as a target with 409; it can still be
queried, compared, branched (the branch starts unlocked) and used as an
adoption source. Unlocking is the same mutation with ``locked: false`` and
lets the drill continue from the new version. The flag is persisted in its own
column; databases created before the column existed are upgraded at startup by
adding it with a default of unlocked, leaving every stored protocol, trace and
version untouched.
"""

import json
import os
import sqlite3
import threading
from uuid import uuid4

from fastapi import HTTPException

from .analyzer import (
    apply_transition,
    build_model,
    enabled_transitions,
    is_completion,
    _configuration_view,
)
from .schemas import DeadlockRequest

_DB_PATH = os.environ.get("SESSIONS_DB", "sessions.db")
_BUSY_TIMEOUT_MS = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    protocol TEXT NOT NULL,
    trace_ids TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0
)
"""

_init_lock = threading.Lock()
_initialized = False


def init_storage() -> None:
    """Create the database directory, file and schema once per process.

    Databases written before the ``locked`` column existed are upgraded in
    place: the column is added with a default of 0 (unlocked), so every stored
    protocol, trace and version is kept exactly as it was.
    """
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        directory = os.path.dirname(os.path.abspath(_DB_PATH))
        os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(_DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            # WAL persists in the database file and lets readers work
            # concurrently with the single writer across processes.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(_SCHEMA)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "locked" not in columns:
                try:
                    connection.execute(
                        "ALTER TABLE sessions "
                        "ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    # Another worker may have added the column first; only a
                    # genuinely missing column is an error.
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(sessions)")
                    }
                    if "locked" not in columns:
                        raise
            connection.commit()
        finally:
            connection.close()
        _initialized = True


def _connect() -> sqlite3.Connection:
    init_storage()
    connection = sqlite3.connect(
        _DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    # Transactions are opened explicitly below; writers that collide with
    # another process wait instead of failing with "database is locked".
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return connection


def create_session(protocol) -> dict:
    """Validate the protocol by building its model, then store it."""
    build_model(protocol)
    session_id = uuid4().hex
    connection = _connect()
    try:
        connection.execute(
            "INSERT INTO sessions (id, version, protocol, trace_ids, locked)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, 0, _dump_protocol(protocol), "[]", 0),
        )
    finally:
        connection.close()
    return _session_view(session_id, 0, protocol, [], False)


def get_session_view(session_id: str) -> dict:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT version, protocol, trace_ids, locked FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown session")
    version, protocol_text, trace_text, locked = row
    return _session_view(
        session_id,
        version,
        DeadlockRequest.model_validate(json.loads(protocol_text)),
        json.loads(trace_text),
        bool(locked),
    )


def execute_transition(session_id: str, transition_id: str, expected_version: int) -> dict:
    def mutate(version: int, protocol, trace_ids: list[str], locked: bool):
        if version != expected_version:
            raise _version_conflict(version)
        if locked:
            raise _session_locked(session_id)

        model = build_model(protocol)
        configuration = _replay(model, trace_ids)

        if is_completion(configuration, model.finals):
            raise HTTPException(
                status_code=409,
                detail={"error": "session_completed", "transition_id": transition_id},
            )

        enabled = enabled_transitions(configuration, model.outgoing)
        if not any(transition.id == transition_id for transition in enabled):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "transition_not_enabled",
                    "transition_id": transition_id,
                    "enabled_transition_ids": [transition.id for transition in enabled],
                },
            )

        return version + 1, protocol, trace_ids + [transition_id], locked

    return _update_session(session_id, mutate)


def replace_protocol(session_id: str, protocol, expected_version: int) -> dict:
    def mutate(version: int, _stored_protocol, trace_ids: list[str], locked: bool):
        if version != expected_version:
            raise _version_conflict(version)
        if locked:
            raise _session_locked(session_id)

        # Replay the already executed ids from the new initial configuration.
        # Every step must be enabled and the trace must not reach a completion
        # configuration before its last step; otherwise the old session stands.
        model = build_model(protocol)
        by_id = {transition.id: transition for transition in model.transitions}

        configuration = model.initial
        for index, transition_id in enumerate(trace_ids, start=1):
            if is_completion(configuration, model.finals):
                raise _replay_failure(index, "configuration already completed")
            transition = by_id.get(transition_id)
            if transition is None:
                raise _replay_failure(index, f"unknown transition id {transition_id!r}")
            if transition not in enabled_transitions(configuration, model.outgoing):
                raise _replay_failure(index, f"transition {transition_id!r} is not enabled")
            configuration = apply_transition(configuration, transition)

        return version + 1, protocol, trace_ids, locked

    return _update_session(session_id, mutate)


def set_session_lock(session_id: str, expected_version: int, locked: bool) -> dict:
    """Set the session's lock flag; only the flag and the version change.

    The version check and the flag write run inside the same immediate
    transaction as every other mutation, so a lock request submitted for one
    version is mutually exclusive with an execution, a protocol replacement or
    an adoption targeting that same version: exactly one of them commits and
    the rest answer 409. A stale ``expected_version`` answers 409 with the
    current version and an unknown session 404; any failure leaves the stored
    session untouched.
    """
    def mutate(version: int, protocol, trace_ids: list[str], _locked: bool):
        if version != expected_version:
            raise _version_conflict(version)
        return version + 1, protocol, trace_ids, locked

    return _update_session(session_id, mutate)


def branch_session(session_id: str, expected_version: int, prefix_length: int) -> dict:
    """Create an independent session from a prefix of the source trace.

    The version check and the copy run inside one immediate transaction, so a
    concurrent modification of the source either commits first (the branch
    then observes the new version and answers 409) or waits until the whole
    snapshot of the expected version has been copied. The source row is never
    written: its version is not consumed and the same version can be branched
    repeatedly. Any failure raises before commit and creates no branch.

    The branch starts at version 0 with the source's current (possibly
    replaced) protocol; its configuration and enabled transitions are
    recomputed by replaying the prefix from the initial configuration, never
    copied from the source's final state. A locked source can still be
    branched, and the new branch always starts unlocked.
    """
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT version, protocol, trace_ids FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")
        version, protocol_text, trace_text = row
        if version != expected_version:
            raise _version_conflict(version)
        trace_ids = json.loads(trace_text)
        if not 0 <= prefix_length <= len(trace_ids):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_prefix_length",
                    "prefix_length": prefix_length,
                    "trace_length": len(trace_ids),
                },
            )

        branch_id = uuid4().hex
        branch_trace_ids = trace_ids[:prefix_length]
        connection.execute(
            "INSERT INTO sessions (id, version, protocol, trace_ids, locked)"
            " VALUES (?, ?, ?, ?, ?)",
            (branch_id, 0, protocol_text, json.dumps(branch_trace_ids), 0),
        )
        connection.commit()
        protocol = DeadlockRequest.model_validate(json.loads(protocol_text))
        return _session_view(branch_id, 0, protocol, branch_trace_ids, False)
    except BaseException:
        # No-op when the commit already succeeded.
        connection.rollback()
        raise
    finally:
        connection.close()


def adopt_trace(
    target_session_id: str,
    source_session_id: str,
    target_expected_version: int,
    source_expected_version: int,
) -> dict:
    """Adopt the source session's remaining trace suffix into the target.

    The target trace id sequence must be a strict prefix of the source trace
    (the source must actually continue it), and both sessions must carry the
    exact same protocol, including list order. Only the source suffix beyond
    the target trace is appended; the source row is never written.

    Both rows are read inside one ``BEGIN IMMEDIATE`` transaction, so the two
    version checks, the lock, equality and prefix checks and the target write
    all observe the same committed snapshot: when either session is modified
    concurrently the request either accepts the whole outcome of the named
    versions or is rejected as stale. Acceptance therefore cannot both win
    against an execution, a protocol replacement or a lock change submitted
    for the same target version. A locked target rejects the adoption with
    409; a locked source is only read, so it can still hand out its trace.
    Any failure raises before commit and writes nothing.
    """
    # A session cannot adopt from itself; this is rejected before any I/O.
    if source_session_id == target_session_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "same_session",
                "session_id": target_session_id,
            },
        )

    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        target_row = connection.execute(
            "SELECT version, protocol, trace_ids, locked FROM sessions WHERE id = ?",
            (target_session_id,),
        ).fetchone()
        if target_row is None:
            raise HTTPException(status_code=404, detail="unknown session")
        source_row = connection.execute(
            "SELECT version, protocol, trace_ids FROM sessions WHERE id = ?",
            (source_session_id,),
        ).fetchone()
        if source_row is None:
            raise HTTPException(status_code=404, detail="unknown session")

        target_version, target_protocol_text, target_trace_text, target_locked = target_row
        source_version, source_protocol_text, source_trace_text = source_row

        # Version checks precede the semantic ones: a stale acceptance is
        # always a conflict, never a protocol/prefix mismatch.
        if target_version != target_expected_version:
            raise _version_conflict(
                target_version, session="target", session_id=target_session_id
            )
        if source_version != source_expected_version:
            raise _version_conflict(
                source_version, session="source", session_id=source_session_id
            )
        if target_locked:
            raise _session_locked(target_session_id, session="target")

        # The stored text is canonical JSON of the validated protocol, so
        # string equality covers every field value as well as the order of the
        # state, final and transition lists.
        if target_protocol_text != source_protocol_text:
            raise HTTPException(
                status_code=422,
                detail={"error": "protocol_mismatch"},
            )

        target_trace_ids = json.loads(target_trace_text)
        source_trace_ids = json.loads(source_trace_text)
        prefix_length = len(target_trace_ids)

        first_divergent_step = next(
            (
                index
                for index, transition_id in enumerate(target_trace_ids, start=1)
                if index > len(source_trace_ids)
                or source_trace_ids[index - 1] != transition_id
            ),
            None,
        )
        if first_divergent_step is not None or prefix_length >= len(source_trace_ids):
            # Strict prefix: the shared ids must match and the source must
            # still have at least one unadopted step (equal lengths, including
            # two empty traces, are rejected too).
            detail = {
                "error": "trace_not_strict_prefix",
                "target_trace_length": prefix_length,
                "source_trace_length": len(source_trace_ids),
            }
            if first_divergent_step is not None:
                detail["first_divergent_step"] = first_divergent_step
            raise HTTPException(status_code=422, detail=detail)

        # Only the suffix is adopted: the target keeps its own prefix (equal
        # to the source prefix by the check above) followed by the source's
        # remaining steps; the source row is never written.
        new_trace_ids = target_trace_ids + source_trace_ids[prefix_length:]

        new_version = target_version + 1
        connection.execute(
            "UPDATE sessions SET version = ?, trace_ids = ? WHERE id = ?",
            (new_version, json.dumps(new_trace_ids), target_session_id),
        )
        connection.commit()
        protocol = DeadlockRequest.model_validate(json.loads(target_protocol_text))
        return _session_view(
            target_session_id, new_version, protocol, new_trace_ids, bool(target_locked)
        )
    except BaseException:
        # No-op when the commit already succeeded.
        connection.rollback()
        raise
    finally:
        connection.close()


def compare_sessions(
    left_session_id: str,
    right_session_id: str,
    left_expected_version: int,
    right_expected_version: int,
) -> dict:
    """Compare two sessions by their per-direction action/message streams.

    Each trace is replayed against the row's own current protocol, and every
    step becomes an ``(action, message)`` event in the direction of its
    message flow (A→B or B→A), preserving the original order within one
    direction. Transition ids, endpoint states, queues and the interleaving
    between the two directions never enter the comparison; the two sessions
    may therefore run entirely different protocols and may even be the same
    id.

    Both rows are read inside one read transaction. In WAL mode a read
    transaction observes one consistent committed snapshot for every
    statement, so the two rows always come from the same committed state even
    when another worker commits mid-request: the response is either the
    complete comparison at the two named versions or a 409 naming the stale
    side (checked left first), never a mix of revisions. The comparison does
    not write and consumes no version.
    """
    connection = _connect()
    try:
        # A plain (deferred) transaction takes no write lock; both SELECTs
        # share the one read snapshot established by the first read.
        connection.execute("BEGIN")
        left_row = connection.execute(
            "SELECT version, protocol, trace_ids FROM sessions WHERE id = ?",
            (left_session_id,),
        ).fetchone()
        right_row = connection.execute(
            "SELECT version, protocol, trace_ids FROM sessions WHERE id = ?",
            (right_session_id,),
        ).fetchone()

        # Unknown sessions take precedence over staleness (same convention as
        # the mutating endpoints); the left side is reported first.
        if left_row is None or right_row is None:
            raise HTTPException(status_code=404, detail="unknown session")

        left_version, left_protocol_text, left_trace_text = left_row
        right_version, right_protocol_text, right_trace_text = right_row

        if left_version != left_expected_version:
            raise _comparison_conflict(
                left_version, side="left", session_id=left_session_id
            )
        if right_version != right_expected_version:
            raise _comparison_conflict(
                right_version, side="right", session_id=right_session_id
            )

        left_events = _direction_events(
            DeadlockRequest.model_validate(json.loads(left_protocol_text)),
            json.loads(left_trace_text),
        )
        right_events = _direction_events(
            DeadlockRequest.model_validate(json.loads(right_protocol_text)),
            json.loads(right_trace_text),
        )

        return {
            "left_session_id": left_session_id,
            "right_session_id": right_session_id,
            "left_version": left_version,
            "right_version": right_version,
            **{
                direction: _compare_directions(
                    left_events[direction], right_events[direction]
                )
                for direction in ("A_to_B", "B_to_A")
            },
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _direction_events(protocol, trace_ids: list[str]) -> dict[str, list[dict]]:
    """Replay the trace under this protocol and project it onto directions.

    The trace is one the session already validated under this protocol, so
    every stored id exists and every step is fireable; replay only resolves
    the observable action/message pair and the message direction of each
    step. Only ``from``/``to`` decide the bucket — a step that sends A→B and
    a later step receiving the same message both land in ``A_to_B``.
    """
    model = build_model(protocol)
    by_id = {transition.id: transition for transition in model.transitions}

    events: dict[str, list[dict]] = {"A_to_B": [], "B_to_A": []}
    for index, transition_id in enumerate(trace_ids, start=1):
        transition = by_id[transition_id]
        direction = (
            "A_to_B"
            if (transition.sender, transition.receiver) == ("A", "B")
            else "B_to_A"
        )
        events[direction].append(
            {
                "action": transition.action,
                "message": transition.message,
                "trace_step": index,
                "transition_id": transition.id,
            }
        )
    return events


def _compare_directions(left: list[dict], right: list[dict]) -> dict:
    """Compare two one-direction event streams by action/message pairs only."""
    shared = min(len(left), len(right))
    first_different = next(
        (
            index
            for index in range(shared)
            if (left[index]["action"], left[index]["message"])
            != (right[index]["action"], right[index]["message"])
        ),
        None,
    )

    if first_different is not None:
        relation = "divergent"
        difference = _difference_view(first_different + 1,
                                      left[first_different],
                                      right[first_different])
    elif len(left) == len(right):
        relation = "equal"
        difference = None
    else:
        # All shared pairs match and the lengths differ: the shorter stream is
        # a strict prefix. The first difference is the longer side's next
        # event; the missing side reports null.
        position = shared + 1
        if len(right) > len(left):
            relation = "left_is_strict_prefix"
            difference = _difference_view(position, None, right[shared])
        else:
            relation = "right_is_strict_prefix"
            difference = _difference_view(position, left[shared], None)

    return {
        "left_event_count": len(left),
        "right_event_count": len(right),
        "relation": relation,
        "first_difference": difference,
    }


def _difference_view(position: int, left: dict | None, right: dict | None) -> dict:
    return {"position": position, "left": left, "right": right}


def _comparison_conflict(current_version: int, side: str,
                         session_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "version_conflict",
            "current_version": current_version,
            "side": side,
            "session_id": session_id,
        },
    )


def _update_session(session_id: str, mutate) -> dict:
    """Apply ``mutate`` to the stored row inside one immediate transaction.

    ``mutate(version, protocol, trace_ids, locked)`` returns the replacement
    quadruple; the transaction commits only when it returns, so any
    HTTPException it raises leaves the stored session untouched.
    ``BEGIN IMMEDIATE`` takes the database write lock before the row is read,
    so the version check, the lock check, the trace replay and the write all
    run against the same committed state even when another process races the
    same session. The returned view is built from the values this transaction
    committed, never re-read afterwards.
    """
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT version, protocol, trace_ids, locked FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")
        version, protocol_text, trace_text, locked = row
        protocol = DeadlockRequest.model_validate(json.loads(protocol_text))
        trace_ids = json.loads(trace_text)

        new_version, new_protocol, new_trace_ids, new_locked = mutate(
            version, protocol, trace_ids, bool(locked)
        )

        connection.execute(
            "UPDATE sessions SET version = ?, protocol = ?, trace_ids = ?, locked = ?"
            " WHERE id = ?",
            (
                new_version,
                _dump_protocol(new_protocol),
                json.dumps(new_trace_ids),
                int(new_locked),
                session_id,
            ),
        )
        connection.commit()
        return _session_view(
            session_id, new_version, new_protocol, new_trace_ids, new_locked
        )
    except BaseException:
        # No-op when the commit already succeeded.
        connection.rollback()
        raise
    finally:
        connection.close()


def _dump_protocol(protocol) -> str:
    return json.dumps(protocol.model_dump(mode="json", by_alias=True))


def _version_conflict(current_version: int, session: str | None = None,
                      session_id: str | None = None) -> HTTPException:
    detail = {"error": "version_conflict", "current_version": current_version}
    # Adoptions verify two sessions; the extra fields name the stale one.
    if session is not None:
        detail["session"] = session
        detail["session_id"] = session_id
    return HTTPException(status_code=409, detail=detail)


def _session_locked(session_id: str, session: str | None = None) -> HTTPException:
    detail = {"error": "session_locked", "session_id": session_id}
    # An adoption names the locked side ("target") like its version conflicts.
    if session is not None:
        detail["session"] = session
    return HTTPException(status_code=409, detail=detail)


def _replay_failure(index: int, reason: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "replay_failed",
            "first_failed_step": index,
            "reason": reason,
        },
    )


def _replay(model, trace_ids: list[str]):
    """Apply the trace ids; the trace is assumed valid for this model."""
    by_id = {transition.id: transition for transition in model.transitions}
    configuration = model.initial
    for transition_id in trace_ids:
        configuration = apply_transition(configuration, by_id[transition_id])
    return configuration


def _session_view(session_id: str, version: int, protocol, trace_ids: list[str],
                  locked: bool) -> dict:
    # Every value comes from one stored revision, so protocol, configuration,
    # trace, lock flag and version always belong to the same committed
    # snapshot.
    model = build_model(protocol)
    by_id = {transition.id: transition for transition in model.transitions}

    configuration = model.initial
    trace = []
    for index, transition_id in enumerate(trace_ids, start=1):
        transition = by_id[transition_id]
        configuration = apply_transition(configuration, transition)
        trace.append(
            {
                "index": index,
                "transition_id": transition.id,
                "from": transition.sender,
                "to": transition.receiver,
                "action": transition.action,
                "message": transition.message,
                **_configuration_view(configuration),
            }
        )

    return {
        "id": session_id,
        "version": version,
        "locked": locked,
        "protocol": protocol,
        "configuration": _configuration_view(configuration),
        "trace": trace,
        "enabled_transition_ids": [
            transition.id
            for transition in enabled_transitions(configuration, model.outgoing)
        ],
    }
