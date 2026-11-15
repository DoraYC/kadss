"""ABC-XYZ 分层（PRD US-2 / 4.5）。

口径：
- ABC 按统计窗口内消耗金额（需求数量 × 单价）降序累计占比划分
- XYZ 按周度聚合需求的变异系数划分（PRD 4.5：周聚合仅用于分层与趋势展示）
- 统计周期为「有需求的周数」，少于 min_periods 的物料不强行分层
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _weekly_demand(history: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy()
    frame["demand_date"] = pd.to_datetime(frame["demand_date"])
    frame["week"] = frame["demand_date"].dt.to_period("W")
    return (
        frame.groupby(["material_id", "week"], observed=True)["demand_qty"]
        .sum()
        .reset_index()
    )


def compute_abc_xyz(
    materials: pd.DataFrame,
    history: pd.DataFrame,
    cfg: dict[str, Any],
    run_id: str,
    created_at: str,
) -> pd.DataFrame:
    params = cfg["abc_xyz"]
    min_periods = int(params["min_periods"])

    weekly = _weekly_demand(history)
    stats = weekly.groupby("material_id")["demand_qty"].agg(
        avg_weekly="mean",
        sigma_weekly=lambda values: values.std(ddof=1),
        active_weeks=lambda values: int((values > 0).sum()),
    )
    stats["sigma_weekly"] = stats["sigma_weekly"].fillna(0.0)
    stats["cv"] = np.where(
        stats["avg_weekly"] > 0, stats["sigma_weekly"] / stats["avg_weekly"], 0.0
    )

    consumed = history.assign(
        amount=history["demand_qty"]
        * history["material_id"].map(materials.set_index("material_id")["unit_price"])
    )
    amount = consumed.groupby("material_id")["amount"].sum().rename("amount")

    frame = materials[["material_id"]].merge(stats[["cv", "active_weeks"]], on="material_id", how="left")
    frame = frame.merge(amount, on="material_id", how="left")
    frame["cv"] = frame["cv"].fillna(0.0)
    frame["active_weeks"] = frame["active_weeks"].fillna(0).astype(int)
    frame["amount"] = frame["amount"].fillna(0.0)
    frame["insufficient"] = frame["active_weeks"] < min_periods

    sufficient = frame.loc[~frame["insufficient"]].sort_values(
        ["amount", "material_id"], ascending=[False, True]
    )
    total_amount = float(sufficient["amount"].sum())
    if total_amount > 0:
        cumulative = sufficient["amount"].cumsum() / total_amount
        sufficient = sufficient.assign(
            abc_class=np.where(
                cumulative <= float(params["abc_a_share"]),
                "A",
                np.where(cumulative <= float(params["abc_b_share"]), "B", "C"),
            ),
            amount_share=sufficient["amount"] / total_amount,
        )
    else:
        sufficient = sufficient.assign(abc_class="C", amount_share=0.0)

    sufficient = sufficient.assign(
        xyz_class=np.where(
            sufficient["cv"] < float(params["xyz_x_cv"]),
            "X",
            np.where(sufficient["cv"] <= float(params["xyz_y_cv"]), "Y", "Z"),
        )
    )

    frame = frame.merge(
        sufficient[["material_id", "abc_class", "xyz_class", "amount_share"]],
        on="material_id",
        how="left",
    )
    combined = frame["abc_class"].fillna("") + frame["xyz_class"].fillna("")
    frame["combined_class"] = combined.where(frame["abc_class"].notna(), None)
    frame["amount_share"] = frame["amount_share"].fillna(0.0)
    frame["confidence_level"] = np.where(frame["insufficient"], "low", "high")
    frame["data_status"] = np.where(frame["insufficient"], "insufficient_data", "ok")
    frame["run_id"] = run_id
    frame["created_at"] = created_at

    return frame[
        [
            "run_id",
            "material_id",
            "abc_class",
            "xyz_class",
            "combined_class",
            "cv",
            "amount_share",
            "confidence_level",
            "data_status",
            "created_at",
        ]
    ].sort_values("material_id").reset_index(drop=True)
