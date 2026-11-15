"""齐套概率评估（PRD US-5 / 4.5）。

模型：订单中每个存在缺口的物料，需要补货在 required_date 前到货；
以该物料总提前期分布 N(lt_avg, σ_LT) 为随机输入做蒙特卡洛，所有缺口物料都及时到货才算按时齐套。
- 订单含低置信度物料时概率输出 NULL，风险等级「数据不足」
- 每个订单用 order_id 派生的固定种子，结果可复现
"""

from __future__ import annotations

import zlib
from typing import Any

import numpy as np
import pandas as pd


def _risk_level(probability: float, cfg: dict[str, Any]) -> str:
    if probability >= float(cfg["probability"]["risk_low"]):
        return "低风险"
    if probability >= float(cfg["probability"]["risk_medium"]):
        return "中风险"
    return "高风险"


def compute_probability(
    kitting: pd.DataFrame,
    orders: pd.DataFrame,
    safety_stock: pd.DataFrame,
    cfg: dict[str, Any],
    base_date: pd.Timestamp,
    run_id: str,
    created_at: str,
) -> pd.DataFrame:
    params = cfg["probability"]
    default_level = int(cfg["safety_stock"]["default_service_level"])
    simulation_count = int(params["simulation_count"])
    seed = int(params["seed"])

    baseline = kitting[
        (kitting["strategy"] == "baseline") & (kitting["service_level"] == default_level)
    ]
    grouped = {str(order_id): group for order_id, group in baseline.groupby("order_id")}

    lead_time = safety_stock[safety_stock["service_level"] == default_level].set_index(
        "material_id"
    )

    rows: list[dict[str, Any]] = []
    required_dates = orders.set_index("order_id")["required_date"]
    for order_id in sorted(grouped):
        group = grouped[order_id]
        has_low = bool((group["confidence_level"] == "low").any())
        gap_rows = group[group["gap_qty"] > 0]
        gap_amount = float(group["gap_amount"].sum())
        gap_count = int(len(gap_rows))

        if has_low:
            status, probability, risk = "数据不足", None, "数据不足"
        else:
            status = "不齐套" if gap_amount > 0 else "齐套"
            if gap_rows.empty:
                probability = 1.0
            else:
                days_available = int(
                    (
                        pd.Timestamp(required_dates.loc[order_id]).normalize()
                        - base_date
                    ).days
                )
                if days_available <= 0:
                    probability = 0.0
                else:
                    rng = np.random.default_rng(
                        seed + zlib.crc32(order_id.encode("utf-8"))
                    )
                    means = gap_rows["material_id"].map(lead_time["lt_avg"]).to_numpy(dtype=float)
                    sigmas = gap_rows["material_id"].map(lead_time["sigma_lt"]).to_numpy(dtype=float)
                    draws = rng.normal(
                        loc=means,
                        scale=sigmas,
                        size=(simulation_count, means.size),
                    )
                    draws = np.clip(draws, 0.0, None)
                    on_time = (draws <= days_available).all(axis=1)
                    probability = float(on_time.mean())
            risk = _risk_level(probability, cfg)

        rows.append(
            {
                "run_id": run_id,
                "order_id": order_id,
                "kitting_status": status,
                "on_time_probability": probability,
                "risk_level": risk,
                "gap_rows": gap_count,
                "gap_amount": gap_amount,
                "sim_count": simulation_count,
                "created_at": created_at,
            }
        )

    return pd.DataFrame(rows).sort_values("order_id").reset_index(drop=True)
