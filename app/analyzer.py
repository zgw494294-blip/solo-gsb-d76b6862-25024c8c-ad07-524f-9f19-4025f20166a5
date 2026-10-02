from collections import deque
from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class Transition:
    id: str
    sender: str
    receiver: str
    action: str
    message: str
    source: object
    target: object


@dataclass(frozen=True)
class Configuration:
    state_a: object
    state_b: object
    queue_a_to_b: str | None
    queue_b_to_a: str | None


@dataclass(frozen=True)
class ProtocolModel:
    transitions: tuple[Transition, ...]
    outgoing: dict[str, dict[object, tuple[Transition, ...]]]
    initial: Configuration
    finals: dict[str, frozenset]


def build_model(request) -> ProtocolModel:
    transitions = tuple(
        Transition(
            id=item.id,
            sender=item.sender,
            receiver=item.receiver,
            action=item.action,
            message=item.message,
            source=item.source,
            target=item.target,
        )
        for item in sorted(request.transitions, key=lambda transition: transition.id)
    )

    outgoing = _build_outgoing(transitions)

    initial = Configuration(
        request.A.initial,
        request.B.initial,
        None,
        None,
    )
    finals = {
        "A": frozenset(request.A.finals),
        "B": frozenset(request.B.finals),
    }

    return ProtocolModel(
        transitions=transitions,
        outgoing=outgoing,
        initial=initial,
        finals=finals,
    )


def _build_outgoing(transitions) -> dict[str, dict[object, tuple[Transition, ...]]]:
    outgoing: dict[str, dict[object, list[Transition]]] = {"A": {}, "B": {}}
    for transition in transitions:
        owner = transition.sender if transition.action == "send" else transition.receiver
        outgoing[owner].setdefault(transition.source, []).append(transition)
    return {
        endpoint: {source: tuple(items) for source, items in by_source.items()}
        for endpoint, by_source in outgoing.items()
    }


def is_completion(configuration: Configuration, finals: dict[str, frozenset]) -> bool:
    return (
        configuration.state_a in finals["A"]
        and configuration.state_b in finals["B"]
        and configuration.queue_a_to_b is None
        and configuration.queue_b_to_a is None
    )


def enabled_transitions(
    configuration: Configuration,
    outgoing: dict[str, dict[object, tuple[Transition, ...]]],
) -> list[Transition]:
    """All transitions fireable in this configuration, sorted by transition id.

    Shares the one-slot queue semantics of the protocol: a sender may only fire
    when its own direction's slot is empty, and a receive requires the opposite
    slot's head to equal the transition's message.
    """
    enabled = []
    for endpoint in ("A", "B"):
        state = configuration.state_a if endpoint == "A" else configuration.state_b
        for transition in outgoing[endpoint].get(state, ()):
            if transition.action == "send":
                own_queue = (
                    configuration.queue_a_to_b
                    if transition.sender == "A"
                    else configuration.queue_b_to_a
                )
                if own_queue is None:
                    enabled.append(transition)
            else:
                opposite_queue = (
                    configuration.queue_b_to_a
                    if transition.receiver == "A"
                    else configuration.queue_a_to_b
                )
                if opposite_queue == transition.message:
                    enabled.append(transition)

    enabled.sort(key=lambda transition: transition.id)
    return enabled


def apply_transition(configuration: Configuration, transition: Transition) -> Configuration:
    if transition.action == "send":
        if transition.sender == "A":
            return Configuration(
                transition.target,
                configuration.state_b,
                transition.message,
                configuration.queue_b_to_a,
            )
        return Configuration(
            configuration.state_a,
            transition.target,
            configuration.queue_a_to_b,
            transition.message,
        )

    if transition.receiver == "A":
        return Configuration(
            transition.target,
            configuration.state_b,
            configuration.queue_a_to_b,
            None,
        )
    return Configuration(
        configuration.state_a,
        transition.target,
        None,
        configuration.queue_b_to_a,
    )


def _explore_reachable(
    model: ProtocolModel,
    outgoing=None,
) -> tuple[
    dict[Configuration, list[tuple[Transition, Configuration]]],
    list[Configuration],
    dict[Configuration, tuple[Configuration | None, Transition | None]],
    Configuration | None,
]:
    """BFS over every reachable configuration.

    Returns the labelled adjacency (completion and deadlock configurations keep
    empty successor lists), configurations in discovery order, the BFS parent of
    each configuration, and the first deadlock reached.

    Configurations are dequeued in shortest-path order; successors are generated
    in transition-id order, so both the first deadlock and the lexicographic
    tie-break match the required BFS policy.
    """
    if outgoing is None:
        outgoing = model.outgoing

    adjacency: dict[Configuration, list[tuple[Transition, Configuration]]] = {
        model.initial: []
    }
    order: list[Configuration] = []
    parents: dict[Configuration, tuple[Configuration | None, Transition | None]] = {
        model.initial: (None, None)
    }
    queue = deque([model.initial])
    deadlock: Configuration | None = None

    while queue:
        current = queue.popleft()
        order.append(current)

        if is_completion(current, model.finals):
            # Completion configurations are never expanded.
            continue

        edges = []
        for transition in enabled_transitions(current, outgoing):
            nxt = apply_transition(current, transition)
            edges.append((transition, nxt))
            if nxt not in parents:
                adjacency.setdefault(nxt, [])
                parents[nxt] = (current, transition)
                queue.append(nxt)
        adjacency[current] = edges

        if not edges and deadlock is None:
            deadlock = current

    return adjacency, order, parents, deadlock


def analyze(request) -> dict:
    model = build_model(request)

    adjacency, _order, parents, deadlock = _explore_reachable(model)

    response = {
        "deadlock": deadlock is not None,
        "visited_states": len(parents),
        "initial": _configuration_view(model.initial),
        "deadlock_state": None,
        "path": None,
    }

    if deadlock is not None:
        chain = _rebuild_chain(parents, deadlock, configuration_of=lambda node: node)
        steps = _steps_view(chain)

        response["deadlock_state"] = _configuration_view(deadlock)
        response["path"] = steps

    return response


def analyze_guarantee(request) -> dict:
    """Completion guarantee over every interleaving of the existing protocol.

    Completion is guaranteed only when every run ends in a completion
    configuration (both endpoints final, both queues empty). A reachable
    configuration without enabled transitions outside completion is a deadlock;
    otherwise, if some reachable configuration lies on a directed cycle, that
    cycle can be repeated forever (its exits towards completion do not remove
    the infinite run). Deadlocks take precedence over cycles.
    """
    model = build_model(request)

    adjacency, order, parents, deadlock = _explore_reachable(model)

    response = {
        "result": "guaranteed",
        "visited_states": len(adjacency),
        "initial": _configuration_view(model.initial),
        "witness": None,
    }

    if deadlock is not None:
        chain = _rebuild_chain(parents, deadlock, configuration_of=lambda node: node)
        response["result"] = "deadlock"
        response["witness"] = {
            "kind": "deadlock",
            "deadlock_state": _configuration_view(deadlock),
            "path": _steps_view(chain),
        }
        return response

    cyclic = _cyclic_configurations(adjacency)
    if cyclic:
        # Discovery order makes the prefix shortest, and equal lengths break
        # towards the lexicographically smallest transition-id sequence.
        entry = next(configuration for configuration in order if configuration in cyclic)
        prefix = _rebuild_chain(parents, entry, configuration_of=lambda node: node)
        cycle_chain = _shortest_cycle(adjacency, entry)
        response["result"] = "cycle"
        response["witness"] = {
            "kind": "cycle",
            "entry_state": _configuration_view(entry),
            "prefix": _steps_view(prefix),
            "cycle": _steps_view(cycle_chain),
        }
        return response

    return response



def plan_completion(request) -> dict:
    model = build_model(request)
    required = frozenset(request.required_transition_ids)

    # Search the product of configurations and the set of required transitions
    # already hit. A required transition only joins the set on its first firing;
    # other transitions leave it unchanged.
    Node = tuple[Configuration, frozenset]
    start: Node = (model.initial, frozenset())

    # BFS with successors generated in transition-id order: the first completion
    # node dequeued uses the fewest transitions, and ties resolve to the
    # lexicographically smallest transition-id sequence.
    queue = deque([start])
    parents: dict[Node, tuple[Node | None, Transition | None]] = {start: (None, None)}
    goal: Node | None = None

    while queue:
        configuration, hit = queue.popleft()

        if is_completion(configuration, model.finals):
            if goal is None and required <= hit:
                goal = (configuration, hit)
            # Completion configurations are never expanded.
            continue

        for transition in enabled_transitions(configuration, model.outgoing):
            next_configuration = apply_transition(configuration, transition)
            next_hit = hit | ({transition.id} if transition.id in required else frozenset())
            nxt: Node = (next_configuration, next_hit)
            if nxt not in parents:
                parents[nxt] = ((configuration, hit), transition)
                queue.append(nxt)

    response = {
        "completable": goal is not None,
        "visited_combinations": len(parents),
        "initial": _configuration_view(model.initial),
        "path": None,
    }

    if goal is not None:
        chain = _rebuild_chain(parents, goal, configuration_of=lambda node: node[0])
        response["path"] = _steps_view(chain)  # [] when the initial configuration already satisfies

    return response


def plan_deactivation(request) -> dict:
    model = build_model(request)
    removable = sorted(request.removable_transition_ids)

    # Candidates are tried by ascending deactivation count. For equal counts,
    # combinations over the sorted ids are generated in lexicographic order of
    # the ascending id lists, so the first qualifying candidate is the answer.
    for size in range(len(removable) + 1):
        for combo in combinations(removable, size):
            deactivated = frozenset(combo)
            kept = tuple(
                transition for transition in model.transitions if transition.id not in deactivated
            )
            outgoing = _build_outgoing(kept)
            adjacency = _reachable_graph(model, outgoing)
            if _all_reach_completion(adjacency, model.finals):
                return {
                    "solvable": True,
                    "deactivated_transition_ids": list(combo),
                    "reachable_configurations": len(adjacency),
                }

    return {
        "solvable": False,
        "deactivated_transition_ids": None,
        "reachable_configurations": None,
    }


def _reachable_graph(model, outgoing) -> dict[Configuration, list[Configuration]]:
    """Reachable configurations from the initial one, mapped to their successors.

    Shares the one-slot queue semantics of the protocol; completion
    configurations are never expanded, so they keep empty successor lists.
    """
    adjacency: dict[Configuration, list[Configuration]] = {model.initial: []}
    queue = deque([model.initial])

    while queue:
        current = queue.popleft()

        if is_completion(current, model.finals):
            # Completion configurations are never expanded.
            continue

        successors = []
        for transition in enabled_transitions(current, outgoing):
            nxt = apply_transition(current, transition)
            successors.append(nxt)
            if nxt not in adjacency:
                adjacency[nxt] = []
                queue.append(nxt)
        adjacency[current] = successors

    return adjacency


def _all_reach_completion(adjacency, finals) -> bool:
    """True when every reachable configuration can reach a completion configuration."""
    completions = [config for config in adjacency if is_completion(config, finals)]
    if not completions:
        return False

    reverse: dict[Configuration, list[Configuration]] = {config: [] for config in adjacency}
    for current, successors in adjacency.items():
        for nxt in successors:
            reverse[nxt].append(current)

    good = set(completions)
    stack = list(completions)
    while stack:
        current = stack.pop()
        for previous in reverse[current]:
            if previous not in good:
                good.add(previous)
                stack.append(previous)

    return len(good) == len(adjacency)


def _cyclic_configurations(
    adjacency: dict[Configuration, list[tuple[Transition, Configuration]]],
) -> set[Configuration]:
    """Configurations that belong to a directed cycle (including self-loops).

    Such a configuration starts an infinite run: the cycle can be repeated
    forever even when it has an exit towards completion.
    """
    reverse: dict[Configuration, list[Configuration]] = {configuration: [] for configuration in adjacency}
    for current, edges in adjacency.items():
        for _transition, nxt in edges:
            reverse[nxt].append(current)

    # Iterative Kosaraju; configurations carry no meaningful order here.
    visited: set[Configuration] = set()
    finish_order: list[Configuration] = []
    for start in adjacency:
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[Configuration, int]] = [(start, 0)]
        while stack:
            current, edge_index = stack[-1]
            successors = adjacency[current]
            if edge_index < len(successors):
                stack[-1] = (current, edge_index + 1)
                nxt = successors[edge_index][1]
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, 0))
            else:
                finish_order.append(current)
                stack.pop()

    cyclic: set[Configuration] = set()
    assigned: set[Configuration] = set()
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component = [start]
        assigned.add(start)
        stack = [start]
        while stack:
            current = stack.pop()
            for previous in reverse[current]:
                if previous not in assigned:
                    assigned.add(previous)
                    component.append(previous)
                    stack.append(previous)
        has_self_loop = any(
            nxt is node for node in component for _transition, nxt in adjacency[node]
        )
        if len(component) > 1 or has_self_loop:
            cyclic.update(component)

    return cyclic


def _shortest_cycle(
    adjacency: dict[Configuration, list[tuple[Transition, Configuration]]],
    entry: Configuration,
) -> list[tuple[Configuration, Transition]]:
    """Shortest non-empty walk from ``entry`` back to itself.

    Edge lists are already transition-id sorted, so ties resolve to the
    lexicographically smallest transition-id sequence.
    """
    # (predecessor, transition taken); the entry starts unvisited so its own
    # self-loop (or any other edge returning to it) closes the cycle.
    parents: dict[Configuration, tuple[Configuration, Transition | None]] = {}
    queue = deque([entry])
    closing: tuple[Transition, Configuration] | None = None

    while queue and closing is None:
        current = queue.popleft()
        for transition, nxt in adjacency[current]:
            if nxt == entry:
                closing = (transition, current)
                break
            if nxt not in parents:
                parents[nxt] = (current, transition)
                queue.append(nxt)

    if closing is None:
        raise RuntimeError("entry configuration is not on a cycle")

    transition, predecessor = closing

    # Walk from the predecessor back to the entry, then append the closing edge.
    chain: list[tuple[Configuration, Transition]] = []
    cursor: Configuration | None = predecessor
    while cursor is not None and cursor in parents:
        previous, edge = parents[cursor]
        chain.append((cursor, edge))
        cursor = previous
    chain.reverse()
    chain.append((entry, transition))
    return chain


def _steps_view(chain: list[tuple[Configuration, Transition]]) -> list[dict]:
    """Numbered transition records; numbering restarts at 1 for each witness part."""
    return [
        {
            "index": index,
            "transition_id": transition.id,
            "from": transition.sender,
            "to": transition.receiver,
            "action": transition.action,
            "message": transition.message,
            **_configuration_view(configuration),
        }
        for index, (configuration, transition) in enumerate(chain, start=1)
    ]


def _rebuild_chain(parents, target, configuration_of) -> list[tuple[Configuration, Transition]]:
    """Return the (destination configuration, transition) pairs on the path to target."""
    chain: list[tuple[Configuration, Transition]] = []
    cursor = target
    while cursor is not None:
        parent, transition = parents[cursor]
        if transition is None:
            break
        chain.append((configuration_of(cursor), transition))
        cursor = parent
    chain.reverse()
    return chain


def _configuration_view(configuration: Configuration) -> dict:
    return {
        "state": {"A": configuration.state_a, "B": configuration.state_b},
        "queues": {
            "A_to_B": configuration.queue_a_to_b,
            "B_to_A": configuration.queue_b_to_a,
        },
    }
