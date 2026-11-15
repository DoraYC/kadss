"""BOM 齐套分析（PRD US-4 / 4.5）。

口径：
- 多层级 BOM 递归展开，超过 max_depth 停止展开
- 替代料行（is_substitute = 1）不参与毛需求，只在父件行标记 has_substitute
- 可用量 = 可配置口径；多个订单共用物料时按 required_date 升序先到先得
- 缺口 = max(毛需求 - 可用量, 0)，缺口金额 = 缺口数量 × 单价
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from kadss.availability import available_quantity


def build_children_map(bom: pd.DataFrame) -> dict[str, list[tuple[str, float]]]:
    primary = bom[bom["is_substitute"] == 0]
    children: dict[str, list[tuple[str, float]]] = {}
    for parent, child, quantity in zip(
        primary["parent_material_id"], primary["child_material_id"], primary["quantity_per"]
    ):
        children.setdefault(parent, []).append((str(child), float(quantity)))
    return children


def expand_requirements(
    children: dict[str, list[tuple[str, float]]],
    material_id: str,
    quantity: float,
    max_depth: int,
) -> dict[str, float]:
    """递归展开毛需求，同一物料跨层级累加；用路径集合兜底防环。"""
    totals: dict[str, float] = {}

    def visit(current: str, required: float, depth: int, path: frozenset[str]) -> None:
        totals[current] = totals.get(current, 0.0) + required
        if depth >= max_depth:
            return
        for child, quantity_per in children.get(current, []):
            if child in path:
                continue
            visit(child, required * quantity_per, depth + 1, path | {child})

    visit(material_id, quantity, 0, frozenset({material_id}))
    return totals


def describe_scope(scope: dict[str, bool], service_level: int, ss_label: str) -> str:
    text = "on_hand"
    if scope["in_transit"]:
        text += "+in_transit"
    if scope["allocated"]:
        text += "-allocated"
    if scope["quality_hold"]:
        text += "-quality_hold"
    if scope["safety_stock"]:
        text += f"-safety_stock({service_level}%,{ss_label})"
    return text


def compute_kitting(
    orders: pd.DataFrame,
    materials: pd.DataFrame,
    inventory: pd.DataFrame,
    bom: pd.DataFrame,
    safety_stock: pd.DataFrame,
    confidence_by_material: dict[str, str],
    cfg: dict[str, Any],
    run_id: str,
    created_at: str,
    strategy: str = "baseline",
    service_level: int | None = None,
    scope_override: dict[str, bool] | None = None,
) -> pd.DataFrame:
    """strategy 为 baseline 时不扣减安全库存；fixed / dynamic 按对应策略扣减。"""
    service_level = int(
        service_level
        if service_level is not None
        else cfg["safety_stock"]["default_service_level"]
    )
    max_depth = int(cfg["kitting"]["max_depth"])
    scope = dict(cfg["kitting"]["available_scope"])
    if scope_override:
        scope.update(scope_override)
    if strategy != "baseline":
        scope["safety_stock"] = True

    price_by_material = materials.set_index("material_id")["unit_price"]
    stock = inventory.set_index("material_id")
    children = build_children_map(bom)
    substitute_parents = set(bom.loc[bom["is_substitute"] == 1, "parent_material_id"])

    level_rows = safety_stock[safety_stock["service_level"] == service_level]
    level_rows = level_rows.set_index("material_id")
    if strategy == "fixed":
        deduct_by_material = level_rows["fixed_ss"]
    elif strategy == "dynamic":
        deduct_by_material = level_rows["dynamic_ss"]
    else:
        deduct_by_material = None
    ss_label = strategy if strategy != "baseline" else "none"
    scope_text = describe_scope(scope, service_level, ss_label)

    remaining: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    ordered = orders.sort_values(["required_date", "order_id"])

    for order in ordered.itertuples(index=False):
        requirements = expand_requirements(
            children, str(order.material_id), float(order.qty), max_depth
        )
        for material_id in sorted(requirements):
            required = float(requirements[material_id])
            if material_id not in remaining:
                deduct = (
                    0.0
                    if deduct_by_material is None
                    else float(deduct_by_material.get(material_id, 0.0))
                )
                remaining[material_id] = available_quantity(
                    stock.loc[material_id], scope, deduct
                )
            available = remaining[material_id]
            # 可用量为负说明已被前置订单占用完，按 0 参与缺口计算，避免缺口超过毛需求
            gap = max(required - max(available, 0.0), 0.0)
            stock_row = stock.loc[material_id]
            rows.append(
                {
                    "run_id": run_id,
                    "order_id": str(order.order_id),
                    "material_id": material_id,
                    "service_level": service_level,
                    "strategy": strategy,
                    "req_qty": required,
                    "on_hand": float(stock_row["on_hand"]),
                    "in_transit": float(stock_row["in_transit"]),
                    "allocated": float(stock_row["allocated"]),
                    "quality_hold": float(stock_row["quality_hold"]),
                    "safety_stock_deduct": float(
                        0.0
                        if deduct_by_material is None
                        else deduct_by_material.get(material_id, 0.0)
                    ),
                    "available_qty": available,
                    "gap_qty": gap,
                    "gap_amount": gap * float(price_by_material.get(material_id, 0.0)),
                    "available_scope": scope_text,
                    "has_substitute": int(material_id in substitute_parents),
                    "confidence_level": confidence_by_material.get(material_id, "high"),
                    "created_at": created_at,
                }
            )
            remaining[material_id] = max(available - required, 0.0)

    frame = pd.DataFrame(rows)
    return frame.sort_values(
        ["order_id", "material_id", "strategy", "service_level"]
    ).reset_index(drop=True)
