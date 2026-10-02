from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, WrapValidator, model_validator
from typing_extensions import Self


def _validate_state_id(value: Any, handler: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("state id must be an integer or string")
    return handler(value)


NonEmptyId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
StateId = Annotated[
    Union[int, NonEmptyId],
    WrapValidator(_validate_state_id),
]


class ProcessInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    states: Annotated[list[StateId], Field(min_length=1, max_length=8)]
    initial: StateId
    finals: Annotated[list[StateId], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def validate_states(self) -> Self:
        if len(set(self.states)) != len(self.states):
            raise ValueError("states must not contain duplicate values")
        if self.initial not in self.states:
            raise ValueError("initial state must be one of states")
        if len(set(self.finals)) != len(self.finals):
            raise ValueError("finals must not contain duplicate values")
        unknown = [state for state in self.finals if state not in self.states]
        if unknown:
            raise ValueError("every final state must be one of states")
        return self


class TransitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: NonEmptyId
    sender: Literal["A", "B"] = Field(alias="from")
    receiver: Literal["A", "B"] = Field(alias="to")
    action: Literal["send", "receive"]
    message: NonEmptyId
    source: StateId
    target: StateId

    @model_validator(mode="after")
    def validate_endpoints(self) -> Self:
        if self.sender == self.receiver:
            raise ValueError("from and to must be different endpoints")
        return self


class DeadlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    A: ProcessInput
    B: ProcessInput
    transitions: Annotated[list[TransitionInput], Field(max_length=30)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_transitions(self) -> Self:
        ids = [transition.id for transition in self.transitions]
        if len(set(ids)) != len(ids):
            raise ValueError("transition ids must be globally unique")

        messages = {transition.message for transition in self.transitions}
        if len(messages) > 8:
            raise ValueError("there can be at most 8 distinct message types")

        processes = {"A": self.A, "B": self.B}
        for transition in self.transitions:
            owner = transition.sender if transition.action == "send" else transition.receiver
            process = processes[owner]
            if transition.source not in process.states:
                raise ValueError(
                    f"transition {transition.id!r} source is not a state of endpoint {owner}"
                )
            if transition.target not in process.states:
                raise ValueError(
                    f"transition {transition.id!r} target is not a state of endpoint {owner}"
                )
        return self


class CompletionRequest(DeadlockRequest):
    required_transition_ids: Annotated[list[NonEmptyId], Field(min_length=0, max_length=6)] = Field(
        default_factory=list
    )
    @model_validator(mode="after")
    def validate_required_transition_ids(self) -> Self:
        if len(set(self.required_transition_ids)) != len(self.required_transition_ids):
            raise ValueError("required_transition_ids must not contain duplicates")
        known = {transition.id for transition in self.transitions}
        unknown = [transition_id for transition_id in self.required_transition_ids if transition_id not in known]
        if unknown:
            raise ValueError(f"unknown required_transition_ids: {unknown}")
        return self


class DeactivationRequest(DeadlockRequest):
    removable_transition_ids: Annotated[list[NonEmptyId], Field(min_length=0, max_length=8)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_removable_transition_ids(self) -> Self:
        if len(set(self.removable_transition_ids)) != len(self.removable_transition_ids):
            raise ValueError("removable_transition_ids must not contain duplicates")
        known = {transition.id for transition in self.transitions}
        unknown = [transition_id for transition_id in self.removable_transition_ids if transition_id not in known]
        if unknown:
            raise ValueError(f"unknown removable_transition_ids: {unknown}")
        return self


# The completion-guarantee endpoint accepts the existing protocol request
# unchanged; an alias keeps the endpoint self-documenting.
GuaranteeRequest = DeadlockRequest


class StateView(BaseModel):
    A: StateId
    B: StateId


class QueuesView(BaseModel):
    A_to_B: str | None
    B_to_A: str | None


class ConfigurationView(BaseModel):
    state: StateView
    queues: QueuesView


class PathStep(BaseModel):
    index: int
    transition_id: str
    from_: Literal["A", "B"] = Field(alias="from")
    to: Literal["A", "B"]
    action: Literal["send", "receive"]
    message: str
    state: StateView
    queues: QueuesView

    model_config = ConfigDict(populate_by_name=True)


class DeadlockResponse(BaseModel):
    deadlock: bool
    visited_states: int
    initial: ConfigurationView
    deadlock_state: ConfigurationView | None = None
    path: list[PathStep] | None = None


class CompletionResponse(BaseModel):
    completable: bool
    visited_combinations: int
    initial: ConfigurationView
    path: list[PathStep] | None = None


class DeactivationResponse(BaseModel):
    solvable: bool
    deactivated_transition_ids: list[str] | None = None
    reachable_configurations: int | None = None


class DeadlockWitness(BaseModel):
    kind: Literal["deadlock"]
    deadlock_state: ConfigurationView
    path: list[PathStep]


class CycleWitness(BaseModel):
    kind: Literal["cycle"]
    entry_state: ConfigurationView
    prefix: list[PathStep]
    cycle: list[PathStep]


GuaranteeWitness = Annotated[
    Union[DeadlockWitness, CycleWitness],
    Field(discriminator="kind"),
]


class GuaranteeResponse(BaseModel):
    result: Literal["guaranteed", "deadlock", "cycle"]
    visited_states: int
    initial: ConfigurationView
    witness: GuaranteeWitness | None = None


# ---------------------------------------------------------------------------
# Stateful protocol-drill sessions
# ---------------------------------------------------------------------------


# A session is created with exactly the same protocol payload as the offline
# analysis endpoints, so their validation (unique ids, state references, message
# limits, ...) is reused unchanged.
SessionCreateRequest = DeadlockRequest


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transition_id: NonEmptyId
    expected_version: Annotated[int, Field(ge=0)]


class ProtocolReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    protocol: DeadlockRequest
    expected_version: Annotated[int, Field(ge=0)]


def _validate_prefix_length(value: Any, handler: Any) -> Any:
    # Booleans are ints in Python and would otherwise be coerced silently.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("prefix_length must be an integer")
    return handler(value)


class BranchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    expected_version: Annotated[int, Field(ge=0)]
    # The upper bound (the source session's current trace length) is checked
    # against the stored snapshot when the branch is created.
    prefix_length: Annotated[int, Field(ge=0), WrapValidator(_validate_prefix_length)]


class AdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # The other drill session whose already validated continuation is adopted
    # into the target session named by the path.
    source_session_id: NonEmptyId
    target_expected_version: Annotated[int, Field(ge=0)]
    source_expected_version: Annotated[int, Field(ge=0)]


class SessionComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Two sessions compared purely by the per-direction action/message streams
    # obtained by replaying their own current protocols. The same id may be
    # named twice (a session compares equal to itself), and the two protocols
    # may differ entirely — transition ids, states and queues never take part.
    left_session_id: NonEmptyId
    right_session_id: NonEmptyId
    left_expected_version: Annotated[int, Field(ge=0)]
    right_expected_version: Annotated[int, Field(ge=0)]


class SessionView(BaseModel):
    id: str
    version: int
    protocol: DeadlockRequest
    configuration: ConfigurationView
    trace: list[PathStep]
    enabled_transition_ids: list[str]


class ComparisonEvent(BaseModel):
    # One element of a per-direction event stream: only the observable message
    # exchange (the action and its message), located back to the original
    # trace for error reporting.
    action: Literal["send", "receive"]
    message: str
    trace_step: int
    transition_id: str


class DirectionalDifference(BaseModel):
    # Position in the per-direction streams, counted from 1. A strict prefix
    # reports the first position the missing side never reached.
    position: int
    left: ComparisonEvent | None
    right: ComparisonEvent | None


class DirectionComparison(BaseModel):
    # Comparison of one message direction (A_to_B or B_to_A), ignoring how the
    # two directions were interleaved in the original traces.
    left_event_count: int
    right_event_count: int
    relation: Literal[
        "equal",
        "left_is_strict_prefix",
        "right_is_strict_prefix",
        "divergent",
    ]
    first_difference: DirectionalDifference | None = None


class SessionComparisonResponse(BaseModel):
    left_session_id: str
    right_session_id: str
    left_version: int
    right_version: int
    A_to_B: DirectionComparison
    B_to_A: DirectionComparison
