"""模拟数据生成（PRD v1.1 / US-1）。

约束：
- 只产出 db/schema.sql 中 6 张基础表的字段，派生结果表由计算层写入
- 所有随机性来自「主种子 + 场景偏移」，同参数重复运行输出逐行一致
- 每张表使用独立子随机数，新增表不会打乱既有表的数据
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

SUPPLY_TYPES = ("purchase", "make", "outsource")
SUPPLY_WEIGHTS = (0.70, 0.20, 0.10)
ORDER_TYPES = ("生产工单", "客户订单", "补货计划", "维修备件需求", "项目物料需求")
CATEGORIES = ("电子料", "结构件", "标准件", "包材", "备件")
CATEGORY_WEIGHTS = (0.30, 0.25, 0.25, 0.10, 0.10)
WEEKDAY_FACTOR = np.array([1.15, 1.10, 1.05, 1.00, 0.95, 0.60, 0.50])  # 周一至周日
# 按「周聚合 CV」目标反推日粒度噪声：X 稳定、Y 中等、Z 高波动
VOLATILITY_CLASSES = ("X", "Y", "Z")
CLASS_CV_RANGES = {"X": (0.02, 0.05), "Y": (0.25, 0.35), "Z": (0.60, 1.00)}


def _child_rngs(seed: int, count: int) -> list[np.random.Generator]:
    """派生固定数量的子随机数发生器。"""
    seeds = np.random.default_rng(seed).integers(0, 2**32 - 1, count)
    return [np.random.default_rng(int(value)) for value in seeds]


def _date(value: pd.Timestamp) -> str:
    return value.strftime("%Y-%m-%d")


def _timestamp(value: pd.Timestamp) -> str:
    return value.strftime("%Y-%m-%d 00:00:00")


def _assign_volatility_classes(
    levels: np.ndarray, mix: dict[str, float]
) -> np.ndarray:
    """按需求规模从高到低分配 X/Y/Z：高需求物料更可能稳定，避免取整噪声把低量物料推成 Z。"""
    order = np.argsort(-levels, kind="stable")
    count = levels.size
    n_x = int(round(count * float(mix["x"])))
    n_y = int(round(count * float(mix["y"])))
    classes = np.full(count, "Z", dtype=object)
    classes[order[:n_x]] = "X"
    classes[order[n_x : n_x + n_y]] = "Y"
    return classes


def _class_cv(classes: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """按类别抽样日需求噪声强度，使周聚合后的 CV 落入目标分档。"""
    cvs = np.empty(classes.size)
    for label, (low, high) in CLASS_CV_RANGES.items():
        mask = classes == label
        cvs[mask] = rng.uniform(low, high, int(mask.sum()))
    return cvs


def _build_materials(
    rng: np.random.Generator, cfg: dict[str, Any], base_date: pd.Timestamp
) -> dict[str, Any]:
    """物料主数据 + 只用于生成器的内部字段（需求基数、波动类别、噪声强度）。"""
    count = int(cfg["scale"]["materials"])
    supplier_count = int(cfg["supply"]["supplier_pool_size"])

    supply_type = rng.choice(SUPPLY_TYPES, count, p=SUPPLY_WEIGHTS)
    unit_price = np.round(rng.lognormal(3.2, 1.0, count), 2)
    purchase_lt = rng.integers(5, 46, count).astype(float)
    production_lt = rng.integers(3, 21, count).astype(float)
    inspection_lt = rng.integers(0, 4, count).astype(float)
    logistics_lt = rng.integers(1, 8, count).astype(float)
    category = rng.choice(CATEGORIES, count, p=CATEGORY_WEIGHTS)

    suppliers = [f"SUP{i:04d}" for i in range(1, supplier_count + 1)]
    supplier_id = [
        None if kind == "make" else str(rng.choice(suppliers)) for kind in supply_type
    ]

    material_ids = [f"M{i:04d}" for i in range(1, count + 1)]
    frame = pd.DataFrame(
        {
            "material_id": material_ids,
            "material_name": [f"物料-{i:04d}" for i in range(1, count + 1)],
            "category": category,
            "uom": "PCS",
            "supply_type": supply_type,
            "purchase_lt": purchase_lt,
            "production_lt": production_lt,
            "inspection_lt": inspection_lt,
            "logistics_lt": logistics_lt,
            "supplier_id": supplier_id,
            "unit_price": unit_price,
            "status": "active",
            "created_at": _timestamp(base_date - pd.Timedelta(days=180)),
        }
    )
    levels = rng.integers(20, 801, count).astype(float)
    mix = cfg["noise"]["volatility_mix"]
    classes = _assign_volatility_classes(levels, mix)
    return {
        "frame": frame,
        "material_ids": material_ids,
        "supply_type": supply_type,
        "category": category,
        "monthly_level": levels,
        "volatility_class": classes,
        "cv": _class_cv(classes, rng),
    }


def _build_demand_history(
    rng: np.random.Generator,
    cfg: dict[str, Any],
    materials: dict[str, Any],
    scen: dict[str, Any],
    base_date: pd.Timestamp,
) -> pd.DataFrame:
    """日粒度历史需求：星期效应 + 分类噪声 + 按波动类别控制偶发零需求与尖峰。"""
    days = int(cfg["scale"]["history_days"])
    dates = pd.date_range(end=base_date - pd.Timedelta(days=1), periods=days, freq="D")
    weekday = WEEKDAY_FACTOR[dates.dayofweek.to_numpy()]
    noise_cfg = cfg["noise"]
    zero_rate = float(noise_cfg["demand_zero_rate"])
    spike_rate = float(noise_cfg["demand_spike_rate"])
    spike_factor = float(noise_cfg["demand_spike_factor"])
    volatility = float(scen["demand_volatility"])
    zero_by_class = {"X": 0.0, "Y": zero_rate * 0.5, "Z": zero_rate}
    spike_by_class = {"X": 0.0, "Y": spike_rate * 0.5, "Z": spike_rate}
    limited_days = int(cfg["scale"]["limited_history_days"])
    limited_share = float(cfg["scale"]["limited_history_share"])
    material_count = len(materials["material_ids"])
    limited_count = int(round(material_count * limited_share))
    limited = (
        set(rng.choice(material_count, size=limited_count, replace=False).tolist())
        if limited_count
        else set()
    )

    rows: list[tuple[int, str, str, float]] = []
    history_id = 1
    for index, material_id in enumerate(materials["material_ids"]):
        label = materials["volatility_class"][index]
        cv = float(np.clip(materials["cv"][index] * volatility, 0.01, 2.00))
        base_daily = float(materials["monthly_level"][index]) / 30.0
        noise = rng.gamma(1.0 / cv**2, cv**2, days)
        quantity = base_daily * weekday * noise
        quantity[rng.random(days) < zero_by_class[label]] = 0.0
        quantity[rng.random(days) < spike_by_class[label]] *= spike_factor
        quantity = np.round(quantity, 0)
        # 少量物料只有短期历史，用于验证样本不足时的降级与「数据不足」标记
        keep = limited_days if index in limited else days
        for offset, value in enumerate(quantity[-keep:]):
            rows.append((history_id, material_id, _date(dates[-keep:][offset]), float(value)))
            history_id += 1

    return pd.DataFrame(
        rows, columns=["history_id", "material_id", "demand_date", "demand_qty"]
    )


def _build_inventory(
    rng: np.random.Generator,
    materials: dict[str, Any],
    history: pd.DataFrame,
    base_date: pd.Timestamp,
) -> pd.DataFrame:
    """库存快照：覆盖天数按四类提前期之和计算，已分配不超过可用在库+在途。"""
    frame = materials["frame"]
    count = len(frame)
    avg_daily = (
        history.groupby("material_id")["demand_qty"]
        .mean()
        .reindex(frame["material_id"])
        .fillna(0.0)
        .to_numpy()
    )
    lt_total = (
        frame["purchase_lt"]
        + frame["production_lt"]
        + frame["inspection_lt"]
        + frame["logistics_lt"]
    ).to_numpy()
    coverage = avg_daily * lt_total

    on_hand = np.round(coverage * rng.uniform(0.3, 1.3, count), 0)
    in_transit = np.round(coverage * rng.uniform(0.0, 0.6, count), 0)
    allocated = np.minimum(
        np.round(coverage * rng.uniform(0.0, 0.4, count), 0), on_hand + in_transit
    )
    quality_hold = np.minimum(np.round(on_hand * rng.uniform(0.0, 0.1, count), 0), on_hand)

    return pd.DataFrame(
        {
            "inventory_id": np.arange(1, count + 1),
            "material_id": frame["material_id"],
            "on_hand": on_hand,
            "in_transit": in_transit,
            "allocated": allocated,
            "quality_hold": quality_hold,
            "snapshot_date": _date(base_date),
        }
    )


def _build_bom(
    rng: np.random.Generator,
    cfg: dict[str, Any],
    materials: dict[str, Any],
    base_date: pd.Timestamp,
) -> pd.DataFrame:
    """多层级 BOM：父件仅取自制/外包件，子件只从更小下标取，保证无环。"""
    frame = materials["frame"]
    supply_type = materials["supply_type"]
    material_ids = materials["material_ids"]
    bom_cfg = cfg["bom"]
    min_children = int(bom_cfg["min_children"])
    max_children = int(bom_cfg["max_children"])
    substitute_rate = float(bom_cfg["substitute_rate"])
    effective_date = _date(base_date - pd.Timedelta(days=180))

    rows: list[tuple] = []
    bom_id = 1
    for parent_index in np.flatnonzero(supply_type != "purchase"):
        if parent_index < min_children:
            continue
        child_count = int(rng.integers(min_children, max_children + 1))
        candidates = np.arange(parent_index)
        children = rng.choice(candidates, size=min(child_count, candidates.size), replace=False)
        for child_index in children:
            rows.append(
                (
                    bom_id,
                    material_ids[parent_index],
                    material_ids[int(child_index)],
                    float(np.round(rng.uniform(0.5, 2.5), 2)),
                    "PCS",
                    0,
                    0,
                    "V1",
                    effective_date,
                )
            )
            bom_id += 1
        # 替代料单独成行，仅用于展示 has_substitute，不参与毛需求
        if rng.random() < substitute_rate:
            remaining = np.setdiff1d(candidates, children)
            if remaining.size > 0:
                rows.append(
                    (
                        bom_id,
                        material_ids[parent_index],
                        material_ids[int(rng.choice(remaining))],
                        float(np.round(rng.uniform(0.5, 2.5), 2)),
                        "PCS",
                        1,
                        0,
                        "V1",
                        effective_date,
                    )
                )
                bom_id += 1

    return pd.DataFrame(
        rows,
        columns=[
            "bom_id",
            "parent_material_id",
            "child_material_id",
            "quantity_per",
            "unit",
            "is_substitute",
            "is_phantom",
            "version",
            "effective_date",
        ],
    )


def _build_purchase_orders(
    rng: np.random.Generator,
    cfg: dict[str, Any],
    materials: dict[str, Any],
    scen: dict[str, Any],
    base_date: pd.Timestamp,
) -> pd.DataFrame:
    """采购订单：已收货行提供实际交期样本，未结行提供在途预告。"""
    frame = materials["frame"]
    supply_type = materials["supply_type"]
    material_ids = materials["material_ids"]
    monthly_level = materials["monthly_level"]
    purchase_lt = frame["purchase_lt"].to_numpy()
    supply_cfg = cfg["supply"]
    history_days = int(supply_cfg["po_history_days"])
    received_low, received_high = (int(v) for v in supply_cfg["received_po_per_material"])
    open_low, open_high = (int(v) for v in supply_cfg["open_po_per_material"])
    delay_mean = float(scen["lt_delay_days"])
    delay_sigma = float(scen["lt_sigma"])

    rows: list[dict[str, Any]] = []
    po_seq = 1
    for index in np.flatnonzero(supply_type == "purchase"):
        for _ in range(int(rng.integers(received_low, received_high + 1))):
            created = base_date - pd.Timedelta(days=int(rng.integers(30, history_days)))
            promised = float(max(1.0, purchase_lt[index] + rng.normal(0.0, 1.5)))
            actual = float(max(0.5, promised + rng.normal(delay_mean, delay_sigma)))
            rows.append(
                {
                    "po_id": f"PO2026{po_seq:05d}",
                    "material_id": material_ids[index],
                    "qty": float(max(1.0, round(monthly_level[index] * rng.uniform(0.2, 1.0)))),
                    "expected_arrival": _date(created + pd.Timedelta(days=round(promised))),
                    "promised_lt": round(promised, 2),
                    "actual_lt": round(actual, 2),
                    "status": "received",
                    "created_at": _timestamp(created),
                }
            )
            po_seq += 1
        for _ in range(int(rng.integers(open_low, open_high + 1))):
            created = base_date - pd.Timedelta(days=int(rng.integers(0, 20)))
            promised = float(max(1.0, purchase_lt[index] + rng.normal(0.0, 1.5)))
            rows.append(
                {
                    "po_id": f"PO2026{po_seq:05d}",
                    "material_id": material_ids[index],
                    "qty": float(max(1.0, round(monthly_level[index] * rng.uniform(0.2, 1.0)))),
                    "expected_arrival": _date(base_date + pd.Timedelta(days=int(rng.integers(1, 30)))),
                    "promised_lt": round(promised, 2),
                    "actual_lt": None,
                    "status": "open",
                    "created_at": _timestamp(created),
                }
            )
            po_seq += 1

    return pd.DataFrame(rows)


def _build_demand_orders(
    rng: np.random.Generator,
    cfg: dict[str, Any],
    materials: dict[str, Any],
    bom: pd.DataFrame,
    base_date: pd.Timestamp,
) -> pd.DataFrame:
    """需求订单：按订单类型约束可选物料，生产工单只落在有 BOM 的自制件上。"""
    supply_type = materials["supply_type"]
    category = materials["category"]
    material_ids = materials["material_ids"]
    monthly_level = materials["monthly_level"]
    count = int(cfg["scale"]["demand_orders"])

    assemblies = {material_ids[int(i)] for i in np.flatnonzero(supply_type != "purchase")}
    bom_parents = set(bom["parent_material_id"])
    all_indices = list(range(len(material_ids)))
    make_with_bom = [
        i
        for i in all_indices
        if material_ids[i] in assemblies and material_ids[i] in bom_parents
    ]
    purchase_indices = [i for i in all_indices if supply_type[i] == "purchase"]
    spare_indices = [i for i in all_indices if category[i] == "备件"]

    pools = {
        "生产工单": make_with_bom or all_indices,
        "客户订单": make_with_bom or all_indices,
        "补货计划": purchase_indices or all_indices,
        "维修备件需求": spare_indices or purchase_indices or all_indices,
        "项目物料需求": all_indices,
    }

    rows: list[dict[str, Any]] = []
    for sequence in range(1, count + 1):
        order_type = str(rng.choice(ORDER_TYPES))
        index = int(rng.choice(pools[order_type]))
        rows.append(
            {
                "order_id": f"SO2026{sequence:04d}",
                "order_type": order_type,
                "material_id": material_ids[index],
                "qty": float(max(1.0, round(monthly_level[index] * rng.uniform(0.03, 0.15)))),
                "required_date": _date(
                    base_date + pd.Timedelta(days=int(rng.integers(3, 61)))
                ),
                "actual_completion_date": None,
                "status": "open",
                "created_at": _timestamp(
                    base_date - pd.Timedelta(days=int(rng.integers(1, 15)))
                ),
            }
        )

    return pd.DataFrame(rows)


def generate_dataset(scenario: str, cfg: dict[str, Any]) -> dict[str, pd.DataFrame]:
    """生成一个场景的 6 张基础表，键名与数据库表名一致。"""
    scenarios = cfg["scenarios"]
    if scenario not in scenarios:
        raise ValueError(f"未知场景 {scenario!r}，可选：{', '.join(scenarios)}")
    scen = scenarios[scenario]
    seed = int(cfg["seed"]) + int(scen["seed_offset"])
    base_date = pd.Timestamp(cfg["base_date"]).normalize()

    r_material, r_history, r_inventory, r_bom, r_po, r_order = _child_rngs(seed, 6)

    materials = _build_materials(r_material, cfg, base_date)
    history = _build_demand_history(r_history, cfg, materials, scen, base_date)
    inventory = _build_inventory(r_inventory, materials, history, base_date)
    bom = _build_bom(r_bom, cfg, materials, base_date)
    purchase_orders = _build_purchase_orders(r_po, cfg, materials, scen, base_date)
    demand_orders = _build_demand_orders(r_order, cfg, materials, bom, base_date)

    return {
        "t_kadss_material": materials["frame"],
        "t_kadss_bom": bom,
        "t_kadss_inventory": inventory,
        "t_kadss_purchase_order": purchase_orders,
        "t_kadss_demand_order": demand_orders,
        "t_kadss_demand_history": history,
    }
