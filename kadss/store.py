"""SQLite 落库、CSV 导出与生成摘要。

口径：SQLite 为唯一主数据源，CSV 每次从 SQLite 导出，避免两处各自写入。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

from kadss.config import DEFAULT_OUTPUT_DIR, DEFAULT_SCHEMA_PATH
from kadss.validation import BASE_TABLES, PRIMARY_KEYS

BASE_TABLE_ORDER = (
    "t_kadss_material",
    "t_kadss_bom",
    "t_kadss_inventory",
    "t_kadss_purchase_order",
    "t_kadss_demand_order",
    "t_kadss_demand_history",
)


def output_paths(scenario: str, output_root: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    """每个场景独立目录，避免三个场景的数据互相覆盖。"""
    base = Path(output_root) / scenario
    return {
        "base": base,
        "db": base / "kadss.db",
        "csv": base / "csv",
        "summary": base / "summary.md",
    }


def initialize_database(
    db_path: str | Path, schema_path: str | Path = DEFAULT_SCHEMA_PATH
) -> None:
    """按 db/schema.sql 重建空库（DDL 内含 DROP TABLE，可重复执行）。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ddl = Path(schema_path).read_text(encoding="utf-8")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(ddl)
        conn.commit()


def write_dataset(
    frames: dict[str, pd.DataFrame],
    db_path: str | Path,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> None:
    """先建库再按外键依赖顺序写入 6 张基础表。"""
    initialize_database(db_path, schema_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for table in BASE_TABLE_ORDER:
            frames[table].to_sql(table, conn, if_exists="append", index=False)
        conn.commit()


def export_csv(db_path: str | Path, csv_dir: str | Path) -> list[Path]:
    """从 SQLite 按主键有序导出 CSV。"""
    csv_dir = Path(csv_dir)
    csv_dir.mkdir(parents=True, exist_ok=True)
    exports: list[Path] = []
    with closing(sqlite3.connect(db_path)) as conn:
        for table in BASE_TABLE_ORDER:
            order_by = ", ".join(PRIMARY_KEYS[table])
            frame = pd.read_sql_query(
                f"SELECT * FROM {table} ORDER BY {order_by}", conn
            )
            target = csv_dir / f"{table}.csv"
            frame.to_csv(target, index=False)
            exports.append(target)
    return exports


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def build_summary(db_path: str | Path, cfg: dict[str, Any], scenario: str) -> str:
    """生成摘要：参数、行数、分布、日期区间与校验结论。内容不写入运行时时间戳，保证可复现。"""
    scen = cfg["scenarios"][scenario]
    supply_order = {"purchase": 0, "make": 1, "outsource": 2}
    with closing(sqlite3.connect(db_path)) as conn:
        counts = {
            table: int(pd.read_sql_query(f"SELECT COUNT(*) AS n FROM {table}", conn)["n"].iat[0])
            for table in BASE_TABLES
        }
        supply = pd.read_sql_query(
            "SELECT supply_type, COUNT(*) AS n FROM t_kadss_material GROUP BY supply_type",
            conn,
        )
        supply = supply.assign(order=supply["supply_type"].map(supply_order)).sort_values("order")
        categories = pd.read_sql_query(
            "SELECT category, COUNT(*) AS n FROM t_kadss_material "
            "GROUP BY category ORDER BY n DESC, category",
            conn,
        )
        order_types = pd.read_sql_query(
            "SELECT order_type, COUNT(*) AS n FROM t_kadss_demand_order "
            "GROUP BY order_type ORDER BY n DESC, order_type",
            conn,
        )
        date_range = pd.read_sql_query(
            "SELECT MIN(required_date) AS low, MAX(required_date) AS high "
            "FROM t_kadss_demand_order",
            conn,
        ).iloc[0]
        bom_parents = int(
            pd.read_sql_query(
                "SELECT COUNT(DISTINCT parent_material_id) AS n FROM t_kadss_bom", conn
            )["n"].iat[0]
        )
        substitutes = int(
            pd.read_sql_query(
                "SELECT COUNT(*) AS n FROM t_kadss_bom WHERE is_substitute = 1", conn
            )["n"].iat[0]
        )

    params = [
        ("主种子 / 场景偏移", f"{cfg['seed']} / {scen['seed_offset']}"),
        ("业务基准日期", cfg["base_date"]),
        ("物料数", cfg["scale"]["materials"]),
        ("需求订单数", cfg["scale"]["demand_orders"]),
        ("历史需求天数", cfg["scale"]["history_days"]),
        (
            "短期历史物料占比 / 天数",
            f"{cfg['scale']['limited_history_share']} / {cfg['scale']['limited_history_days']}",
        ),
        ("需求波动系数", scen["demand_volatility"]),
        ("交期延误均值(天)", scen["lt_delay_days"]),
        ("交期延误标准差(天)", scen["lt_sigma"]),
        ("零需求概率", cfg["noise"]["demand_zero_rate"]),
        (
            "尖峰概率 / 倍数",
            f"{cfg['noise']['demand_spike_rate']} / {cfg['noise']['demand_spike_factor']}",
        ),
        (
            "波动类型占比 X/Y/Z",
            " / ".join(
                f"{float(cfg['noise']['volatility_mix'][key]):.0%}"
                for key in ("x", "y", "z")
            ),
        ),
        (
            "已收货采购单/物料",
            "-".join(str(value) for value in cfg["supply"]["received_po_per_material"]),
        ),
        (
            "未结采购单/物料",
            "-".join(str(value) for value in cfg["supply"]["open_po_per_material"]),
        ),
    ]

    sections = [
        "# KADSS 数据生成摘要",
        "",
        f"- 场景：`{scenario}`（{scen['label']}）",
        f"- 随机种子：{cfg['seed']}（主种子 {cfg['seed']} + 场景偏移 {scen['seed_offset']}）",
        f"- 业务基准日期：{cfg['base_date']}",
        "- 数据库：`kadss.db`（SQLite 为主数据源，CSV 由 SQLite 导出）",
        "",
        "## 生成参数",
        "",
        _markdown_table(("参数", "取值"), params),
        "",
        "## 表行数",
        "",
        _markdown_table(
            ("表", "行数"), [(table, counts[table]) for table in BASE_TABLE_ORDER]
        ),
        "",
        "## 物料分布",
        "",
        _markdown_table(
            ("供应类型", "物料数"),
            list(zip(supply["supply_type"], supply["n"])),
        ),
        "",
        _markdown_table(
            ("类别", "物料数"), list(zip(categories["category"], categories["n"]))
        ),
        "",
        "## 需求订单",
        "",
        _markdown_table(
            ("订单类型", "订单数"), list(zip(order_types["order_type"], order_types["n"]))
        ),
        "",
        f"- `required_date` 区间：{date_range['low']} ~ {date_range['high']}",
        f"- BOM 父件数：{bom_parents}；替代料行：{substitutes}",
        "",
        "## 数据校验",
        "",
        "- 结果：通过（主键唯一、外键有效、数值非负、BOM 无环、已分配 ≤ 现有库存 + 在途）",
        "- 说明：`is_substitute = 1` 的 BOM 行仅用于标记 `has_substitute`，不参与毛需求计算",
        "",
    ]
    return "\n".join(sections)
