"""Permission system — controls tool execution authorization."""

from __future__ import annotations

from enum import Enum
from typing import Any


class PermissionMode(Enum):
    """Permission levels for tool execution."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"

    def __str__(self) -> str:
        return self.value

    @staticmethod
    def from_str(s: str) -> PermissionMode:
        mapping = {
            "read-only": PermissionMode.READ_ONLY,
            "readonly": PermissionMode.READ_ONLY,
            "ro": PermissionMode.READ_ONLY,
            "workspace-write": PermissionMode.WORKSPACE_WRITE,
            "workspace": PermissionMode.WORKSPACE_WRITE,
            "ww": PermissionMode.WORKSPACE_WRITE,
            "danger-full-access": PermissionMode.DANGER_FULL_ACCESS,
            "danger": PermissionMode.DANGER_FULL_ACCESS,
            "full": PermissionMode.DANGER_FULL_ACCESS,
            "full-access": PermissionMode.DANGER_FULL_ACCESS,
            "fa": PermissionMode.DANGER_FULL_ACCESS,
        }
        result = mapping.get(s.lower())
        if result is None:
            raise ValueError(f"unknown permission mode: {s}")
        return result

    def allows(self, required: PermissionMode) -> bool:
        """Check if this mode permits the required level."""
        hierarchy = {
            PermissionMode.READ_ONLY: 0,
            PermissionMode.WORKSPACE_WRITE: 1,
            PermissionMode.DANGER_FULL_ACCESS: 2,
        }
        return hierarchy[self] >= hierarchy[required]


class PermissionOutcome:
    """Result of a permission check."""

    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    @staticmethod
    def allow() -> PermissionOutcome:
        return PermissionOutcome(allowed=True)

    @staticmethod
    def deny(reason: str) -> PermissionOutcome:
        return PermissionOutcome(allowed=False, reason=reason)


class PermissionPolicy:
    """Evaluates whether a tool call is authorized."""

    def __init__(self, mode: PermissionMode = PermissionMode.READ_ONLY):
        self.mode = mode

    def authorize(
        self,
        tool_name: str,
        tool_input: str,
        required_permission: PermissionMode,
        prompter: Any = None,
    ) -> PermissionOutcome:
        """Check if a tool call is authorized under the current policy.

        If a prompter is provided and the tool requires higher permissions,
        the user will be interactively prompted.
        """
        if self.mode.allows(required_permission):
            return PermissionOutcome.allow()

        # If we have a prompter, ask the user
        if prompter is not None:
            outcome = prompter.ask(tool_name, tool_input, required_permission, self.mode)
            # If the prompter escalated (e.g. "allow for this session"),
            # propagate the new mode back to the policy so subsequent tool
            # calls within the same turn don't re-prompt.
            if outcome.allowed and hasattr(prompter, "mode") and prompter.mode != self.mode:
                self.mode = prompter.mode
            return outcome

        return PermissionOutcome.deny(
            f"tool `{tool_name}` requires {required_permission} but current mode is {self.mode}"
        )
