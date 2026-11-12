import sys,unittest,json,csv
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from analyze import segment_one,stable_corr

class MarzhrutTests(unittest.TestCase):
    def test_segment_bounds(self):
        self.assertEqual(segment_one(-0.1,True),'Высокий сигнал')
        self.assertEqual(segment_one(-0.08,True),'Высокий сигнал')
        self.assertEqual(segment_one(-0.05,True),'Умеренный сигнал')
        self.assertEqual(segment_one(-0.025,True),'Умеренный сигнал')
        self.assertEqual(segment_one(0.1,True),'Не выявлен')
        self.assertEqual(segment_one(-0.8,False),'Недостаточно данных')
    def test_corr_requires_variation(self):
        self.assertIsNone(stable_corr([1,1,1],[0,2,3]))
        self.assertAlmostEqual(stable_corr([1,2,3],[4,3,2]),-1.0)
    def test_business_margin_identity(self):
        margin=14900;opex=1500;density=0.742
        contribution=(margin-opex)*density/1000
        self.assertAlmostEqual(contribution,9.9428)
    def test_scenario_break_even(self):
        c=9.94;r=.4;liters=40;discount=.5;comm=.1
        cutoff=(r*liters*discount+comm)/c
        self.assertGreater(cutoff,0.8);self.assertLess(cutoff,0.85)
    def test_aggregate_conservation(self):
        m=json.loads((ROOT/'outputs/metrics.json').read_text(encoding='utf-8'))
        with (ROOT/'outputs/segments.csv').open(encoding='utf-8',newline='') as f:segments=list(csv.DictReader(f,delimiter=';'))
        self.assertEqual(sum(int(v['customers']) for v in segments),m['counts']['customers'])
        self.assertEqual(sum(int(v['push_opt_in_customers']) for v in segments),m['counts']['push_with_app_consent'])
        self.assertLess(abs(sum(float(v['total_liters_9m']) for v in segments)-m['counts']['total_liters_9m']),1)

if __name__=='__main__':unittest.main()