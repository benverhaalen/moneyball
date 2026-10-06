import unittest

from moneyball.mock_ui_guard import GuardError, inspect_snapshot, require_prepared_turn


MOCK = "9000000000000000003"


def ax(*, turn="7.5", count=6, remaining="1:40", auto=False, completed=False):
    # Minimal synthetic text using field names observed in the native mock.
    status = ("25 text You’re on Auto-Pick\n26 switch Description: Auto-pick, Value: on\n"
              if auto else "")
    clock = ("24 text Draft Completed 🎉\n" if completed else
             f"460 pop up button {turn} ON THE CLOCK {remaining}\n")
    return (f"0 standard window Sleeper - Google Chrome, URL: sleeper.com/beta/draft/nfl/{MOCK}\n"
            "22 text Synthetic Dynasty Mock Draft • 2 Min Per Pick • 28 rounds\n"
            "51 pop up button Your team's actions\n" + status + clock +
            f"595 text ALL {count} / 28\n")


class MockGuardTests(unittest.TestCase):
    def inspect(self, text):
        return inspect_snapshot(text, expected_mock_id=MOCK)

    def test_actual_third_fallback_turn_is_manual(self):
        s = self.inspect(ax())
        self.assertEqual((s.pick_no, s.drafting_slot, s.seconds_remaining), (77, 5, 100))
        require_prepared_turn(s, expected_pick=77, expected_own_count=6)

    def test_even_snake_turn_uses_round_pick_label(self):
        s = self.inspect(ax(turn="8.8", count=7))
        self.assertEqual((s.pick_no, s.drafting_slot), (92, 5))

    def test_timeout_auto_mode_takes_priority_over_other_cpu_clock(self):
        s = self.inspect(ax(turn="8.9", count=8, auto=True))
        self.assertEqual(s.action, "disable_auto_pick_then_read_back")
        self.assertEqual(s.auto_pick_control, 26)
        with self.assertRaises(GuardError):
            require_prepared_turn(s, expected_pick=92, expected_own_count=7)

    def test_late_command_does_not_execute_at_later_own_turn(self):
        s = self.inspect(ax(turn="9.5", count=8))
        with self.assertRaisesRegex(GuardError, "different turn"):
            require_prepared_turn(s, expected_pick=92, expected_own_count=7)

    def test_automatic_holdings_change_invalidates_prepared_plan(self):
        s = self.inspect(ax(turn="14.8", count=13))
        with self.assertRaisesRegex(GuardError, "holdings changed"):
            require_prepared_turn(s, expected_pick=164, expected_own_count=8)

    def test_complete_mock_does_not_trigger_unnecessary_auto_toggle(self):
        s = self.inspect(ax(auto=True, completed=True, count=22))
        self.assertEqual(s.action, "no_action_completed")

    def test_zero_clock_and_insufficient_time_reject_pick(self):
        for remaining in ("0:00", "0:03"):
            with self.subTest(remaining=remaining), self.assertRaises(GuardError):
                require_prepared_turn(self.inspect(ax(remaining=remaining)),
                                      expected_pick=77, expected_own_count=6)

    def test_snapshot_diff_menu_wrong_draft_and_missing_mock_fail_closed(self):
        bad = ["There has been no change in the accessibility tree.",
               "1 menu Description: Draft settings\n6 End draft Complete the draft",
               ax().replace(MOCK, "9000000000000000001"),
               ax().replace("Mock Draft", "Draft")]
        for text in bad:
            with self.subTest(text=text[:50]), self.assertRaises(GuardError):
                self.inspect(text)

    def test_ambiguous_or_hidden_auto_controls_fail_closed(self):
        for text in (ax(auto=True).replace("Value: on", "Value: off"),
                     ax(auto=True) + "30 switch Description: Auto-pick, Value: on\n"):
            with self.assertRaises(GuardError):
                self.inspect(text)

    def test_malformed_and_contradictory_clocks_rejected(self):
        for text in (ax(remaining="1:90"), ax(turn="29.5"), ax(turn="7.13"),
                     ax() + "9 pop up button 7.6 ON THE CLOCK 2:00\n"):
            with self.assertRaises(GuardError):
                self.inspect(text)

    def test_missing_or_conflicting_own_count_fails(self):
        for text in (ax().replace("595 text ALL 6 / 28", ""),
                     ax() + "596 text ALL 7 / 28\n"):
            with self.assertRaises(GuardError):
                self.inspect(text)


if __name__ == "__main__":
    unittest.main()
