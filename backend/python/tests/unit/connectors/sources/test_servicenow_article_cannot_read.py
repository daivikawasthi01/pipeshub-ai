"""ServiceNow article Cannot Read: which readers an article keeps, loses or is skipped for."""

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest
from test_servicenow_connector import TestCriteriaDenyAndMatchAll as T
from test_servicenow_connector import (  # noqa: F401
    mock_config_service,
    mock_data_entities_processor,
    mock_data_store_provider,
    mock_logger,
    servicenow_connector,
)

from app.config.constants.arangodb import Connectors
from app.models.entities import AppUser
from app.models.permission import PermissionType
from app.sources.external.servicenow.models import KBKnowledge, TableAPIResponse


def _art(**kw) -> KBKnowledge:
    return KBKnowledge(sys_id="a1", short_description="t", kb_category="c1", kb_knowledge_base="kb1", **kw)


@pytest.mark.asyncio
async def test_the_kbs_own_cannot_read_still_applies_to_an_article_with_a_deny(servicenow_connector) -> None:  # noqa: F811
    sc = servicenow_connector
    T._wire(sc, {"kbcan": {"sys_id": "kbcan", "user": "u1,u2,u3"}, "kbdeny": {"sys_id": "kbdeny", "user": "u1"},
                 "deny1": {"sys_id": "deny1", "user": "u3"}}, {})
    sc._fetch_kb_permissions_from_criteria = AsyncMock(
        return_value={"read": ["kbcan"], "cannot_read": ["kbdeny"], "write": []})
    perms = await sc._article_deny_permissions(_art(cannot_read_user_criteria="deny1"), [], ["deny1"])
    assert T._users(perms) == ["u2"]


@pytest.mark.asyncio
async def test_with_the_override_property_the_own_allow_minus_deny_is_granted(servicenow_connector) -> None:  # noqa: F811
    sc = servicenow_connector
    sc.apply_article_read_criteria = True
    T._wire(sc, {"own": {"sys_id": "own", "user": "u2,u3,u4"}, "deny1": {"sys_id": "deny1", "user": "u3"}}, {})
    sc._fetch_kb_permissions_from_criteria = AsyncMock(side_effect=AssertionError("KB must not be read"))
    perms = await sc._article_deny_permissions(
        _art(can_read_user_criteria="own", cannot_read_user_criteria="deny1"), ["own"], ["deny1"])
    assert T._users(perms) == ["u2", "u4"]
    assert all(p.type == PermissionType.READ for p in perms)


@pytest.mark.asyncio
async def test_admin_owner_and_manager_keep_an_article_with_its_own_cannot_read(servicenow_connector) -> None:  # noqa: F811
    sc = servicenow_connector
    T._wire(sc, {"kbcan": {"sys_id": "kbcan", "user": "u1,u2,u3,adm,own,mgr"},
                 "deny1": {"sys_id": "deny1", "user": "u3,adm,own,mgr"}}, {})
    sc._fetch_kb_permissions_from_criteria = AsyncMock(return_value={"read": ["kbcan"], "cannot_read": [], "write": []})
    sc._kb_privileged_users = type(sc)._kb_privileged_users.__get__(sc)  # the real one
    sc._get_admin_users = AsyncMock(return_value=[AppUser(
        app_name=Connectors.SERVICENOW, connector_id="c", source_user_id="adm", email="adm@x.com", full_name="Admin")])
    sc._get_fresh_datasource.return_value.get_now_table_tableName.side_effect = _kb_row_then(
        sc._get_fresh_datasource.return_value.get_now_table_tableName.side_effect)
    perms = await sc._article_deny_permissions(_art(cannot_read_user_criteria="deny1"), [], ["deny1"])
    assert T._users(perms) == ["adm", "mgr", "own", "u1", "u2"]  # u3 (a plain denied user) stays denied


def _kb_row_then(real) -> Callable[..., Awaitable[TableAPIResponse]]:
    from test_servicenow_connector import _table_api_response

    async def table(tableName, **kw) -> TableAPIResponse:
        if tableName == "kb_knowledge_base":
            return _table_api_response([{"owner": "own", "kb_managers": "mgr,u9"}])
        return await real(tableName=tableName, **kw)

    return table
