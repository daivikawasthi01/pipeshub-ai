"""DataSourceEntitiesProcessor.note_kept_user_group had no test over the real processor."""

from types import SimpleNamespace
from typing import Never

import pytest

from app.config.constants.arangodb import CollectionNames

from .test_has_stored_permissions import FakeStore, processor_over


async def test_a_kept_user_group_is_noted_as_seen_by_the_full_sync_in_progress() -> None:
    processor = processor_over(FakeStore([]))
    await processor.note_kept_user_group("c1", "ext-1")  # no full sync running: nothing to note
    run = processor.begin_full_sync_run("c1")
    await processor.note_kept_user_group("c1", "ext-1")
    assert run.has(CollectionNames.GROUPS.value, "n1")


async def test_an_unknown_kept_user_group_is_not_noted() -> None:
    processor = processor_over(FakeStore([], node=None))
    run = processor.begin_full_sync_run("c1")
    await processor.note_kept_user_group("c1", "missing")
    assert not run.has(CollectionNames.GROUPS.value, "n1")


async def test_a_failed_group_lookup_raises() -> None:
    store = FakeStore([])

    async def boom(*_: object, **__: object) -> Never:
        raise RuntimeError("lookup failed")

    store.get_user_group_by_external_id = boom  # type: ignore[method-assign]
    processor = processor_over(store)
    processor.begin_full_sync_run("c1")
    with pytest.raises(RuntimeError):
        await processor.note_kept_user_group("c1", "ext-1")
    assert SimpleNamespace  # keep import used
