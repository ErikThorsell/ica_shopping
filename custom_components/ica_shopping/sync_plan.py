"""Three-way merge between a Home Assistant to-do list and an ICA shopping list.

Pure functions only: no Home Assistant, no network. The caller fetches both sides,
loads the persisted pairs, calls plan(), executes the actions and stores the pairs
that remain.

A *pair* is (ha_key, ica_key): the normalised HA title and the normalised ICA text
of one item that was present on both sides after the previous run. The two keys
differ when the ICA text is a translation of the HA title. The pairs are what lets
us tell "new on this side" from "removed on the other side".

Rules, per pair:
  present on both sides          -> keep
  gone (or completed) in HA      -> remove from ICA
  gone (or ticked) in ICA        -> remove from HA
  gone on both                   -> forget
New items are pushed to the other side. Nothing is ever removed because it is
merely absent: absence only counts when the item was paired before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Literal

Kind = Literal["add_ica", "add_ha", "remove_ica", "remove_ha", "purge_ha"]
Pair = tuple[str, str]


def norm(text: str) -> str:
    """Comparison key: case-folded, trimmed, inner whitespace collapsed."""
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class HAItem:
    uid: str
    title: str
    completed: bool = False


@dataclass(frozen=True)
class IcaRow:
    id: str
    text: str
    struck: bool = False


@dataclass(frozen=True)
class Action:
    kind: Kind
    pair: Pair | None = None
    ha_item: HAItem | None = None
    ica_row: IcaRow | None = None
    reason: str = ""


@dataclass
class Plan:
    carry: set[Pair] = field(default_factory=set)
    actions: list[Action] = field(default_factory=list)

    @property
    def destructive(self) -> int:
        """Removals caused by the other side's change (not duplicates or tidying)."""
        return sum(1 for a in self.actions
                   if a.kind in ("remove_ha", "remove_ica") and a.pair is not None)


def too_destructive(plan: Plan, limit: int) -> bool:
    return plan.destructive > limit


def plan(
    ha_items: Iterable[HAItem],
    ica_rows: Iterable[IcaRow],
    pairs: Iterable[Pair],
    ica_name_of: Callable[[str], str] = lambda title: title,
    purge_completed: bool = True,
    remove_struck: bool = True,
) -> Plan:
    """Decide what to do. ica_name_of must be a cheap lookup (cache), not a network call."""
    ha_items = list(ha_items)
    ica_rows = list(ica_rows)
    pairs = set(pairs)
    out = Plan()

    # --- index HA: first open item per key wins, later ones are duplicates ---
    ha_open: dict[str, HAItem] = {}
    for it in ha_items:
        if it.completed:
            continue
        k = norm(it.title)
        if not k:
            continue
        if k in ha_open:
            out.actions.append(Action("remove_ha", ha_item=it, reason="duplicate"))
        else:
            ha_open[k] = it

    # --- index ICA: first open row per key wins; struck rows count as gone ---
    ica_open: dict[str, IcaRow] = {}
    for row in ica_rows:
        k = norm(row.text)
        if row.struck:
            if remove_struck:
                out.actions.append(Action("remove_ica", ica_row=row, reason="ticked off"))
            continue
        if k and k not in ica_open:
            ica_open[k] = row

    # --- known pairs ---
    for hk, ik in sorted(pairs):
        in_ha, in_ica = hk in ha_open, ik in ica_open
        if in_ha and in_ica:
            out.carry.add((hk, ik))
        elif in_ha:
            out.actions.append(Action("remove_ha", (hk, ik), ha_item=ha_open[hk],
                                      reason="removed or ticked on ICA"))
        elif in_ica:
            if any(h in ha_open for h, i in pairs if i == ik):
                continue  # another HA title still maps to this ICA row
            out.actions.append(Action("remove_ica", (hk, ik), ica_row=ica_open[ik],
                                      reason="removed or completed in HA"))
        # gone on both sides: forget the pair

    paired_hk = {hk for hk, _ in pairs}
    paired_ik = {ik for _, ik in pairs}

    # --- new HA items: link to an existing ICA row, or push ---
    unmatched_ica = {ik for ik in ica_open if ik not in paired_ik}
    for hk, item in ha_open.items():
        if hk in paired_hk:
            continue
        ik = norm(ica_name_of(item.title))
        if ik in ica_open:
            out.carry.add((hk, ik))
            unmatched_ica.discard(ik)
        else:
            out.actions.append(Action("add_ica", (hk, ik), ha_item=item, reason="new in HA"))

    # --- new ICA rows nobody claimed: pull into HA ---
    for ik in sorted(unmatched_ica):
        row = ica_open[ik]
        out.actions.append(Action("add_ha", (ik, ik), ica_row=row, reason="new on ICA"))

    # --- tidy completed HA items ---
    if purge_completed:
        for it in ha_items:
            if it.completed:
                out.actions.append(Action("purge_ha", ha_item=it, reason="completed"))

    return out


def next_pairs(plan_: Plan, results: Iterable[tuple[Action, bool]]) -> set[Pair]:
    """Pairs to persist after executing the plan. A failed add is not recorded (it is
    retried next run); a failed removal keeps the pair (so it is retried too)."""
    state = set(plan_.carry)
    for action, ok in results:
        if action.pair is None:
            continue
        if action.kind in ("add_ica", "add_ha") and ok:
            state.add(action.pair)
        elif action.kind in ("remove_ica", "remove_ha") and not ok:
            state.add(action.pair)
    return state
