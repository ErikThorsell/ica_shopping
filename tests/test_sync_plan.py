import importlib.util
import pathlib
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "sync_plan",
    pathlib.Path(__file__).parents[1] / "custom_components" / "ica_shopping" / "sync_plan.py",
)
sp = importlib.util.module_from_spec(_SPEC)
import sys
sys.modules["sync_plan"] = sp  # dataclasses needs the module registered
_SPEC.loader.exec_module(sp)

HA, ROW = sp.HAItem, sp.IcaRow


def kinds(p):
    return sorted((a.kind, (a.ha_item.title if a.ha_item else a.ica_row.text)) for a in p.actions)


class Normalisation(unittest.TestCase):
    def test_case_and_space(self):
        self.assertEqual(sp.norm("  Mjölk\t "), sp.norm("mjölk"))
        self.assertEqual(sp.norm("Ägg"), sp.norm("ägg"))
        self.assertEqual(sp.norm("kaffe  bönor"), "kaffe bönor")


class NewItems(unittest.TestCase):
    def test_ha_item_is_pushed(self):
        p = sp.plan([HA("1", "milk")], [], set())
        self.assertEqual(kinds(p), [("add_ica", "milk")])

    def test_ica_item_is_pulled(self):
        p = sp.plan([], [ROW("r1", "bröd")], set())
        self.assertEqual(kinds(p), [("add_ha", "bröd")])

    def test_same_item_on_both_sides_links_without_actions(self):
        p = sp.plan([HA("1", "Mjölk")], [ROW("r1", "mjölk")], set())
        self.assertEqual(p.actions, [])
        self.assertEqual(p.carry, {("mjölk", "mjölk")})

    def test_translation_links_existing_row(self):
        names = {"milk": "mjölk"}
        p = sp.plan([HA("1", "milk")], [ROW("r1", "mjölk")], set(), ica_name_of=lambda t: names.get(t, t))
        self.assertEqual(p.actions, [])
        self.assertEqual(p.carry, {("milk", "mjölk")})

    def test_translated_push(self):
        p = sp.plan([HA("1", "milk")], [], set(), ica_name_of=lambda t: "mjölk")
        (a,) = p.actions
        self.assertEqual((a.kind, a.pair), ("add_ica", ("milk", "mjölk")))


class Removals(unittest.TestCase):
    PAIRS = {("milk", "mjölk")}

    def test_both_present_is_stable(self):
        p = sp.plan([HA("1", "milk")], [ROW("r", "mjölk")], self.PAIRS)
        self.assertEqual(p.actions, [])
        self.assertEqual(p.carry, self.PAIRS)

    def test_ticked_in_ha_removes_from_ica_and_purges(self):
        p = sp.plan([HA("1", "milk", completed=True)], [ROW("r", "mjölk")], self.PAIRS)
        self.assertEqual(kinds(p), [("purge_ha", "milk"), ("remove_ica", "mjölk")])

    def test_deleted_in_ha_removes_from_ica(self):
        p = sp.plan([], [ROW("r", "mjölk")], self.PAIRS)
        self.assertEqual(kinds(p), [("remove_ica", "mjölk")])

    def test_ticked_on_ica_removes_from_ha_and_tidies_ica(self):
        p = sp.plan([HA("1", "milk")], [ROW("r", "mjölk", struck=True)], self.PAIRS)
        self.assertEqual(kinds(p), [("remove_ha", "milk"), ("remove_ica", "mjölk")])

    def test_deleted_on_ica_removes_from_ha(self):
        p = sp.plan([HA("1", "milk")], [], self.PAIRS)
        self.assertEqual(kinds(p), [("remove_ha", "milk")])

    def test_gone_on_both_just_forgets(self):
        p = sp.plan([], [], self.PAIRS)
        self.assertEqual(p.actions, [])
        self.assertEqual(p.carry, set())

    def test_absence_alone_never_removes(self):
        # The data-loss case: an item added in HA that ICA has not seen yet
        # (push failed, cookie expired) must NOT be removed from HA.
        p = sp.plan([HA("1", "mjölk")], [ROW("r", "ägg")], {("ägg", "ägg")})
        self.assertNotIn(("remove_ha", "mjölk"), kinds(p))
        self.assertIn(("add_ica", "mjölk"), kinds(p))
        # "ägg" was paired, is gone from HA, still on ICA -> legitimately removed from ICA
        self.assertIn(("remove_ica", "ägg"), kinds(p))

    def test_shared_ica_row_survives_while_another_title_maps_to_it(self):
        pairs = {("milk", "mjölk"), ("mjölk", "mjölk")}
        p = sp.plan([HA("2", "mjölk")], [ROW("r", "mjölk")], pairs)
        self.assertEqual([a for a in p.actions if a.kind == "remove_ica"], [])

    def test_struck_row_without_pair_is_only_tidied(self):
        p = sp.plan([], [ROW("r", "ägg", struck=True)], set())
        self.assertEqual(kinds(p), [("remove_ica", "ägg")])
        self.assertEqual(p.destructive, 0)

    def test_struck_not_removed_when_disabled(self):
        p = sp.plan([], [ROW("r", "ägg", struck=True)], set(), remove_struck=False)
        self.assertEqual(p.actions, [])


class Duplicates(unittest.TestCase):
    def test_case_variants_in_ha_collapse_to_one_push_and_one_removal(self):
        p = sp.plan([HA("1", "Milk"), HA("2", "milk ")], [], set())
        self.assertEqual(kinds(p), [("add_ica", "Milk"), ("remove_ha", "milk ")])
        dup = [a for a in p.actions if a.reason == "duplicate"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0].ha_item.uid, "2")  # oldest (first in list) is kept

    def test_voice_and_reminders_same_item_gives_one_ica_row(self):
        p = sp.plan([HA("1", "milk"), HA("2", "Milk")], [ROW("r", "mjölk")], set(),
                    ica_name_of=lambda t: "mjölk")
        self.assertEqual([a for a in p.actions if a.kind.startswith("add")], [])

    def test_duplicate_ica_rows_do_not_double_add(self):
        p = sp.plan([], [ROW("r1", "ägg"), ROW("r2", "Ägg")], set())
        self.assertEqual(kinds(p), [("add_ha", "ägg")])

    def test_blank_titles_are_ignored(self):
        p = sp.plan([HA("1", "   ")], [ROW("r", "")], set())
        self.assertEqual(p.actions, [])


class Purge(unittest.TestCase):
    def test_completed_ha_items_are_purged(self):
        p = sp.plan([HA("1", "ägg", completed=True)], [], set())
        self.assertEqual(kinds(p), [("purge_ha", "ägg")])

    def test_purge_can_be_disabled(self):
        p = sp.plan([HA("1", "ägg", completed=True)], [], set(), purge_completed=False)
        self.assertEqual(p.actions, [])

    def test_reopened_item_after_completion_is_open_again(self):
        items = [HA("1", "milk", completed=True), HA("2", "milk")]
        p = sp.plan(items, [ROW("r", "mjölk")], {("milk", "mjölk")})
        self.assertEqual(p.carry, {("milk", "mjölk")})
        self.assertEqual(kinds(p), [("purge_ha", "milk")])


class Brake(unittest.TestCase):
    def test_counts_only_cross_side_removals(self):
        pairs = {(str(i), str(i)) for i in range(5)}
        p = sp.plan([], [ROW(str(i), str(i)) for i in range(5)], pairs)
        self.assertEqual(p.destructive, 5)
        self.assertTrue(sp.too_destructive(p, 4))
        self.assertFalse(sp.too_destructive(p, 5))


class State(unittest.TestCase):
    def test_failed_add_is_not_recorded_then_retried(self):
        p = sp.plan([HA("1", "milk")], [], set())
        a = p.actions[0]
        self.assertEqual(sp.next_pairs(p, [(a, False)]), set())
        p2 = sp.plan([HA("1", "milk")], [], set())
        self.assertEqual([x.kind for x in p2.actions], ["add_ica"])

    def test_failed_removal_keeps_pair_for_retry(self):
        pairs = {("milk", "mjölk")}
        p = sp.plan([], [ROW("r", "mjölk")], pairs)
        a = p.actions[0]
        self.assertEqual(sp.next_pairs(p, [(a, False)]), pairs)
        self.assertEqual(sp.next_pairs(p, [(a, True)]), set())

    def test_converges(self):
        """Run the planner to a fixed point against two in-memory lists."""
        ha, ica, pairs = [HA("1", "milk"), HA("2", "ägg")], [ROW("r1", "bröd")], set()
        tr = {"milk": "mjölk"}
        for _ in range(4):
            p = sp.plan(ha, ica, pairs, ica_name_of=lambda t: tr.get(t, t))
            res = []
            for a in p.actions:
                if a.kind == "add_ica":
                    ica.append(ROW(f"r{len(ica)+1}", a.pair[1]))
                elif a.kind == "add_ha":
                    ha.append(HA(f"h{len(ha)+1}", a.ica_row.text))
                res.append((a, True))
            pairs = sp.next_pairs(p, res)
        self.assertEqual(sorted(sp.norm(i.title) for i in ha), ["bröd", "milk", "ägg"])
        self.assertEqual(sorted(sp.norm(r.text) for r in ica), ["bröd", "mjölk", "ägg"])
        self.assertEqual(sp.plan(ha, ica, pairs, ica_name_of=lambda t: tr.get(t, t)).actions, [])


if __name__ == "__main__":
    unittest.main()
