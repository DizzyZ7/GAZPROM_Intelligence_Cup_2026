import math
import csv
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from power import PowerPlan
from economics import price_to_liter_margin
from pilot import relative_uplift


class ExperimentPlanningTests(unittest.TestCase):
    def test_measured_scenario_pilot_is_underpowered_for_small_uplift(self):
        x = PowerPlan(eligible=4050, sample_share=.1, baseline_sd_liters=50.71,
                      target_effect_liters=3)
        self.assertEqual(x.arms, (202, 203))
        self.assertGreater(x.mde_liters, 14)
        self.assertLess(x.mde_liters, 14.2)
        self.assertFalse(x.report()['enough_candidates_for_target'])

    def test_full_population_has_more_precision(self):
        small = PowerPlan(4050, .1, 50.71, 3)
        full = PowerPlan(4050, 1., 50.71, 3)
        self.assertAlmostEqual(full.mde_liters, 4.46, delta=.03)
        self.assertLess(full.mde_liters, small.mde_liters)
        self.assertEqual(full.arms, (2025, 2025))

    def test_var_reduction_only_as_explicit_assumption(self):
        a = PowerPlan(4050, 1, 50.71, 3)
        b = PowerPlan(4050, 1, 50.71, 3, variance_reduction=.5)
        self.assertAlmostEqual(b.mde_liters/a.mde_liters, math.sqrt(.5))
        self.assertEqual(b.report()['assumptions']['variance_reduction'], .5)

    def test_reject_invalid_specifications(self):
        for changes in [{'baseline_sd_liters': float('nan')},
                        {'sample_share': 0}, {'variance_reduction': 1},
                        {'alpha': 0.8}, {'power': 1.0}, {'eligible': 2},
                        {'sample_share': 0.0001}]:
            values = dict(eligible=4050, sample_share=.1,
                          baseline_sd_liters=50.71, target_effect_liters=3)
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PowerPlan(**values)

    def test_published_power_matches_aggregates(self):
        root = Path(__file__).resolve().parents[1]
        with (root / 'outputs/pilot_groups.csv').open(encoding='utf-8', newline='') as f:
            groups = list(csv.DictReader(f, delimiter=';'))
        selected = next(g for g in groups if g['segment'] == 'Высокий сигнал')
        with (root / 'outputs/power_plan.json').open(encoding='utf-8') as f:
            report = json.load(f)
        self.assertEqual(int(selected['eligible_push_margin_customers']), report['assumptions']['eligible'])
        self.assertAlmostEqual(float(selected['sd_last_3m_avg_liters']), report['assumptions']['baseline_sd_liters'])
        self.assertEqual(report['sample_total'], report['treatment']+report['control'])
        self.assertFalse(report['enough_candidates_for_target'])

    def test_financial_units_must_be_finite(self):
        for invalid in [float('nan'), float('inf'), -float('inf')]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                price_to_liter_margin(14900, 1500, invalid, subtract_opex=True)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                price_to_liter_margin(invalid, 1500, .742, subtract_opex=True)

    def test_uplift_inputs_finite(self):
        for invalid in [float('nan'), float('inf'), -float('inf')]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                relative_uplift(invalid, 10, 100, 10)


if __name__ == '__main__':
    unittest.main()