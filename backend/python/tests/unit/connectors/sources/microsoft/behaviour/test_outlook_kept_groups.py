"""A failed Outlook group member read records the group as kept."""

from ms_graph_fakes import graph_error
from test_outlook_sync_behaviour import (  # noqa: F401
    ANA,
    GROUP,
    api,
    connector,
    db,
    serve_group,
)


async def test_a_failed_member_read_notes_the_group_as_kept(connector, api, db) -> None:  # noqa: F811
    serve_group(api, graph_error(500, "ErrorInternalServerError"))
    db.user_groups[GROUP] = [ANA]

    await connector.run_sync()

    assert db.kept_user_groups == [GROUP]
