import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from economics import OfferScenario, price_to_liter_margin, can_offer
from pilot import assign_arm, relative_uplift


class ScenarioTests(unittest.TestCase):
    def test_break_even_and_full_basket_cost(self):
        s = OfferScenario(10, 2, 0.5, 40, 0.4, 0.1)
        self.assertAlmostEqual(s.expected_reward_cost_rub, 8)
        self.assertAlmostEqual(s.break_even_liters, 0.81)
        self.assertAlmostEqual(s.incremental_profit_rub, 11.9)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            OfferScenario(0, 2, 0.5, 40, 0.4)
        with self.assertRaises(ValueError):
            OfferScenario(10, 2, 0.5, 40, 1.2)
        with self.assertRaises(ValueError):
            OfferScenario(10, float('nan'), 0.5, 40, 0.4)

    def test_offer_requires_proof_and_consent(self):
        s = OfferScenario(10, 2, 0.5, 40, 0.4)
        self.assertFalse(can_offer(True, True, s))
        self.assertFalse(can_offer(False, True, s, verified_by_experiment=True))
        self.assertTrue(can_offer(True, True, s, verified_by_experiment=True))

    def test_opex_accounting_explicit(self):
        self.assertAlmostEqual(price_to_liter_margin(14900, 1500, .742, True), 9.9428)
        self.assertAlmostEqual(price_to_liter_margin(14900, 1500, .742, False), 11.0558)
        with self.assertRaises(ValueError):
            price_to_liter_margin(1000, 2000, .742, True)

    def test_assign_arm_deterministic_and_changes_by_experiment(self):
        a = assign_arm('synthetic-001', 'pilot-1', 'unit-test-secret')
        self.assertIn(a, ['treatment', 'control'])
        self.assertEqual(a, assign_arm('synthetic-001', 'pilot-1', 'unit-test-secret'))
        groups = {assign_arm('synthetic-'+str(i), 'pilot-1', 'unit-test-secret') for i in range(100)}
        self.assertEqual(groups, {'treatment', 'control'})
        with self.assertRaises(ValueError):
            assign_arm('fake', 'pilot-1', '')

    def test_diff_in_means_and_invalid_group(self):
        self.assertAlmostEqual(relative_uplift(120, 2, 90, 2), 15)
        with self.assertRaises(ValueError):
            relative_uplift(1, 0, 20, 2)


if __name__ == '__main__':
    unittest.main()