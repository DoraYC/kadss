"""可用量统一口径（AD-3 / TD-2）。

约定：
- 基础口径 = on_hand + in_transit - allocated，可选再扣 quality_hold
- 安全库存预留由调用方换算成扣减量传入，不在此处读取策略
- 成本仿真的期初库存只含现有与在途，使用 SIMULATION_SCOPE
"""

from __future__ import annotations

from typing import Mapping

import pandas as pd

BASE_SCOPE = {"in_transit": True, "allocated": True, "quality_hold": False}
SIMULATION_SCOPE = {"in_transit": True, "allocated": False, "quality_hold": False}


def _values(
    frame: pd.DataFrame,
    scope: Mapping[str, bool],
    safety_stock_deduct: float | pd.Series,
) -> pd.Series:
    values = frame["on_hand"].astype(float)
    if scope.get("in_transit", True):
        values = values + frame["in_transit"].astype(float)
    if scope.get("allocated", True):
        values = values - frame["allocated"].astype(float)
    if scope.get("quality_hold", False):
        values = values - frame["quality_hold"].astype(float)
    if isinstance(safety_stock_deduct, pd.Series):
        values = values - safety_stock_deduct.reindex(values.index).fillna(0.0)
    else:
        values = values - float(safety_stock_deduct)
    return values


def available_quantity(
    stock_row: pd.Series,
    scope: Mapping[str, bool],
    safety_stock_deduct: float = 0.0,
) -> float:
    """单行可用量，供齐套逐物料计算。"""
    frame = pd.DataFrame([stock_row])
    return float(_values(frame, scope, safety_stock_deduct).iloc[0])


def available_by_material(
    inventory: pd.DataFrame,
    scope: Mapping[str, bool] = BASE_SCOPE,
    safety_stock_deduct: pd.Series | None = None,
) -> pd.Series:
    """按 material_id 汇总可用量，供安全库存与成本仿真使用。"""
    frame = inventory.set_index("material_id")
    deduct = 0.0 if safety_stock_deduct is None else safety_stock_deduct
    return _values(frame, scope, deduct)
