"""KADSS 计算层与持久化契约测试（PRD US-1 ~ US-6 / TAD AD-2、AD-3、AD-7、AD-8）。"""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from kadss.availability import (
    BASE_SCOPE,
    SIMULATION_SCOPE,
    available_by_material,
    available_quantity,
)
from kadss.config import DEFAULT_OUTPUT_DIR, load_config
from kadss.generator import generate_dataset
from kadss.kitting import compute_kitting
from kadss.metrics import gap_rates, kitting_status, order_status
from kadss.pipeline import load_base_tables, load_results, run_scenario, verify_results
from kadss.probability import compute_probability
from kadss.safety_stock import compute_safety_stock

DEFAULT_LEVEL = 95


def _inventory_row(**overrides: float) -> pd.Series:
    values = {"on_hand": 10.0, "in_transit": 3.0, "allocated": 2.0, "quality_hold": 1.0}
    values.update(overrides)
    return pd.Series(values)


class AvailabilityTest(unittest.TestCase):
    def test_scopes_apply_expected_additions_and_deductions(self) -> None:
        row = _inventory_row()
        self.assertAlmostEqual(available_quantity(row, BASE_SCOPE), 11.0)
        self.assertAlmostEqual(available_quantity(row, SIMULATION_SCOPE), 13.0)
        self.assertAlmostEqual(available_quantity(row, BASE_SCOPE, 4.0), 7.0)

    def test_available_by_material_supports_series_deduction(self) -> None:
        inventory = pd.DataFrame(
            {
                "material_id": ["M1", "M2"],
                "on_hand": [10.0, 5.0],
                "in_transit": [0.0, 1.0],
                "allocated": [2.0, 1.0],
                "quality_hold": [0.0, 0.0],
            }
        )
        deduct = pd.Series({"M1": 3.0, "M2": 1.0})
        result = available_by_material(inventory, BASE_SCOPE, deduct)
        self.assertAlmostEqual(result.loc["M1"], 5.0)
        self.assertAlmostEqual(result.loc["M2"], 4.0)


def _kitting_fixture() -> dict[str, pd.DataFrame]:
    materials = pd.DataFrame(
        {
            "material_id": ["M1", "M2"],
            "unit_price": [10.0, 5.0],
        }
    )
    bom = pd.DataFrame(
        {
            "parent_material_id": ["M1"],
            "child_material_id": ["M2"],
            "quantity_per": [2.0],
            "is_substitute": [0],
        }
    )
    inventory = pd.DataFrame(
        {
            "material_id": ["M1", "M2"],
            "on_hand": [5.0, 4.0],
            "in_transit": [0.0, 0.0],
            "allocated": [0.0, 0.0],
            "quality_hold": [0.0, 0.0],
        }
    )
    orders = pd.DataFrame(
        {
            "order_id": ["SO1", "SO2"],
            "material_id": ["M1", "M1"],
            "qty": [3.0, 3.0],
            "required_date": ["2026-11-20", "2026-11-21"],
        }
    )
    safety_stock = pd.DataFrame(
        {
            "material_id": ["M1", "M1", "M1", "M2", "M2", "M2"],
            "service_level": [90, 95, 99, 90, 95, 99],
            "dynamic_ss": [1.0] * 6,
            "fixed_ss": [2.0] * 6,
        }
    )
    return {
        "materials": materials,
        "bom": bom,
        "inventory": inventory,
        "orders": orders,
        "safety_stock": safety_stock,
    }


class KittingTest(unittest.TestCase):
    def test_shared_material_allocated_by_required_date(self) -> None:
        fixture = _kitting_fixture()
        frame = compute_kitting(
            orders=fixture["orders"],
            materials=fixture["materials"],
            inventory=fixture["inventory"],
            bom=fixture["bom"],
            safety_stock=fixture["safety_stock"],
            confidence_by_material={"M1": "high", "M2": "high"},
            cfg=load_config(),
            run_id="run",
            created_at="2026-11-16 00:00:00",
            strategy="baseline",
            service_level=DEFAULT_LEVEL,
        )
        indexed = frame.set_index(["order_id", "material_id"])
        self.assertAlmostEqual(indexed.loc[("SO1", "M1"), "gap_qty"], 0.0)
        self.assertAlmostEqual(indexed.loc[("SO1", "M2"), "gap_qty"], 2.0)
        self.assertAlmostEqual(indexed.loc[("SO2", "M1"), "available_qty"], 2.0)
        self.assertAlmostEqual(indexed.loc[("SO2", "M1"), "gap_qty"], 1.0)
        self.assertAlmostEqual(indexed.loc[("SO2", "M2"), "gap_qty"], 6.0)
        self.assertAlmostEqual(float(frame["gap_amount"].sum()), 50.0)

    def test_dynamic_strategy_deducts_safety_stock(self) -> None:
        fixture = _kitting_fixture()
        frame = compute_kitting(
            orders=fixture["orders"],
            materials=fixture["materials"],
            inventory=fixture["inventory"],
            bom=fixture["bom"],
            safety_stock=fixture["safety_stock"],
            confidence_by_material={"M1": "high", "M2": "high"},
            cfg=load_config(),
            run_id="run",
            created_at="2026-11-16 00:00:00",
            strategy="dynamic",
            service_level=DEFAULT_LEVEL,
        )
        first = frame[(frame["order_id"] == "SO1") & (frame["material_id"] == "M1")]
        self.assertAlmostEqual(float(first["safety_stock_deduct"].iat[0]), 1.0)
        self.assertAlmostEqual(float(first["available_qty"].iat[0]), 4.0)


class MetricsTest(unittest.TestCase):
    def test_order_status_and_shortage_rates(self) -> None:
        kitting = pd.DataFrame(
            {
                "order_id": ["SO1", "SO2", "SO3"],
                "gap_qty": [0.0, 5.0, 0.0],
                "gap_amount": [0.0, 25.0, 0.0],
                "confidence_level": ["high", "high", "low"],
            }
        )
        status = order_status(kitting).set_index("order_id")
        self.assertEqual(status.loc["SO1", "kitting_status"], "齐套")
        self.assertEqual(status.loc["SO2", "kitting_status"], "不齐套")
        self.assertEqual(status.loc["SO3", "kitting_status"], "数据不足")
        self.assertEqual(status.loc["SO2", "gap_rows"], 1)

        material_rate, order_rate = gap_rates(kitting)
        self.assertAlmostEqual(material_rate, 1 / 3)
        self.assertAlmostEqual(order_rate, 1 / 3)
        self.assertEqual(kitting_status(False, 0.0), "齐套")
        self.assertEqual(kitting_status(True, 0.0), "数据不足")


class SafetyStockTest(unittest.TestCase):
    def test_dynamic_formula_and_lead_time_source(self) -> None:
        materials = pd.DataFrame(
            {
                "material_id": ["M1"],
                "supply_type": ["purchase"],
                "purchase_lt": [5.0],
                "production_lt": [0.0],
                "inspection_lt": [0.0],
                "logistics_lt": [0.0],
                "unit_price": [10.0],
            }
        )
        history = pd.DataFrame(
            {
                "material_id": ["M1"] * 3,
                "demand_qty": [8.0, 10.0, 12.0],
            }
        )
        purchase_orders = pd.DataFrame(
            {
                "material_id": ["M1"] * 3,
                "status": ["received"] * 3,
                "actual_lt": [4.0, 6.0, 8.0],
            }
        )
        inventory = pd.DataFrame(
            {
                "material_id": ["M1"],
                "on_hand": [0.0],
                "in_transit": [0.0],
                "allocated": [0.0],
                "quality_hold": [0.0],
            }
        )
        cfg = load_config()
        frame = compute_safety_stock(
            materials, history, purchase_orders, inventory, cfg, "run", "2026-11-16 00:00:00"
        )
        level = frame[frame["service_level"] == DEFAULT_LEVEL].iloc[0]

        # d_avg = 10，σ_d = 2；历史交期 4/6/8 天，lt_avg = 6，σ_LT = 2
        expected = 1.65 * np.sqrt(6 * 2**2 + 10**2 * 2**2)
        self.assertAlmostEqual(float(level["dynamic_ss"]), expected)
        self.assertAlmostEqual(float(level["fixed_ss"]), 70.0)
        self.assertEqual(level["lt_source"], "history")
        self.assertEqual(level["confidence_level"], "high")


class ProbabilityTest(unittest.TestCase):
    def _probability(self, confidence: str, gap_qty: float) -> pd.DataFrame:
        kitting = pd.DataFrame(
            {
                "order_id": ["SO1"],
                "material_id": ["M1"],
                "strategy": ["baseline"],
                "service_level": [DEFAULT_LEVEL],
                "confidence_level": [confidence],
                "gap_qty": [gap_qty],
                "gap_amount": [gap_qty * 10.0],
            }
        )
        orders = pd.DataFrame(
            {"order_id": ["SO1"], "required_date": ["2026-12-01"]}
        )
        safety_stock = pd.DataFrame(
            {
                "material_id": ["M1"],
                "service_level": [DEFAULT_LEVEL],
                "lt_avg": [5.0],
                "sigma_lt": [1.0],
            }
        )
        return compute_probability(
            kitting,
            orders,
            safety_stock,
            load_config(),
            pd.Timestamp("2026-11-16"),
            "run",
            "2026-11-16 00:00:00",
        )

    def test_low_confidence_order_returns_null_probability(self) -> None:
        row = self._probability("low", 0.0).iloc[0]
        self.assertTrue(pd.isna(row["on_time_probability"]))
        self.assertEqual(row["kitting_status"], "数据不足")
        self.assertEqual(row["risk_level"], "数据不足")

    def test_no_gap_order_is_fully_kitted(self) -> None:
        row = self._probability("high", 0.0).iloc[0]
        self.assertAlmostEqual(float(row["on_time_probability"]), 1.0)
        self.assertEqual(row["kitting_status"], "齐套")
        self.assertEqual(row["risk_level"], "低风险")


class ProfileTest(unittest.TestCase):
    def test_profiles_resolve_scale_and_output_root(self) -> None:
        small = load_config()
        large = load_config(profile="large")
        self.assertEqual(small["scale"]["materials"], 200)
        self.assertEqual(large["scale"]["materials"], 2000)
        self.assertEqual(large["scale"]["demand_orders"], 300)
        self.assertEqual(large["scale"]["history_days"], 180)
        self.assertTrue(str(small["output_root"]).endswith("data"))
        self.assertTrue(str(large["output_root"]).endswith("data_large"))

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_config(profile="huge")


class PersistenceTest(unittest.TestCase):
    def test_generation_is_reproducible(self) -> None:
        cfg = load_config()
        first = generate_dataset("normal", cfg)
        second = generate_dataset("normal", cfg)
        for table in first:
            pd.testing.assert_frame_equal(first[table], second[table])

    def test_verify_results_rejects_config_mismatch(self) -> None:
        cfg = load_config()
        db_path = DEFAULT_OUTPUT_DIR / "normal" / "kadss.db"
        run_id = verify_results(db_path, "normal", cfg)
        results = load_results(db_path, run_id)
        for table in results:
            self.assertEqual(set(results[table]["run_id"]), {run_id})

        mismatched = copy.deepcopy(cfg)
        mismatched["seed"] = cfg["seed"] + 1
        with self.assertRaises(ValueError):
            verify_results(db_path, "normal", mismatched)

    def test_scenario_rerun_is_row_identical(self) -> None:
        cfg = load_config()
        source = DEFAULT_OUTPUT_DIR / "normal" / "kadss.db"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "normal"
            target.mkdir(parents=True)
            shutil.copy(source, target / "kadss.db")
            first_frames, first_run = run_scenario("normal", cfg, tmp)
            second_frames, second_run = run_scenario("normal", cfg, tmp)
            self.assertEqual(first_run, second_run)
            for table in first_frames:
                pd.testing.assert_frame_equal(first_frames[table], second_frames[table])
            self.assertEqual(
                set(load_base_tables(target / "kadss.db")["t_kadss_material"]["material_id"]),
                set(first_frames["t_kadss_abc_xyz_result"]["material_id"]),
            )


if __name__ == "__main__":
    unittest.main()
