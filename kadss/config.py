"""配置加载：默认值 + config/kadss.json 覆盖，避免把参数写死在代码里。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "kadss.json"
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data"

DEFAULTS: dict[str, Any] = {
    "seed": 42,
    "base_date": "2026-11-16",
    "scenarios": {
        "normal": {
            "label": "正常",
            "seed_offset": 0,
            "demand_volatility": 1.0,
            "lt_delay_days": 0.0,
            "lt_sigma": 1.5,
        },
        "high_volatility": {
            "label": "高波动",
            "seed_offset": 1,
            "demand_volatility": 1.8,
            "lt_delay_days": 0.0,
            "lt_sigma": 1.5,
        },
        "leadtime_deterioration": {
            "label": "交期恶化",
            "seed_offset": 2,
            "demand_volatility": 1.0,
            "lt_delay_days": 6.0,
            "lt_sigma": 2.5,
        },
    },
    "scale": {"materials": 200, "demand_orders": 50, "history_days": 90},
    "bom": {"min_children": 2, "max_children": 6, "substitute_rate": 0.05},
    "noise": {"demand_zero_rate": 0.05, "demand_spike_rate": 0.01, "demand_spike_factor": 3.0},
    "supply": {
        "supplier_pool_size": 30,
        "po_history_days": 180,
        "received_po_per_material": [4, 10],
        "open_po_per_material": [0, 2],
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并配置，未覆盖的键保留默认值。"""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取配置文件；文件不存在时使用默认值。"""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return deepcopy(DEFAULTS)
    with config_path.open(encoding="utf-8") as handle:
        return _deep_merge(DEFAULTS, json.load(handle))
