"""计算层编排：读取基础表 → 依次计算 → 写入 5 张结果表。

run_id = 场景 + 数据版本 + 参数哈希，同一参数重复运行会覆盖同 run_id 的结果，保证可复算。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

from kadss.abc_xyz import compute_abc_xyz
from kadss.config import DEFAULT_OUTPUT_DIR
from kadss.cost import simulate_cost
from kadss.kitting import compute_kitting
from kadss.probability import compute_probability
from kadss.safety_stock import compute_safety_stock
from kadss.store import output_paths
from kadss.validation import PRIMARY_KEYS

BASE_TABLES = (
    "t_kadss_material",
    "t_kadss_bom",
    "t_kadss_inventory",
    "t_kadss_purchase_order",
    "t_kadss_demand_order",
    "t_kadss_demand_history",
)

RESULT_TABLES = (
    "t_kadss_abc_xyz_result",
    "t_kadss_safety_stock_result",
    "t_kadss_kitting_result",
    "t_kadss_kitting_probability",
    "t_kadss_cost_simulation_result",
)

RUN_ID_PARAMS = (
    "seed",
    "base_date",
    "abc_xyz",
    "safety_stock",
    "kitting",
    "probability",
    "cost",
)

RESULT_ORDER_KEYS = {
    "t_kadss_abc_xyz_result": ("run_id", "material_id"),
    "t_kadss_safety_stock_result": ("run_id", "material_id", "service_level"),
    "t_kadss_kitting_result": (
        "run_id",
        "order_id",
        "material_id",
        "service_level",
        "strategy",
    ),
    "t_kadss_kitting_probability": ("run_id", "order_id"),
    "t_kadss_cost_simulation_result": (
        "run_id",
        "material_id",
        "strategy",
        "service_level",
    ),
}


def _data_digest(data: dict[str, pd.DataFrame]) -> str:
    """基础数据内容摘要，数据重新生成后 run_id 会随内容变化。"""
    digest = hashlib.sha256()
    for table in BASE_TABLES:
        digest.update(table.encode("utf-8"))
        digest.update(
            pd.util.hash_pandas_object(data[table], index=False).to_numpy().tobytes()
        )
    return digest.hexdigest()[:8]


def compute_run_id(
    scenario: str, cfg: dict[str, Any], data: dict[str, pd.DataFrame]
) -> str:
    """run_id = 场景 + 参数哈希 + 基础数据内容摘要。"""
    payload = {"scenario": scenario}
    payload.update({key: cfg[key] for key in RUN_ID_PARAMS})
    params_digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"{scenario}-{params_digest}-{_data_digest(data)}"


def load_base_tables(db_path: str | Path) -> dict[str, pd.DataFrame]:
    """按主键排序读取基础表，确保数据摘要与 run_id 不受物理行序影响。"""
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            table: pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY {', '.join(PRIMARY_KEYS[table])}", conn
            )
            for table in BASE_TABLES
        }


def _assert_run_id(frames: dict[str, pd.DataFrame], run_id: str) -> None:
    for table in RESULT_TABLES:
        values = set(frames[table]["run_id"].astype(str))
        if values != {run_id}:
            raise ValueError(
                f"结果表 {table} 的 run_id 必须唯一且等于 {run_id}，实际为 {sorted(values)}"
            )


def write_results(db_path: str | Path, frames: dict[str, pd.DataFrame], run_id: str) -> None:
    """每个场景一个库，单事务覆盖 5 张结果表，只保留当前 run_id 的结果。"""
    _assert_run_id(frames, run_id)
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            for table in RESULT_TABLES:
                conn.execute(f"DELETE FROM {table}")
            for table in RESULT_TABLES:
                frame = frames[table]
                columns = ", ".join(frame.columns)
                placeholders = ", ".join("?" for _ in frame.columns)
                rows = frame.astype(object).where(pd.notna(frame), None).values.tolist()
                conn.executemany(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    [tuple(row) for row in rows],
                )


def result_run_ids(db_path: str | Path) -> dict[str, set[str]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            table: set(
                pd.read_sql_query(f"SELECT DISTINCT run_id FROM {table}", conn)[
                    "run_id"
                ].astype(str)
            )
            for table in RESULT_TABLES
        }


def load_results(
    db_path: str | Path, run_id: str | None = None
) -> dict[str, pd.DataFrame]:
    """只读 5 张结果表；给定 run_id 时只取该版本，避免跨 run 聚合。"""
    frames: dict[str, pd.DataFrame] = {}
    with closing(sqlite3.connect(db_path)) as conn:
        for table in RESULT_TABLES:
            query = f"SELECT * FROM {table}"
            params: tuple[str, ...] = ()
            if run_id is not None:
                query += " WHERE run_id = ?"
                params = (run_id,)
            query += " ORDER BY " + ", ".join(RESULT_ORDER_KEYS[table])
            frames[table] = pd.read_sql_query(query, conn, params=params)
    return frames


def verify_results(
    db_path: str | Path,
    scenario: str,
    cfg: dict[str, Any],
    data: dict[str, pd.DataFrame] | None = None,
) -> str:
    """读库前自检：结果表 run_id 一致，且与当前 seed、base_date 和基础数据推导值一致。"""
    data = load_base_tables(db_path) if data is None else data
    expected = compute_run_id(scenario, cfg, data)
    actual = result_run_ids(db_path)
    mismatched = {
        table: sorted(values) for table, values in actual.items() if values != {expected}
    }
    if mismatched:
        raise ValueError(
            "结果库与当前配置不一致，请重新运行 python -m kadss.build_results："
            f"期望 run_id={expected}（seed={cfg['seed']}，base_date={cfg['base_date']}），"
            f"实际={mismatched}"
        )
    return expected


def _confidence_map(
    abc: pd.DataFrame, safety_stock: pd.DataFrame, default_level: int
) -> dict[str, str]:
    low = set(abc.loc[abc["confidence_level"] == "low", "material_id"])
    low |= set(
        safety_stock.loc[
            (safety_stock["service_level"] == default_level)
            & (safety_stock["confidence_level"] == "low"),
            "material_id",
        ]
    )
    material_ids = set(abc["material_id"])
    return {str(mid): ("low" if mid in low else "high") for mid in material_ids}


def run_scenario(
    scenario: str,
    cfg: dict[str, Any],
    output_root: str | Path = DEFAULT_OUTPUT_DIR,
) -> tuple[dict[str, pd.DataFrame], str]:
    paths = output_paths(scenario, output_root)
    base_date = pd.Timestamp(cfg["base_date"]).normalize()
    created_at = f"{cfg['base_date']} 00:00:00"
    default_level = int(cfg["safety_stock"]["default_service_level"])

    data = load_base_tables(paths["db"])
    run_id = compute_run_id(scenario, cfg, data)
    materials = data["t_kadss_material"]
    bom = data["t_kadss_bom"]
    inventory = data["t_kadss_inventory"]
    purchase_orders = data["t_kadss_purchase_order"]
    orders = data["t_kadss_demand_order"]
    history = data["t_kadss_demand_history"]

    abc = compute_abc_xyz(materials, history, cfg, run_id, created_at)
    safety_stock = compute_safety_stock(
        materials, history, purchase_orders, inventory, cfg, run_id, created_at
    )
    confidence = _confidence_map(abc, safety_stock, default_level)

    baseline = compute_kitting(
        orders,
        materials,
        inventory,
        bom,
        safety_stock,
        confidence,
        cfg,
        run_id,
        created_at,
        strategy="baseline",
        service_level=default_level,
    )
    strategy_frames = [
        compute_kitting(
            orders,
            materials,
            inventory,
            bom,
            safety_stock,
            confidence,
            cfg,
            run_id,
            created_at,
            strategy=strategy,
            service_level=level,
        )
        for level in [int(value) for value in cfg["safety_stock"]["service_levels"]]
        for strategy in ("fixed", "dynamic")
    ]
    kitting = pd.concat([baseline, *strategy_frames], ignore_index=True)
    kitting = kitting.sort_values(
        ["order_id", "material_id", "strategy", "service_level"]
    ).reset_index(drop=True)

    probability = compute_probability(
        kitting, orders, safety_stock, cfg, base_date, run_id, created_at
    )
    cost = simulate_cost(
        materials, history, safety_stock, inventory, cfg, run_id, created_at
    )

    frames = {
        "t_kadss_abc_xyz_result": abc,
        "t_kadss_safety_stock_result": safety_stock,
        "t_kadss_kitting_result": kitting,
        "t_kadss_kitting_probability": probability,
        "t_kadss_cost_simulation_result": cost,
    }
    write_results(paths["db"], frames, run_id)
    verify_results(paths["db"], scenario, cfg, data)
    return frames, run_id
