from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from proxy.app.config import settings as app_settings
from proxy.app.main import app


def _mcpproxy_response(status: int = 200, body: bytes = b'{"status":"accepted"}'):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.headers = {"content-type": "application/json"}
    return resp


async def test_mcp_call_valid_scope_reaches_mcpproxy(auth_client, jwt_factory):
    """AC2: a valid Bearer token with mcp:{server}:invoke scope reaches the MCP Reverse Proxy."""
    client, _ = auth_client
    app.state.mcpproxy_client.post = AsyncMock(return_value=_mcpproxy_response())
    token = jwt_factory(scope="mcp:github-mcp:invoke")

    response = await client.post(
        "/v1/mcp/github-mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": {"name": "create_pr"}},
    )

    assert response.status_code == 200
    app.state.mcpproxy_client.post.assert_awaited_once()


async def test_mcp_call_missing_scope_rejected(auth_client, jwt_factory):
    """AC3: missing the mcp:{server}:invoke scope is rejected with 403, mcpproxy never called."""
    client, _ = auth_client
    app.state.mcpproxy_client.post = AsyncMock(return_value=_mcpproxy_response())
    token = jwt_factory(scope="mcp:other-server:invoke")

    response = await client.post(
        "/v1/mcp/github-mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": {"name": "create_pr"}},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "missing_scope"
    app.state.mcpproxy_client.post.assert_not_awaited()


async def test_mcp_call_no_scope_claim_rejected(auth_client, jwt_factory):
    """AC3: a token with no scope claim at all is rejected the same way."""
    client, _ = auth_client
    app.state.mcpproxy_client.post = AsyncMock(return_value=_mcpproxy_response())
    token = jwt_factory()

    response = await client.post(
        "/v1/mcp/github-mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": {"name": "create_pr"}},
    )

    assert response.status_code == 403
    app.state.mcpproxy_client.post.assert_not_awaited()


async def test_mcp_call_agent_client_missing_act_claim_rejected(auth_client, jwt_factory):
    """AC4: a registered agent-runtime client ID with no act claim is rejected 401 missing_act_claim."""
    client, _ = auth_client
    app.state.mcpproxy_client.post = AsyncMock(return_value=_mcpproxy_response())
    original = app_settings.agent_runtime_client_ids
    app_settings.agent_runtime_client_ids = ["agent-runtime-1"]
    try:
        token = jwt_factory(scope="mcp:github-mcp:invoke", client_id="agent-runtime-1")

        response = await client.post(
            "/v1/mcp/github-mcp/call",
            headers={"Authorization": f"Bearer {token}"},
            json={"tool": {"name": "create_pr"}},
        )

        assert response.status_code == 401
        assert response.json()["detail"]["error"]["type"] == "missing_act_claim"
        app.state.mcpproxy_client.post.assert_not_awaited()
    finally:
        app_settings.agent_runtime_client_ids = original


async def test_mcp_call_agent_client_duplicate_act_sub_rejected(auth_client, jwt_factory):
    """AC4: act.sub equal to sub (not distinct) is also rejected as a missing act claim."""
    client, _ = auth_client
    app.state.mcpproxy_client.post = AsyncMock(return_value=_mcpproxy_response())
    original = app_settings.agent_runtime_client_ids
    app_settings.agent_runtime_client_ids = ["agent-runtime-1"]
    try:
        token = jwt_factory(
            scope="mcp:github-mcp:invoke",
            client_id="agent-runtime-1",
            act_sub="test-user",  # matches make_jwt's default user_id -> not distinct
        )

        response = await client.post(
            "/v1/mcp/github-mcp/call",
            headers={"Authorization": f"Bearer {token}"},
            json={"tool": {"name": "create_pr"}},
        )

        assert response.status_code == 401
        assert response.json()["detail"]["error"]["type"] == "missing_act_claim"
        app.state.mcpproxy_client.post.assert_not_awaited()
    finally:
        app_settings.agent_runtime_client_ids = original


async def test_mcp_call_human_caller_proceeds(auth_client, jwt_factory):
    """AC5: a human caller (client_id not registered as an agent runtime) proceeds."""
    client, _ = auth_client
    app.state.mcpproxy_client.post = AsyncMock(return_value=_mcpproxy_response())
    token = jwt_factory(scope="mcp:github-mcp:invoke")

    response = await client.post(
        "/v1/mcp/github-mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": {"name": "create_pr"}},
    )

    assert response.status_code == 200
    app.state.mcpproxy_client.post.assert_awaited_once()


async def test_mcp_call_agent_with_valid_act_claim_proceeds(auth_client, jwt_factory):
    """AC5: an agent caller with a valid, distinct act claim proceeds to the MCP Reverse Proxy."""
    client, _ = auth_client
    app.state.mcpproxy_client.post = AsyncMock(return_value=_mcpproxy_response())
    original = app_settings.agent_runtime_client_ids
    app_settings.agent_runtime_client_ids = ["agent-runtime-1"]
    try:
        token = jwt_factory(
            scope="mcp:github-mcp:invoke",
            client_id="agent-runtime-1",
            act_sub="delegating-human-user",
        )

        response = await client.post(
            "/v1/mcp/github-mcp/call",
            headers={"Authorization": f"Bearer {token}"},
            json={"tool": {"name": "create_pr"}},
        )

        assert response.status_code == 200
        app.state.mcpproxy_client.post.assert_awaited_once()
    finally:
        app_settings.agent_runtime_client_ids = original


async def test_mcp_call_server_segment_passed_through_unmodified(auth_client, jwt_factory):
    """AC7: the {server} path segment is forwarded unmodified to mcpproxy as tool.server."""
    client, _ = auth_client
    mock_post = AsyncMock(return_value=_mcpproxy_response())
    app.state.mcpproxy_client.post = mock_post
    token = jwt_factory(scope="mcp:weird-Server_Name.42:invoke")

    response = await client.post(
        "/v1/mcp/weird-Server_Name.42/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": {"name": "create_pr"}, "context": {"resource": "repo:org/repo"}},
    )

    assert response.status_code == 200
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["tool"]["server"] == "weird-Server_Name.42"
    assert kwargs["json"]["tool"]["name"] == "create_pr"
