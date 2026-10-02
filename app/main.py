from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import sessions
from .analyzer import analyze, analyze_guarantee, plan_completion, plan_deactivation
from .schemas import (
    AdoptionRequest,
    BranchRequest,
    CompletionRequest,
    CompletionResponse,
    DeadlockRequest,
    DeadlockResponse,
    DeactivationRequest,
    DeactivationResponse,
    ExecutionRequest,
    GuaranteeRequest,
    GuaranteeResponse,
    ProtocolReplaceRequest,
    SessionComparisonRequest,
    SessionComparisonResponse,
    SessionCreateRequest,
    SessionView,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the session database (directory, file, schema) before serving;
    # every worker process runs this, and the schema creation is idempotent.
    sessions.init_storage()
    yield


app = FastAPI(
    title="Offline Two-Party Message Protocol Deadlock Analyzer",
    version="1.6.0",
    lifespan=lifespan,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/deadlocks", response_model=DeadlockResponse)
def detect_deadlocks(request: DeadlockRequest) -> dict:
    return analyze(request)


@app.post("/v1/completion-plans", response_model=CompletionResponse)
def plan_completion_endpoint(request: CompletionRequest) -> dict:
    return plan_completion(request)


@app.post("/v1/deactivation-plans", response_model=DeactivationResponse)
def plan_deactivation_endpoint(request: DeactivationRequest) -> dict:
    return plan_deactivation(request)


@app.post("/v1/completion-guarantees", response_model=GuaranteeResponse)
def analyze_completion_guarantee(request: GuaranteeRequest) -> dict:
    return analyze_guarantee(request)


# ---------------------------------------------------------------------------
# Stateful protocol-drill sessions
# ---------------------------------------------------------------------------


@app.post("/v1/sessions", response_model=SessionView, status_code=201)
def create_session(request: SessionCreateRequest) -> dict:
    return sessions.create_session(request)


@app.get("/v1/sessions/{session_id}", response_model=SessionView)
def read_session(session_id: str) -> dict:
    return sessions.get_session_view(session_id)


@app.post("/v1/sessions/{session_id}/transitions", response_model=SessionView)
def execute_session_transition(session_id: str, request: ExecutionRequest) -> dict:
    return sessions.execute_transition(
        session_id, request.transition_id, request.expected_version
    )


@app.put("/v1/sessions/{session_id}/protocol", response_model=SessionView)
def replace_session_protocol(session_id: str, request: ProtocolReplaceRequest) -> dict:
    return sessions.replace_protocol(
        session_id, request.protocol, request.expected_version
    )


@app.post("/v1/sessions/{session_id}/branches", response_model=SessionView, status_code=201)
def branch_session(session_id: str, request: BranchRequest) -> dict:
    return sessions.branch_session(
        session_id, request.expected_version, request.prefix_length
    )


@app.post("/v1/sessions/{session_id}/adoptions", response_model=SessionView)
def adopt_session_trace(session_id: str, request: AdoptionRequest) -> dict:
    return sessions.adopt_trace(
        session_id,
        request.source_session_id,
        request.target_expected_version,
        request.source_expected_version,
    )


@app.post("/v1/session-comparisons", response_model=SessionComparisonResponse)
def compare_session_traces(request: SessionComparisonRequest) -> dict:
    return sessions.compare_sessions(
        request.left_session_id,
        request.right_session_id,
        request.left_expected_version,
        request.right_expected_version,
    )
