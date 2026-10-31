"""Сценарная экономика Pulse. Значения не являются фактами заказчика."""
from dataclasses import dataclass
from decimal import Decimal
import json

D = Decimal

@dataclass(frozen=True)
class Scenario:
    name: str
    baseline_hours_month: int
    review_hours_month: int
    operations_hours_month: int
    loaded_hourly_rub: int
    realization_ratio: str
    development_rub: int
    infrastructure_rub_year: int
    support_rub_year: int
    discount_rate: str = '0.15'

    def calculate(self):
        values = (self.baseline_hours_month, self.review_hours_month,
                  self.operations_hours_month, self.loaded_hourly_rub,
                  self.development_rub, self.infrastructure_rub_year,
                  self.support_rub_year)
        if any(v < 0 for v in values):
            raise ValueError('Значения не могут быть отрицательными')
        k, r = D(self.realization_ratio), D(self.discount_rate)
        if not 0 <= k <= 1 or r < 0:
            raise ValueError('Недопустимая доля реализации или ставка дисконтирования')
        hours = max(0, self.baseline_hours_month-self.review_hours_month-self.operations_hours_month)
        benefit = D(hours)*12*D(self.loaded_hourly_rub)*k
        running = D(self.infrastructure_rub_year+self.support_rub_year)
        pv_benefit = sum((benefit/(1+r)**year for year in range(1,4)),D(0))
        pv_running = sum((running/(1+r)**year for year in range(1,4)),D(0))
        ceiling = pv_benefit-pv_running
        npv = ceiling-D(self.development_rub)
        return {'scenario':self.name,'net_hours_month':hours,
                'capacity_value_rub_year':round(benefit),
                'running_cost_rub_year':round(running),
                'annual_net_capacity_value_rub':round(benefit-running),
                'three_year_npv_capacity_proxy_rub':round(npv),
                'max_initial_investment_rub_for_3y':round(ceiling),
                'financially_justified_on_capacity_proxy':npv>=0,
                'warning':'Высвобождение емкости не равно подтвержденной денежной экономии; все входные значения сценарные.'}

SCENARIOS = [
    Scenario('Осторожный',80,35,15,1500,'0.7',1800000,240000,360000),
    Scenario('Базовый',180,45,20,2000,'0.8',2500000,300000,420000),
    Scenario('Благоприятный',360,65,25,2500,'0.9',3500000,420000,600000),
]

if __name__ == '__main__':
    print(json.dumps([s.calculate() for s in SCENARIOS],ensure_ascii=False,indent=2))
