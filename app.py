"""KADSS 看板：只读三场景预计算结果，参数变更只在内存重算。

对应 PRD US-7 与 TAD AD-1 / AD-9：
- 场景切换只切换 data/<scenario>/kadss.db，不触发生成或计算
- 可用量口径 → 齐套明细、缺口与缺料率；服务水平 → 安全库存与成本对比；
  惩罚系数 / 模拟次数 → 成本与齐套概率
- 重算结果不写回 SQLite；缓存键 = run_id + 可用量口径 + 服务水平 + 惩罚系数 + 模拟次数
"""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from kadss.config import DEFAULT_OUTPUT_DIR, load_config
from kadss.cost import simulate_cost
from kadss.kitting import compute_kitting
from kadss.labels import (
    DEFAULT_SCOPE_NOTE,
    MATERIAL_SHORTAGE_RATE,
    ORDER_SHORTAGE_RATE,
    STRATEGY_SCOPE_NOTE_SHORT,
    TRADEOFF_NOTE,
)
from kadss.metrics import gap_rates, order_status
from kadss.pipeline import load_base_tables, load_results, verify_results
from kadss.probability import compute_probability
from kadss.store import output_paths

st.set_page_config(page_title="KADSS · 供应链齐套预警", page_icon="K", layout="wide")

ABC_LEVELS = ("A", "B", "C")
XYZ_LEVELS = ("X", "Y", "Z")
UNCLASSIFIED = "未分层"
RISK_ORDER = ("低风险", "中风险", "高风险", "数据不足")
SCOPE_KEYS = ("in_transit", "allocated", "quality_hold", "safety_stock")
STRATEGY_LABELS = {"fixed": "固定安全库存", "dynamic": "动态安全库存"}
STRATEGY_ORDER = {"fixed": 0, "dynamic": 1}
CONFIDENCE_LABELS = {"high": "高", "low": "低"}
LT_SOURCE_LABELS = {"history": "历史采购交期", "master_data": "主数据标准提前期"}
DATA_STATUS_LABELS = {"ok": "正常", "insufficient_data": "样本不足"}


@st.cache_data(show_spinner="加载场景结果…")
def load_scene(scenario: str) -> dict[str, Any]:
    """读取基础表与结果表，并在读库前完成 run_id 自检。"""
    cfg = load_config()
    paths = output_paths(scenario, DEFAULT_OUTPUT_DIR)
    base = load_base_tables(paths["db"])
    run_id = verify_results(paths["db"], scenario, cfg, data=base)
    results = load_results(paths["db"], run_id)
    return {"run_id": run_id, "cfg": cfg, "base": base, "results": results}


@st.cache_data(show_spinner="按当前口径重算齐套结果…")
def kitting_for_scope(scenario: str, scope_key: tuple[bool, ...]) -> pd.DataFrame:
    """默认口径直接读落盘 baseline；其他口径按同一计算函数在内存重算。"""
    scene = load_scene(scenario)
    cfg = scene["cfg"]
    results = scene["results"]
    run_id = scene["run_id"]
    default_level = int(cfg["safety_stock"]["default_service_level"])
    scope = dict(zip(SCOPE_KEYS, scope_key))

    stored = results["t_kadss_kitting_result"]
    baseline = stored[
        (stored["strategy"] == "baseline")
        & (stored["service_level"] == default_level)
    ]
    default_scope = {
        key: bool(value) for key, value in cfg["kitting"]["available_scope"].items()
    }
    if scope == default_scope:
        return baseline.reset_index(drop=True)

    # 勾选安全库存时恒扣 default_service_level 的 dynamic_ss（AD-9）
    strategy = "dynamic" if scope["safety_stock"] else "baseline"
    confidence = baseline.groupby("material_id")["confidence_level"].first().to_dict()
    frame = compute_kitting(
        orders=scene["base"]["t_kadss_demand_order"],
        materials=scene["base"]["t_kadss_material"],
        inventory=scene["base"]["t_kadss_inventory"],
        bom=scene["base"]["t_kadss_bom"],
        safety_stock=results["t_kadss_safety_stock_result"],
        confidence_by_material=confidence,
        cfg=cfg,
        run_id=run_id,
        created_at=f"{cfg['base_date']} 00:00:00",
        strategy=strategy,
        service_level=default_level,
        scope_override=scope,
    )
    return frame


@st.cache_data(show_spinner="按惩罚系数重算成本…")
def cost_for_penalty(scenario: str, penalty: float) -> pd.DataFrame:
    scene = load_scene(scenario)
    cfg = scene["cfg"]
    results = scene["results"]
    if penalty == float(cfg["cost"]["penalty_factor"]):
        return results["t_kadss_cost_simulation_result"]
    overridden = copy.deepcopy(cfg)
    overridden["cost"]["penalty_factor"] = penalty
    return simulate_cost(
        materials=scene["base"]["t_kadss_material"],
        history=scene["base"]["t_kadss_demand_history"],
        safety_stock=results["t_kadss_safety_stock_result"],
        inventory=scene["base"]["t_kadss_inventory"],
        cfg=overridden,
        run_id=scene["run_id"],
        created_at=f"{cfg['base_date']} 00:00:00",
    )


@st.cache_data(show_spinner="按模拟次数重算齐套概率…")
def probability_for_sim_count(scenario: str, sim_count: int) -> pd.DataFrame:
    scene = load_scene(scenario)
    cfg = scene["cfg"]
    results = scene["results"]
    if sim_count == int(cfg["probability"]["simulation_count"]):
        return results["t_kadss_kitting_probability"]
    default_level = int(cfg["safety_stock"]["default_service_level"])
    stored = results["t_kadss_kitting_result"]
    baseline = stored[
        (stored["strategy"] == "baseline")
        & (stored["service_level"] == default_level)
    ]
    overridden = copy.deepcopy(cfg)
    overridden["probability"]["simulation_count"] = sim_count
    return compute_probability(
        kitting=baseline,
        orders=scene["base"]["t_kadss_demand_order"],
        safety_stock=results["t_kadss_safety_stock_result"],
        cfg=overridden,
        base_date=pd.Timestamp(cfg["base_date"]).normalize(),
        run_id=scene["run_id"],
        created_at=f"{cfg['base_date']} 00:00:00",
    )


@st.cache_data(show_spinner=False)
def strategy_gap_rates(scenario: str) -> pd.DataFrame:
    """策略口径的缺料率：可用量已扣除对应档位安全库存（落盘结果）。"""
    scene = load_scene(scenario)
    cfg = scene["cfg"]
    kitting = scene["results"]["t_kadss_kitting_result"]
    levels = [int(value) for value in cfg["safety_stock"]["service_levels"]]
    rows: list[dict[str, Any]] = []
    for level in levels:
        for strategy in ("fixed", "dynamic"):
            subset = kitting[
                (kitting["strategy"] == strategy)
                & (kitting["service_level"] == level)
            ]
            material_rate, order_rate = gap_rates(subset)
            rows.append(
                {
                    "service_level": level,
                    "strategy": strategy,
                    "material_rate": material_rate,
                    "order_rate": order_rate,
                }
            )
    return pd.DataFrame(rows)


def scope_text(scope: dict[str, bool], default_level: int) -> str:
    text = "当前库存"
    if scope["in_transit"]:
        text += " + 在途"
    if scope["allocated"]:
        text += " - 已分配"
    if scope["quality_hold"]:
        text += " - 质检锁定"
    if scope["safety_stock"]:
        text += f" - 安全库存（{default_level}% 服务水平动态安全库存）"
    return text


def confidence_series(values: pd.Series) -> pd.Series:
    return values.map(CONFIDENCE_LABELS).fillna(values)


def main() -> None:
    cfg = load_config()
    scenario_keys = list(cfg["scenarios"])

    with st.sidebar:
        st.header("参数与筛选")
        scenario = st.selectbox(
            "场景",
            scenario_keys,
            format_func=lambda key: cfg["scenarios"][key]["label"],
        )

    try:
        scene = load_scene(scenario)
    except ValueError as error:
        st.error(str(error))
        st.stop()

    base = scene["base"]
    results = scene["results"]
    run_id = scene["run_id"]
    materials = base["t_kadss_material"]
    demand_orders = base["t_kadss_demand_order"]
    safety_stock = results["t_kadss_safety_stock_result"]
    default_level = int(cfg["safety_stock"]["default_service_level"])
    levels = [int(value) for value in cfg["safety_stock"]["service_levels"]]

    abc = results["t_kadss_abc_xyz_result"].merge(
        materials[
            [
                "material_id",
                "material_name",
                "category",
                "supply_type",
                "unit_price",
                "uom",
            ]
        ],
        on="material_id",
        how="left",
    )
    abc["abc_label"] = abc["abc_class"].fillna(UNCLASSIFIED)
    abc["xyz_label"] = abc["xyz_class"].fillna(UNCLASSIFIED)

    default_scope = {
        key: bool(value) for key, value in cfg["kitting"]["available_scope"].items()
    }

    with st.sidebar:
        st.subheader("可用量口径")
        st.checkbox("当前库存", value=True, disabled=True)
        inc_transit = st.checkbox("在途", value=default_scope["in_transit"])
        inc_allocated = st.checkbox("已分配", value=default_scope["allocated"])
        inc_quality_hold = st.checkbox("质检锁定", value=default_scope["quality_hold"])
        inc_safety_stock = st.checkbox(
            f"安全库存（默认采用 {default_level}% 服务水平档位）",
            value=default_scope["safety_stock"],
        )

        st.subheader("安全库存参数")
        service_level = st.selectbox(
            "目标服务水平（%）",
            levels,
            index=levels.index(default_level) if default_level in levels else 0,
        )

        st.subheader("成本与模拟参数")
        penalty = st.slider(
            "缺料惩罚系数（倍）",
            2.0,
            5.0,
            float(cfg["cost"]["penalty_factor"]),
            0.5,
        )
        sim_count = st.slider(
            "蒙特卡洛模拟次数",
            100,
            1000,
            int(cfg["probability"]["simulation_count"]),
            100,
        )

        st.subheader("筛选器")
        abc_selected = st.multiselect(
            "ABC 分类", [*ABC_LEVELS, UNCLASSIFIED], default=[*ABC_LEVELS, UNCLASSIFIED]
        )
        xyz_selected = st.multiselect(
            "XYZ 分类", [*XYZ_LEVELS, UNCLASSIFIED], default=[*XYZ_LEVELS, UNCLASSIFIED]
        )

        min_date = pd.to_datetime(demand_orders["required_date"]).min().date()
        max_date = pd.to_datetime(demand_orders["required_date"]).max().date()
        date_range = st.date_input(
            "需求日期窗口",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
        )
        risk_selected = st.multiselect("订单风险等级", list(RISK_ORDER), default=list(RISK_ORDER))
        top_n = st.slider("预警表显示条数", 10, 100, 20, 10)

    scope = {
        "in_transit": inc_transit,
        "allocated": inc_allocated,
        "quality_hold": inc_quality_hold,
        "safety_stock": inc_safety_stock,
    }
    scope_key = tuple(bool(scope[key]) for key in SCOPE_KEYS)

    abc_view = abc[
        abc["abc_label"].isin(abc_selected) & abc["xyz_label"].isin(xyz_selected)
    ].copy()
    if abc_view.empty:
        st.warning("当前筛选条件下没有物料，请调整 ABC / XYZ 筛选。")
        st.stop()
    material_ids = set(abc_view["material_id"])

    kitting = kitting_for_scope(scenario, scope_key)
    probability = probability_for_sim_count(scenario, sim_count)

    risk_by_order = probability.set_index("order_id")[["on_time_probability", "risk_level"]]
    order_meta = demand_orders[["order_id", "order_type", "required_date"]].copy()
    order_meta["required_date"] = pd.to_datetime(order_meta["required_date"])
    order_meta = order_meta.join(risk_by_order, on="order_id")

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = min_date, max_date
    selected_orders = order_meta[
        (order_meta["required_date"].dt.date >= start_date)
        & (order_meta["required_date"].dt.date <= end_date)
        & (order_meta["risk_level"].isin(risk_selected))
    ]
    order_ids = set(selected_orders["order_id"])

    kitting_view = kitting[
        kitting["order_id"].isin(order_ids)
        & kitting["material_id"].isin(material_ids)
    ].copy()
    kitting_view = kitting_view.merge(
        abc_view[["material_id", "material_name", "abc_label", "xyz_label"]],
        on="material_id",
        how="left",
    )
    status_view = (
        order_status(kitting_view)
        if not kitting_view.empty
        else pd.DataFrame(
            columns=["order_id", "kitting_status", "gap_rows", "gap_amount"]
        )
    )
    order_view = (
        selected_orders.merge(status_view, on="order_id", how="inner")
        .merge(probability[["order_id", "sim_count"]], on="order_id", how="left")
        .sort_values(["gap_amount", "order_id"], ascending=[False, True])
    )

    material_rate, order_rate = gap_rates(kitting_view)
    high_risk_orders = int(order_view["risk_level"].isin(["高风险", "数据不足"]).sum())
    valid_probability = order_view["on_time_probability"].dropna()
    average_probability = (
        float(valid_probability.mean()) if len(valid_probability) else float("nan")
    )

    # ---------------- 头部 ----------------
    st.title("KADSS · 供应链齐套预警与动态安全库存优化")
    st.caption(
        f"数据版本 run_id：{run_id} ｜ 场景：{cfg['scenarios'][scenario]['label']} ｜ "
        f"seed={cfg['seed']} ｜ base_date={cfg['base_date']} ｜ "
        f"{cfg['scale']['materials']} SKU / {cfg['scale']['demand_orders']} 个需求订单 / "
        f"{cfg['scale']['history_days']} 天历史需求"
    )
    st.caption(f"当前可用量口径：{scope_text(scope, default_level)} ｜ {DEFAULT_SCOPE_NOTE}")
    if inc_safety_stock:
        st.info(
            f"已勾选安全库存：齐套明细按 {default_level}% 服务水平档位的动态安全库存扣减可用量，"
            "仅在内存重算。"
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(MATERIAL_SHORTAGE_RATE, f"{material_rate:.1%}")
    c2.metric(ORDER_SHORTAGE_RATE, f"{order_rate:.1%}")
    c3.metric("高风险 / 数据不足订单", high_risk_orders)
    c4.metric(
        "平均齐套概率",
        "—" if np.isnan(average_probability) else f"{average_probability:.0%}",
    )
    st.caption("以上指标按当前筛选范围统计；齐套概率与风险等级基于落盘 baseline 口径。")

    tab_layers, tab_kitting, tab_stock, tab_cost = st.tabs(
        ["物料分层", "齐套预警", "库存策略", "成本对比"]
    )

    # ---------------- Tab 1 物料分层 ----------------
    with tab_layers:
        st.subheader("ABC-XYZ 矩阵")
        matrix_rows = abc_view[abc_view["abc_class"].notna()]
        if matrix_rows.empty:
            st.info("当前筛选下没有可分层物料（样本不足的物料不强行分层）。")
        else:
            count_matrix = (
                matrix_rows.pivot_table(
                    index="abc_class",
                    columns="xyz_class",
                    values="material_id",
                    aggfunc="count",
                )
                .reindex(index=list(ABC_LEVELS), columns=list(XYZ_LEVELS), fill_value=0)
                .fillna(0)
            )
            amount_matrix = (
                matrix_rows.pivot_table(
                    index="abc_class",
                    columns="xyz_class",
                    values="amount_share",
                    aggfunc="sum",
                )
                .reindex(index=list(ABC_LEVELS), columns=list(XYZ_LEVELS), fill_value=0)
                .fillna(0)
            )
            texts = [
                [
                    f"{int(count_matrix.loc[abc_level, xyz_level])} 个"
                    f"<br>{amount_matrix.loc[abc_level, xyz_level]:.1%} 金额"
                    for xyz_level in XYZ_LEVELS
                ]
                for abc_level in ABC_LEVELS
            ]
            figure = go.Figure(
                go.Heatmap(
                    z=count_matrix.values,
                    x=list(XYZ_LEVELS),
                    y=list(ABC_LEVELS),
                    text=texts,
                    texttemplate="%{text}",
                    colorscale="Blues",
                    colorbar={"title": "物料数"},
                )
            )
            figure.update_layout(
                height=360,
                xaxis_title="XYZ（需求波动，按 CV 分档）",
                yaxis_title="ABC（消耗金额累计占比）",
                margin={"l": 40, "r": 20, "t": 20, "b": 40},
            )
            st.plotly_chart(figure, width="stretch")
            st.caption(
                "单元格显示物料数与金额占比；金额占比为占全部物料的年消耗金额份额。"
                "样本不足的物料不进入矩阵，在明细中标注数据置信度。"
            )

        st.subheader("分层明细（含数据置信度）")
        layer_table = abc_view[
            [
                "material_id",
                "material_name",
                "category",
                "abc_class",
                "xyz_class",
                "combined_class",
                "cv",
                "amount_share",
                "confidence_level",
                "data_status",
            ]
        ].copy()
        layer_table["amount_share"] = layer_table["amount_share"] * 100
        layer_table["confidence_level"] = confidence_series(
            layer_table["confidence_level"]
        )
        layer_table["data_status"] = layer_table["data_status"].map(DATA_STATUS_LABELS)
        st.dataframe(
            layer_table.rename(
                columns={
                    "material_id": "物料编码",
                    "material_name": "物料名称",
                    "category": "类别",
                    "abc_class": "ABC",
                    "xyz_class": "XYZ",
                    "combined_class": "组合",
                    "cv": "变异系数 CV",
                    "amount_share": "金额占比（%）",
                    "confidence_level": "数据置信度",
                    "data_status": "数据状态",
                }
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "金额占比（%）": st.column_config.NumberColumn(format="%.2f"),
                "变异系数 CV": st.column_config.NumberColumn(format="%.2f"),
            },
        )

    # ---------------- Tab 2 齐套预警 ----------------
    with tab_kitting:
        st.subheader("订单齐套预警")
        if order_view.empty:
            st.info("当前筛选条件下没有需求订单。")
        else:
            sort_label = st.radio(
                "排序方式",
                ["按缺口金额", "按齐套概率"],
                horizontal=True,
            )
            if sort_label == "按缺口金额":
                sorted_orders = order_view.sort_values(
                    ["gap_amount", "order_id"], ascending=[False, True]
                )
            else:
                sorted_orders = order_view.sort_values(
                    ["on_time_probability", "order_id"],
                    ascending=[True, True],
                    na_position="last",
                )
            order_table = sorted_orders.head(top_n).copy()
            order_table["probability_pct"] = order_table["on_time_probability"] * 100
            st.dataframe(
                order_table[
                    [
                        "order_id",
                        "order_type",
                        "required_date",
                        "kitting_status",
                        "gap_rows",
                        "gap_amount",
                        "probability_pct",
                        "risk_level",
                    ]
                ].rename(
                    columns={
                        "order_id": "订单号",
                        "order_type": "需求类型",
                        "required_date": "需求日期",
                        "kitting_status": "齐套结论",
                        "gap_rows": "缺口物料数",
                        "gap_amount": "缺口金额",
                        "probability_pct": "齐套概率（%）",
                        "risk_level": "风险等级",
                    }
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "需求日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                    "缺口金额": st.column_config.NumberColumn(format="%.2f"),
                    "齐套概率（%）": st.column_config.NumberColumn(format="%.0f"),
                },
            )
            st.caption(
                "齐套结论按当前可用量口径在内存重算；齐套概率与风险等级取落盘 baseline 结果，"
                f"模拟次数 {sim_count}。"
            )

            st.markdown("##### 订单缺口明细与交期分布假设")
            detail_order = st.selectbox(
                "选择订单", sorted(order_ids & set(order_view["order_id"]))
            )
            if detail_order:
                default_lt = safety_stock[
                    safety_stock["service_level"] == default_level
                ][["material_id", "lt_avg", "sigma_lt", "lt_source"]]
                detail = kitting_view[kitting_view["order_id"] == detail_order].merge(
                    default_lt, on="material_id", how="left"
                )
                detail = detail.sort_values("gap_amount", ascending=False)
                detail["confidence_level"] = confidence_series(
                    detail["confidence_level"]
                )
                detail["lt_source"] = detail["lt_source"].map(LT_SOURCE_LABELS)
                st.dataframe(
                    detail[
                        [
                            "material_id",
                            "material_name",
                            "abc_label",
                            "xyz_label",
                            "req_qty",
                            "available_qty",
                            "gap_qty",
                            "gap_amount",
                            "lt_avg",
                            "sigma_lt",
                            "lt_source",
                            "confidence_level",
                        ]
                    ].rename(
                        columns={
                            "material_id": "物料编码",
                            "material_name": "物料名称",
                            "abc_label": "ABC",
                            "xyz_label": "XYZ",
                            "req_qty": "毛需求",
                            "available_qty": "可用量",
                            "gap_qty": "缺口",
                            "gap_amount": "缺口金额",
                            "lt_avg": "总提前期均值（天）",
                            "sigma_lt": "总提前期标准差（天）",
                            "lt_source": "交期来源",
                            "confidence_level": "数据置信度",
                        }
                    ),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "毛需求": st.column_config.NumberColumn(format="%.1f"),
                        "可用量": st.column_config.NumberColumn(format="%.1f"),
                        "缺口": st.column_config.NumberColumn(format="%.1f"),
                        "缺口金额": st.column_config.NumberColumn(format="%.2f"),
                        "总提前期均值（天）": st.column_config.NumberColumn(format="%.1f"),
                        "总提前期标准差（天）": st.column_config.NumberColumn(format="%.1f"),
                    },
                )
                st.caption(
                    "交期分布假设：齐套概率以各缺口物料的总提前期 N(总提前期均值, 标准差) 为随机输入，"
                    f"按订单独立抽样 {sim_count} 次；交期样本来自历史已收货采购单，样本不足时回退主数据标准提前期。"
                )

    # ---------------- Tab 3 库存策略 ----------------
    with tab_stock:
        st.info(STRATEGY_SCOPE_NOTE_SHORT)
        st.caption(TRADEOFF_NOTE)

        level_rows = safety_stock[safety_stock["service_level"] == service_level].merge(
            abc_view[["material_id", "material_name", "abc_label", "xyz_label"]],
            on="material_id",
            how="inner",
        )
        z_value = float(level_rows["z_value"].iat[0]) if not level_rows.empty else float("nan")
        total_suggested = float(level_rows["suggested_amount"].sum())
        low_confidence = int((level_rows["confidence_level"] == "low").sum())

        p1, p2, p3 = st.columns(3)
        p1.metric("目标服务水平", f"{service_level}%")
        p2.metric("Z 值", "—" if np.isnan(z_value) else f"{z_value:.2f}")
        p3.metric("建议补货金额合计", f"{total_suggested:,.0f}")

        if level_rows.empty:
            st.info("当前筛选下没有安全库存结果。")
        else:
            st.subheader(f"建议补货金额 Top 10（{service_level}% 服务水平）")
            top_stock = level_rows.sort_values("suggested_amount", ascending=False).head(10)
            stock_figure = px.bar(
                top_stock.sort_values("suggested_amount"),
                x="suggested_amount",
                y="material_id",
                orientation="h",
                labels={
                    "suggested_amount": "建议补货金额",
                    "material_id": "物料编码",
                },
                title=f"建议补货金额 Top 10 · {service_level}% 服务水平",
            )
            stock_figure.update_layout(height=380, margin={"l": 10, "r": 20, "t": 50, "b": 40})
            st.plotly_chart(stock_figure, width="stretch")
            st.caption(
                "建议补货量 = MAX(0, 动态安全库存 + 日均需求 × LT_total − 当前可用量)；"
                "提前期内需求按日均需求 × LT_total 估算，当前可用量按落盘默认口径计算。"
                f" 低置信物料 {low_confidence} 个，数值保留但需谨慎解读。"
            )

            stock_table = level_rows[
                [
                    "material_id",
                    "material_name",
                    "abc_label",
                    "xyz_label",
                    "lt_total",
                    "dynamic_ss",
                    "fixed_ss",
                    "delta",
                    "suggested_qty",
                    "suggested_amount",
                    "lt_source",
                    "confidence_level",
                ]
            ].copy()
            stock_table["lt_source"] = stock_table["lt_source"].map(LT_SOURCE_LABELS)
            stock_table["confidence_level"] = confidence_series(
                stock_table["confidence_level"]
            )
            st.dataframe(
                stock_table.rename(
                    columns={
                        "material_id": "物料编码",
                        "material_name": "物料名称",
                        "abc_label": "ABC",
                        "xyz_label": "XYZ",
                        "lt_total": "LT_total（天）",
                        "dynamic_ss": "动态安全库存",
                        "fixed_ss": "固定安全库存",
                        "delta": "动态 − 固定",
                        "suggested_qty": "建议补货量",
                        "suggested_amount": "建议补货金额",
                        "lt_source": "交期来源",
                        "confidence_level": "数据置信度",
                    }
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "LT_total（天）": st.column_config.NumberColumn(format="%.0f"),
                    "动态安全库存": st.column_config.NumberColumn(format="%.1f"),
                    "固定安全库存": st.column_config.NumberColumn(format="%.1f"),
                    "动态 − 固定": st.column_config.NumberColumn(format="%.1f"),
                    "建议补货量": st.column_config.NumberColumn(format="%.1f"),
                    "建议补货金额": st.column_config.NumberColumn(format="%.2f"),
                },
            )

    # ---------------- Tab 4 成本对比 ----------------
    with tab_cost:
        st.info(STRATEGY_SCOPE_NOTE_SHORT)
        st.caption(TRADEOFF_NOTE)

        cost = cost_for_penalty(scenario, penalty)
        aggregate = cost.groupby(["strategy", "service_level"], as_index=False).agg(
            avg_inventory_value=("avg_inventory_value", "sum"),
            holding_cost=("holding_cost", "sum"),
            shortage_qty=("shortage_qty", "sum"),
            shortage_days=("shortage_days", "sum"),
            shortage_cost=("shortage_cost", "sum"),
        )
        aggregate = aggregate.merge(
            strategy_gap_rates(scenario), on=["strategy", "service_level"], how="left"
        )
        aggregate["策略"] = aggregate["strategy"].map(STRATEGY_LABELS)
        aggregate["strategy_order"] = aggregate["strategy"].map(STRATEGY_ORDER)
        aggregate = (
            aggregate.sort_values(["service_level", "strategy_order"])
            .drop(columns="strategy_order")
            .reset_index(drop=True)
        )

        metric_options = {
            "平均库存金额": "avg_inventory_value",
            "持有成本": "holding_cost",
            "缺料成本": "shortage_cost",
            MATERIAL_SHORTAGE_RATE: "material_rate",
            ORDER_SHORTAGE_RATE: "order_rate",
        }
        metric_label = st.selectbox("对比指标", list(metric_options), index=0)
        metric_column = metric_options[metric_label]
        is_rate = metric_column in ("material_rate", "order_rate")

        chart_rows = aggregate.copy()
        chart_rows["指标值"] = (
            chart_rows[metric_column] * 100 if is_rate else chart_rows[metric_column]
        )
        axis_title = f"{metric_label}（%）" if is_rate else metric_label
        cost_figure = px.bar(
            chart_rows,
            x="service_level",
            y="指标值",
            color="策略",
            barmode="group",
            text_auto=".2f" if is_rate else ".3s",
            title=f"{metric_label} · 固定 vs 动态安全库存（惩罚系数 {penalty:.1f} 倍）",
            labels={"service_level": "服务水平（%）", "指标值": axis_title},
        )
        cost_figure.update_layout(height=420, margin={"t": 60})
        st.plotly_chart(cost_figure, width="stretch")

        cost_table = aggregate[
            [
                "策略",
                "service_level",
                "avg_inventory_value",
                "holding_cost",
                "shortage_qty",
                "shortage_days",
                "shortage_cost",
                "material_rate",
                "order_rate",
            ]
        ].copy()
        cost_table["material_rate"] = cost_table["material_rate"] * 100
        cost_table["order_rate"] = cost_table["order_rate"] * 100
        cost_table = cost_table.rename(
            columns={
                "service_level": "服务水平（%）",
                "avg_inventory_value": "平均库存金额",
                "holding_cost": "持有成本",
                "shortage_qty": "缺料数量",
                "shortage_days": "缺料天数",
                "shortage_cost": "缺料成本",
                "material_rate": MATERIAL_SHORTAGE_RATE,
                "order_rate": ORDER_SHORTAGE_RATE,
            }
        )
        st.dataframe(
            cost_table,
            width="stretch",
            hide_index=True,
            column_config={
                "平均库存金额": st.column_config.NumberColumn(format="%.0f"),
                "持有成本": st.column_config.NumberColumn(format="%.0f"),
                "缺料数量": st.column_config.NumberColumn(format="%.1f"),
                "缺料成本": st.column_config.NumberColumn(format="%.0f"),
                MATERIAL_SHORTAGE_RATE: st.column_config.NumberColumn(format="%.2f"),
                ORDER_SHORTAGE_RATE: st.column_config.NumberColumn(format="%.2f"),
            },
        )

        delta_rows: list[dict[str, Any]] = []
        for level in levels:
            level_cost = aggregate[aggregate["service_level"] == level].set_index("strategy")
            if not {"fixed", "dynamic"} <= set(level_cost.index):
                continue
            delta_rows.append(
                {
                    "服务水平（%）": level,
                    "平均库存金额变化": level_cost.loc["dynamic", "avg_inventory_value"]
                    - level_cost.loc["fixed", "avg_inventory_value"],
                    "物料缺料率变化（百分点）": (
                        level_cost.loc["dynamic", "material_rate"]
                        - level_cost.loc["fixed", "material_rate"]
                    )
                    * 100,
                    "订单缺料率变化（百分点）": (
                        level_cost.loc["dynamic", "order_rate"]
                        - level_cost.loc["fixed", "order_rate"]
                    )
                    * 100,
                }
            )
        if delta_rows:
            st.caption("策略差异（动态 − 固定），不预设更优结论，由数据决定。")
            st.dataframe(
                pd.DataFrame(delta_rows),
                width="stretch",
                hide_index=True,
                column_config={
                    "平均库存金额变化": st.column_config.NumberColumn(format="%.0f"),
                    "物料缺料率变化（百分点）": st.column_config.NumberColumn(format="%.2f"),
                    "订单缺料率变化（百分点）": st.column_config.NumberColumn(format="%.2f"),
                },
            )
        st.caption(
            f"成本参数为假设值：年持有成本率 {cfg['cost']['holding_rate']:.0%}、"
            f"仿真周期 {cfg['cost']['simulation_days']} 天、缺料惩罚系数 {penalty:.1f} 倍。"
        )

    st.divider()
    st.caption(
        "适用边界：本系统面向离散制造与装配型场景，流程制造（如化工、材料、晶圆前道）"
        "需结合批次、配方与工艺参数重新建模。MVP 仅计算理论可用量，不区分物理批次、库位与保质期。"
    )
    st.caption(
        "看板只读预计算结果；可用量口径、服务水平与模拟参数变更在内存重算，不写回 SQLite。"
    )


if __name__ == "__main__":
    main()
