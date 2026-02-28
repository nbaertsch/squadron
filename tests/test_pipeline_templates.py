"""Tests for pipeline template expression resolution (AD-019 Phase 5)."""

from __future__ import annotations

import pytest

from squadron.pipeline.templates import TemplateResolver, resolve_templates


# ── TemplateResolver: basic path resolution ──────────────────────────────────


class TestTemplateResolverPaths:
    def test_simple_path(self):
        resolver = TemplateResolver({"context": {"pr_number": 42}})
        assert resolver.resolve("{{ context.pr_number }}") == 42

    def test_nested_path(self):
        resolver = TemplateResolver({"trigger": {"pull_request": {"base": {"ref": "main"}}}})
        assert resolver.resolve("{{ trigger.pull_request.base.ref }}") == "main"

    def test_array_index(self):
        resolver = TemplateResolver({"context": {"prs": [10, 20, 30]}})
        assert resolver.resolve("{{ context.prs[0] }}") == 10
        assert resolver.resolve("{{ context.prs[2] }}") == 30

    def test_array_index_out_of_bounds(self):
        resolver = TemplateResolver({"context": {"prs": [10]}})
        assert resolver.resolve("{{ context.prs[5] }}") is None

    def test_missing_path_returns_none(self):
        resolver = TemplateResolver({"context": {}})
        assert resolver.resolve("{{ context.nonexistent }}") is None

    def test_deep_missing_path(self):
        resolver = TemplateResolver({"context": {"a": 1}})
        assert resolver.resolve("{{ context.a.b.c }}") is None

    def test_no_template_passthrough(self):
        resolver = TemplateResolver({})
        assert resolver.resolve("plain text") == "plain text"

    def test_non_string_passthrough(self):
        resolver = TemplateResolver({})
        assert resolver.resolve(42) == 42
        assert resolver.resolve(True) is True
        assert resolver.resolve(None) is None

    def test_empty_namespace(self):
        resolver = TemplateResolver({})
        assert resolver.resolve("{{ missing }}") is None


# ── TemplateResolver: string interpolation ───────────────────────────────────


class TestTemplateResolverInterpolation:
    def test_single_expression_preserves_type(self):
        resolver = TemplateResolver({"context": {"count": 5}})
        result = resolver.resolve("{{ context.count }}")
        assert result == 5
        assert isinstance(result, int)

    def test_mixed_text_interpolates_as_string(self):
        resolver = TemplateResolver({"context": {"pr_number": 42}})
        result = resolver.resolve("PR #{{ context.pr_number }} ready")
        assert result == "PR #42 ready"
        assert isinstance(result, str)

    def test_multiple_expressions(self):
        resolver = TemplateResolver({"context": {"owner": "acme", "repo": "app"}})
        result = resolver.resolve("{{ context.owner }}/{{ context.repo }}")
        assert result == "acme/app"

    def test_none_value_interpolates_as_empty(self):
        resolver = TemplateResolver({"context": {}})
        result = resolver.resolve("value: {{ context.missing }}")
        assert result == "value: "


# ── TemplateResolver: filters ────────────────────────────────────────────────


class TestTemplateResolverFilters:
    def test_str_filter(self):
        resolver = TemplateResolver({"context": {"num": 42}})
        result = resolver.resolve("{{ context.num | str }}")
        assert result == "42"
        assert isinstance(result, str)

    def test_int_filter(self):
        resolver = TemplateResolver({"context": {"val": "123"}})
        result = resolver.resolve("{{ context.val | int }}")
        assert result == 123

    def test_int_filter_invalid(self):
        resolver = TemplateResolver({"context": {"val": "abc"}})
        assert resolver.resolve("{{ context.val | int }}") == 0

    def test_default_filter_with_value(self):
        resolver = TemplateResolver({"context": {"val": "hello"}})
        assert resolver.resolve("{{ context.val | default }}") == "hello"

    def test_default_filter_with_none(self):
        resolver = TemplateResolver({"context": {}})
        assert resolver.resolve("{{ context.missing | default }}") == ""

    def test_str_filter_on_none(self):
        resolver = TemplateResolver({"context": {}})
        assert resolver.resolve("{{ context.missing | str }}") == ""

    def test_custom_filter(self):
        resolver = TemplateResolver(
            {"context": {"val": 5}},
            filters={"double": lambda x: x * 2},
        )
        assert resolver.resolve("{{ context.val | double }}") == 10

    def test_unknown_filter_returns_value(self):
        resolver = TemplateResolver({"context": {"val": 42}})
        assert resolver.resolve("{{ context.val | unknown_filter }}") == 42


# ── TemplateResolver: comparisons ────────────────────────────────────────────


class TestTemplateResolverComparisons:
    def test_not_equal_null_with_value(self):
        resolver = TemplateResolver({"context": {"pr_number": 42}})
        assert resolver.resolve("{{ context.pr_number != null }}") is True

    def test_not_equal_null_with_none(self):
        resolver = TemplateResolver({"context": {}})
        assert resolver.resolve("{{ context.missing != null }}") is False

    def test_equal_true(self):
        resolver = TemplateResolver({"context": {"ready": True}})
        assert resolver.resolve("{{ context.ready == true }}") is True

    def test_equal_false(self):
        resolver = TemplateResolver({"context": {"ready": True}})
        assert resolver.resolve("{{ context.ready == false }}") is False

    def test_equal_string(self):
        resolver = TemplateResolver({"context": {"status": "success"}})
        assert resolver.resolve('{{ context.status == "success" }}') is True

    def test_equal_int(self):
        resolver = TemplateResolver({"context": {"count": 3}})
        assert resolver.resolve("{{ context.count == 3 }}") is True
        assert resolver.resolve("{{ context.count == 5 }}") is False

    def test_not_equal_int(self):
        resolver = TemplateResolver({"context": {"count": 3}})
        assert resolver.resolve("{{ context.count != 3 }}") is False
        assert resolver.resolve("{{ context.count != 5 }}") is True


# ── TemplateResolver: dict and list recursion ────────────────────────────────


class TestTemplateResolverRecursion:
    def test_resolve_dict_values(self):
        resolver = TemplateResolver({"context": {"pr": 42}})
        result = resolver.resolve({"pr_number": "{{ context.pr }}", "static": "hello"})
        assert result == {"pr_number": 42, "static": "hello"}

    def test_resolve_list_items(self):
        resolver = TemplateResolver({"context": {"a": 1, "b": 2}})
        result = resolver.resolve(["{{ context.a }}", "{{ context.b }}", "literal"])
        assert result == [1, 2, "literal"]

    def test_resolve_nested_dicts(self):
        resolver = TemplateResolver({"context": {"url": "https://example.com"}})
        result = resolver.resolve({"request": {"url": "{{ context.url }}"}})
        assert result == {"request": {"url": "https://example.com"}}


# ── resolve_templates convenience function ───────────────────────────────────


class TestResolveTemplates:
    def test_basic_context_resolution(self):
        result = resolve_templates(
            "{{ context.pr_number }}",
            context={"pr_number": 42},
        )
        assert result == 42

    def test_trigger_namespace(self):
        result = resolve_templates(
            "{{ trigger.action }}",
            trigger={"action": "opened"},
        )
        assert result == "opened"

    def test_github_namespace(self):
        result = resolve_templates(
            "{{ github.pr.mergeable }}",
            github={"pr": {"mergeable": True}},
        )
        assert result is True

    def test_branches_namespace(self):
        result = resolve_templates(
            "{{ branches.security.result }}",
            branches={"security": {"result": "pass"}},
        )
        assert result == "pass"

    def test_stages_namespace(self):
        result = resolve_templates(
            "{{ stages.review.status }}",
            stages={"review": {"status": "completed"}},
        )
        assert result == "completed"

    def test_multiple_namespaces(self):
        result = resolve_templates(
            "PR {{ context.pr }} triggered by {{ trigger.action }}",
            context={"pr": 42},
            trigger={"action": "opened"},
        )
        assert result == "PR 42 triggered by opened"

    def test_custom_filters(self):
        result = resolve_templates(
            "{{ context.val | triple }}",
            context={"val": 3},
            filters={"triple": lambda x: x * 3},
        )
        assert result == 9

    def test_no_args_empty_namespace(self):
        result = resolve_templates("static text")
        assert result == "static text"


# ── TemplateResolver: _parse_literal ─────────────────────────────────────────


class TestParseLiteral:
    def test_null(self):
        assert TemplateResolver._parse_literal("null") is None
        assert TemplateResolver._parse_literal("None") is None

    def test_booleans(self):
        assert TemplateResolver._parse_literal("true") is True
        assert TemplateResolver._parse_literal("True") is True
        assert TemplateResolver._parse_literal("false") is False
        assert TemplateResolver._parse_literal("False") is False

    def test_quoted_string(self):
        assert TemplateResolver._parse_literal('"hello"') == "hello"
        assert TemplateResolver._parse_literal("'world'") == "world"

    def test_integer(self):
        assert TemplateResolver._parse_literal("42") == 42
        assert TemplateResolver._parse_literal("-1") == -1

    def test_float(self):
        assert TemplateResolver._parse_literal("3.14") == pytest.approx(3.14)

    def test_unknown_passthrough(self):
        assert TemplateResolver._parse_literal("something") == "something"
