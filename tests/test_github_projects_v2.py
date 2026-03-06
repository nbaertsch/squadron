"""Tests for GitHub Projects V2 API tools.

Tests the GraphQL-backed Projects V2 tools added to GitHubClient and
SquadronTools in issue #152. Uses respx to mock the GitHub GraphQL endpoint.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from squadron.github_client import GitHubClient
from squadron.tools.squadron_tools import (
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

# ── Helpers ───────────────────────────────────────────────────────────────────

GRAPHQL_URL = "https://api.github.com/graphql"


def graphql_response(data: dict) -> httpx.Response:
    """Build a mock GraphQL success response."""
    return httpx.Response(200, json={"data": data})


def graphql_error_response(message: str) -> httpx.Response:
    """Build a mock GraphQL error response."""
    return httpx.Response(200, json={"errors": [{"message": message}]})


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def github():
    """GitHubClient with a pre-loaded fake token (skips JWT exchange)."""
    client = GitHubClient(
        app_id="12345",
        private_key="fake",
        webhook_secret="test-secret",
        installation_id="67890",
    )
    client._token = "ghs_fake_token"
    client._token_expires_at = time.time() + 3600
    return client


@pytest.fixture
async def started_github(github):
    """Started GitHubClient."""
    await github.start()
    yield github
    await github.close()


@pytest.fixture
def tools(started_github):
    """SquadronTools bound to the started github client (minimal setup)."""
    return SquadronTools(
        registry=None,  # not needed for Projects V2 tools
        github=started_github,
        agent_inboxes={},
        owner="nbaertsch",
        repo="squadron",
    )


# ── GitHubClient.graphql ──────────────────────────────────────────────────────


class TestGraphQL:
    @respx.mock
    async def test_success(self, started_github):
        route = respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({"viewer": {"login": "bot"}})
        )
        data = await started_github.graphql("{ viewer { login } }")
        assert route.called
        assert data == {"viewer": {"login": "bot"}}
        req = route.calls[0].request
        assert req.headers["Authorization"] == "token ghs_fake_token"

    @respx.mock
    async def test_graphql_errors_raise(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_error_response("Not Found")
        )
        with pytest.raises(RuntimeError, match="GraphQL error.*Not Found"):
            await started_github.graphql("{ viewer { login } }")

    @respx.mock
    async def test_variables_sent(self, started_github):
        route = respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({"repository": {"owner": {"id": "MDQ6VXNlcjE=", "login": "nbaertsch"}}})
        )
        await started_github.graphql("query($owner: String!) { repository(owner: $owner) { owner { id login } } }", {"owner": "nbaertsch"})
        body = json.loads(route.calls[0].request.content)
        assert body["variables"] == {"owner": "nbaertsch"}


# ── GitHubClient: get_repo_owner_id ──────────────────────────────────────────


class TestGetRepoOwnerId:
    @respx.mock
    async def test_returns_owner_id(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "repository": {"owner": {"id": "MDQ6VXNlcjE=", "login": "nbaertsch"}}
            })
        )
        owner_id = await started_github.get_repo_owner_id("nbaertsch", "squadron")
        assert owner_id == "MDQ6VXNlcjE="


# ── GitHubClient: create_project ─────────────────────────────────────────────


class TestCreateProjectClient:
    @respx.mock
    async def test_returns_project_fields(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "createProjectV2": {
                    "projectV2": {
                        "id": "PVT_abc123",
                        "number": 1,
                        "title": "Epic Board",
                        "url": "https://github.com/orgs/nbaertsch/projects/1",
                    }
                }
            })
        )
        result = await started_github.create_project("MDQ6VXNlcjE=", "Epic Board")
        assert result["id"] == "PVT_abc123"
        assert result["number"] == 1
        assert result["title"] == "Epic Board"


# ── GitHubClient: list_projects ───────────────────────────────────────────────


class TestListProjectsClient:
    @respx.mock
    async def test_returns_project_list(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "repository": {
                    "projectsV2": {
                        "nodes": [
                            {
                                "id": "PVT_abc123",
                                "number": 1,
                                "title": "Epic Board",
                                "shortDescription": "Main board",
                                "url": "https://github.com/orgs/nbaertsch/projects/1",
                                "items": {"totalCount": 5},
                            }
                        ]
                    }
                }
            })
        )
        projects = await started_github.list_projects("nbaertsch", "squadron")
        assert len(projects) == 1
        assert projects[0]["id"] == "PVT_abc123"
        assert projects[0]["items"]["totalCount"] == 5


# ── GitHubClient: add_issue_to_project ────────────────────────────────────────


class TestAddIssueToProjectClient:
    @respx.mock
    async def test_returns_item_id(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "addProjectV2ItemById": {"item": {"id": "PVTI_xyz789"}}
            })
        )
        item_id = await started_github.add_issue_to_project("PVT_abc123", "I_issue456")
        assert item_id == "PVTI_xyz789"


# ── GitHubClient: remove_issue_from_project ───────────────────────────────────


class TestRemoveIssueFromProjectClient:
    @respx.mock
    async def test_returns_deleted_id(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "deleteProjectV2Item": {"deletedItemId": "PVTI_xyz789"}
            })
        )
        deleted = await started_github.remove_issue_from_project("PVT_abc123", "PVTI_xyz789")
        assert deleted == "PVTI_xyz789"


# ── GitHubClient: update_project_item_field ───────────────────────────────────


class TestUpdateProjectItemFieldClient:
    @respx.mock
    async def test_text_field(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_xyz789"}}
            })
        )
        result = await started_github.update_project_item_field(
            "PVT_abc123", "PVTI_xyz789", "FIELD_id", {"text": "hello"}
        )
        assert result == "PVTI_xyz789"

    @respx.mock
    async def test_number_field(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_xyz789"}}
            })
        )
        result = await started_github.update_project_item_field(
            "PVT_abc123", "PVTI_xyz789", "FIELD_wave", {"number": 1}
        )
        assert result == "PVTI_xyz789"


# ── GitHubClient: get_project_items ──────────────────────────────────────────


class TestGetProjectItemsClient:
    @respx.mock
    async def test_flattens_field_values(self, started_github):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "PVTI_xyz789",
                                "type": "ISSUE",
                                "content": {
                                    "id": "I_issue152",
                                    "number": 152,
                                    "title": "feat: Projects V2 tools",
                                    "url": "https://github.com/nbaertsch/squadron/issues/152",
                                    "state": "OPEN",
                                },
                                "fieldValues": {
                                    "nodes": [
                                        {
                                            "field": {"name": "Status"},
                                            "name": "In Progress",
                                            "optionId": "opt_123",
                                        },
                                        {
                                            "field": {"name": "Wave"},
                                            "number": 1,
                                        },
                                        {
                                            "field": {"name": "Agent Role"},
                                            "text": "feat-dev",
                                        },
                                    ]
                                },
                            }
                        ]
                    }
                }
            })
        )
        items = await started_github.get_project_items("PVT_abc123")
        assert len(items) == 1
        item = items[0]
        assert item["id"] == "PVTI_xyz789"
        assert item["type"] == "ISSUE"
        assert item["content"]["number"] == 152
        assert item["fields"]["Status"] == "In Progress"
        assert item["fields"]["Wave"] == 1
        assert item["fields"]["Agent Role"] == "feat-dev"

    @respx.mock
    async def test_handles_null_field_values(self, started_github):
        """Items with no field values should not raise."""
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "PVTI_empty",
                                "type": "ISSUE",
                                "content": {"id": "I_1", "number": 1, "title": "Test", "url": "", "state": "OPEN"},
                                "fieldValues": {"nodes": [None, None]},
                            }
                        ]
                    }
                }
            })
        )
        items = await started_github.get_project_items("PVT_abc123")
        assert len(items) == 1
        assert items[0]["fields"] == {}


# ── SquadronTools: create_project ────────────────────────────────────────────


class TestCreateProjectTool:
    @respx.mock
    async def test_returns_project_data(self, tools):
        # First call: get owner ID
        # Second call: create project
        # Third call (optional): set description
        call_count = 0

        def handle_graphql(request):
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            if "getRepoOwner" in body["query"] or "owner { id" in body["query"]:
                return graphql_response({"repository": {"owner": {"id": "MDQ6VXNlcjE=", "login": "nbaertsch"}}})
            elif "createProjectV2" in body["query"]:
                return graphql_response({
                    "createProjectV2": {
                        "projectV2": {
                            "id": "PVT_abc123",
                            "number": 1,
                            "title": "Test Board",
                            "url": "https://github.com/orgs/nbaertsch/projects/1",
                        }
                    }
                })
            elif "updateProjectV2" in body["query"]:
                return graphql_response({"updateProjectV2": {"projectV2": {"id": "PVT_abc123"}}})
            return graphql_response({})

        respx.post(GRAPHQL_URL).mock(side_effect=handle_graphql)

        result = await tools.create_project("agent-1", CreateProjectParams(title="Test Board", description="A test project"))
        data = json.loads(result)
        assert data["project_id"] == "PVT_abc123"
        assert data["number"] == 1
        assert data["title"] == "Test Board"
        assert "url" in data


# ── SquadronTools: list_projects ─────────────────────────────────────────────


class TestListProjectsTool:
    @respx.mock
    async def test_returns_all_projects(self, tools):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "repository": {
                    "projectsV2": {
                        "nodes": [
                            {
                                "id": "PVT_abc123",
                                "number": 1,
                                "title": "Board One",
                                "shortDescription": "First board",
                                "url": "https://github.com/orgs/nbaertsch/projects/1",
                                "items": {"totalCount": 3},
                            },
                            {
                                "id": "PVT_def456",
                                "number": 2,
                                "title": "Board Two",
                                "shortDescription": None,
                                "url": "https://github.com/orgs/nbaertsch/projects/2",
                                "items": {"totalCount": 0},
                            },
                        ]
                    }
                }
            })
        )
        result = await tools.list_projects("agent-1", ListProjectsParams())
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["id"] == "PVT_abc123"
        assert data[0]["item_count"] == 3
        assert data[1]["description"] == ""  # None → ""


# ── SquadronTools: add_project_field ─────────────────────────────────────────


class TestAddProjectFieldTool:
    @respx.mock
    async def test_text_field(self, tools):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "createProjectV2Field": {
                    "projectV2Field": {
                        "id": "PVTF_effort",
                        "name": "Effort",
                        "dataType": "TEXT",
                    }
                }
            })
        )
        result = await tools.add_project_field(
            "agent-1",
            AddProjectFieldParams(project_id="PVT_abc123", name="Effort", data_type="TEXT"),
        )
        data = json.loads(result)
        assert data["id"] == "PVTF_effort"
        assert data["data_type"] == "TEXT"

    @respx.mock
    async def test_single_select_field(self, tools):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "createProjectV2Field": {
                    "projectV2Field": {
                        "id": "PVTSSF_status",
                        "name": "Status",
                        "dataType": "SINGLE_SELECT",
                        "options": [
                            {"id": "opt_backlog", "name": "Backlog", "color": "GRAY", "description": ""},
                            {"id": "opt_inprog", "name": "In Progress", "color": "BLUE", "description": ""},
                            {"id": "opt_blocked", "name": "Blocked", "color": "RED", "description": ""},
                            {"id": "opt_done", "name": "Done", "color": "GREEN", "description": ""},
                        ],
                    }
                }
            })
        )
        result = await tools.add_project_field(
            "agent-1",
            AddProjectFieldParams(
                project_id="PVT_abc123",
                name="Status",
                data_type="SINGLE_SELECT",
                single_select_options=[
                    {"name": "Backlog", "color": "GRAY"},
                    {"name": "In Progress", "color": "BLUE"},
                    {"name": "Blocked", "color": "RED"},
                    {"name": "Done", "color": "GREEN"},
                ],
            ),
        )
        data = json.loads(result)
        assert data["id"] == "PVTSSF_status"
        assert data["data_type"] == "SINGLE_SELECT"
        assert len(data["options"]) == 4
        option_names = [o["name"] for o in data["options"]]
        assert "Backlog" in option_names
        assert "Done" in option_names


# ── SquadronTools: add_issue_to_project ──────────────────────────────────────


class TestAddIssueToProjectTool:
    @respx.mock
    async def test_returns_item_id(self, tools):
        call_num = 0

        def handle(request):
            nonlocal call_num
            call_num += 1
            body = json.loads(request.content)
            if "getIssueId" in body["query"] or "issue(number:" in body["query"]:
                return graphql_response({
                    "repository": {"issue": {"id": "I_issue152"}}
                })
            else:
                return graphql_response({
                    "addProjectV2ItemById": {"item": {"id": "PVTI_new_item"}}
                })

        respx.post(GRAPHQL_URL).mock(side_effect=handle)

        result = await tools.add_issue_to_project(
            "agent-1",
            AddIssueToProjectParams(project_id="PVT_abc123", issue_number=152),
        )
        data = json.loads(result)
        assert data["item_id"] == "PVTI_new_item"
        assert data["issue_number"] == 152


# ── SquadronTools: remove_issue_from_project ──────────────────────────────────


class TestRemoveIssueFromProjectTool:
    @respx.mock
    async def test_returns_confirmation(self, tools):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "deleteProjectV2Item": {"deletedItemId": "PVTI_xyz789"}
            })
        )
        result = await tools.remove_issue_from_project(
            "agent-1",
            RemoveIssueFromProjectParams(
                project_id="PVT_abc123", item_id="PVTI_xyz789"
            ),
        )
        assert "PVTI_xyz789" in result


# ── SquadronTools: update_project_item_field ──────────────────────────────────


class TestUpdateProjectItemFieldTool:
    @respx.mock
    async def test_returns_confirmation(self, tools):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_xyz789"}}
            })
        )
        result = await tools.update_project_item_field(
            "agent-1",
            UpdateProjectItemFieldParams(
                project_id="PVT_abc123",
                item_id="PVTI_xyz789",
                field_id="PVTSSF_status",
                value={"singleSelectOptionId": "opt_inprog"},
            ),
        )
        assert "PVTI_xyz789" in result


# ── SquadronTools: get_project_items ─────────────────────────────────────────


class TestGetProjectItemsTool:
    @respx.mock
    async def test_returns_all_items(self, tools):
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "PVTI_1",
                                "type": "ISSUE",
                                "content": {"id": "I_1", "number": 152, "title": "feat: tools", "url": "", "state": "OPEN"},
                                "fieldValues": {"nodes": [
                                    {"field": {"name": "Status"}, "name": "In Progress", "optionId": "opt_1"},
                                    {"field": {"name": "Wave"}, "number": 1},
                                ]},
                            },
                            {
                                "id": "PVTI_2",
                                "type": "ISSUE",
                                "content": {"id": "I_2", "number": 153, "title": "feat: pm", "url": "", "state": "OPEN"},
                                "fieldValues": {"nodes": [
                                    {"field": {"name": "Status"}, "name": "Blocked", "optionId": "opt_2"},
                                    {"field": {"name": "Wave"}, "number": 2},
                                ]},
                            },
                        ]
                    }
                }
            })
        )
        result = await tools.get_project_items(
            "agent-1",
            GetProjectItemsParams(project_id="PVT_abc123"),
        )
        items = json.loads(result)
        assert len(items) == 2

    @respx.mock
    async def test_filter_by_status(self, tools):
        """Client-side filtering by field value works correctly."""
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "PVTI_1",
                                "type": "ISSUE",
                                "content": {"id": "I_1", "number": 152, "title": "feat: tools", "url": "", "state": "OPEN"},
                                "fieldValues": {"nodes": [
                                    {"field": {"name": "Status"}, "name": "In Progress", "optionId": "opt_1"},
                                ]},
                            },
                            {
                                "id": "PVTI_2",
                                "type": "ISSUE",
                                "content": {"id": "I_2", "number": 153, "title": "feat: pm", "url": "", "state": "OPEN"},
                                "fieldValues": {"nodes": [
                                    {"field": {"name": "Status"}, "name": "Blocked", "optionId": "opt_2"},
                                ]},
                            },
                        ]
                    }
                }
            })
        )
        result = await tools.get_project_items(
            "agent-1",
            GetProjectItemsParams(
                project_id="PVT_abc123",
                filter_field="Status",
                filter_value="Blocked",
            ),
        )
        items = json.loads(result)
        assert len(items) == 1
        assert items[0]["id"] == "PVTI_2"

    @respx.mock
    async def test_filter_by_wave_number(self, tools):
        """Numeric filtering works for number fields."""
        respx.post(GRAPHQL_URL).mock(
            return_value=graphql_response({
                "node": {
                    "items": {
                        "nodes": [
                            {
                                "id": "PVTI_1",
                                "type": "ISSUE",
                                "content": {"id": "I_1", "number": 152, "title": "Wave 1 issue", "url": "", "state": "OPEN"},
                                "fieldValues": {"nodes": [{"field": {"name": "Wave"}, "number": 1}]},
                            },
                            {
                                "id": "PVTI_2",
                                "type": "ISSUE",
                                "content": {"id": "I_2", "number": 153, "title": "Wave 2 issue", "url": "", "state": "OPEN"},
                                "fieldValues": {"nodes": [{"field": {"name": "Wave"}, "number": 2}]},
                            },
                        ]
                    }
                }
            })
        )
        result = await tools.get_project_items(
            "agent-1",
            GetProjectItemsParams(
                project_id="PVT_abc123",
                filter_field="Wave",
                filter_value="1",
            ),
        )
        items = json.loads(result)
        assert len(items) == 1
        assert items[0]["fields"]["Wave"] == 1


# ── ALL_TOOL_NAMES registry ───────────────────────────────────────────────────


class TestToolNamesRegistry:
    def test_all_projects_v2_tools_registered(self):
        from squadron.tools.squadron_tools import ALL_TOOL_NAMES

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
        registered = set(ALL_TOOL_NAMES)
        missing = expected - registered
        assert not missing, f"Missing from ALL_TOOL_NAMES: {missing}"
