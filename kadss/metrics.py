"""公共指标口径：订单齐套状态与缺料率（AD-3 / TD-2）。"""

from __future__ import annotations

import pandas as pd


def kitting_status(has_low_confidence: bool, gap_amount: float) -> str:
    """订单级结论的唯一判定规则。"""
    if has_low_confidence:
        return "数据不足"
    return "不齐套" if gap_amount > 0 else "齐套"


def order_status(kitting: pd.DataFrame) -> pd.DataFrame:
    """按订单汇总齐套状态、缺口行数与缺口金额。"""
    records: list[dict[str, object]] = []
    for order_id, group in kitting.groupby("order_id", sort=True):
        gap_amount = float(group["gap_amount"].sum())
        gap_rows = int((group["gap_qty"] > 0).sum())
        records.append(
            {
                "order_id": str(order_id),
                "kitting_status": kitting_status(
                    bool((group["confidence_level"] == "low").any()), gap_amount
                ),
                "gap_rows": gap_rows,
                "gap_amount": gap_amount,
            }
        )
    return pd.DataFrame(records)


def gap_rates(kitting: pd.DataFrame) -> tuple[float, float]:
    """物料缺料率（按行数）与订单缺料率（按订单数）。"""
    rows = len(kitting)
    gap_rows = int((kitting["gap_qty"] > 0).sum())
    orders = kitting["order_id"].nunique()
    gap_orders = int(
        kitting.groupby("order_id")["gap_qty"].apply(lambda values: (values > 0).any()).sum()
    )
    return (
        gap_rows / rows if rows else 0.0,
        gap_orders / orders if orders else 0.0,
    )
