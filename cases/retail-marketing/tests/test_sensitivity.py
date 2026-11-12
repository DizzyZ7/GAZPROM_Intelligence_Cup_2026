"""Проверки корректности сценариев без загрузки конкурсных клиентских данных."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from sensitivity import classify, segment_stress, offer_stress


class RobustnessTests(unittest.TestCase):
    def test_boundary_and_ineligibility(self):
        result = classify(np.array([-.1, -.08, -.05, -.025, 0]),
                          np.array([True, True, True, True, False]), -.08, -.025)
        self.assertEqual(result.tolist(), [0, 0, 1, 1, 3])

    def test_monotonicity_and_conservation(self):
        scores = np.array([-.2, -.12, -.085, -.08, -.07, -.05, -.027, -.025, -.02, 0])
        eligible = np.array([True] * 9 + [False])
        push = np.array([True] * 10)
        result = segment_stress(scores, eligible, push)['scenarios']
        self.assertGreaterEqual(result[0]['candidates'], result[1]['candidates'])
        self.assertGreaterEqual(result[1]['candidates'], result[2]['candidates'])
        for case in result:
            self.assertEqual(sum(case[k] for k in ('high_signal','moderate_signal',
                              'no_signal','insufficient_data')), 10)
        self.assertEqual(result[1]['changed_segment_vs_baseline'], 0)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            classify([float('nan')], [True], -.08, -.025)
        with self.assertRaises(ValueError):
            classify([1], [True], -.025, -.08)
        with self.assertRaises(ValueError):
            segment_stress([.5], [True, False], [True])

    def test_economics_full_basket_and_break_even(self):
        scenarios = offer_stress()['rows']
        self.assertEqual(len(scenarios), 15)
        base = next(r for r in scenarios if r['bonus_rub_per_liter'] == .5
                    and r['assumed_uplift_liters'] == 0)
        self.assertEqual(base['reward_cost_rub'], 8)
        self.assertAlmostEqual(base['break_even_liters'], .815)
        self.assertLess(base['scenario_profit_rub_per_treatment'], 0)
        profitable = next(r for r in scenarios if r['bonus_rub_per_liter'] == .5
                          and r['assumed_uplift_liters'] == 3)
        self.assertGreater(profitable['scenario_profit_rub_per_treatment'], 0)

    def test_published_sensitivity_matches_existing_metrics(self):
        result = json.loads((ROOT / 'outputs/sensitivity.json').read_text(encoding='utf-8'))
        metrics = json.loads((ROOT / 'outputs/metrics.json').read_text(encoding='utf-8'))
        baseline = next(x for x in result['segmentation']['scenarios'] if x['scenario'] == 'базовый')
        self.assertEqual(baseline['customers'] if 'customers' in baseline else result['segmentation']['customers'],
                         metrics['counts']['customers'])
        self.assertEqual(baseline['candidates'], metrics['counts']['eligible_price_signal'] - 15689)
        self.assertEqual(baseline['target_with_historical_push'], metrics['counts']['offer_target_with_push'])
        self.assertEqual(baseline['changed_segment_vs_baseline'], 0)


if __name__ == '__main__':
    unittest.main()