"""Key sequences: modifiers first, hardware-like flag bits on arrows and function keys (macOS's own
shortcuts such as Spaces, Mission Control and Show Desktop are matched with those bits)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import keys_mac  # noqa: E402

CTRL, CMD, FN, NUMPAD = 1 << 18, 1 << 20, 1 << 23, 1 << 21


class SequenceTest(unittest.TestCase):
    def test_arrow_with_ctrl_presses_fn_too(self):
        self.assertEqual(keys_mac.event_sequence(["ctrl", "right"]),
                         [(63, True, FN), (59, True, FN | CTRL), (124, True, FN | CTRL), (124, False, FN | CTRL), (59, False, FN), (63, False, 0)])
        explicit = keys_mac.event_sequence(["ctrl", "fn", "right"])          # fn given by hand: pressed once, not twice
        self.assertEqual([c for c, d, f in explicit if d], [59, 63, 124])
        self.assertEqual(explicit[2], (124, True, FN | CTRL))

    def test_function_key_carries_fn_bit(self):
        self.assertEqual(keys_mac.event_sequence(["f11"]), [(63, True, FN), (103, True, FN), (103, False, FN), (63, False, 0)])
        self.assertEqual(keys_mac.event_sequence(["ctrl", "up"])[2], (126, True, FN | CTRL))

    def test_plain_keys_unchanged(self):
        self.assertEqual(keys_mac.event_sequence(["cmd", "space"]), [(55, True, CMD), (49, True, CMD), (49, False, CMD), (55, False, 0)])
        self.assertEqual(keys_mac.event_sequence(["a"]), [(0, True, 0), (0, False, 0)])

    def test_hardware_flags_table(self):
        self.assertEqual(keys_mac.HARDWARE_FLAGS[123], FN)
        self.assertEqual(keys_mac.HARDWARE_FLAGS[keys_mac.KEYCODES["f1"]], FN)
        self.assertEqual(keys_mac.HARDWARE_FLAGS[keys_mac.KEYCODES["f20"]], FN)
        self.assertNotIn(49, keys_mac.HARDWARE_FLAGS)     # space


if __name__ == "__main__":
    unittest.main(verbosity=2)
