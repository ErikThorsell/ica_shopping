"""Runs the three-way sync between a Home Assistant to-do list and one ICA list.

The decisions are made by sync_plan.plan(); this module fetches both sides, applies
the plan and persists the pairs. It fails closed: if anything needed to decide is
missing or unreadable, nothing is changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .ica_api import ICAApi, ICAApiError
from .sync_plan import Action, HAItem, IcaRow, next_pairs, plan, too_destructive

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_ICA_ITEMS = 250
MAX_REMOVALS = 15
SESSION_WARN_DAYS = 75  # the ICA cookie lasts about 90 days; warn with ~2 weeks left
SESSION_EVENT = "ica_shopping_session_expiring"


class ListSync:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: ICAApi) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.pairs.{entry.entry_id}")
        self._session_store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.session.{entry.entry_id}")
        self._lock = asyncio.Lock()
        self.busy = False
        self.last_finished: float = 0.0

    # --- configuration (options override data) ---
    def _opt(self, key, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def list_id(self) -> str:
        return self._opt("ica_list_id")

    @property
    def todo_entity(self) -> str:
        return self._opt("todo_entity_id")

    @property
    def tidy(self) -> bool:
        """Remove ticked ICA rows and completed HA items."""
        return bool(self.entry.options.get("remove_striked", self.entry.data.get("remove_striked", True)))

    # --- persistence ---
    async def _load_pairs(self) -> set[tuple[str, str]]:
        data = await self._store.async_load() or {}
        if data.get("list_id") != self.list_id or data.get("todo_entity") != self.todo_entity:
            if data:
                _LOGGER.warning(
                    "ICA list or to-do entity changed since the last sync; starting from a clean slate"
                )
            return set()
        return {(a, b) for a, b in data.get("pairs", [])}

    async def _save_pairs(self, pairs: set[tuple[str, str]]) -> None:
        await self._store.async_save(
            {"list_id": self.list_id, "todo_entity": self.todo_entity, "pairs": sorted(pairs)}
        )

    # --- issues ---
    def _issue(self, issue_id: str, **placeholders) -> None:
        ir.async_create_issue(
            self.hass, DOMAIN, f"{issue_id}_{self.entry.entry_id}",
            is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key=issue_id, translation_placeholders={k: str(v) for k, v in placeholders.items()},
        )

    def _clear_issue(self, issue_id: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"{issue_id}_{self.entry.entry_id}")

    # --- the run ---
    async def async_run(self, force: bool = False) -> bool:
        """Sync once. Returns True if a plan was applied, False if it aborted."""
        async with self._lock:
            self.busy = True
            try:
                return await self._run(force)
            except Exception:  # never let a bug take down the timer
                _LOGGER.exception("ICA sync failed unexpectedly; nothing further was changed")
                return False
            finally:
                self.busy = False
                self.last_finished = self.hass.loop.time()

    async def _fetch_ha(self) -> list[HAItem] | None:
        state = self.hass.states.get(self.todo_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.warning("To-do entity %s is not available; skipping sync", self.todo_entity)
            return None
        try:
            result = await self.hass.services.async_call(
                "todo", "get_items",
                {"entity_id": self.todo_entity, "status": ["needs_action", "completed"]},
                blocking=True, return_response=True,
            )
            raw = result[self.todo_entity]["items"]
        except Exception as err:
            _LOGGER.warning("Could not read %s: %s; skipping sync", self.todo_entity, err)
            return None
        return [
            HAItem(uid=i["uid"], title=i.get("summary", ""), completed=i.get("status") == "completed")
            for i in raw if i.get("uid")
        ]

    async def _fetch_ica(self) -> list[IcaRow] | None:
        try:
            lists = await self.api.fetch_lists_strict()
        except ICAApiError as err:
            _LOGGER.warning("Could not read ICA: %s; skipping sync", err)
            return None
        the_list = next((l for l in lists if l.get("id") == self.list_id), None)
        if the_list is None:
            _LOGGER.error("ICA list %s no longer exists; skipping sync", self.list_id)
            self._issue("list_missing", list_id=self.list_id)
            return None
        self._clear_issue("list_missing")
        return [
            IcaRow(id=r["id"], text=r.get("text", ""), struck=bool(r.get("isStriked")))
            for r in the_list.get("rows", []) if isinstance(r, dict) and r.get("id")
        ]

    async def _check_session_age(self) -> None:
        """Warn before the ICA cookie expires.

        ICA does not report when the cookie ends, so the age is counted from the day
        this cookie was first seen. A new cookie (different digest) restarts the count.
        """
        session_id = self.entry.options.get("session_id", self.entry.data.get("session_id", ""))
        digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
        now = dt_util.utcnow()
        data = await self._session_store.async_load() or {}
        if data.get("digest") != digest:
            data = {"digest": digest, "since": now.isoformat()}
            await self._session_store.async_save(data)
        age = (now - datetime.fromisoformat(data["since"])).days
        issue = f"session_expiring_{self.entry.entry_id}"
        if age < SESSION_WARN_DAYS:
            ir.async_delete_issue(self.hass, DOMAIN, issue)
            return
        if ir.async_get(self.hass).async_get_issue(DOMAIN, issue) is None:
            self.hass.bus.async_fire(SESSION_EVENT, {"entry_id": self.entry.entry_id, "age_days": age})
        ir.async_create_issue(
            self.hass, DOMAIN, issue, is_fixable=True, severity=ir.IssueSeverity.WARNING,
            translation_key="session_expiring", translation_placeholders={"days": str(age)},
            data={"entry_id": self.entry.entry_id},
        )

    async def _run(self, force: bool) -> bool:
        try:
            await self._check_session_age()
        except Exception:  # advisory only; never block the sync
            _LOGGER.exception("Could not check the age of the ICA session")
        ica_rows = await self._fetch_ica()
        if ica_rows is None:
            return False
        ha_items = await self._fetch_ha()
        if ha_items is None:
            return False
        pairs = await self._load_pairs()

        p = plan(ha_items, ica_rows, pairs, purge_completed=self.tidy, remove_struck=self.tidy)

        if too_destructive(p, MAX_REMOVALS) and not force:
            _LOGGER.error(
                "Sync would remove %s items at once (limit %s); nothing changed. "
                "Check both lists, then call ica_shopping.refresh with force: true.",
                p.destructive, MAX_REMOVALS,
            )
            self._issue("sync_aborted", count=p.destructive, limit=MAX_REMOVALS)
            return False
        self._clear_issue("sync_aborted")

        actions = p.actions
        room = MAX_ICA_ITEMS - len([r for r in ica_rows if not r.struck])
        if sum(a.kind == "add_ica" for a in actions) > max(room, 0):
            _LOGGER.error("ICA list is full (%s items); not adding more", MAX_ICA_ITEMS)
            kept, left = [], max(room, 0)
            for a in actions:
                if a.kind == "add_ica":
                    if left <= 0:
                        continue
                    left -= 1
                kept.append(a)
            actions = kept

        results: list[tuple[Action, bool]] = []
        for a in actions:
            results.append((a, await self._apply(a)))

        await self._save_pairs(next_pairs(p, results))
        self._log_summary(ha_items, ica_rows, results)
        await self._refresh_sensor()
        return True

    def _log_summary(self, ha_items, ica_rows, results: list[tuple[Action, bool]]) -> None:
        """One line per run: INFO when something changed or failed, DEBUG when nothing did."""
        done = {k: sum(1 for a, ok in results if ok and a.kind == k) for k in
                ("add_ica", "add_ha", "remove_ica", "remove_ha", "purge_ha")}
        failed = sum(1 for _, ok in results if not ok)
        msg = (
            "ICA sync: to ICA +%d -%d, to HA +%d -%d, purged %d completed, %d failed "
            "(before: HA %d items, ICA %d rows)"
        )
        args = (
            done["add_ica"], done["remove_ica"], done["add_ha"], done["remove_ha"],
            done["purge_ha"], failed, len(ha_items), len(ica_rows),
        )
        _LOGGER.log(logging.INFO if results else logging.DEBUG, msg, *args)

    async def _apply(self, a: Action) -> bool:
        try:
            if a.kind == "add_ica":
                return bool(await self.api.add_to_list(self.list_id, a.ha_item.title.strip()))
            if a.kind == "remove_ica":
                return bool(await self.api.remove_item(a.ica_row.id))
            if a.kind == "add_ha":
                await self.hass.services.async_call(
                    "todo", "add_item",
                    {"entity_id": self.todo_entity, "item": a.ica_row.text.strip()}, blocking=True,
                )
                return True
            if a.kind in ("remove_ha", "purge_ha"):
                await self.hass.services.async_call(
                    "todo", "remove_item",
                    {"entity_id": self.todo_entity, "item": [a.ha_item.uid]}, blocking=True,
                )
                return True
        except Exception as err:
            _LOGGER.warning("ICA sync: %s failed (%s); will retry next run", a.kind, err)
        return False

    async def _refresh_sensor(self) -> None:
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(self.hass)
        entity_id = next(
            (e.entity_id for e in reg.entities.values() if e.unique_id == f"shoppinglist_{self.list_id}"),
            None,
        )
        if entity_id:
            try:
                await self.hass.services.async_call(
                    "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
                )
            except Exception as err:  # cosmetic; the sync itself is already applied and saved
                _LOGGER.debug("Could not refresh sensor %s: %s", entity_id, err)
