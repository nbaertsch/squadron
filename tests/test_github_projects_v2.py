"""Tests for GitHub Projects V2 API tools.

Covers:
- GitHubClient.graphql() — success, error propagation, variable passing
- All 9 GraphQL helper methods
- All 9 SquadronTools tool methods
- Client-side filtering by string and numeric field values
- ALL_TOOL_NAMES registration check

All tests use respx mocks — zero real network calls.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from squadron.github_client import GitHubClient
from squadron.tools.squadron_tools import (
    ALL_TOOL_NAMES,
    AddIssueToProjectParams,
    AddProjectFieldParams,
    CreateProjectParams,
    GetProjectItemsParams,
    GetProjectParams,
    ListProjectFieldsParams,
    ListProjectsParams,
    RemoveIssueFromProjectParams,
    SquadronTools,
    UpdateProjectItemFieldParams,
)

GRAPHQL_URL = "https://api.github.com/graphql"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def github():
    """GitHubClient pre-seeded with a fake installation token."""
    client = GitHubClient(
        app_id="12345",
        private_key="fake",
        webhook_secret="test-secret",
        installation_id="67890",
    )
    client._token = "ghs_fake_installation_token"
    client._token_expires_at = time.time() + 3600
    return client


@pytest.fixture
def tools():
    """SquadronTools with a fully mocked GitHubClient."""
    gh = AsyncMock()
    return SquadronTools(
        registry=AsyncMock(),
        github=gh,
        agent_inboxes={},
        owner="acme",
        repo="widgets",
    ), gh


# ── GitHubClient.graphql() ────────────────────────────────────────────────────


class TestGraphQL:
    @respx.mock
    async def test_success(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(200, json={"data": {"viewer": {"login": "bot"}}})
        )
        data = await github.graphql("{ viewer { login } }")
        assert data == {"viewer": {"login": "bot"}}

    @respx.mock
    async def test_error_propagation(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"errors": [{"message": "Not Found"}]},
            )
        )
        with pytest.raises(RuntimeError, match="GraphQL errors"):
            await github.graphql("{ bad }")

    @respx.mock
    async def test_variables_passed(self, github):
        route = respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(200, json={"data": {}})
        )
        await github.graphql("query($id: ID!) { node(id: $id) { id } }", {"id": "PVT_abc"})
        body = json.loads(route.calls[0].request.content)
        assert body["variables"] == {"id": "PVT_abc"}


# ── GitHubClient GraphQL helper methods ──────────────────────────────────────


class TestGetRepoOwnerId:
    @respx.mock
    async def test_returns_owner_id(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"repositoryOwner": {"id": "O_owner123"}}},
            )
        )
        result = await github.get_repo_owner_id("acme", "widgets")
        assert result == "O_owner123"


class TestCreateProject:
    @respx.mock
    async def test_returns_project(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "createProjectV2": {
                            "projectV2": {
                                "id": "PVT_abc",
                                "number": 1,
                                "title": "Sprint 1",
                                "url": "https://github.com/orgs/acme/projects/1",
                            }
                        }
                    }
                },
            )
        )
        project = await github.create_project("O_owner123", "Sprint 1")
        assert project["id"] == "PVT_abc"
        assert project["title"] == "Sprint 1"


class TestGetProjectByNumber:
    @respx.mock
    async def test_returns_project(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "projectV2": {
                                "id": "PVT_abc",
                                "number": 1,
                                "title": "Sprint 1",
                                "url": "https://github.com/orgs/acme/projects/1",
                            }
                        }
                    }
                },
            )
        )
        project = await github.get_project_by_number("acme", "widgets", 1)
        assert project["number"] == 1


class TestGetProjectById:
    @respx.mock
    async def test_returns_project(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "node": {
                            "id": "PVT_abc",
                            "number": 1,
                            "title": "Sprint 1",
                            "url": "https://github.com/orgs/acme/projects/1",
                        }
                    }
                },
            )
        )
        project = await github.get_project_by_id("PVT_abc")
        assert project["id"] == "PVT_abc"


class TestListProjects:
    @respx.mock
    async def test_returns_list(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "projectsV2": {
                                "nodes": [
                                    {"id": "PVT_abc", "number": 1, "title": "Sprint 1", "url": "u"}
                                ]
                            }
                        }
                    }
                },
            )
        )
        projects = await github.list_projects("acme", "widgets")
        assert len(projects) == 1
        assert projects[0]["id"] == "PVT_abc"


class TestAddProjectField:
    @respx.mock
    async def test_returns_field(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "createProjectV2Field": {
                            "projectV2Field": {
                                "id": "PVTF_123",
                                "name": "Priority",
                                "dataType": "SINGLE_SELECT",
                                "options": [
                                    {"id": "OPT_1", "name": "High"},
                                    {"id": "OPT_2", "name": "Low"},
                                ],
                            }
                        }
                    }
                },
            )
        )
        field = await github.add_project_field(
            "PVT_abc", "Priority", "SINGLE_SELECT", ["High", "Low"]
        )
        assert field["id"] == "PVTF_123"
        assert len(field["options"]) == 2


class TestListProjectFields:
    @respx.mock
    async def test_returns_fields(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "node": {
                            "fields": {
                                "nodes": [
                                    {"id": "PVTF_1", "name": "Title", "dataType": "TEXT"},
                                    {
                                        "id": "PVTF_2",
                                        "name": "Status",
                                        "dataType": "SINGLE_SELECT",
                                        "options": [{"id": "OPT_1", "name": "Todo"}],
                                    },
                                ]
                            }
                        }
                    }
                },
            )
        )
        fields = await github.list_project_fields("PVT_abc")
        assert len(fields) == 2
        assert fields[0]["name"] == "Title"


class TestGetIssueNodeId:
    @respx.mock
    async def test_returns_node_id(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"repository": {"issue": {"id": "I_issue123"}}}},
            )
        )
        node_id = await github.get_issue_node_id("acme", "widgets", 42)
        assert node_id == "I_issue123"


class TestAddIssueToProject:
    @respx.mock
    async def test_returns_item(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_item1"}}}},
            )
        )
        item = await github.add_issue_to_project("PVT_abc", "I_issue123")
        assert item["id"] == "PVTI_item1"


class TestRemoveIssueFromProject:
    @respx.mock
    async def test_no_error(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"deleteProjectV2Item": {"deletedItemId": "PVTI_item1"}}},
            )
        )
        # Should not raise
        await github.remove_issue_from_project("PVT_abc", "PVTI_item1")


class TestUpdateProjectItemField:
    @respx.mock
    async def test_returns_updated_item(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "PVTI_item1"}
                        }
                    }
                },
            )
        )
        result = await github.update_project_item_field(
            "PVT_abc", "PVTI_item1", "PVTF_123", {"singleSelectOptionId": "OPT_1"}
        )
        assert result["id"] == "PVTI_item1"


class TestGetProjectItems:
    @respx.mock
    async def test_returns_items_with_fields(self, github):
        respx.post(GRAPHQL_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "node": {
                            "items": {
                                "nodes": [
                                    {
                                        "id": "PVTI_item1",
                                        "fieldValues": {
                                            "nodes": [
                                                {
                                                    "text": "High priority",
                                                    "field": {"name": "Notes"},
                                                },
                                                {
                                                    "name": "In Progress",
                                                    "field": {"name": "Status"},
                                                },
                                            ]
                                        },
                                        "content": {"title": "Fix bug", "number": 42},
                                    }
                                ]
                            }
                        }
                    }
                },
            )
        )
        items = await github.get_project_items("PVT_abc")
        assert len(items) == 1
        assert items[0]["title"] == "Fix bug"
        assert items[0]["fields"]["Notes"] == "High priority"
        assert items[0]["fields"]["Status"] == "In Progress"


# ── SquadronTools tool methods ────────────────────────────────────────────────


class TestCreateProjectTool:
    async def test_creates_project(self, tools):
        sq, gh = tools
        gh.get_repo_owner_id = AsyncMock(return_value="O_owner123")
        gh.create_project = AsyncMock(
            return_value={"id": "PVT_abc", "number": 1, "title": "Sprint 1", "url": "u"}
        )
        result = await sq.create_project(
            "agent1", CreateProjectParams(owner="acme", title="Sprint 1")
        )
        data = json.loads(result)
        assert data["id"] == "PVT_abc"
        gh.get_repo_owner_id.assert_called_once_with("acme", "widgets")
        gh.create_project.assert_called_once_with("O_owner123", "Sprint 1")


class TestGetProjectTool:
    async def test_by_id(self, tools):
        sq, gh = tools
        gh.get_project_by_id = AsyncMock(
            return_value={"id": "PVT_abc", "number": 1, "title": "Sprint 1", "url": "u"}
        )
        result = await sq.get_project("agent1", GetProjectParams(project_id="PVT_abc"))
        data = json.loads(result)
        assert data["id"] == "PVT_abc"

    async def test_by_number(self, tools):
        sq, gh = tools
        gh.get_project_by_number = AsyncMock(
            return_value={"id": "PVT_abc", "number": 1, "title": "Sprint 1", "url": "u"}
        )
        result = await sq.get_project("agent1", GetProjectParams(project_number=1))
        data = json.loads(result)
        assert data["number"] == 1

    async def test_missing_params_raises(self, tools):
        sq, gh = tools
        with pytest.raises(ValueError):
            await sq.get_project("agent1", GetProjectParams())


class TestListProjectsTool:
    async def test_returns_list(self, tools):
        sq, gh = tools
        gh.list_projects = AsyncMock(
            return_value=[{"id": "PVT_abc", "number": 1, "title": "Sprint 1", "url": "u"}]
        )
        result = await sq.list_projects("agent1", ListProjectsParams())
        data = json.loads(result)
        assert len(data) == 1


class TestAddProjectFieldTool:
    async def test_adds_field(self, tools):
        sq, gh = tools
        gh.add_project_field = AsyncMock(
            return_value={"id": "PVTF_123", "name": "Priority", "dataType": "SINGLE_SELECT"}
        )
        result = await sq.add_project_field(
            "agent1",
            AddProjectFieldParams(
                project_id="PVT_abc",
                name="Priority",
                data_type="SINGLE_SELECT",
                options=["High", "Low"],
            ),
        )
        data = json.loads(result)
        assert data["id"] == "PVTF_123"


class TestListProjectFieldsTool:
    async def test_lists_fields(self, tools):
        sq, gh = tools
        gh.list_project_fields = AsyncMock(
            return_value=[{"id": "PVTF_1", "name": "Title", "dataType": "TEXT"}]
        )
        result = await sq.list_project_fields(
            "agent1", ListProjectFieldsParams(project_id="PVT_abc")
        )
        data = json.loads(result)
        assert data[0]["name"] == "Title"


class TestAddIssueToProjectTool:
    async def test_adds_issue(self, tools):
        sq, gh = tools
        gh.get_issue_node_id = AsyncMock(return_value="I_issue123")
        gh.add_issue_to_project = AsyncMock(return_value={"id": "PVTI_item1"})
        result = await sq.add_issue_to_project(
            "agent1", AddIssueToProjectParams(project_id="PVT_abc", issue_number=42)
        )
        data = json.loads(result)
        assert data["id"] == "PVTI_item1"
        gh.get_issue_node_id.assert_called_once_with("acme", "widgets", 42)


class TestRemoveIssueFromProjectTool:
    async def test_removes_issue(self, tools):
        sq, gh = tools
        gh.remove_issue_from_project = AsyncMock()
        result = await sq.remove_issue_from_project(
            "agent1",
            RemoveIssueFromProjectParams(project_id="PVT_abc", item_id="PVTI_item1"),
        )
        assert "PVTI_item1" in result
        gh.remove_issue_from_project.assert_called_once_with("PVT_abc", "PVTI_item1")


class TestUpdateProjectItemFieldTool:
    async def test_updates_field(self, tools):
        sq, gh = tools
        gh.update_project_item_field = AsyncMock(return_value={"id": "PVTI_item1"})
        result = await sq.update_project_item_field(
            "agent1",
            UpdateProjectItemFieldParams(
                project_id="PVT_abc",
                item_id="PVTI_item1",
                field_id="PVTF_123",
                value={"singleSelectOptionId": "OPT_1"},
            ),
        )
        data = json.loads(result)
        assert data["id"] == "PVTI_item1"


class TestGetProjectItemsTool:
    async def test_returns_items(self, tools):
        sq, gh = tools
        gh.get_project_items = AsyncMock(
            return_value=[
                {
                    "id": "PVTI_item1",
                    "title": "Fix bug",
                    "number": 42,
                    "fields": {"Status": "Todo"},
                },
                {
                    "id": "PVTI_item2",
                    "title": "Add feature",
                    "number": 43,
                    "fields": {"Status": "Done"},
                },
            ]
        )
        result = await sq.get_project_items(
            "agent1", GetProjectItemsParams(project_id="PVT_abc")
        )
        data = json.loads(result)
        assert len(data) == 2

    async def test_string_field_filter(self, tools):
        sq, gh = tools
        gh.get_project_items = AsyncMock(
            return_value=[
                {
                    "id": "PVTI_item1",
                    "title": "Fix bug",
                    "number": 42,
                    "fields": {"Status": "Todo"},
                },
                {
                    "id": "PVTI_item2",
                    "title": "Add feature",
                    "number": 43,
                    "fields": {"Status": "Done"},
                },
            ]
        )
        result = await sq.get_project_items(
            "agent1",
            GetProjectItemsParams(
                project_id="PVT_abc", filter_field="Status", filter_value="Todo"
            ),
        )
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["id"] == "PVTI_item1"

    async def test_numeric_field_filter(self, tools):
        sq, gh = tools
        gh.get_project_items = AsyncMock(
            return_value=[
                {"id": "PVTI_item1", "title": "A", "number": 1, "fields": {"Points": 5}},
                {"id": "PVTI_item2", "title": "B", "number": 2, "fields": {"Points": 8}},
            ]
        )
        result = await sq.get_project_items(
            "agent1",
            GetProjectItemsParams(
                project_id="PVT_abc", filter_field="Points", filter_value="5"
            ),
        )
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["id"] == "PVTI_item1"


# ── ALL_TOOL_NAMES registration ───────────────────────────────────────────────


class TestAllToolNamesRegistration:
    def test_all_project_tools_registered(self):
        expected = {
            "create_project",
            "get_project",
            "list_projects",
            "add_project_field",
            "list_project_fields",
            "add_issue_to_project",
            "remove_issue_from_project",
            "update_project_item_field",
            "get_project_items",
        }
        assert expected.issubset(set(ALL_TOOL_NAMES))
