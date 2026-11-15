"""KADSS 看板骨架（假数据）

对应 PRD：离散制造供应链齐套预警与动态安全库存优化系统 v1.1
- 侧边栏：场景切换、可用量口径、服务水平、安全库存系数、缺料惩罚系数、筛选器
- 主区：ABC-XYZ 矩阵、齐套预警表、库存策略、成本对比
- 数据：按 PRD 口径生成的可复现假数据（seed=42），不做跨订单共享料分配
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="KADSS · 供应链齐套预警", page_icon="K", layout="wide")

SEED = 42
N_MATERIALS = 200
N_ORDERS = 50

SCENARIOS = {
    "正常": {"key": "normal", "offset": 0, "lt_sigma": 1.5},
    "高波动": {"key": "high_volatility", "offset": 1, "lt_sigma": 2.5},
    "交期恶化": {"key": "leadtime_deterioration", "offset": 2, "lt_sigma": 5.0},
}
ORDER_TYPES = ["生产工单", "客户订单", "补货计划", "维修备件需求", "项目物料需求"]
CATEGORIES = ["电子料", "结构件", "标准件", "包材", "备件"]
ABC_LEVELS = ["A", "B", "C"]
XYZ_LEVELS = ["X", "Y", "Z"]
RISK_LEVELS = ["低风险", "中风险", "高风险", "数据不足"]
Z_BY_LEVEL = {90: 1.28, 95: 1.65, 99: 2.33}


@st.cache_data(show_spinner=False)
def build_demo_data(scenario: str, seed: int = SEED) -> dict[str, pd.DataFrame]:
    """按场景生成假数据；同一 seed 派生，保证结果可复现。"""
    scen = SCENARIOS[scenario]
    rng = np.random.default_rng(seed + scen["offset"])

    n = N_MATERIALS
    supply_type = rng.choice(["purchase", "make", "outsource"], n, p=[0.70, 0.20, 0.10])
    monthly_demand = rng.integers(20, 800, n)
    cv = np.round(rng.gamma(2.0, 0.35, n), 2)
    unit_price = np.round(rng.lognormal(3.2, 1.0, n), 2)
    purchase_lt = rng.integers(5, 45, n)
    production_lt = rng.integers(3, 20, n)
    inspection_lt = rng.integers(0, 3, n)
    logistics_lt = rng.integers(1, 7, n)

    lt_total = np.where(
        supply_type == "purchase",
        purchase_lt + inspection_lt + logistics_lt,
        np.where(
            supply_type == "make",
            production_lt + inspection_lt,
            production_lt + logistics_lt,
        ),
    ).astype(float)

    materials = pd.DataFrame(
        {
            "material_id": [f"M{i + 1:04d}" for i in range(n)],
            "material_name": [f"物料-{i + 1:04d}" for i in range(n)],
            "category": rng.choice(CATEGORIES, n),
            "supply_type": supply_type,
            "uom": "PCS",
            "unit_price": unit_price,
            "monthly_demand": monthly_demand,
            "cv": cv,
            "purchase_lt": purchase_lt,
            "production_lt": production_lt,
            "inspection_lt": inspection_lt,
            "logistics_lt": logistics_lt,
            "lt_total": lt_total,
        }
    )

    # 消耗金额与 ABC 分层
    materials["annual_amount"] = materials["unit_price"] * materials["monthly_demand"] * 12
    materials = materials.sort_values("annual_amount", ascending=False).reset_index(drop=True)
    materials["amount_share"] = materials["annual_amount"] / materials["annual_amount"].sum()
    cum_share = materials["amount_share"].cumsum()
    materials["abc_class"] = np.where(cum_share <= 0.7, "A", np.where(cum_share <= 0.9, "B", "C"))

    # 需求波动与 XYZ 分层
    materials["xyz_class"] = np.where(
        materials["cv"] < 0.5, "X", np.where(materials["cv"] <= 1.0, "Y", "Z")
    )
    materials["combined_class"] = materials["abc_class"] + materials["xyz_class"]

    # 置信度：约 8% 物料样本不足
    materials["confidence_level"] = np.where(rng.random(n) < 0.08, "low", "high")

    # 动态安全库存（默认 95% 服务水平）
    daily_demand = materials["monthly_demand"] / 30
    sigma_d = daily_demand * materials["cv"]
    sigma_lt = np.where(materials["supply_type"] == "purchase", scen["lt_sigma"], 0.0)
    materials["safety_stock"] = Z_BY_LEVEL[95] * np.sqrt(
        materials["lt_total"] * sigma_d**2 + daily_demand**2 * sigma_lt**2
    )
    materials["fixed_ss"] = daily_demand * materials["lt_total"] * 0.5 + 1

    # 库存快照
    materials["on_hand"] = daily_demand * materials["lt_total"] * rng.uniform(0.3, 1.3, n)
    materials["in_transit"] = daily_demand * rng.uniform(0, 1, n) * materials["lt_total"] * 0.5
    materials["allocated"] = daily_demand * rng.uniform(0, 1, n) * materials["lt_total"] * 0.3
    materials["quality_hold"] = daily_demand * rng.uniform(0, 1, n) * materials["lt_total"] * 0.1

    # 需求订单与齐套明细
    order_rows: list[dict] = []
    kit_rows: list[dict] = []
    base_date = pd.Timestamp("2026-11-16")
    for i in range(N_ORDERS):
        order_id = f"SO2026{i + 1:04d}"
        order_type = ORDER_TYPES[int(rng.integers(0, len(ORDER_TYPES)))]
        required_date = base_date + pd.Timedelta(days=int(rng.integers(3, 60)))
        order_rows.append(
            {
                "order_id": order_id,
                "order_type": order_type,
                "required_date": required_date,
                "status": "open",
            }
        )
        picked = rng.choice(n, size=int(rng.integers(2, 6)), replace=False)
        for idx in picked:
            m = materials.iloc[int(idx)]
            req_qty = max(1, int(round(m["monthly_demand"] * rng.uniform(0.1, 0.8))))
            kit_rows.append(
                {
                    "order_id": order_id,
                    "order_type": order_type,
                    "required_date": required_date,
                    "material_id": m["material_id"],
                    "material_name": m["material_name"],
                    "abc_class": m["abc_class"],
                    "xyz_class": m["xyz_class"],
                    "confidence_level": m["confidence_level"],
                    "supply_type": m["supply_type"],
                    "uom": m["uom"],
                    "unit_price": float(m["unit_price"]),
                    "req_qty": float(req_qty),
                    "safety_stock": float(m["safety_stock"]),
                    "fixed_ss": float(m["fixed_ss"]),
                    "on_hand": float(m["on_hand"]),
                    "in_transit": float(m["in_transit"]),
                    "allocated": float(m["allocated"]),
                    "quality_hold": float(m["quality_hold"]),
                    "noise": float(rng.random()),
                }
            )

    orders = pd.DataFrame(order_rows)
    kitting = pd.DataFrame(kit_rows)
    return {"materials": materials, "orders": orders, "kitting": kitting}


def compute_available(df: pd.DataFrame, options: dict[str, bool], ss_scale: float) -> pd.Series:
    """按勾选口径计算可用量，默认：当前库存 + 在途 - 已分配。"""
    available = df["on_hand"].astype(float).copy()
    if options["在途"]:
        available = available + df["in_transit"]
    if options["已分配"]:
        available = available - df["allocated"]
    if options["质检锁定"]:
        available = available - df["quality_hold"]
    if options["安全库存"]:
        available = available - df["safety_stock"] * ss_scale
    return available.clip(lower=0)


def apply_kitting_calc(
    kitting: pd.DataFrame, options: dict[str, bool], ss_scale: float
) -> pd.DataFrame:
    """计算缺口与单行齐套概率（骨架用近似公式，正式版由蒙特卡洛替换）。"""
    df = kitting.copy()
    df["available_qty"] = compute_available(df, options, ss_scale)
    df["gap_qty"] = (df["req_qty"] - df["available_qty"]).clip(lower=0)
    df["gap_amount"] = df["gap_qty"] * df["unit_price"]
    gap_ratio = (df["gap_qty"] / df["req_qty"]).clip(upper=1.0)
    df["row_probability"] = (0.97 - 0.55 * gap_ratio - 0.10 * df["noise"]).clip(0.02, 0.99)
    return df


def build_order_view(kitting: pd.DataFrame) -> pd.DataFrame:
    """订单级汇总：存在低置信度物料时概率为 null，风险等级标记数据不足。"""
    records: list[dict] = []
    for order_id, group in kitting.groupby("order_id", sort=False):
        row = {
            "order_id": order_id,
            "order_type": group["order_type"].iat[0],
            "required_date": group["required_date"].iat[0],
            "material_rows": len(group),
            "gap_rows": int((group["gap_qty"] > 0).sum()),
            "gap_amount": float(group["gap_amount"].sum()),
        }
        if (group["confidence_level"] == "low").any():
            row["on_time_probability"] = np.nan
            row["risk_level"] = "数据不足"
        else:
            probability = float(group["row_probability"].min())
            row["on_time_probability"] = probability
            row["risk_level"] = (
                "低风险" if probability >= 0.90 else "中风险" if probability >= 0.70 else "高风险"
            )
        records.append(row)
    return pd.DataFrame(records)


def build_cost_simulation(
    materials: pd.DataFrame, penalty: float, ss_scale: float
) -> pd.DataFrame:
    """假数据成本仿真：固定 vs 动态安全库存 × 三档服务水平。"""
    records: list[dict] = []
    for level in (90, 95, 99):
        factor = Z_BY_LEVEL[level] / Z_BY_LEVEL[95]
        for strategy in ("固定安全库存", "动态安全库存"):
            if strategy == "固定安全库存":
                ss = materials["fixed_ss"]
                shortage_rate = 0.12 - (level - 90) / 30 * 0.03
            else:
                ss = materials["safety_stock"] * factor * ss_scale
                shortage_rate = 0.08 - (level - 90) / 30 * 0.035
            avg_inventory_value = float((materials["unit_price"] * ss * 1.4).sum())
            holding_cost = avg_inventory_value * 0.2
            shortage_qty = float(materials["monthly_demand"].sum() * shortage_rate * 0.3)
            shortage_cost = shortage_qty * float(materials["unit_price"].mean()) * penalty
            records.append(
                {
                    "服务水平": level,
                    "策略": strategy,
                    "平均库存金额": avg_inventory_value,
                    "持有成本": holding_cost,
                    "缺料成本": shortage_cost,
                    "物料缺料率（按行数）": shortage_rate,
                }
            )
    return pd.DataFrame(records)


def main() -> None:
    data = build_demo_data("正常")
    materials_all = data["materials"]
    orders_all = data["orders"]
    kitting_all = data["kitting"]

    # ---------------- 侧边栏 ----------------
    with st.sidebar:
        st.header("参数与筛选")
        scenario = st.selectbox("演示场景", list(SCENARIOS.keys()), index=0)
        if scenario != "正常":
            data = build_demo_data(scenario)
            materials_all = data["materials"]
            orders_all = data["orders"]
            kitting_all = data["kitting"]

        st.subheader("可用量口径")
        st.checkbox("当前库存", value=True, disabled=True)
        inc_transit = st.checkbox("在途", value=True)
        inc_allocated = st.checkbox("已分配", value=True)
        inc_quality_hold = st.checkbox("质检锁定", value=False)
        inc_safety_stock = st.checkbox("安全库存（默认 95% 服务水平）", value=False)

        st.subheader("安全库存参数")
        service_level = st.slider("目标服务水平（%）", 90, 99, 95, 1)
        ss_scale = st.slider("安全库存调整系数", 0.5, 2.0, 1.0, 0.1)

        st.subheader("成本与模拟参数")
        penalty = st.slider("缺料惩罚系数（倍）", 2.0, 5.0, 3.0, 0.5)
        sim_count = st.slider("蒙特卡洛模拟次数", 100, 1000, 100, 100)

        st.subheader("筛选器")
        abc_selected = st.multiselect("ABC 分类", ABC_LEVELS, default=ABC_LEVELS)
        xyz_selected = st.multiselect("XYZ 分类", XYZ_LEVELS, default=XYZ_LEVELS)

        min_date = orders_all["required_date"].min().date()
        max_date = orders_all["required_date"].max().date()
        date_range = st.date_input(
            "需求日期窗口", value=(min_date, max_date), min_value=min_date, max_value=max_date
        )
        risk_selected = st.multiselect("订单风险等级", RISK_LEVELS, default=RISK_LEVELS)
        top_n = st.slider("预警表显示条数", 10, 100, 20, 10)

    options = {
        "在途": inc_transit,
        "已分配": inc_allocated,
        "质检锁定": inc_quality_hold,
        "安全库存": inc_safety_stock,
    }

    # ---------------- 数据计算 ----------------
    materials = materials_all[
        materials_all["abc_class"].isin(abc_selected)
        & materials_all["xyz_class"].isin(xyz_selected)
    ].copy()
    if materials.empty:
        st.warning("当前筛选条件下没有物料，请调整 ABC / XYZ 筛选。")
        st.stop()
    material_ids = set(materials["material_id"])

    kitting = apply_kitting_calc(kitting_all, options, ss_scale)
    kitting = kitting[kitting["material_id"].isin(material_ids)].copy()

    orders = build_order_view(kitting)
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = min_date, max_date
    order_mask = (
        (orders["required_date"].dt.date >= start_date)
        & (orders["required_date"].dt.date <= end_date)
        & (orders["risk_level"].isin(risk_selected))
    )
    orders = orders[order_mask].copy()
    kitting = kitting[kitting["order_id"].isin(set(orders["order_id"]))].copy()

    # 口径说明
    option_names = ["当前库存"] + [name for name, enabled in options.items() if enabled]
    scope_text = " + ".join(option_names[:1])
    for name in option_names[1:]:
        scope_text += f" {'-' if name in ('已分配', '质检锁定', '安全库存') else '+'} {name}"
    if inc_safety_stock:
        scope_text += f"（安全库存按 {service_level}% 服务水平 × {ss_scale:.1f}）"

    # ---------------- 头部 ----------------
    st.title("KADSS · 供应链齐套预警与动态安全库存优化")
    st.caption(
        f"演示场景：{scenario} ｜ seed={SEED} ｜ {N_MATERIALS} SKU ｜ {N_ORDERS} 个需求订单 ｜ "
        f"模拟次数配置 {sim_count}（骨架阶段未实际执行）"
    )
    st.caption(f"当前可用量口径：{scope_text}")

    # ---------------- KPI ----------------
    material_shortage_rate = (
        float((kitting["gap_qty"] > 0).mean()) if len(kitting) else 0.0
    )
    order_shortage_rate = (
        float((orders["gap_rows"] > 0).mean()) if len(orders) else 0.0
    )
    high_risk_orders = int(orders["risk_level"].isin(["高风险", "数据不足"]).sum())
    valid_prob = orders["on_time_probability"].dropna()
    avg_probability = float(valid_prob.mean()) if len(valid_prob) else float("nan")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("物料缺料率（按行数）", f"{material_shortage_rate:.1%}")
    c2.metric("订单缺料率（按订单数）", f"{order_shortage_rate:.1%}")
    c3.metric("高风险 / 数据不足订单", high_risk_orders)
    c4.metric(
        "平均齐套概率",
        "—" if np.isnan(avg_probability) else f"{avg_probability:.0%}",
    )

    tab_layers, tab_kitting, tab_stock, tab_cost = st.tabs(
        ["物料分层", "齐套预警", "库存策略", "成本对比"]
    )

    # ---------------- Tab 1 物料分层 ----------------
    with tab_layers:
        st.subheader("ABC-XYZ 矩阵（占位）")
        count_matrix = (
            materials.pivot_table(
                index="abc_class", columns="xyz_class", values="material_id", aggfunc="count"
            )
            .reindex(index=ABC_LEVELS, columns=XYZ_LEVELS, fill_value=0)
            .fillna(0)
        )
        amount_matrix = (
            materials.pivot_table(
                index="abc_class", columns="xyz_class", values="amount_share", aggfunc="sum"
            )
            .reindex(index=ABC_LEVELS, columns=XYZ_LEVELS, fill_value=0)
            .fillna(0)
        )
        texts = [
            [
                f"{int(count_matrix.loc[abc, xyz])} 个<br>{amount_matrix.loc[abc, xyz]:.0%} 金额"
                for xyz in XYZ_LEVELS
            ]
            for abc in ABC_LEVELS
        ]
        figure = go.Figure(
            go.Heatmap(
                z=count_matrix.values,
                x=XYZ_LEVELS,
                y=ABC_LEVELS,
                text=texts,
                texttemplate="%{text}",
                colorscale="Blues",
                colorbar={"title": "物料数"},
            )
        )
        figure.update_layout(
            height=360,
            xaxis_title="XYZ（需求波动）",
            yaxis_title="ABC（消耗金额）",
            margin={"l": 40, "r": 20, "t": 20, "b": 40},
        )
        st.plotly_chart(figure, width="stretch")

        st.subheader("分层明细（含数据置信度）")
        layer_table = materials[
            [
                "material_id",
                "material_name",
                "abc_class",
                "xyz_class",
                "combined_class",
                "cv",
                "amount_share",
                "confidence_level",
                "safety_stock",
            ]
        ].rename(
            columns={
                "material_id": "物料编码",
                "material_name": "物料名称",
                "abc_class": "ABC",
                "xyz_class": "XYZ",
                "combined_class": "组合",
                "cv": "变异系数 CV",
                "amount_share": "金额占比（%）",
                "confidence_level": "数据置信度",
                "safety_stock": "动态安全库存",
            }
        )
        layer_table["金额占比（%）"] = layer_table["金额占比（%）"] * 100
        st.dataframe(
            layer_table,
            width="stretch",
            hide_index=True,
            column_config={
                "金额占比（%）": st.column_config.NumberColumn(format="%.2f"),
                "变异系数 CV": st.column_config.NumberColumn(format="%.2f"),
                "动态安全库存": st.column_config.NumberColumn(format="%.1f"),
            },
        )

    # ---------------- Tab 2 齐套预警 ----------------
    with tab_kitting:
        st.subheader("订单齐套预警")
        order_table = orders.sort_values("gap_amount", ascending=False).head(top_n).copy()
        order_table = order_table.merge(
            kitting[["order_id", "material_id"]]
            .groupby("order_id")
            .agg(涉及物料数=("material_id", "nunique"))
            .reset_index(),
            on="order_id",
            how="left",
        )
        order_table["probability_pct"] = order_table["on_time_probability"] * 100
        display_orders = order_table[
            [
                "order_id",
                "order_type",
                "required_date",
                "涉及物料数",
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
                "gap_rows": "缺口物料数",
                "gap_amount": "缺口金额",
                "probability_pct": "齐套概率（%）",
                "risk_level": "风险等级",
            }
        )
        st.dataframe(
            display_orders,
            width="stretch",
            hide_index=True,
            column_config={
                "需求日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "缺口金额": st.column_config.NumberColumn(format="%.2f"),
                "齐套概率（%）": st.column_config.NumberColumn(format="%.0f"),
            },
        )

        st.caption("缺口明细（按缺口金额排序）")
        detail = kitting.sort_values("gap_amount", ascending=False).head(top_n)
        detail_table = detail[
            [
                "order_id",
                "material_id",
                "material_name",
                "abc_class",
                "xyz_class",
                "req_qty",
                "available_qty",
                "gap_qty",
                "gap_amount",
                "confidence_level",
            ]
        ].rename(
            columns={
                "order_id": "订单号",
                "material_id": "物料编码",
                "material_name": "物料名称",
                "abc_class": "ABC",
                "xyz_class": "XYZ",
                "req_qty": "毛需求",
                "available_qty": "可用量",
                "gap_qty": "缺口",
                "gap_amount": "缺口金额",
                "confidence_level": "数据置信度",
            }
        )
        st.dataframe(
            detail_table,
            width="stretch",
            hide_index=True,
            column_config={
                "毛需求": st.column_config.NumberColumn(format="%.0f"),
                "可用量": st.column_config.NumberColumn(format="%.0f"),
                "缺口": st.column_config.NumberColumn(format="%.0f"),
                "缺口金额": st.column_config.NumberColumn(format="%.2f"),
            },
        )

    # ---------------- Tab 3 库存策略 ----------------
    with tab_stock:
        st.subheader("安全库存参数与建议补货量")
        p1, p2, p3 = st.columns(3)
        p1.metric("目标服务水平", f"{service_level}%")
        p2.metric("安全库存调整系数", f"{ss_scale:.1f}")
        p3.metric("缺料惩罚系数", f"{penalty:.1f} 倍")

        stock_view = materials.copy()
        stock_view["available_qty"] = compute_available(stock_view, options, ss_scale)
        stock_view["daily_demand"] = stock_view["monthly_demand"] / 30
        stock_view["dynamic_ss"] = stock_view["safety_stock"] * ss_scale
        stock_view["suggested_qty"] = (
            stock_view["dynamic_ss"]
            + stock_view["daily_demand"] * stock_view["lt_total"]
            - stock_view["available_qty"]
        ).clip(lower=0)
        stock_view["suggested_amount"] = stock_view["suggested_qty"] * stock_view["unit_price"]
        top_stock = stock_view.sort_values("suggested_amount", ascending=False).head(10)
        stock_figure = px.bar(
            top_stock.sort_values("suggested_amount"),
            x="suggested_amount",
            y="material_id",
            orientation="h",
            labels={"suggested_amount": "建议补货金额", "material_id": "物料编码"},
            title="建议补货金额 Top 10（假数据）",
        )
        stock_figure.update_layout(height=380, margin={"l": 10, "r": 20, "t": 50, "b": 40})
        st.plotly_chart(stock_figure, width="stretch")
        st.caption("建议补货量 = MAX(0, 安全库存 + 日均需求 × 总提前期 − 当前可用量)")

    # ---------------- Tab 4 成本对比 ----------------
    with tab_cost:
        st.subheader("固定 vs 动态安全库存成本对比")
        cost_df = build_cost_simulation(materials, penalty, ss_scale)
        metric = st.selectbox(
            "对比指标",
            ["平均库存金额", "持有成本", "缺料成本", "物料缺料率（按行数）"],
            index=0,
        )
        cost_figure = px.bar(
            cost_df,
            x="服务水平",
            y=metric,
            color="策略",
            barmode="group",
            text_auto=".2s",
            title=f"{metric} · 固定 vs 动态（惩罚系数 {penalty:.1f} 倍）",
        )
        cost_figure.update_layout(height=420, xaxis_title="服务水平（%）", margin={"t": 60})
        st.plotly_chart(cost_figure, width="stretch")

        cost_table = cost_df.copy()
        cost_table["物料缺料率（按行数）"] = cost_table["物料缺料率（按行数）"].map(
            lambda value: f"{value:.1%}"
        )
        st.dataframe(
            cost_table.rename(columns={"服务水平": "服务水平（%）"}),
            width="stretch",
            hide_index=True,
            column_config={
                "平均库存金额": st.column_config.NumberColumn(format="%.0f"),
                "持有成本": st.column_config.NumberColumn(format="%.0f"),
                "缺料成本": st.column_config.NumberColumn(format="%.0f"),
            },
        )
        st.caption("成本参数为假设值，可在侧边栏调整惩罚系数；结果随参数变化可复算。")

    st.divider()
    st.caption(
        "适用边界：本系统面向离散制造与装配型场景，流程制造（如化工、材料、晶圆前道）需结合批次、配方与工艺参数重新建模。"
        "MVP 仅计算理论可用量，不区分物理批次、库位与保质期。"
    )
    st.caption(
        "骨架说明：数据由 seed=42 生成，齐套概率为近似公式占位，跨订单共享料分配、离线预计算与三场景结果落盘尚未实现。"
    )


if __name__ == "__main__":
    main()
