"""Template expression resolution for pipeline definitions.

AD-019 Phase 5: Resolves ``{{ expression }}`` templates in pipeline YAML
values against the pipeline's runtime context.

Supports:
    - Dotted path access: ``{{ context.pr_number }}``
    - Index access: ``{{ context.prs[0] }}``
    - Nested paths: ``{{ trigger.pull_request.base.ref }}``
    - Filter functions: ``{{ context.pr_number | linked_issue }}``
    - Comparison expressions: ``{{ context.pr_number != null }}``
    - Literal passthrough for non-string values

The resolver is intentionally simple — no Jinja2 dependency, no arbitrary
code execution, no loops/conditionals. It resolves expressions against a
flat namespace of known roots (``context``, ``trigger``, ``github``,
``branches``, ``stages``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("squadron.pipeline.templates")

# Matches {{ expression }} with optional whitespace
_TEMPLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")

# Matches a filter: expr | filter_name
_FILTER_RE = re.compile(r"^(.+?)\s*\|\s*([a-zA-Z_][a-zA-Z0-9_]*)$")

# Matches comparison: expr != value  or  expr == value
_COMPARISON_RE = re.compile(r"^(.+?)\s*(!=|==)\s*(.+)$")

# Matches dotted path with optional array index: context.prs[0].name
_PATH_SEGMENT_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)(?:\[(\d+)\])?")


class TemplateResolver:
    """Resolves ``{{ expression }}`` templates against a namespace of values.

    Usage::

        resolver = TemplateResolver({
            "context": run.context,
            "trigger": payload,
        })
        result = resolver.resolve("PR #{{ context.pr_number }} ready")
        # → "PR #42 ready"
    """

    def __init__(
        self,
        namespace: dict[str, Any] | None = None,
        *,
        filters: dict[str, Any] | None = None,
    ):
        self._namespace = namespace or {}
        self._filters: dict[str, Any] = filters or {}

    def resolve(self, value: Any) -> Any:
        """Resolve template expressions in a value.

        - Strings with ``{{ }}`` are resolved.
        - Dicts have their values recursively resolved.
        - Lists have their items recursively resolved.
        - Other types are returned as-is.
        """
        if isinstance(value, str):
            return self._resolve_string(value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    def resolve_expr(self, expr: str) -> Any:
        """Resolve a single expression string (without {{ }} delimiters).

        Returns the resolved value, or None if the path does not exist.
        """
        # Check for filter: expr | filter_name
        filter_match = _FILTER_RE.match(expr)
        if filter_match:
            inner_expr = filter_match.group(1).strip()
            filter_name = filter_match.group(2)
            inner_value = self._resolve_path(inner_expr)
            return self._apply_filter(filter_name, inner_value)

        # Check for comparison: expr != value  or  expr == value
        cmp_match = _COMPARISON_RE.match(expr)
        if cmp_match:
            lhs_expr = cmp_match.group(1).strip()
            operator = cmp_match.group(2)
            rhs_raw = cmp_match.group(3).strip()
            lhs_value = self._resolve_path(lhs_expr)
            rhs_value = self._parse_literal(rhs_raw)
            if operator == "!=":
                return lhs_value != rhs_value
            return lhs_value == rhs_value

        # Simple path resolution
        return self._resolve_path(expr)

    def _resolve_string(self, text: str) -> Any:
        """Resolve all ``{{ }}`` expressions in a string.

        If the entire string is a single expression, return the raw value
        (preserving type). Otherwise, interpolate as string.
        """
        # Fast path: no templates
        if "{{" not in text:
            return text

        # Check if the entire string is a single expression
        stripped = text.strip()
        single_match = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", stripped)
        if single_match:
            return self.resolve_expr(single_match.group(1))

        # Multiple expressions or mixed text — interpolate as string
        def _replacer(m: re.Match) -> str:
            result = self.resolve_expr(m.group(1))
            if result is None:
                return ""
            return str(result)

        return _TEMPLATE_RE.sub(_replacer, text)

    def _resolve_path(self, path: str) -> Any:
        """Resolve a dotted path like ``context.prs[0]`` against the namespace."""
        path = path.strip()

        # Split on dots, handling segments with array indices
        segments = path.split(".")
        current: Any = self._namespace

        for segment in segments:
            if current is None:
                return None

            match = _PATH_SEGMENT_RE.fullmatch(segment)
            if not match:
                return None

            key = match.group(1)
            idx_str = match.group(2)

            # Navigate into dict or object
            if isinstance(current, dict):
                current = current.get(key)
            elif hasattr(current, key):
                current = getattr(current, key)
            else:
                return None

            # Handle array index
            if idx_str is not None:
                try:
                    idx = int(idx_str)
                    if isinstance(current, (list, tuple)) and 0 <= idx < len(current):
                        current = current[idx]
                    else:
                        return None
                except (ValueError, TypeError):
                    return None

        return current

    def _apply_filter(self, filter_name: str, value: Any) -> Any:
        """Apply a named filter function to a value."""
        if filter_name in self._filters:
            fn = self._filters[filter_name]
            try:
                return fn(value)
            except Exception:
                logger.warning("Filter '%s' failed on value %r", filter_name, value)
                return value

        # Built-in filters
        if filter_name == "str":
            return str(value) if value is not None else ""
        if filter_name == "int":
            try:
                return int(value)
            except (ValueError, TypeError):
                return 0
        if filter_name == "default":
            return value if value is not None else ""

        logger.warning("Unknown template filter: '%s'", filter_name)
        return value

    @staticmethod
    def _parse_literal(raw: str) -> Any:
        """Parse a literal value from an expression RHS."""
        if raw == "null" or raw == "None":
            return None
        if raw == "true" or raw == "True":
            return True
        if raw == "false" or raw == "False":
            return False
        if raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1]
        if raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1]
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw


def resolve_templates(
    value: Any,
    *,
    context: dict[str, Any] | None = None,
    trigger: dict[str, Any] | None = None,
    github: dict[str, Any] | None = None,
    branches: dict[str, Any] | None = None,
    stages: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
) -> Any:
    """Convenience function to resolve templates against pipeline context.

    Builds a namespace from the provided keyword arguments and resolves
    any ``{{ expression }}`` in the value.

    Args:
        value: The value (string, dict, list, or scalar) to resolve.
        context: Pipeline run context dict.
        trigger: Trigger event payload.
        github: Live GitHub data (for poll conditions).
        branches: Parallel branch outputs (keyed by branch ID).
        stages: Completed stage outputs (keyed by stage ID).
        filters: Custom filter functions (name → callable).

    Returns:
        The resolved value with all template expressions expanded.
    """
    namespace: dict[str, Any] = {}
    if context is not None:
        namespace["context"] = context
    if trigger is not None:
        namespace["trigger"] = trigger
    if github is not None:
        namespace["github"] = github
    if branches is not None:
        namespace["branches"] = branches
    if stages is not None:
        namespace["stages"] = stages
    return TemplateResolver(namespace, filters=filters).resolve(value)
