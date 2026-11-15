"""动态安全库存（PRD US-3 / 4.5）。

公式：SS = Z × √(LT_avg × σ_d² + d_avg² × σ_LT²)
- 采购件：LT_avg / σ_LT 取历史实际交期（已收货采购单 actual_lt）；样本不足回退物料主数据标准提前期
- 自制 / 外包件：MVP 直接用四项标准提前期之和，σ_LT = 0，标记 lt_source = master_data
- 固定策略基准：d_avg × fixed_coverage_days（模拟「一刀切」的固定覆盖天数）
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from kadss.availability import BASE_SCOPE, available_by_material


def _daily_demand_stats(history: pd.DataFrame) -> pd.DataFrame:
    stats = history.groupby("material_id")["demand_qty"].agg(
        avg_demand="mean",
        sigma_demand=lambda values: values.std(ddof=1),
        active_days=lambda values: int((values > 0).sum()),
    )
    stats["sigma_demand"] = stats["sigma_demand"].fillna(0.0)
    return stats


def _lead_time_stats(purchase_orders: pd.DataFrame) -> pd.DataFrame:
    received = purchase_orders[purchase_orders["status"] == "received"]
    received = received.dropna(subset=["actual_lt"])
    stats = received.groupby("material_id")["actual_lt"].agg(
        lt_avg_history="mean",
        sigma_lt_history=lambda values: values.std(ddof=1),
        lt_samples="count",
    )
    stats["sigma_lt_history"] = stats["sigma_lt_history"].fillna(0.0)
    return stats


def compute_safety_stock(
    materials: pd.DataFrame,
    history: pd.DataFrame,
    purchase_orders: pd.DataFrame,
    inventory: pd.DataFrame,
    cfg: dict[str, Any],
    run_id: str,
    created_at: str,
) -> pd.DataFrame:
    params = cfg["safety_stock"]
    min_periods = int(params["min_periods"])
    levels = [int(level) for level in params["service_levels"]]
    z_values = {str(level): float(value) for level, value in params["z_values"].items()}

    frame = materials[
        [
            "material_id",
            "supply_type",
            "purchase_lt",
            "production_lt",
            "inspection_lt",
            "logistics_lt",
            "unit_price",
        ]
    ].copy()
    frame["lt_total"] = (
        frame["purchase_lt"]
        + frame["production_lt"]
        + frame["inspection_lt"]
        + frame["logistics_lt"]
    )
    frame = frame.merge(_daily_demand_stats(history), on="material_id", how="left")
    frame = frame.merge(_lead_time_stats(purchase_orders), on="material_id", how="left")
    frame["avg_demand"] = frame["avg_demand"].fillna(0.0)
    frame["sigma_demand"] = frame["sigma_demand"].fillna(0.0)
    frame["active_days"] = frame["active_days"].fillna(0).astype(int)
    frame["lt_samples"] = frame["lt_samples"].fillna(0).astype(int)

    use_history = (frame["supply_type"] == "purchase") & (
        frame["lt_samples"] >= min_periods
    )
    frame["lt_avg"] = np.where(use_history, frame["lt_avg_history"], frame["lt_total"])
    frame["sigma_lt"] = np.where(use_history, frame["sigma_lt_history"], 0.0)
    frame["lt_source"] = np.where(use_history, "history", "master_data")

    available = available_by_material(inventory, BASE_SCOPE)
    frame["available_qty"] = frame["material_id"].map(available).fillna(0.0)

    frame["fixed_ss"] = frame["avg_demand"] * float(params["fixed_coverage_days"])
    frame["confidence_level"] = np.where(
        (frame["active_days"] < min_periods)
        | ((frame["supply_type"] == "purchase") & (frame["lt_samples"] < min_periods)),
        "low",
        "high",
    )

    rows: list[pd.DataFrame] = []
    for level in levels:
        z_value = z_values[str(level)]
        dynamic_ss = z_value * np.sqrt(
            frame["lt_avg"] * frame["sigma_demand"] ** 2
            + frame["avg_demand"] ** 2 * frame["sigma_lt"] ** 2
        )
        suggested_qty = np.maximum(
            0.0, dynamic_ss + frame["avg_demand"] * frame["lt_total"] - frame["available_qty"]
        )
        rows.append(
            pd.DataFrame(
                {
                    "run_id": run_id,
                    "material_id": frame["material_id"],
                    "service_level": level,
                    "z_value": z_value,
                    "lt_total": frame["lt_total"],
                    "lt_purchase": frame["purchase_lt"],
                    "lt_production": frame["production_lt"],
                    "lt_inspection": frame["inspection_lt"],
                    "lt_logistics": frame["logistics_lt"],
                    "lt_avg": frame["lt_avg"],
                    "sigma_lt": frame["sigma_lt"],
                    "avg_demand": frame["avg_demand"],
                    "sigma_demand": frame["sigma_demand"],
                    "dynamic_ss": dynamic_ss,
                    "fixed_ss": frame["fixed_ss"],
                    "delta": dynamic_ss - frame["fixed_ss"],
                    "suggested_qty": suggested_qty,
                    "suggested_amount": suggested_qty * frame["unit_price"],
                    "lt_source": frame["lt_source"],
                    "confidence_level": frame["confidence_level"],
                    "created_at": created_at,
                }
            )
        )

    return (
        pd.concat(rows, ignore_index=True)
        .sort_values(["material_id", "service_level"])
        .reset_index(drop=True)
    )
