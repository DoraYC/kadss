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

from kadss.config import load_config
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

STYLE_RED = "background-color: #fde2e1; color: #8a1c1c; font-weight: 600"
STYLE_YELLOW = "background-color: #fff3cd; color: #7a5b00; font-weight: 600"
STYLE_GREEN = "background-color: #d9f2e3; color: #145c33; font-weight: 600"
LAYER_COLUMN_STYLES = {
    "ABC": {"A": STYLE_RED, "B": STYLE_YELLOW, "C": STYLE_GREEN},
    "XYZ": {"X": STYLE_GREEN, "Y": STYLE_YELLOW, "Z": STYLE_RED},
    "组合": {
        "AY": STYLE_RED,
        "AZ": STYLE_RED,
        "BZ": STYLE_RED,
        "AX": STYLE_YELLOW,
        "BY": STYLE_YELLOW,
        "CZ": STYLE_YELLOW,
        "BX": STYLE_GREEN,
        "CX": STYLE_GREEN,
        "CY": STYLE_GREEN,
    },
    "数据置信度": {"高": STYLE_GREEN, "低": STYLE_RED},
    "数据状态": {"正常": STYLE_GREEN, "样本不足": STYLE_YELLOW},
}


def scene_stamp(profile: str, scenario: str) -> str:
    """场景库文件指纹：重算并覆写库后变化，用于让看板缓存自动失效。"""
    cfg = load_config(profile=profile)
    db = output_paths(scenario, cfg["output_root"])["db"]
    if not db.exists():
        return "missing"
    stat = db.stat()
    return f"{stat.st_mtime_ns}-{stat.st_size}"


@st.cache_data(show_spinner="加载场景结果…")
def load_scene(profile: str, scenario: str, stamp: str) -> dict[str, Any]:
    """读取基础表与结果表，并在读库前完成 run_id 自检。stamp 仅参与缓存键。"""
    cfg = load_config(profile=profile)
    paths = output_paths(scenario, cfg["output_root"])
    if not paths["db"].exists():
        raise FileNotFoundError(
            f"场景库不存在：{paths['db']}。请先运行 python -m kadss.build_dataset "
            f"--profile {profile} --scenario {scenario} 与 python -m kadss.build_results "
            f"--profile {profile} --scenario {scenario}"
        )
    base = load_base_tables(paths["db"])
    run_id = verify_results(paths["db"], scenario, cfg, data=base)
    results = load_results(paths["db"], run_id)
    return {"run_id": run_id, "cfg": cfg, "base": base, "results": results}


@st.cache_data(show_spinner="按当前口径重算齐套结果…")
def kitting_for_scope(
    profile: str, scenario: str, scope_key: tuple[bool, ...], stamp: str
) -> pd.DataFrame:
    """默认口径直接读落盘 baseline；其他口径按同一计算函数在内存重算。"""
    scene = load_scene(profile, scenario, stamp)
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
def cost_for_penalty(
    profile: str, scenario: str, penalty: float, stamp: str
) -> pd.DataFrame:
    scene = load_scene(profile, scenario, stamp)
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


@st.cache_data(show_spinner="按当前口径重算齐套概率…")
def probability_for_scope(
    profile: str, scenario: str, scope_key: tuple[bool, ...], sim_count: int, stamp: str
) -> pd.DataFrame:
    """按当前可用量口径的齐套明细重算按时齐套概率，避免结论与概率口径不一致。"""
    scene = load_scene(profile, scenario, stamp)
    cfg = scene["cfg"]
    overridden = copy.deepcopy(cfg)
    overridden["probability"]["simulation_count"] = sim_count
    return compute_probability(
        kitting=kitting_for_scope(profile, scenario, scope_key, stamp),
        orders=scene["base"]["t_kadss_demand_order"],
        safety_stock=scene["results"]["t_kadss_safety_stock_result"],
        cfg=overridden,
        base_date=pd.Timestamp(cfg["base_date"]).normalize(),
        run_id=scene["run_id"],
        created_at=f"{cfg['base_date']} 00:00:00",
    )


@st.cache_data(show_spinner=False)
def strategy_gap_rates(profile: str, scenario: str, stamp: str) -> pd.DataFrame:
    """策略口径的缺料率：可用量已扣除对应档位安全库存（落盘结果）。"""
    scene = load_scene(profile, scenario, stamp)
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


PAGE_SIZES = (10, 20, 30, 50)


def _set_page(page_key: str, value: int) -> None:
    st.session_state[page_key] = value


def page_slice(
    frame: pd.DataFrame, page_key: str, size_key: str, default_size: int = 20
) -> tuple[pd.DataFrame, int, int, int, int]:
    """按会话状态切分当前页，返回（本页数据, 起始序号, 结束序号, 页码, 总页数）。"""
    size = int(st.session_state.get(size_key, default_size))
    pages = max(1, (len(frame) + size - 1) // size)
    page = min(max(int(st.session_state.get(page_key, 1)), 1), pages)
    st.session_state[page_key] = page
    start = (page - 1) * size
    end = min(start + size, len(frame))
    return frame.iloc[start:end], start, end, page, pages


def pagination_bar(
    page_key: str, size_key: str, size: int, page: int, pages: int, total: int
) -> None:
    """列表底部分页：左侧留白，右侧显示每页条数与翻页控件。"""
    _, right = st.columns([3, 2], vertical_alignment="center")
    with right:
        size_col, prev_col, next_col, info_col = st.columns(
            [1.4, 0.8, 0.8, 1.4], vertical_alignment="center"
        )
        size_col.selectbox(
            "每页条数",
            PAGE_SIZES,
            index=PAGE_SIZES.index(size),
            key=size_key,
            format_func=lambda value: f"每页 {value} 条",
            label_visibility="collapsed",
        )
        prev_col.button(
            "<",
            key=f"{page_key}_prev",
            disabled=page <= 1,
            on_click=_set_page,
            args=(page_key, page - 1),
            width="stretch",
        )
        next_col.button(
            ">",
            key=f"{page_key}_next",
            disabled=page >= pages,
            on_click=_set_page,
            args=(page_key, page + 1),
            width="stretch",
        )
        info_col.caption(f"共 {total} 条")


def layer_table_styler(frame: pd.DataFrame) -> Any:
    """按枚举给分层明细着色：红=重点关注、黄=观察、绿=稳定/低关注。"""
    styled = frame.style
    for column, mapping in LAYER_COLUMN_STYLES.items():
        if column in frame.columns:
            styled = styled.map(
                lambda value, colors=mapping: colors.get(value, ""), subset=[column]
            )
    return styled.format({"金额占比（%）": "{:.2f}", "变异系数 CV": "{:.2f}"})


ORDER_COLUMN_STYLES = {
    "现货齐套": {"不齐套": STYLE_RED, "数据不足": STYLE_YELLOW, "齐套": STYLE_GREEN},
    "风险等级": {
        "高风险": STYLE_RED,
        "中风险": STYLE_YELLOW,
        "低风险": STYLE_GREEN,
        "数据不足": STYLE_YELLOW,
    },
}


def _probability_color(text: Any, risk_low: float, risk_medium: float) -> str:
    """按时齐套概率按风险阈值着色，空值标黄提示数据不足。"""
    if not isinstance(text, str) or not text.endswith("%"):
        return STYLE_YELLOW
    probability = float(text.rstrip("%")) / 100
    if probability >= risk_low:
        return STYLE_GREEN
    if probability >= risk_medium:
        return STYLE_YELLOW
    return STYLE_RED


def order_table_styler(
    frame: pd.DataFrame, risk_low: float, risk_medium: float
) -> Any:
    """订单齐套表：红=高风险/不齐套、黄=中风险/数据不足、绿=低风险/齐套。"""
    styled = frame.style
    for column, mapping in ORDER_COLUMN_STYLES.items():
        if column in frame.columns:
            styled = styled.map(
                lambda value, colors=mapping: colors.get(value, ""), subset=[column]
            )
    if "按时齐套概率" in frame.columns:
        styled = styled.map(
            lambda value: _probability_color(value, risk_low, risk_medium),
            subset=["按时齐套概率"],
        )
    return styled


def layer_column_help(cfg: dict[str, Any]) -> dict[str, str]:
    """分层明细列头说明：列名 → 含义，悬停列头 ⓘ 可查看。"""
    params = cfg["abc_xyz"]
    return {
        "ABC": (
            "按统计窗口内消耗金额（需求数量 × 单价）降序累计占比分档："
            f"累计占比 ≤ {float(params['abc_a_share']):.0%} 为 A，"
            f"≤ {float(params['abc_b_share']):.0%} 为 B，其余为 C；金额关注度 A > B > C。"
        ),
        "XYZ": (
            "按周度需求变异系数 CV 分档："
            f"CV < {float(params['xyz_x_cv']):g} 为 X（稳定），"
            f"< {float(params['xyz_y_cv']):g} 为 Y（波动），"
            "其余为 Z（高波动）。"
        ),
        "组合": "ABC 与 XYZ 的组合（如 AY），用于制定差异化库存策略。",
        "变异系数 CV": "周度需求标准差 ÷ 周度需求均值，衡量需求波动程度，数值越大越不稳定。",
        "金额占比（%）": "该物料统计窗口内消耗金额 ÷ 全部可分层物料的消耗金额合计。",
        "数据置信度": (
            f"有效需求周数是否达到最小样本要求（≥ {int(params['min_periods'])} 周）："
            "高 = 样本充分；低 = 样本不足，分层结论仅供参考。"
        ),
        "数据状态": "正常 = 已完成 ABC/XYZ 分层；样本不足 = 有效需求周数不足，不强行分层。",
    }


def main() -> None:
    base_cfg = load_config()
    scenario_keys = list(base_cfg["scenarios"])
    profile_keys = list(base_cfg["profiles"])

    with st.sidebar:
        st.header("参数与筛选")
        profile = st.selectbox(
            "数据规模",
            profile_keys,
            index=profile_keys.index(base_cfg["profile"]),
            format_func=lambda key: base_cfg["profiles"][key]["label"],
        )
        scenario = st.selectbox(
            "场景",
            scenario_keys,
            format_func=lambda key: base_cfg["scenarios"][key]["label"],
        )

    cfg = load_config(profile=profile)
    stamp = scene_stamp(profile, scenario)
    try:
        scene = load_scene(profile, scenario, stamp)
    except FileNotFoundError as error:
        st.warning(str(error))
        st.stop()
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
            1.5,
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

    kitting = kitting_for_scope(profile, scenario, scope_key, stamp)
    probability = probability_for_scope(
        profile, scenario, scope_key, sim_count, stamp
    )

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
        f"数据规模：{cfg['profiles'][profile]['label']} ｜ "
        f"seed={cfg['seed']} ｜ base_date={cfg['base_date']} ｜ "
        f"{cfg['scale']['materials']} SKU / {cfg['scale']['demand_orders']} 个需求订单 / "
        f"{cfg['scale']['history_days']} 天历史需求"
    )
    st.caption(
        DEFAULT_SCOPE_NOTE
        if scope == default_scope
        else f"当前可用量口径：{scope_text(scope, default_level)}"
    )
    if inc_safety_stock:
        st.info(
            f"已勾选安全库存：齐套明细按 {default_level}% 服务水平档位的动态安全库存扣减可用量，"
            "仅在内存重算。"
        )

    st.markdown(
        """
        <style>
        .st-key-metric-high-risk-orders [data-testid="stMetricValue"] {
            color: #b91c1c;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(MATERIAL_SHORTAGE_RATE, f"{material_rate:.1%}")
    c2.metric(ORDER_SHORTAGE_RATE, f"{order_rate:.1%}")
    with c3.container(key="metric-high-risk-orders"):
        st.metric("高风险 / 数据不足订单", high_risk_orders)
    c4.metric(
        "平均齐套概率",
        "—" if np.isnan(average_probability) else f"{average_probability:.0%}",
    )
    st.caption("以上指标按当前筛选范围统计；齐套概率与风险等级按当前可用量口径重算。")

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
                    f"<br>金额占比 {amount_matrix.loc[abc_level, xyz_level]:.1%}"
                    for xyz_level in XYZ_LEVELS
                ]
                for abc_level in ABC_LEVELS
            ]
            figure = go.Figure(
                go.Heatmap(
                    z=count_matrix.values,
                    x=list(XYZ_LEVELS),
                    y=list(ABC_LEVELS),
                    customdata=amount_matrix.values,
                    text=texts,
                    texttemplate="%{text}",
                    colorscale="Blues",
                    colorbar={"title": "物料数"},
                    hovertemplate=(
                        "ABC %{y} · XYZ %{x}<br>"
                        "物料数 %{z}<br>"
                        "金额占比 %{customdata:.1%}<extra></extra>"
                    ),
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
                "单元格显示物料数与金额占比。金额占比 = 该格内物料的统计窗口消耗金额"
                "（需求数量 × 单价）合计 ÷ 全部物料消耗金额，例如 B-Y 的 8 个物料合计占 3.0%。"
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
        layer_display = layer_table.rename(
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
        )
        st.dataframe(
            layer_table_styler(layer_display),
            width="stretch",
            hide_index=True,
            column_config={
                name: st.column_config.Column(name, help=help_text)
                for name, help_text in layer_column_help(cfg).items()
            },
        )
        st.caption(
            "颜色说明：ABC 中 A 红 / B 黄 / C 绿（金额关注度递减，越红越需重点关注）；"
            "XYZ 中 Z 红 / Y 黄 / X 绿（需求波动递增）；"
            "组合按金额关注度与波动综合着色；数据置信度低标红，样本不足标黄。"
        )

    # ---------------- Tab 2 齐套预警 ----------------
    with tab_kitting:
        st.subheader(
            "订单齐套预警",
            help=(
                "「现货齐套」按当前可用量口径判断此刻是否齐套；「按时齐套概率」是缺口物料在"
                "需求日期前全部到货的概率，风险等级按其分档。因此现货缺口存在、但交期充裕时，"
                f"概率可达 100%、风险为低；概率按当前口径重算，模拟次数 {sim_count}。"
                "颜色：红 = 高风险 / 不齐套，黄 = 中风险 / 数据不足，绿 = 低风险 / 齐套。"
            ),
        )
        if order_view.empty:
            st.info("当前筛选条件下没有需求订单。")
        else:
            sorted_orders = order_view.sort_values(
                ["gap_amount", "order_id"], ascending=[False, True]
            )
            order_table, _, _, page, pages = page_slice(
                sorted_orders, "order_page", "order_page_size"
            )
            order_table = order_table.copy()
            order_table["probability_text"] = order_table["on_time_probability"].map(
                lambda value: "—" if pd.isna(value) else f"{value:.0%}"
            )
            risk_low = float(cfg["probability"]["risk_low"])
            risk_medium = float(cfg["probability"]["risk_medium"])
            order_help = {
                "现货齐套": (
                    "按当前可用量口径（当前库存 + 在途 − 已分配等）判断此刻是否齐套："
                    "订单任一物料可用量不足即为「不齐套」；含样本不足物料时标注「数据不足」。"
                ),
                "缺口物料数": "该订单存在缺口的物料行数（毛需求 > 可用量），一行对应一个物料。",
                "缺口金额（元）": "该订单全部缺口金额合计（缺口数量 × 单价）。",
                "按时齐套概率": (
                    "缺口物料在需求日期前全部到货的概率（按当前口径与模拟次数做蒙特卡洛重算）。"
                    "现货缺口存在但交期充裕时，该概率可接近或达到 100%。"
                ),
                "风险等级": (
                    f"按按时齐套概率分档：≥ {risk_low:.0%} 低风险、"
                    f"{risk_medium:.0%}–{risk_low:.0%} 中风险、< {risk_medium:.0%} 高风险；"
                    "概率为空时标注「数据不足」。"
                ),
            }
            order_display = order_table[
                [
                    "order_id",
                    "order_type",
                    "required_date",
                    "kitting_status",
                    "gap_rows",
                    "gap_amount",
                    "probability_text",
                    "risk_level",
                ]
            ].rename(
                columns={
                    "order_id": "订单号",
                    "order_type": "需求类型",
                    "required_date": "需求日期",
                    "kitting_status": "现货齐套",
                    "gap_rows": "缺口物料数",
                    "gap_amount": "缺口金额（元）",
                    "probability_text": "按时齐套概率",
                    "risk_level": "风险等级",
                }
            )
            st.dataframe(
                order_table_styler(order_display, risk_low, risk_medium),
                width="stretch",
                hide_index=True,
                column_config={
                    "需求日期": st.column_config.DateColumn(format="YYYY-MM-DD"),
                    "缺口金额（元）": st.column_config.NumberColumn(
                        format="%.2f", help=order_help["缺口金额（元）"]
                    ),
                    **{
                        name: st.column_config.Column(name, help=text)
                        for name, text in order_help.items()
                        if name != "缺口金额（元）"
                    },
                },
            )
            pagination_bar(
                "order_page",
                "order_page_size",
                int(st.session_state.get("order_page_size", 20)),
                page,
                pages,
                len(order_view),
            )

            head_col, select_col = st.columns(
                [1.6, 2], vertical_alignment="center"
            )
            head_col.markdown(
                "<div style='height:40px;display:flex;align-items:center;font-size:1.25rem;"
                "font-weight:600;white-space:nowrap;'>订单缺口明细与交期分布假设</div>",
                unsafe_allow_html=True,
            )
            detail_order = select_col.selectbox(
                "选择订单",
                ["全部订单", *sorted(order_ids & set(order_view["order_id"]))],
                label_visibility="collapsed",
            )
            default_lt = safety_stock[
                safety_stock["service_level"] == default_level
            ][["material_id", "lt_avg", "sigma_lt", "lt_source"]]
            detail_source = (
                kitting_view
                if detail_order == "全部订单"
                else kitting_view[kitting_view["order_id"] == detail_order]
            )
            detail = detail_source[detail_source["gap_qty"] > 0].merge(
                default_lt, on="material_id", how="left"
            )
            detail = detail.sort_values(
                ["order_id", "gap_amount"], ascending=[True, False]
            )
            if detail.empty:
                st.info("所选范围内没有缺口物料。")
            else:
                detail["lt_source"] = detail["lt_source"].map(LT_SOURCE_LABELS)
                detail_page, _, _, page_no, page_total = page_slice(
                    detail,
                    f"detail_page_{detail_order}",
                    "detail_page_size",
                    default_size=10,
                )
                detail_display = detail_page[
                    [
                        "order_id",
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
                    ]
                ].rename(
                    columns={
                        "order_id": "订单号",
                        "material_id": "物料编码",
                        "material_name": "物料名称",
                        "abc_label": "ABC",
                        "xyz_label": "XYZ",
                        "req_qty": "毛需求",
                        "available_qty": "可用量",
                        "gap_qty": "缺口",
                        "gap_amount": "缺口金额（元）",
                        "lt_avg": "总提前期均值（天）",
                        "sigma_lt": "总提前期标准差（天）",
                        "lt_source": "交期来源",
                    }
                )
                st.dataframe(
                    layer_table_styler(detail_display),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "毛需求": st.column_config.NumberColumn(format="%.1f"),
                        "可用量": st.column_config.NumberColumn(format="%.1f"),
                        "缺口": st.column_config.NumberColumn(format="%.1f"),
                        "缺口金额（元）": st.column_config.NumberColumn(format="%.2f"),
                        "总提前期均值（天）": st.column_config.NumberColumn(format="%.1f"),
                        "总提前期标准差（天）": st.column_config.NumberColumn(format="%.1f"),
                    },
                )
                pagination_bar(
                    f"detail_page_{detail_order}",
                    "detail_page_size",
                    int(st.session_state.get("detail_page_size", 10)),
                    page_no,
                    page_total,
                    len(detail),
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
        p3.metric("建议补货金额合计（元）", f"{total_suggested:,.2f}")

        if level_rows.empty:
            st.info("当前筛选下没有安全库存结果。")
        else:
            st.subheader(f"建议补货金额 Top 10（元） · {service_level}% 服务水平")
            top_stock = level_rows.sort_values("suggested_amount", ascending=False).head(10)
            stock_figure = px.bar(
                top_stock.sort_values("suggested_amount"),
                x="suggested_amount",
                y="material_id",
                orientation="h",
                labels={
                    "suggested_amount": "建议补货金额（元）",
                    "material_id": "物料编码",
                },
                title=f"建议补货金额 Top 10（元） · {service_level}% 服务水平",
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
            stock_display = stock_table.rename(
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
                    "suggested_amount": "建议补货金额（元）",
                    "lt_source": "交期来源",
                    "confidence_level": "数据置信度",
                }
            )
            st.dataframe(
                layer_table_styler(stock_display),
                width="stretch",
                hide_index=True,
                column_config={
                    "LT_total（天）": st.column_config.NumberColumn(
                        format="%.0f",
                        help=(
                            "总提前期 = 采购 + 生产 + 检验 + 物流提前期（天）；"
                            "历史收货样本足够时，安全库存计算改用历史提前期均值。"
                        ),
                    ),
                    "动态安全库存": st.column_config.NumberColumn(
                        format="%.1f",
                        help=(
                            "Z × √(LT_avg × 需求标准差² + 日均需求² × 交期标准差²)，"
                            "同时考虑需求波动与交期波动；Z 由目标服务水平决定。"
                        ),
                    ),
                    "固定安全库存": st.column_config.NumberColumn(
                        format="%.1f",
                        help="日均需求 × 固定覆盖天数（配置项），不考虑需求与交期波动。",
                    ),
                    "动态 − 固定": st.column_config.NumberColumn(format="%.1f"),
                    "建议补货量": st.column_config.NumberColumn(format="%.1f"),
                    "建议补货金额（元）": st.column_config.NumberColumn(format="%.2f"),
                },
            )

    # ---------------- Tab 4 成本对比 ----------------
    with tab_cost:
        st.info(STRATEGY_SCOPE_NOTE_SHORT)
        st.caption(TRADEOFF_NOTE)

        cost = cost_for_penalty(profile, scenario, penalty, stamp)
        aggregate = cost.groupby(["strategy", "service_level"], as_index=False).agg(
            avg_inventory_value=("avg_inventory_value", "sum"),
            holding_cost=("holding_cost", "sum"),
            shortage_qty=("shortage_qty", "sum"),
            shortage_days=("shortage_days", "sum"),
            shortage_cost=("shortage_cost", "sum"),
        )
        aggregate = aggregate.merge(
            strategy_gap_rates(profile, scenario, stamp), on=["strategy", "service_level"], how="left"
        )
        aggregate["策略"] = aggregate["strategy"].map(STRATEGY_LABELS)
        aggregate["strategy_order"] = aggregate["strategy"].map(STRATEGY_ORDER)
        aggregate = (
            aggregate.sort_values(["service_level", "strategy_order"])
            .drop(columns="strategy_order")
            .reset_index(drop=True)
        )

        metric_options = {
            "平均库存金额（元）": "avg_inventory_value",
            "持有成本（元）": "holding_cost",
            "缺料成本（元）": "shortage_cost",
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
                "avg_inventory_value": "平均库存金额（元）",
                "holding_cost": "持有成本（元）",
                "shortage_qty": "缺料数量",
                "shortage_days": "缺料天数",
                "shortage_cost": "缺料成本（元）",
                "material_rate": MATERIAL_SHORTAGE_RATE,
                "order_rate": ORDER_SHORTAGE_RATE,
            }
        )
        st.dataframe(
            cost_table,
            width="stretch",
            hide_index=True,
            column_config={
                "平均库存金额（元）": st.column_config.NumberColumn(format="%.2f"),
                "持有成本（元）": st.column_config.NumberColumn(format="%.2f"),
                "缺料数量": st.column_config.NumberColumn(format="%.1f"),
                "缺料成本（元）": st.column_config.NumberColumn(format="%.2f"),
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
                    "平均库存金额变化（元）": level_cost.loc["dynamic", "avg_inventory_value"]
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
                    "平均库存金额变化（元）": st.column_config.NumberColumn(format="%.2f"),
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
