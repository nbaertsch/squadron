"""Squadron tools — custom tools that bridge agents ↔ framework.

These are the ONLY way agents interact with the Squadron framework
during execution. Designed for the Copilot SDK's @define_tool decorator.

All tools live in a unified SquadronTools class with per-role
tool selection (D-7: enforced tool boundaries).

See runtime-architecture.md "Agent-Host Communication" section.
"""

from squadron.tools.squadron_tools import SquadronTools

__all__ = ["SquadronTools"]
