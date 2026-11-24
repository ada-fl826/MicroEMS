"""Regression checks for replay causality, terminal fairness and PV provenance."""

import copy
import unittest
from unittest.mock import patch

from backend.schemas import CreateSimulation, EnterpriseSettings
from backend.settlement import settle_interval
from scripts.economic_evaluation import (
    available_forecast,
    feasible_action,
    replay,
)


def small_scenario(pv, load):
    return {
        "forecast": [
            {
                "slot": t,
                "time": str(t),
                "pv_kw": pv_kw,
                "load_kw": load_kw,
                "buy_price": 0.288,
                "sell_price": 0.4153,
            }
            for t, (pv_kw, load_kw) in enumerate(zip(pv, load))
        ],
        "actual_pv_kw": pv,
        "actual_load_kw": load,
        "pv_capacity_kw": 3,
    }


class EconomicEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.request = CreateSimulation(
            enterprise=EnterpriseSettings(import_target_kw=None)
        )

    def test_current_and_future_actuals_do_not_change_optimizer_input(self):
        scenario = small_scenario([0, 1, 2], [2, 2, 2])
        changed = copy.deepcopy(scenario)
        changed["actual_pv_kw"][1:] = [100, 100]
        changed["actual_load_kw"][1:] = [100, 100]
        self.assertEqual(
            available_forecast(scenario, 1), available_forecast(changed, 1)
        )
        changed["actual_load_kw"][0] = 4
        self.assertNotEqual(
            available_forecast(scenario, 1), available_forecast(changed, 1)
        )

    def test_endpoint_recovery_is_billed_and_soc_is_continuous(self):
        scenario = small_scenario([0] * 8, [2] * 8)
        f = replay(scenario, "Self-Consumption RBC", self.request)
        self.assertAlmostEqual(f.soc.iloc[-1], 0.5)
        self.assertGreater(f.charge_kw.sum(), 0)
        self.assertGreater(f.cost_yuan.sum(), 2 * 8 * 0.25 * 0.288)
        self.assertEqual(f.filter(like="violation_").to_numpy().sum(), 0)
        self.assertEqual(f.pv_delivered_kwh.sum(), 0)

    def test_final_surplus_is_exported_instead_of_free_terminal_inventory(self):
        scenario = small_scenario([3], [1])
        f = replay(scenario, "Self-Consumption RBC", self.request)
        self.assertAlmostEqual(f.soc.iloc[-1], 0.5)
        self.assertAlmostEqual(f.export_kw.iloc[0], 2)
        self.assertAlmostEqual(f.cost_yuan.sum(), -0.5 * 0.4153)

    def test_grid_charging_is_never_counted_as_pv(self):
        scenario = small_scenario([0] * 8, [2] * 8)
        f = replay(scenario, "Time-of-Use RBC", self.request)
        self.assertGreater(f.charge_kw.sum(), 0)
        self.assertEqual(f.pv_inventory_kwh.sum(), 0)
        self.assertEqual(f.pv_delivered_kwh.sum(), 0)

    def test_pv_delivery_excludes_losses_and_terminal_inventory(self):
        scenario = small_scenario([3, 0, 0], [1, 2, 2])
        self.request.equipment.initial_soc = 0.3
        self.request.equipment.terminal_soc = 0.3
        f = replay(scenario, "Self-Consumption RBC", self.request)
        # Stored energy is proportionally mixed with the initial reserve.
        fraction = (0.5 * 0.95) / (1.5 + 0.5 * 0.95)
        expected = 0.25 + (0.5 * 0.95**2) * fraction
        self.assertAlmostEqual(f.pv_delivered_kwh.sum(), expected)
        self.assertGreater(f.pv_inventory_kwh.iloc[-1], 0)
        self.assertLess(f.pv_delivered_kwh.sum(), 0.75)

    def test_forecast_import_limit_overrides_planned_charge(self):
        action = feasible_action(2, 2.5, 4.5, 95, self.request)
        self.assertAlmostEqual(action, 0.5)
        with self.assertRaisesRegex(ValueError, "cannot satisfy"):
            feasible_action(2, 1.5, 8, 95, self.request)

    def test_current_actuals_change_settlement_without_changing_dispatch(self):
        scenario = small_scenario([0] * 8, [2] * 8)
        changed = copy.deepcopy(scenario)
        changed["actual_load_kw"][0] = 4
        for name in ("Self-Consumption RBC", "Time-of-Use RBC", "Intelligent EMS"):
            with (
                self.subTest(name=name),
                patch(
                    "scripts.economic_evaluation.solve_dispatch",
                    return_value={"points": [{"charge_kw": 2, "discharge_kw": 0}]},
                ),
            ):
                first = replay(scenario, name, self.request).iloc[0]
                other = replay(changed, name, self.request).iloc[0]
                for key in ("desired_battery_kw", "charge_kw", "discharge_kw", "soc"):
                    self.assertAlmostEqual(first[key], other[key])
                self.assertGreater(other.cost_yuan, first.cost_yuan)

    def test_settlement_curtails_only_pv_and_exposes_residual_export(self):
        self.request.equipment.grid_export_capacity_kw = 0.25
        point = small_scenario([0.5], [1])["forecast"][0]
        actual = settle_interval(point, 0.5, 1, 0, 2, self.request)
        self.assertAlmostEqual(actual["curtail_kw"], 0.5)
        self.assertAlmostEqual(actual["export_kw"], 1)


if __name__ == "__main__":
    unittest.main()
