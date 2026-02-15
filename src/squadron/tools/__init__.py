"""Framework tools — custom tools that bridge agents ↔ framework.

These are the ONLY way agents interact with the Squadron framework
during execution. Designed for the Copilot SDK's @define_tool decorator.

For the prototype, these are standalone async functions that can be
called directly or registered as tools when the SDK is integrated.

See runtime-architecture.md "Agent-Host Communication" section.
"""

from squadron.tools.framework import FrameworkTools
from squadron.tools.pm_tools import PMTools

__all__ = ["FrameworkTools", "PMTools"]
