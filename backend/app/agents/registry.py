"""Dynamic agent registry.

The registry is the single authority for the *shape* of the agent
hierarchy (CTO -> Managers -> Workers, plus flat System Agents). It does
not contain any hierarchy-specific business logic itself -- individual
agent implementations must not hard-code assumptions about the tree shape;
they should query the registry instead.

The hierarchy is built dynamically at runtime (e.g. from an LLM-driven
planning step or from `config/agents.yaml` for bootstrapping/examples). No
fixed team layout is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.state.enums import AgentType, SystemAgentKind
from app.state.models import AgentDefinition


class RegistryError(Exception):
    """Raised for invalid registry operations (duplicate IDs, bad type, etc.)."""


class HierarchyValidationError(Exception):
    """Raised when `validate_hierarchy` finds structural problems.

    `errors` contains every problem found (validation collects everything
    rather than failing on the first issue) so callers can report a
    complete diagnostic.
    """

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class AgentNode:
    """A node in the built hierarchy tree, wrapping an `AgentDefinition`."""

    definition: AgentDefinition
    children: list[AgentNode] = field(default_factory=list)

    @property
    def agent_id(self) -> str:
        return self.definition.agent_id

    def to_dict(self) -> dict:
        return {
            "agent_id": self.definition.agent_id,
            "name": self.definition.name,
            "agent_type": self.definition.agent_type.value,
            "system_kind": (
                self.definition.system_kind.value if self.definition.system_kind else None
            ),
            "team_name": self.definition.team_name,
            "children": [child.to_dict() for child in self.children],
        }


class AgentRegistry:
    """In-memory registry of `AgentDefinition`s and their parent/child links.

    Thread/async-safety: this registry is intended to be owned and mutated
    by a single orchestration coroutine at a time. Callers that need
    concurrent access should guard it externally (e.g. with an
    `asyncio.Lock`) -- no locking is performed internally.
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}
        # child_id set, keyed by parent_id, for O(1) child lookups.
        self._children_index: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_agent(self, agent: AgentDefinition) -> AgentDefinition:
        """Register a new agent definition.

        Raises:
            RegistryError: if the agent_id is already registered, the
                declared parent does not exist, the agent type is invalid,
                or a WORKER is registered without a MANAGER/CTO parent.
        """

        if agent.agent_id in self._agents:
            raise RegistryError(f"Agent id '{agent.agent_id}' is already registered.")

        if not isinstance(agent.agent_type, AgentType):
            raise RegistryError(f"Invalid agent_type: {agent.agent_type!r}")

        if agent.agent_type == AgentType.SYSTEM_AGENT and agent.system_kind is None:
            raise RegistryError(
                f"Agent '{agent.agent_id}' is a SYSTEM_AGENT but has no system_kind."
            )
        if agent.agent_type != AgentType.SYSTEM_AGENT and agent.system_kind is not None:
            raise RegistryError(
                f"Agent '{agent.agent_id}' has system_kind set but is not a SYSTEM_AGENT."
            )

        if agent.parent_agent_id is not None:
            if agent.parent_agent_id == agent.agent_id:
                raise RegistryError(f"Agent '{agent.agent_id}' cannot be its own parent.")
            if agent.parent_agent_id not in self._agents:
                raise RegistryError(
                    f"Parent agent '{agent.parent_agent_id}' does not exist "
                    f"(registering '{agent.agent_id}')."
                )

        if agent.agent_type == AgentType.WORKER:
            parent = (
                self._agents.get(agent.parent_agent_id) if agent.parent_agent_id else None
            )
            if parent is None or parent.agent_type not in (
                AgentType.MANAGER,
                AgentType.CTO,
            ):
                raise RegistryError(
                    f"Worker '{agent.agent_id}' must have a MANAGER or CTO parent "
                    "(orphan workers are not allowed)."
                )

        self._agents[agent.agent_id] = agent
        self._children_index.setdefault(agent.agent_id, set())
        if agent.parent_agent_id is not None:
            self._children_index.setdefault(agent.parent_agent_id, set())
            self._children_index[agent.parent_agent_id].add(agent.agent_id)

        return agent

    def remove_agent(self, agent_id: str, cascade: bool = True) -> None:
        """Remove an agent from the registry.

        Args:
            agent_id: the agent to remove.
            cascade: if True (default), all descendants are removed too.
                If False and the agent has children, raises RegistryError.
        """

        if agent_id not in self._agents:
            raise RegistryError(f"Agent id '{agent_id}' is not registered.")

        children = self._children_index.get(agent_id, set())
        if children and not cascade:
            raise RegistryError(
                f"Agent '{agent_id}' has {len(children)} child agent(s); "
                "pass cascade=True to remove them too."
            )

        for child_id in list(children):
            self.remove_agent(child_id, cascade=True)

        agent = self._agents.pop(agent_id)
        self._children_index.pop(agent_id, None)
        if agent.parent_agent_id is not None:
            parent_children = self._children_index.get(agent.parent_agent_id)
            if parent_children is not None:
                parent_children.discard(agent_id)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_agent(self, agent_id: str) -> AgentDefinition:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise RegistryError(f"Agent id '{agent_id}' is not registered.")
        return agent

    def get_agent_or_none(self, agent_id: str) -> AgentDefinition | None:
        return self._agents.get(agent_id)

    def get_children(self, agent_id: str) -> list[AgentDefinition]:
        if agent_id not in self._agents:
            raise RegistryError(f"Agent id '{agent_id}' is not registered.")
        return [self._agents[cid] for cid in sorted(self._children_index.get(agent_id, set()))]

    def get_parent(self, agent_id: str) -> AgentDefinition | None:
        agent = self.get_agent(agent_id)
        if agent.parent_agent_id is None:
            return None
        return self._agents.get(agent.parent_agent_id)

    def get_descendants(self, agent_id: str) -> list[AgentDefinition]:
        """Return all descendants (children, grandchildren, ...) of an agent."""

        if agent_id not in self._agents:
            raise RegistryError(f"Agent id '{agent_id}' is not registered.")

        result: list[AgentDefinition] = []
        stack = list(self._children_index.get(agent_id, set()))
        seen: set[str] = set()
        while stack:
            current_id = stack.pop()
            if current_id in seen:
                continue
            seen.add(current_id)
            current = self._agents.get(current_id)
            if current is None:
                continue
            result.append(current)
            stack.extend(self._children_index.get(current_id, set()))
        return result

    def get_ancestors(self, agent_id: str) -> list[AgentDefinition]:
        """Return the chain of ancestors from immediate parent up to the root."""

        result: list[AgentDefinition] = []
        current = self.get_agent(agent_id)
        seen: set[str] = {agent_id}
        while current.parent_agent_id is not None:
            if current.parent_agent_id in seen:
                # Cycle guard -- validate_hierarchy() is the authoritative
                # cycle detector, but we must not infinite-loop here.
                break
            parent = self._agents.get(current.parent_agent_id)
            if parent is None:
                break
            result.append(parent)
            seen.add(parent.agent_id)
            current = parent
        return result

    def get_roots(self) -> list[AgentDefinition]:
        """Return every agent with no parent (CTO plus any top-level system agents)."""

        return [a for a in self._agents.values() if a.parent_agent_id is None]

    def all_agents(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_hierarchy(self) -> None:
        """Validate the full registry, raising `HierarchyValidationError` on failure.

        Checks performed:
            * duplicate IDs (structurally impossible via register_agent, but
              re-checked here defensively)
            * missing parents
            * circular parent relationships
            * invalid agent types / system_kind consistency
            * orphan workers (no MANAGER/CTO ancestor)
        """

        errors: list[str] = []

        seen_ids: set[str] = set()
        for agent_id, agent in self._agents.items():
            if agent_id != agent.agent_id:
                errors.append(
                    f"Index/key mismatch: stored under '{agent_id}' but agent_id is "
                    f"'{agent.agent_id}'."
                )
            if agent.agent_id in seen_ids:
                errors.append(f"Duplicate agent id detected: '{agent.agent_id}'.")
            seen_ids.add(agent.agent_id)

            if not isinstance(agent.agent_type, AgentType):
                errors.append(f"Agent '{agent.agent_id}' has invalid agent_type.")

            if agent.agent_type == AgentType.SYSTEM_AGENT:
                if agent.system_kind is None or not isinstance(
                    agent.system_kind, SystemAgentKind
                ):
                    errors.append(
                        f"System agent '{agent.agent_id}' is missing a valid system_kind."
                    )
            elif agent.system_kind is not None:
                errors.append(
                    f"Agent '{agent.agent_id}' has system_kind set but is type "
                    f"{agent.agent_type}."
                )

            if agent.parent_agent_id is not None and agent.parent_agent_id not in self._agents:
                errors.append(
                    f"Agent '{agent.agent_id}' references missing parent "
                    f"'{agent.parent_agent_id}'."
                )

        # Circular relationship detection via three-colour DFS.
        WHITE, GRAY, BLACK = 0, 1, 2
        colour: dict[str, int] = {aid: WHITE for aid in self._agents}

        def visit(aid: str, path: list[str]) -> None:
            colour[aid] = GRAY
            agent = self._agents.get(aid)
            parent_id = agent.parent_agent_id if agent else None
            if parent_id is not None and parent_id in self._agents:
                if colour.get(parent_id) == GRAY:
                    cycle = " -> ".join(path + [parent_id])
                    errors.append(f"Circular parent relationship detected: {cycle}.")
                elif colour.get(parent_id) == WHITE:
                    visit(parent_id, path + [parent_id])
            colour[aid] = BLACK

        for aid in list(self._agents.keys()):
            if colour.get(aid) == WHITE:
                visit(aid, [aid])

        # Orphan worker detection: every WORKER must have a MANAGER/CTO
        # ancestor reachable via valid (acyclic) parent links.
        for agent in self._agents.values():
            if agent.agent_type != AgentType.WORKER:
                continue
            ancestors = self._safe_ancestors(agent.agent_id)
            if not any(a.agent_type in (AgentType.MANAGER, AgentType.CTO) for a in ancestors):
                errors.append(
                    f"Worker '{agent.agent_id}' has no MANAGER/CTO ancestor (orphan worker)."
                )

        if errors:
            raise HierarchyValidationError(errors)

    def _safe_ancestors(self, agent_id: str, _max_depth: int = 1000) -> list[AgentDefinition]:
        """Like get_ancestors but bounded, for use during validation of a
        possibly-cyclic graph."""

        result: list[AgentDefinition] = []
        seen: set[str] = {agent_id}
        current = self._agents.get(agent_id)
        depth = 0
        while current is not None and current.parent_agent_id is not None and depth < _max_depth:
            depth += 1
            if current.parent_agent_id in seen:
                break
            parent = self._agents.get(current.parent_agent_id)
            if parent is None:
                break
            result.append(parent)
            seen.add(parent.agent_id)
            current = parent
        return result

    # ------------------------------------------------------------------
    # Tree construction
    # ------------------------------------------------------------------

    def build_tree(self) -> list[AgentNode]:
        """Build the full hierarchy as a forest of `AgentNode` trees.

        Returns one tree per root agent (typically the single CTO agent,
        plus any standalone system agents with no parent). Raises
        `HierarchyValidationError` first if the registry is structurally
        invalid.
        """

        self.validate_hierarchy()

        nodes: dict[str, AgentNode] = {
            aid: AgentNode(definition=agent) for aid, agent in self._agents.items()
        }
        roots: list[AgentNode] = []

        for aid, agent in self._agents.items():
            node = nodes[aid]
            if agent.parent_agent_id is None:
                roots.append(node)
            else:
                parent_node = nodes.get(agent.parent_agent_id)
                if parent_node is not None:
                    parent_node.children.append(node)

        for node in nodes.values():
            node.children.sort(key=lambda n: n.definition.name)
        roots.sort(key=lambda n: n.definition.name)

        return roots

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._agents
