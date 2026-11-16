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
    "profile": "small",
    "profiles": {
        "small": {
            "label": "标准（200 物料）",
            "output_root": "data",
            "scale": {
                "materials": 200,
                "demand_orders": 50,
                "history_days": 90,
                "limited_history_share": 0.03,
                "limited_history_days": 10,
            },
            "bom": {"min_children": 2, "max_children": 6, "substitute_rate": 0.15},
        },
        "large": {
            "label": "大规模（2,000 物料）",
            "output_root": "data_large",
            "scale": {
                "materials": 2000,
                "demand_orders": 300,
                "history_days": 180,
                "limited_history_share": 0.03,
                "limited_history_days": 10,
            },
            "bom": {"min_children": 2, "max_children": 6, "substitute_rate": 0.15},
        },
    },
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
    "noise": {
        "demand_zero_rate": 0.05,
        "demand_spike_rate": 0.01,
        "demand_spike_factor": 3.0,
        "volatility_mix": {"x": 0.1, "y": 0.4, "z": 0.5},
    },
    "supply": {
        "supplier_pool_size": 30,
        "po_history_days": 180,
        "received_po_per_material": [4, 10],
        "open_po_per_material": [0, 2],
    },
    "abc_xyz": {
        "abc_a_share": 0.70,
        "abc_b_share": 0.90,
        "xyz_x_cv": 0.10,
        "xyz_y_cv": 0.25,
        "min_periods": 3,
    },
    "safety_stock": {
        "service_levels": [90, 95, 99],
        "default_service_level": 95,
        "fixed_coverage_days": 7,
        "min_periods": 3,
        "z_values": {"90": 1.28, "95": 1.65, "99": 2.33},
    },
    "kitting": {
        "max_depth": 10,
        "available_scope": {
            "in_transit": True,
            "allocated": True,
            "quality_hold": False,
            "safety_stock": False,
        },
    },
    "probability": {
        "simulation_count": 1000,
        "risk_low": 0.90,
        "risk_medium": 0.70,
        "seed": 20261116,
    },
    "cost": {
        "holding_rate": 0.20,
        "penalty_factor": 3.0,
        "simulation_days": 90,
        "seed": 20261116,
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


def _resolve_profile(cfg: dict[str, Any], profile: str | None) -> dict[str, Any]:
    """把选定规模的 scale / bom / output_root 展开为可直接使用的配置。"""
    selected = str(profile or cfg.get("profile", "small"))
    profiles = cfg.get("profiles", {})
    if selected not in profiles:
        raise ValueError(
            f"未知数据规模 {selected!r}，可选：{', '.join(sorted(profiles))}"
        )
    resolved = _deep_merge(cfg, profiles[selected])
    resolved["profile"] = selected
    resolved["scale"] = deepcopy(profiles[selected]["scale"])
    resolved["bom"] = deepcopy(profiles[selected]["bom"])
    output_root = Path(str(profiles[selected]["output_root"]))
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    resolved["output_root"] = output_root
    return resolved


def load_config(
    path: str | Path | None = None, profile: str | None = None
) -> dict[str, Any]:
    """读取配置文件；文件不存在时使用默认值，profile 可覆盖当前数据规模。"""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return _resolve_profile(deepcopy(DEFAULTS), profile)
    with config_path.open(encoding="utf-8") as handle:
        merged = _deep_merge(DEFAULTS, json.load(handle))
    return _resolve_profile(merged, profile)
