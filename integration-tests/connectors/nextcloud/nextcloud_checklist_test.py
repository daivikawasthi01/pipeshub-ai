# pyright: ignore-file

"""
Nextcloud connector: the connector-checklist audit cases, live
==============================================================

Same fixture as nextcloud_integration_test.py (its own connector and run
folder, full sync first).

  TC-ACTIVITY-BACKLOG   — more than one page of activity (Nextcloud gives 200 a page) is caught up in one sync
  TC-NO-FS-ACCESS       — an app password without filesystem access removes nothing and cannot be turned on
  TC-MOUNT-OFFLINE      — what was synced from external storage that went offline is kept
  TC-FAILURE-NOTIFIED   — a sync that fails tells the user, naming the connector

The last two need ``occ`` on the server (``NEXTCLOUD_CONTAINER``, or the stack's
nextcloud-source service) and skip without it.
"""

import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from connector_lifecycle import create_connector_and_await_sync, destructor  # type: ignore[import-not-found]
from connectors.nextcloud.nextcloud_source_helper import (  # type: ignore[import-not-found]
    NextcloudSourceHelper,
)
from helper.compose_control import ComposeUnavailable
from helper.graph_provider import GraphProviderProtocol
from helper.graph_provider_utils import wait_for_sync_completion
from helper.storage_incremental import (
    DEFAULT_SYNC_TIMEOUT_SEC,
    settle_record_baseline,
    sync_until_names_visible,
)
from pipeshub_client import (
    PipeshubClient,  # type: ignore[import-not-found]
)

logger = logging.getLogger("nextcloud-checklist-test")

# More rows than one page of the Activity API holds.
BACKLOG_FILES = 230


def _occ_or_skip(source: NextcloudSourceHelper, *args: str) -> str:
    try:
        return source.occ(*args)
    except ComposeUnavailable as e:
        pytest.skip(f"occ is not available on the test Nextcloud: {e}")


def _instance_name(client: PipeshubClient, connector_id: str) -> str:
    connector = client.get_connector(connector_id)
    return str(connector.get("instanceName") or connector.get("name") or "")


def _notification_titled(client: PipeshubClient, prefix: str, timeout: float = 60) -> str | None:
    """The title of a notification of the user's that starts with ``prefix``; they go through the broker."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.request("GET", "/api/v1/notifications", params={"limit": 50})
        if resp.status_code == 200:
            for n in resp.json().get("notifications", []):
                if str(n.get("title", "")).startswith(prefix):
                    return n["title"]
        time.sleep(3)
    return None


async def _full_sync(client: PipeshubClient, graph: GraphProviderProtocol, connector_id: str) -> None:
    client.resync_connector(connector_id, full_sync=True)
    await wait_for_sync_completion(client, graph, connector_id, timeout=DEFAULT_SYNC_TIMEOUT_SEC)


@pytest.mark.integration
@pytest.mark.nextcloud
@pytest.mark.asyncio(loop_scope="session")
class TestNextcloudChecklist:
    @pytest.mark.order(1)
    async def test_tc_activity_backlog_caught_up_in_one_sync(
        self,
        nextcloud_connector: dict[str, Any],
        nextcloud_source: NextcloudSourceHelper,
        pipeshub_client: PipeshubClient,
        graph_provider: GraphProviderProtocol,
    ) -> None:
        """TC-ACTIVITY-BACKLOG: Nextcloud pages its feed at 200 rows; one incremental sync reads every page."""
        connector_id = nextcloud_connector["connector_id"]
        folder = f"{nextcloud_connector['folder']}/backlog-{uuid.uuid4().hex[:6]}"
        # The folder is synced first: a folder created in the same run is saved with all it
        # holds, which would hide a feed read only in part.
        nextcloud_source.put(f"{folder}/first.txt", "The folder exists before the backlog.")
        await sync_until_names_visible(pipeshub_client, graph_provider, connector_id, ["first.txt"])
        before = await settle_record_baseline(pipeshub_client, graph_provider, connector_id)
        for i in range(BACKLOG_FILES):
            nextcloud_source.put(f"{folder}/note-{i:03d}.txt", f"Backlog note number {i}.")

        pipeshub_client.resync_connector(connector_id, full_sync=False)
        await wait_for_sync_completion(pipeshub_client, graph_provider, connector_id, timeout=DEFAULT_SYNC_TIMEOUT_SEC)

        after = await graph_provider.count_records(connector_id)
        assert after == before + BACKLOG_FILES, (
            f"TC-ACTIVITY-BACKLOG: one incremental sync brought {after - before} of "
            f"{BACKLOG_FILES} new files; the rest of the feed was left for a later run"
        )

    @pytest.mark.order(2)
    async def test_tc_no_fs_access_removes_nothing(
        self,
        nextcloud_connector: dict[str, Any],
        nextcloud_source: NextcloudSourceHelper,
        pipeshub_client: PipeshubClient,
        graph_provider: GraphProviderProtocol,
    ) -> None:
        """TC-NO-FS-ACCESS: unticking "Allow filesystem access" makes the drive look empty; nothing is removed."""
        name = f"pipeshub-it-{uuid.uuid4().hex[:6]}"
        app_password, token_id = nextcloud_source.create_app_password(name)
        instance = f"nextcloud-fs-access-{uuid.uuid4().hex[:8]}"
        state: dict[str, Any] = {"resource_name": nextcloud_source.user, "folder": f"never-created-{name}"}
        try:
            await create_connector_and_await_sync(
                pipeshub_client, graph_provider, state,
                connector_type="Nextcloud",
                connector_name=instance,
                connector_config={"auth": {
                    "baseUrl": os.getenv("NEXTCLOUD_CONNECTOR_URL", "http://nextcloud-source"),
                    "username": nextcloud_source.user,
                    "password": app_password,
                }},
                expected_records=nextcloud_connector["uploaded_count"],
                scope="personal",
            )
            connector_id = state["connector_id"]
            linked = await graph_provider.count_records(connector_id, scoped=True)
            assert linked >= nextcloud_connector["uploaded_count"]

            nextcloud_source.set_filesystem_access(token_id, name, allowed=False)
            await _full_sync(pipeshub_client, graph_provider, connector_id)

            after = await graph_provider.count_records(connector_id, scoped=True)
            assert after == linked, (
                f"TC-NO-FS-ACCESS: {linked - after} record(s) were removed when the app password lost filesystem access"
            )
            assert _notification_titled(pipeshub_client, f"{instance}: Nextcloud app password cannot read files"), (
                "TC-NO-FS-ACCESS: the user was not told the app password cannot read files"
            )

            pipeshub_client.toggle_sync(connector_id, enable=False)
            resp = pipeshub_client.request(
                "POST", f"/api/v1/connectors/{connector_id}/toggle", json={"type": "sync"},
            )
            assert resp.status_code == 400 and "Allow filesystem access" in resp.text, (
                f"TC-NO-FS-ACCESS: turning the connector on must be refused with the fix; got HTTP "
                f"{resp.status_code}: {resp.text[:300]}"
            )
        finally:
            nextcloud_source.set_filesystem_access(token_id, name, allowed=True)
            if state.get("connector_id"):
                await destructor(nextcloud_source, pipeshub_client, graph_provider, state, connector_type="Nextcloud")
            nextcloud_source.delete_app_password(token_id)

    @pytest.mark.order(3)
    async def test_tc_mount_offline_keeps_what_was_synced(
        self,
        nextcloud_connector: dict[str, Any],
        nextcloud_source: NextcloudSourceHelper,
        pipeshub_client: PipeshubClient,
        graph_provider: GraphProviderProtocol,
    ) -> None:
        """TC-MOUNT-OFFLINE: storage that goes offline lists as an empty folder with file id -1; its records stay."""
        connector_id = nextcloud_connector["connector_id"]
        token = uuid.uuid4().hex[:6]
        owner = (f"mount-owner-{token}", f"Owner-Pass-{token}-2026!")
        mount, folder, files = f"it-mount-{token}", f"shelf-{token}", [f"atlas-{token}.txt", f"ledger-{token}.txt"]
        _occ_or_skip(nextcloud_source, "status")
        nextcloud_source.ensure_other_user(*owner)
        mount_id = ""
        try:
            for name in files:
                nextcloud_source.put(f"{folder}/{name}", f"Kept on the mount: {name}.", auth=owner)
            mount_id = nextcloud_source.mount_other_users_files(mount, owner)
            nextcloud_source.wait_until_listed(f"{mount}/{folder}")
            # A new mount logs no activity; the full listing finds it.
            await _full_sync(pipeshub_client, graph_provider, connector_id)
            await sync_until_names_visible(pipeshub_client, graph_provider, connector_id, [mount, folder, *files])
            linked = await graph_provider.count_records(connector_id, scoped=True)

            nextcloud_source.take_mount_offline(mount_id)
            await _full_sync(pipeshub_client, graph_provider, connector_id)

            after = await graph_provider.count_records(connector_id, scoped=True)
            assert after == linked, (
                f"TC-MOUNT-OFFLINE: {linked - after} record(s) were removed while the mount was offline"
            )
            assert await graph_provider.get_record_by_external_id(connector_id, "-1") is None, (
                "TC-MOUNT-OFFLINE: a record was saved under Nextcloud's file id -1"
            )
            instance = _instance_name(pipeshub_client, connector_id)
            assert _notification_titled(pipeshub_client, f"{instance}: some Nextcloud folders could not be read"), (
                "TC-MOUNT-OFFLINE: the user was not told a folder could not be read"
            )
        finally:
            if mount_id:
                nextcloud_source.occ("files_external:delete", "-y", mount_id)
            nextcloud_source.delete_user(owner[0])

    @pytest.mark.order(4)
    async def test_tc_failure_notified(
        self,
        nextcloud_connector: dict[str, Any],
        nextcloud_source: NextcloudSourceHelper,
        pipeshub_client: PipeshubClient,
        graph_provider: GraphProviderProtocol,
    ) -> None:
        """TC-FAILURE-NOTIFIED: a full sync that cannot read the drive (maintenance mode) is reported to the user."""
        connector_id = nextcloud_connector["connector_id"]
        instance = _instance_name(pipeshub_client, connector_id)
        _occ_or_skip(nextcloud_source, "maintenance:mode", "--on")
        try:
            await _full_sync(pipeshub_client, graph_provider, connector_id)
        finally:
            nextcloud_source.occ("maintenance:mode", "--off")

        assert _notification_titled(pipeshub_client, f"{instance}: Nextcloud sync failed"), (
            "TC-FAILURE-NOTIFIED: a failed sync was not reported to the user"
        )
        await _full_sync(pipeshub_client, graph_provider, connector_id)
