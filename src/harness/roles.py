"""Agent roles: what an agent does, separate from the tier (model) that runs it.

The office shows every agent as "<Title> · <tier>", with a figure picked by `figure` (a CSS
key on the page). Chat agents are builders or researchers; the router picks the role and the
tier independently (`harness.planner.Routing`)."""

from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

Role = Literal["orchestrator", "planner", "builder", "reviewer", "researcher"]
ChatRole = Literal["builder", "researcher"]
CHAT_ROLES: tuple[ChatRole, ...] = ("builder", "researcher")
DEFAULT_ROLE: ChatRole = "builder"

_PROMPTS = Path(__file__).parent / "prompts"


class RoleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    figure: str


ROLES: dict[Role, RoleSpec] = {
    "orchestrator": RoleSpec(title="Orchestrator", figure="orchestrator"),
    "planner": RoleSpec(title="Planner", figure="planner"),
    "builder": RoleSpec(title="Builder", figure="builder"),
    "reviewer": RoleSpec(title="Reviewer", figure="reviewer"),
    "researcher": RoleSpec(title="Researcher", figure="researcher"),
}


def role_or_default(value: object) -> ChatRole:
    """`value` (case and spaces ignored) when it is a chat role, else DEFAULT_ROLE."""
    if isinstance(value, str):
        value = value.strip().lower()
    return cast(ChatRole, value) if value in CHAT_ROLES else DEFAULT_ROLE


def role_title(role: object) -> str:
    """The display title of `role`; the builder's title for anything unknown."""
    spec = ROLES.get(cast(Role, role))
    return spec.title if spec is not None else ROLES["builder"].title


def researcher_prompt() -> str:
    """The researcher's system-prompt append (prompts/researcher_system.md)."""
    return (_PROMPTS / "researcher_system.md").read_text(encoding="utf-8")
