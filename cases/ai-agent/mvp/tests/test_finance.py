import unittest
from finance import Scenario, SCENARIOS

class FinanceTests(unittest.TestCase):
    def test_all_scenarios(self):
        self.assertEqual(len([s.calculate() for s in SCENARIOS]),3)
    def test_baseline_value(self):
        self.assertEqual(SCENARIOS[1].calculate()['capacity_value_rub_year'],2208000)
    def test_costs_counted(self):
        self.assertEqual(SCENARIOS[1].calculate()['running_cost_rub_year'],720000)
    def test_no_negative_saved_hours(self):
        self.assertEqual(Scenario('x',10,20,30,1000,'1',0,0,0).calculate()['net_hours_month'],0)
    def test_invalid_realization(self):
        with self.assertRaises(ValueError):
            Scenario('x',10,0,0,1000,'1.1',0,0,0).calculate()
    def test_invalid_cost(self):
        with self.assertRaises(ValueError):
            Scenario('x',10,0,0,1000,'1',-1,0,0).calculate()
    def test_npv_costs_reduce_ceiling(self):
        a=Scenario('x',100,0,0,1000,'1',0,0,0).calculate()
        b=Scenario('x',100,0,0,1000,'1',0,100000,0).calculate()
        self.assertLess(b['max_initial_investment_rub_for_3y'],a['max_initial_investment_rub_for_3y'])

if __name__ == '__main__': unittest.main()
