
import unittest

from paper_bot import atr, ema, equity, new_state, rsi


class PaperBotTests(unittest.TestCase):
    def test_ema_length_and_seed(self):
        result = ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(result[:2], [None, None])
        self.assertAlmostEqual(result[2], 2.0)
        self.assertEqual(len(result), 5)

    def test_rsi_flat_market(self):
        self.assertEqual(rsi([10.0] * 20), 50.0)

    def test_atr_constant_range(self):
        candles = [
            {"high": 11.0, "low": 9.0, "close": 10.0}
            for _ in range(20)
        ]
        self.assertAlmostEqual(atr(candles, 14), 2.0)

    def test_equity_includes_open_position(self):
        state = new_state()
        state["cash"] = 80.0
        state["positions"]["BTCUSDT"] = {
            "qty": 0.01,
            "entry": 2000.0,
        }
        self.assertAlmostEqual(
            equity(state, {"BTCUSDT": 3000.0}),
            110.0,
        )


if __name__ == "__main__":
    unittest.main()
