"""基础表完整性校验（PRD US-1：主键唯一、外键有效、数值非负、BOM 无环）。"""

from __future__ import annotations

import re
from collections import defaultdict

import pandas as pd

BASE_TABLES = (
    "t_kadss_material",
    "t_kadss_bom",
    "t_kadss_inventory",
    "t_kadss_purchase_order",
    "t_kadss_demand_order",
    "t_kadss_demand_history",
)

PRIMARY_KEYS = {
    "t_kadss_material": ("material_id",),
    "t_kadss_bom": ("bom_id",),
    "t_kadss_inventory": ("inventory_id",),
    "t_kadss_purchase_order": ("po_id",),
    "t_kadss_demand_order": ("order_id",),
    "t_kadss_demand_history": ("history_id",),
}

BUSINESS_UNIQUE_KEYS = {
    "t_kadss_bom": ("parent_material_id", "child_material_id", "version"),
    "t_kadss_inventory": ("material_id", "snapshot_date"),
    "t_kadss_demand_history": ("material_id", "demand_date"),
}

NOT_NULL_COLUMNS = {
    "t_kadss_material": (
        "material_id",
        "material_name",
        "uom",
        "supply_type",
        "purchase_lt",
        "production_lt",
        "inspection_lt",
        "logistics_lt",
        "unit_price",
        "status",
    ),
    "t_kadss_bom": (
        "bom_id",
        "parent_material_id",
        "child_material_id",
        "quantity_per",
        "unit",
        "is_substitute",
        "is_phantom",
        "version",
    ),
    "t_kadss_inventory": (
        "inventory_id",
        "material_id",
        "on_hand",
        "in_transit",
        "allocated",
        "quality_hold",
        "snapshot_date",
    ),
    "t_kadss_purchase_order": ("po_id", "material_id", "qty", "status"),
    "t_kadss_demand_order": (
        "order_id",
        "order_type",
        "material_id",
        "qty",
        "required_date",
        "status",
    ),
    "t_kadss_demand_history": ("history_id", "material_id", "demand_date", "demand_qty"),
}

NON_NEGATIVE_COLUMNS = {
    "t_kadss_material": (
        "purchase_lt",
        "production_lt",
        "inspection_lt",
        "logistics_lt",
        "unit_price",
    ),
    "t_kadss_bom": ("quantity_per",),
    "t_kadss_inventory": ("on_hand", "in_transit", "allocated", "quality_hold"),
    "t_kadss_purchase_order": ("qty", "promised_lt", "actual_lt"),
    "t_kadss_demand_order": ("qty",),
    "t_kadss_demand_history": ("demand_qty",),
}

POSITIVE_COLUMNS = {
    "t_kadss_bom": ("quantity_per",),
    "t_kadss_purchase_order": ("qty",),
    "t_kadss_demand_order": ("qty",),
}

FOREIGN_KEYS = (
    ("t_kadss_bom", "parent_material_id"),
    ("t_kadss_bom", "child_material_id"),
    ("t_kadss_inventory", "material_id"),
    ("t_kadss_purchase_order", "material_id"),
    ("t_kadss_demand_order", "material_id"),
    ("t_kadss_demand_history", "material_id"),
)

DATE_COLUMNS = {
    "t_kadss_bom": ("effective_date",),
    "t_kadss_inventory": ("snapshot_date",),
    "t_kadss_purchase_order": ("expected_arrival",),
    "t_kadss_demand_order": ("required_date", "actual_completion_date"),
    "t_kadss_demand_history": ("demand_date",),
}

ENUM_COLUMNS = {
    "t_kadss_material": {
        "supply_type": {"purchase", "make", "outsource"},
        "status": {"active", "inactive"},
    },
    "t_kadss_purchase_order": {"status": {"open", "received", "cancelled"}},
    "t_kadss_demand_order": {
        "order_type": {"生产工单", "客户订单", "补货计划", "维修备件需求", "项目物料需求"},
        "status": {"open", "closed", "cancelled"},
    },
}

FLAG_COLUMNS = {("t_kadss_bom", "is_substitute"), ("t_kadss_bom", "is_phantom")}
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DataValidationError(RuntimeError):
    """数据未通过完整性校验时抛出。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        message = "数据完整性校验未通过：\n" + "\n".join(f"- {item}" for item in self.errors)
        super().__init__(message)


def _missing_frames(frames: dict[str, pd.DataFrame]) -> list[str]:
    return [f"缺少数据表：{table}" for table in BASE_TABLES if table not in frames]


def _duplicate_key_errors(table: str, frame: pd.DataFrame, columns: tuple[str, ...]) -> list[str]:
    duplicated = frame.duplicated(subset=list(columns), keep=False)
    if not duplicated.any():
        return []
    samples = frame.loc[duplicated, list(columns)].head(3).to_dict("records")
    return [f"{table}：键 {columns} 存在重复，示例 {samples}"]


def _null_errors(table: str, frame: pd.DataFrame) -> list[str]:
    errors: list[str] = []
    for column in NOT_NULL_COLUMNS.get(table, ()):
        if column not in frame.columns:
            errors.append(f"{table}：缺少字段 {column}")
            continue
        null_count = int(frame[column].isna().sum())
        if null_count:
            errors.append(f"{table}.{column}：存在 {null_count} 个空值")
    return errors


def _numeric_errors(table: str, frame: pd.DataFrame) -> list[str]:
    errors: list[str] = []
    for column in NON_NEGATIVE_COLUMNS.get(table, ()):
        negative = int((frame[column] < 0).sum())
        if negative:
            errors.append(f"{table}.{column}：存在 {negative} 个负值")
    for column in POSITIVE_COLUMNS.get(table, ()):
        invalid = int((frame[column] <= 0).sum())
        if invalid:
            errors.append(f"{table}.{column}：存在 {invalid} 个非正数")
    return errors


def _enum_errors(table: str, frame: pd.DataFrame) -> list[str]:
    errors: list[str] = []
    for column, allowed in ENUM_COLUMNS.get(table, {}).items():
        invalid_values = set(frame[column].dropna().unique()) - allowed
        if invalid_values:
            errors.append(f"{table}.{column}：存在非法取值 {sorted(invalid_values)}")
    return errors


def _flag_errors(table: str, frame: pd.DataFrame) -> list[str]:
    errors: list[str] = []
    for flag_table, column in FLAG_COLUMNS:
        if flag_table != table:
            continue
        invalid_values = set(frame[column].dropna().unique()) - {0, 1}
        if invalid_values:
            errors.append(f"{table}.{column}：取值必须为 0/1，实际 {sorted(invalid_values)}")
    return errors


def _date_errors(table: str, frame: pd.DataFrame) -> list[str]:
    errors: list[str] = []
    for column in DATE_COLUMNS.get(table, ()):
        values = frame[column].dropna().astype(str)
        invalid = values[~values.str.match(DATE_PATTERN)]
        if not invalid.empty:
            errors.append(
                f"{table}.{column}：{len(invalid)} 个日期不是 YYYY-MM-DD 格式，"
                f"示例 {invalid.head(3).tolist()}"
            )
    return errors


def _foreign_key_errors(frames: dict[str, pd.DataFrame]) -> list[str]:
    errors: list[str] = []
    material_ids = set(frames["t_kadss_material"]["material_id"])
    for table, column in FOREIGN_KEYS:
        frame = frames[table]
        orphan = set(frame[column]) - material_ids
        if orphan:
            errors.append(
                f"{table}.{column}：{len(orphan)} 个外键在物料主数据中不存在，"
                f"示例 {sorted(orphan)[:3]}"
            )
    return errors


def _inventory_rule_errors(frames: dict[str, pd.DataFrame]) -> list[str]:
    inventory = frames["t_kadss_inventory"]
    over_allocated = inventory["allocated"] > inventory["on_hand"] + inventory["in_transit"]
    if not over_allocated.any():
        return []
    samples = inventory.loc[over_allocated, ["material_id", "on_hand", "in_transit", "allocated"]]
    return [
        f"t_kadss_inventory：{int(over_allocated.sum())} 行已分配大于 现有库存+在途，"
        f"示例 {samples.head(3).to_dict('records')}"
    ]


def _find_bom_cycle(bom: pd.DataFrame) -> list[str] | None:
    graph: dict[str, list[str]] = defaultdict(list)
    for parent, child in zip(bom["parent_material_id"], bom["child_material_id"]):
        graph[parent].append(child)

    color: dict[str, int] = {}
    for start in list(graph):
        if color.get(start, 0) != 0:
            continue
        color[start] = 1
        path = [start]
        stack = [(start, iter(graph[start]))]
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                state = color.get(child, 0)
                if state == 1:
                    return [*path[path.index(child) :], child]
                if state == 0:
                    color[child] = 1
                    path.append(child)
                    stack.append((child, iter(graph[child])))
                    advanced = True
                    break
            if not advanced:
                color[node] = 2
                path.pop()
                stack.pop()
    return None


def _bom_errors(frames: dict[str, pd.DataFrame]) -> list[str]:
    bom = frames["t_kadss_bom"]
    errors: list[str] = []
    self_reference = int((bom["parent_material_id"] == bom["child_material_id"]).sum())
    if self_reference:
        errors.append(f"t_kadss_bom：存在 {self_reference} 行父件与子件相同")
    cycle = _find_bom_cycle(bom)
    if cycle is not None:
        errors.append(f"t_kadss_bom：存在循环引用 {' -> '.join(cycle)}")
    return errors


def validate_dataset(frames: dict[str, pd.DataFrame]) -> list[str]:
    """返回全部校验错误；空列表表示通过。"""
    errors = _missing_frames(frames)
    if errors:
        return errors

    for table in BASE_TABLES:
        frame = frames[table]
        errors.extend(_duplicate_key_errors(table, frame, PRIMARY_KEYS[table]))
        business_keys = BUSINESS_UNIQUE_KEYS.get(table)
        if business_keys:
            errors.extend(_duplicate_key_errors(table, frame, business_keys))
        errors.extend(_null_errors(table, frame))
        errors.extend(_numeric_errors(table, frame))
        errors.extend(_enum_errors(table, frame))
        errors.extend(_flag_errors(table, frame))
        errors.extend(_date_errors(table, frame))

    errors.extend(_foreign_key_errors(frames))
    errors.extend(_inventory_rule_errors(frames))
    errors.extend(_bom_errors(frames))
    return errors


def assert_valid(frames: dict[str, pd.DataFrame]) -> None:
    errors = validate_dataset(frames)
    if errors:
        raise DataValidationError(errors)
