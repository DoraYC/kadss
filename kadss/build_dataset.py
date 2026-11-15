"""命令行入口：生成模拟数据、校验并落库。

用法：
    python -m kadss.build_dataset                    # 默认生成 normal 场景
    python -m kadss.build_dataset --scenario all     # 三个场景全部生成
    python -m kadss.build_dataset --scenario normal --seed 42 --output data
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from kadss.config import DEFAULT_OUTPUT_DIR, load_config
from kadss.generator import generate_dataset
from kadss.store import build_summary, export_csv, output_paths, write_dataset
from kadss.validation import DataValidationError, assert_valid


def build_scenario(
    scenario: str, cfg: dict, output_root: str | Path
) -> tuple[dict[str, Path], int]:
    """生成 → 校验 → 落库 → 导出 CSV → 写摘要，返回路径与 CSV 数量。"""
    frames = generate_dataset(scenario, cfg)
    assert_valid(frames)
    paths = output_paths(scenario, output_root)
    write_dataset(frames, paths["db"])
    exports = export_csv(paths["db"], paths["csv"])
    paths["summary"].write_text(build_summary(paths["db"], cfg, scenario), encoding="utf-8")
    return paths, len(exports)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 KADSS 模拟数据并写入 SQLite")
    parser.add_argument(
        "--scenario",
        default="normal",
        help="场景名，或 all 生成全部场景（默认 normal）",
    )
    parser.add_argument("--seed", type=int, help="覆盖配置文件中的主种子")
    parser.add_argument("--config", type=Path, help="配置文件路径，默认 config/kadss.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="输出根目录")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.seed is not None:
        cfg["seed"] = args.seed

    scenarios = list(cfg["scenarios"]) if args.scenario == "all" else [args.scenario]
    try:
        for scenario in scenarios:
            paths, csv_count = build_scenario(scenario, cfg, args.output)
            print(
                f"[{scenario}] 数据库 {paths['db']} | "
                f"CSV {csv_count} 个 | 摘要 {paths['summary']}"
            )
    except DataValidationError as error:
        print(error)
        return 1
    except ValueError as error:
        print(error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
