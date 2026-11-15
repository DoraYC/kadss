"""命令行入口：计算 ABC-XYZ、安全库存、齐套、齐套概率与成本仿真并写入结果表。

用法：
    python -m kadss.build_results                    # 默认 normal 场景
    python -m kadss.build_results --scenario all     # 三个场景全部计算
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from kadss.config import load_config
from kadss.labels import (
    DEFAULT_SCOPE_NOTE,
    MATERIAL_SHORTAGE_RATE,
    ORDER_SHORTAGE_RATE,
    STRATEGY_SCOPE_NOTE,
    TRADEOFF_NOTE,
)
from kadss.metrics import gap_rates
from kadss.pipeline import run_scenario


def build_report(frames: dict[str, pd.DataFrame], cfg: dict[str, Any], run_id: str) -> str:
    default_level = int(cfg["safety_stock"]["default_service_level"])
    abc = frames["t_kadss_abc_xyz_result"]
    safety_stock = frames["t_kadss_safety_stock_result"]
    kitting = frames["t_kadss_kitting_result"]
    probability = frames["t_kadss_kitting_probability"]
    cost = frames["t_kadss_cost_simulation_result"]

    lines = [f"run_id: {run_id}"]

    combined = abc["combined_class"].value_counts()
    insufficient = int((abc["data_status"] == "insufficient_data").sum())
    lines.append(
        "ABC-XYZ："
        + " ".join(f"{key}={combined.get(key, 0)}" for key in ("AX", "AY", "AZ", "BX", "BY", "BZ", "CX", "CY", "CZ"))
        + f" 未分层={insufficient}"
    )

    level_ss = safety_stock[safety_stock["service_level"] == default_level]
    lt_history = int((level_ss["lt_source"] == "history").sum())
    lines.append(
        f"动态安全库存（{default_level}%）：交期用历史={lt_history} 用主数据={len(level_ss) - lt_history} "
        f"低置信物料={int((level_ss['confidence_level'] == 'low').sum())}"
    )

    status_counts = probability["kitting_status"].value_counts().to_dict()
    risk_counts = probability["risk_level"].value_counts().to_dict()
    valid = probability["on_time_probability"].dropna()
    summary = f"订单齐套结论：{status_counts}；风险分布：{risk_counts}"
    if len(valid):
        summary += f"；平均齐套概率={valid.mean():.3f}"
    lines.append(summary)

    baseline = kitting[
        (kitting["strategy"] == "baseline") & (kitting["service_level"] == default_level)
    ]
    row_rate, order_rate = gap_rates(baseline)
    lines.append(
        f"默认口径（{DEFAULT_SCOPE_NOTE}）：{MATERIAL_SHORTAGE_RATE}={row_rate:.1%} "
        f"{ORDER_SHORTAGE_RATE}={order_rate:.1%}"
    )

    lines.append("策略对比（按服务水平，可用量已扣除对应策略的安全库存预留）：")
    lines.append(STRATEGY_SCOPE_NOTE)
    lines.append(TRADEOFF_NOTE)
    totals = cost.groupby(["service_level", "strategy"])[
        ["avg_inventory_value", "holding_cost", "shortage_qty", "shortage_cost"]
    ].sum()
    for level in sorted(cost["service_level"].unique()):
        strategy_rates = {}
        for strategy in ("fixed", "dynamic"):
            subset = kitting[
                (kitting["strategy"] == strategy) & (kitting["service_level"] == level)
            ]
            strategy_rates[strategy] = gap_rates(subset)
        fixed = totals.loc[(level, "fixed")]
        dynamic = totals.loc[(level, "dynamic")]
        lines.append(
            f"  {level}%：库存金额 固定={fixed['avg_inventory_value']:.0f} "
            f"动态={dynamic['avg_inventory_value']:.0f} | 总成本 固定="
            f"{fixed['holding_cost'] + fixed['shortage_cost']:.0f} 动态="
            f"{dynamic['holding_cost'] + dynamic['shortage_cost']:.0f} | "
            f"物料缺料率(行) {strategy_rates['fixed'][0]:.1%}→{strategy_rates['dynamic'][0]:.1%} | "
            f"订单缺料率(单) {strategy_rates['fixed'][1]:.1%}→{strategy_rates['dynamic'][1]:.1%}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="计算 KADSS 结果表并写入 SQLite")
    parser.add_argument("--scenario", default="normal", help="场景名，或 all 计算全部场景")
    parser.add_argument("--profile", help="数据规模，取自 profiles（如 small / large）")
    parser.add_argument("--config", type=Path, help="配置文件路径，默认 config/kadss.json")
    parser.add_argument("--output", type=Path, help="数据根目录，默认取 profile 的 output_root")
    args = parser.parse_args(argv)

    cfg = load_config(args.config, profile=args.profile)
    output_root = args.output or Path(cfg["output_root"])
    scenarios = list(cfg["scenarios"]) if args.scenario == "all" else [args.scenario]
    try:
        for scenario in scenarios:
            frames, run_id = run_scenario(scenario, cfg, output_root)
            print(f"=== {scenario} ===")
            print(build_report(frames, cfg, run_id))
    except ValueError as error:
        print(error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
