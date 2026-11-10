"""Runs the real integration in Home Assistant against a fake ICA backend."""
from unittest.mock import patch

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ica_shopping.const import DOMAIN
from custom_components.ica_shopping.ica_api import ICAApi, ICAApiError

TODO = "todo.shopping_list"


class FakeIca:
    def __init__(self, rows=()):
        self.rows = [{"id": f"r{i}", "text": t, "isStriked": False} for i, t in enumerate(rows)]
        self.n = len(self.rows)
        self.fail_fetch = False
        self.fail_add = False
        self.list_exists = True
        self.list_id = "L1"
        self.calls = []

    def texts(self):
        return sorted(r["text"] for r in self.rows if not r["isStriked"])

    def tick(self, text):
        for r in self.rows:
            if r["text"] == text:
                r["isStriked"] = True

    async def fetch_lists_strict(self, *_):
        if self.fail_fetch:
            raise ICAApiError("boom")
        if not self.list_exists:
            return [{"id": "OTHER", "rows": []}]
        return [{"id": self.list_id, "name": "Test", "rows": [dict(r) for r in self.rows]}]

    async def fetch_lists(self, *_):
        try:
            return await self.fetch_lists_strict()
        except ICAApiError:
            return []

    async def add_to_list(self, _self, list_id, text):
        self.calls.append(("add", text))
        if self.fail_add:
            return False
        self.n += 1
        self.rows.append({"id": f"r{self.n}", "text": text, "isStriked": False})
        return True

    async def remove_item(self, _self, row_id):
        self.calls.append(("remove", row_id))
        self.rows = [r for r in self.rows if r["id"] != row_id]
        return True


@pytest.fixture
async def env(hass):
    """Returns (fake, entry); the shopping list and the integration are set up."""
    fake = FakeIca()
    sl = MockConfigEntry(domain="shopping_list")
    sl.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sl.entry_id)
    await hass.async_block_till_done()

    # the shopping list persists between tests; start every test empty
    left = await hass.services.async_call(
        "todo", "get_items", {"entity_id": TODO, "status": ["needs_action", "completed"]},
        blocking=True, return_response=True)
    uids = [i["uid"] for i in left[TODO]["items"]]
    if uids:
        await hass.services.async_call("todo", "remove_item", {"entity_id": TODO, "item": uids}, blocking=True)

    async def token(_self):
        return "t"

    patches = [
        patch.object(ICAApi, "fetch_lists_strict", lambda s: fake.fetch_lists_strict()),
        patch.object(ICAApi, "fetch_lists", lambda s: fake.fetch_lists()),
        patch.object(ICAApi, "add_to_list", lambda s, l, t: fake.add_to_list(s, l, t)),
        patch.object(ICAApi, "remove_item", lambda s, r: fake.remove_item(s, r)),
        patch.object(ICAApi, "_get_token_from_session_id", token),
    ]
    for p in patches:
        p.start()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"session_id": "s", "ica_list_id": "L1", "todo_entity_id": TODO},
    )
    entry.add_to_hass(hass)
    yield fake, entry
    if entry.state.name == "LOADED":
        await hass.config_entries.async_unload(entry.entry_id)
    for p in patches:
        p.stop()


async def start(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def ha_add(hass, *items):
    for it in items:
        await hass.services.async_call("todo", "add_item", {"entity_id": TODO, "item": it}, blocking=True)


async def ha_items(hass, status=("needs_action", "completed")):
    r = await hass.services.async_call(
        "todo", "get_items", {"entity_id": TODO, "status": list(status)}, blocking=True, return_response=True)
    return sorted((i["summary"], i["status"]) for i in r[TODO]["items"])


async def ha_open(hass):
    return [s for s, st in await ha_items(hass) if st == "needs_action"]


async def refresh(hass, **kw):
    await hass.services.async_call(DOMAIN, "refresh", kw, blocking=True)
    await hass.async_block_till_done()


async def test_first_run_merges_both_sides(hass, env):
    fake, entry = env
    fake.rows = [{"id": "r0", "text": "bröd", "isStriked": False}]; fake.n = 1
    await ha_add(hass, "milk")
    await start(hass, entry)
    await refresh(hass)
    assert fake.texts() == ["bröd", "milk"]
    assert await ha_open(hass) == ["bröd", "milk"]


async def test_second_run_is_a_no_op(hass, env):
    fake, entry = env
    await ha_add(hass, "milk")
    await start(hass, entry)
    await refresh(hass)
    fake.calls.clear()
    await refresh(hass)
    assert fake.calls == []
    assert fake.texts() == ["milk"]


async def test_ticked_in_ha_leaves_ica_and_is_purged(hass, env):
    fake, entry = env
    await ha_add(hass, "milk", "eggs")
    await start(hass, entry)
    await refresh(hass)
    await hass.services.async_call("todo", "update_item",
        {"entity_id": TODO, "item": "milk", "status": "completed"}, blocking=True)
    await refresh(hass)
    assert fake.texts() == ["eggs"]
    assert await ha_items(hass) == [("eggs", "needs_action")]


async def test_ticked_on_ica_leaves_ha(hass, env):
    fake, entry = env
    await ha_add(hass, "milk", "eggs")
    await start(hass, entry)
    await refresh(hass)
    fake.tick("milk")
    await refresh(hass)
    assert await ha_open(hass) == ["eggs"]
    assert fake.texts() == ["eggs"]


async def test_new_item_on_ica_arrives_in_ha(hass, env):
    fake, entry = env
    await start(hass, entry)
    await refresh(hass)
    await fake.add_to_list(None, "L1", "smör")
    await refresh(hass)
    assert await ha_open(hass) == ["smör"]


async def test_unpushed_item_survives_ica_outage_and_is_retried(hass, env):
    fake, entry = env
    await start(hass, entry)
    await refresh(hass)                       # establish state with an empty list
    await ha_add(hass, "milk")
    fake.fail_add = True
    await refresh(hass)
    assert await ha_open(hass) == ["milk"]    # NOT deleted just because ICA lacks it
    fake.fail_add = False
    await refresh(hass)
    assert fake.texts() == ["milk"]


async def test_fetch_failure_changes_nothing(hass, env):
    fake, entry = env
    await ha_add(hass, "milk", "eggs")
    await start(hass, entry)
    await refresh(hass)
    fake.fail_fetch = True
    fake.rows = []                            # even if it would look empty
    await refresh(hass)
    assert await ha_open(hass) == ["eggs", "milk"]


async def test_empty_ica_response_is_not_trusted_blindly_but_removals_are_braked(hass, env):
    fake, entry = env
    await ha_add(hass, *[f"item{i}" for i in range(20)])
    await start(hass, entry)
    await refresh(hass)
    assert len(fake.texts()) == 20
    fake.rows = []                            # ICA "loses" everything (e.g. API returns an empty list)
    await refresh(hass)
    assert len(await ha_open(hass)) == 20     # 20 removals > limit: refused
    reg = ir.async_get(hass)
    assert any(i.translation_key == "sync_aborted" for i in reg.issues.values())
    await refresh(hass, force=True)
    assert await ha_open(hass) == []          # explicit force applies it


async def test_missing_list_pauses_sync_and_raises_issue(hass, env):
    fake, entry = env
    await ha_add(hass, "milk")
    await start(hass, entry)
    await refresh(hass)
    fake.list_exists = False
    await refresh(hass)
    assert await ha_open(hass) == ["milk"]
    reg = ir.async_get(hass)
    assert any(i.translation_key == "list_missing" for i in reg.issues.values())
    fake.list_exists = True
    await refresh(hass)
    assert not any(i.translation_key == "list_missing" for i in reg.issues.values())


async def test_case_variant_duplicates_give_one_ica_row_and_one_ha_item(hass, env):
    fake, entry = env
    await ha_add(hass, "Milk", "milk ")
    await start(hass, entry)
    await refresh(hass)
    assert fake.texts() == ["Milk"]
    assert await ha_open(hass) == ["Milk"]


async def test_changing_the_ica_list_does_not_delete_ha_items(hass, env):
    fake, entry = env
    await ha_add(hass, "milk")
    await start(hass, entry)
    await refresh(hass)
    assert fake.texts() == ["milk"]
    # switch to a different, empty ICA list: stale pairs must not read as "removed on ICA"
    fake.list_id, fake.rows = "L2", []
    hass.config_entries.async_update_entry(entry, options={
        "ica_list_id": "L2", "todo_entity_id": TODO, "session_id": "s", "remove_striked": True})
    await hass.async_block_till_done()
    await refresh(hass)
    assert await ha_open(hass) == ["milk"]
    assert fake.texts() == ["milk"]


# --- session age warning ---

from datetime import timedelta
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.ica_shopping.sync import SESSION_EVENT


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"session_expiring_{entry.entry_id}")


async def _age_cookie(hass, entry, days):
    from homeassistant.helpers.storage import Store
    import hashlib
    digest = hashlib.sha256(entry.data["session_id"].encode()).hexdigest()[:16]
    await Store(hass, 1, f"{DOMAIN}.session.{entry.entry_id}").async_save(
        {"digest": digest, "since": (dt_util.utcnow() - timedelta(days=days)).isoformat()})


async def test_fresh_cookie_raises_nothing(hass, env):
    fake, entry = env
    await start(hass, entry)
    await refresh(hass)
    assert _issue(hass, entry) is None


async def test_old_cookie_raises_one_repair_and_one_event(hass, env):
    fake, entry = env
    await _age_cookie(hass, entry, 80)
    events = async_capture_events(hass, SESSION_EVENT)
    await start(hass, entry)
    await refresh(hass)
    await refresh(hass)
    issue = _issue(hass, entry)
    assert issue is not None and issue.is_fixable
    assert len(events) == 1 and events[0].data["age_days"] == 80


async def test_new_cookie_restarts_the_count(hass, env):
    fake, entry = env
    await _age_cookie(hass, entry, 80)
    await start(hass, entry)
    await refresh(hass)
    assert _issue(hass, entry) is not None
    hass.config_entries.async_update_entry(entry, options={"session_id": "fresh"})
    await hass.async_block_till_done()
    await refresh(hass)
    assert _issue(hass, entry) is None


# --- log summary ---

async def test_summary_line_counts_changes(hass, env, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="custom_components.ica_shopping")
    fake, entry = env
    fake.rows = [{"id": "r0", "text": "bröd", "isStriked": False}]; fake.n = 1
    await ha_add(hass, "milk")
    await start(hass, entry)
    await refresh(hass)
    lines = [r for r in caplog.records if r.getMessage().startswith("ICA sync: to ICA")]
    assert any("to ICA +1 -0, to HA +1 -0, purged 0 completed, 0 failed" in r.getMessage()
               and r.levelno == logging.INFO for r in lines)


async def test_noop_run_logs_at_debug_only(hass, env, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="custom_components.ica_shopping")
    fake, entry = env
    await ha_add(hass, "milk")
    await start(hass, entry)
    await refresh(hass)
    caplog.clear()
    await refresh(hass)
    lines = [r for r in caplog.records if r.getMessage().startswith("ICA sync: to ICA")]
    assert lines and all(r.levelno == logging.DEBUG for r in lines)
