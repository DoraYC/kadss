"""库存成本仿真（PRD US-6）。

口径：
- 策略：fixed（固定安全库存基准）vs dynamic（动态安全库存）
- 同一物料在两策略下使用同一组日需求抽样与同一采购提前期，保证可比
- 补货策略：库存位置 ≤ 再订货点时补货至「再订货点 + 提前期内需求」，LT 天后到货
- 提前期取安全库存结果的 lt_avg（采购件含采购、检验与物流段）
- 再订货点 = 提前期内需求（日均需求 × 提前期）+ 安全库存缓冲（固定覆盖天数 / 动态安全库存）
- 持有成本 = 平均库存金额 × 年持有成本率 × 仿真天数 / 365
- 缺料成本 = 缺料数量 × 单价 × 缺料惩罚系数
"""

from __future__ import annotations

import zlib
from typing import Any

import numpy as np
import pandas as pd

from kadss.availability import SIMULATION_SCOPE, available_by_material


def _simulate(
    demand: np.ndarray,
    initial_stock: float,
    reorder_point: float,
    order_up_to: float,
    lead_time_days: int,
) -> tuple[float, float, int]:
    stock = initial_stock
    pipeline: list[tuple[int, float]] = []
    total_stock = 0.0
    shortage_qty = 0.0
    shortage_days = 0

    for day, demand_today in enumerate(demand):
        if pipeline:
            arrivals = sum(quantity for arrival, quantity in pipeline if arrival == day)
            stock += arrivals
            pipeline = [(arrival, quantity) for arrival, quantity in pipeline if arrival > day]

        served = min(stock, float(demand_today))
        stock -= served
        shortage = float(demand_today) - served
        if shortage > 0:
            shortage_days += 1
            shortage_qty += shortage

        position = stock + sum(quantity for _, quantity in pipeline)
        if position <= reorder_point:
            quantity = order_up_to - position
            if quantity > 0:
                pipeline.append((day + lead_time_days, quantity))
        total_stock += stock

    average_inventory = total_stock / len(demand) if len(demand) else 0.0
    return average_inventory, shortage_qty, shortage_days


def simulate_cost(
    materials: pd.DataFrame,
    history: pd.DataFrame,
    safety_stock: pd.DataFrame,
    inventory: pd.DataFrame,
    cfg: dict[str, Any],
    run_id: str,
    created_at: str,
) -> pd.DataFrame:
    params = cfg["cost"]
    levels = [int(level) for level in cfg["safety_stock"]["service_levels"]]
    default_level = int(cfg["safety_stock"]["default_service_level"])
    simulation_days = int(params["simulation_days"])
    holding_rate = float(params["holding_rate"])
    penalty = float(params["penalty_factor"])
    seed = int(params["seed"])

    price = materials.set_index("material_id")["unit_price"]
    initial_stock_by_material = available_by_material(inventory, SIMULATION_SCOPE)
    history_by_material = {
        str(material_id): group["demand_qty"].to_numpy(dtype=float)
        for material_id, group in history.groupby("material_id")
    }
    base_level = safety_stock[safety_stock["service_level"] == default_level].set_index(
        "material_id"
    )

    rows: list[dict[str, Any]] = []
    for material_id in materials["material_id"]:
        material_id = str(material_id)
        historical = history_by_material.get(material_id)
        if historical is None or historical.size == 0:
            continue

        rng = np.random.default_rng(seed + zlib.crc32(material_id.encode("utf-8")))
        demand = rng.choice(historical, size=simulation_days, replace=True)
        lead_time_days = max(1, int(round(float(base_level.loc[material_id, "lt_avg"]))))
        average_daily = float(base_level.loc[material_id, "avg_demand"])
        initial_stock = float(initial_stock_by_material.loc[material_id])
        fixed_point = float(base_level.loc[material_id, "fixed_ss"])
        unit_price = float(price.get(material_id, 0.0))

        lead_time_demand = average_daily * lead_time_days
        for level in levels:
            dynamic_point = float(
                safety_stock[
                    (safety_stock["service_level"] == level)
                    & (safety_stock["material_id"] == material_id)
                ]["dynamic_ss"].iat[0]
            )
            for strategy, buffer_point in (
                ("fixed", fixed_point),
                ("dynamic", dynamic_point),
            ):
                reorder_point = lead_time_demand + buffer_point
                average_inventory, shortage_qty, shortage_days = _simulate(
                    demand=demand,
                    initial_stock=initial_stock,
                    reorder_point=reorder_point,
                    order_up_to=reorder_point + lead_time_demand,
                    lead_time_days=lead_time_days,
                )
                average_value = average_inventory * unit_price
                rows.append(
                    {
                        "run_id": run_id,
                        "material_id": material_id,
                        "strategy": strategy,
                        "service_level": level,
                        "penalty_factor": penalty,
                        "avg_inventory_qty": average_inventory,
                        "avg_inventory_value": average_value,
                        "holding_cost": average_value * holding_rate * simulation_days / 365.0,
                        "shortage_qty": shortage_qty,
                        "shortage_days": shortage_days,
                        "shortage_cost": shortage_qty * unit_price * penalty,
                        "created_at": created_at,
                    }
                )

    return pd.DataFrame(rows).sort_values(
        ["material_id", "strategy", "service_level"]
    ).reset_index(drop=True)
