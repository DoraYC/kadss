-- KADSS 数据表结构（SQLite）
-- 对应 PRD v1.1：离散制造供应链齐套预警与动态安全库存优化系统
-- 口径：
--   1. SQLite 为本地持久化与查询主库；CSV 仅作为从 SQLite 导出的交付格式
--   2. BOM 仅存直接父子关系，层级在 MRP 展开时递归计算，不冗余 level
--   3. 历史需求按日存储；安全库存基于日需求计算，看板按周聚合仅用于展示
-- 约定：
--   - 表名统一 t_kadss_ 前缀，字段小写 snake_case
--   - 日期统一 TEXT，格式 YYYY-MM-DD；时间戳格式 YYYY-MM-DD HH:MM:SS
--   - 结果表统一带 run_id（数据版本 + 参数哈希），支持复算与口径追溯
--   - 提前期单位统一为天

PRAGMA foreign_keys = ON;

-- 依赖倒序清理，便于重复执行
DROP TABLE IF EXISTS t_kadss_cost_simulation_result;
DROP TABLE IF EXISTS t_kadss_kitting_probability;
DROP TABLE IF EXISTS t_kadss_kitting_result;
DROP TABLE IF EXISTS t_kadss_safety_stock_result;
DROP TABLE IF EXISTS t_kadss_abc_xyz_result;
DROP TABLE IF EXISTS t_kadss_demand_history;
DROP TABLE IF EXISTS t_kadss_demand_order;
DROP TABLE IF EXISTS t_kadss_purchase_order;
DROP TABLE IF EXISTS t_kadss_inventory;
DROP TABLE IF EXISTS t_kadss_bom;
DROP TABLE IF EXISTS t_kadss_material;

-- ---------------------------------------------------------------
-- 基础表
-- ---------------------------------------------------------------

-- 物料主数据
CREATE TABLE t_kadss_material (
    material_id       TEXT PRIMARY KEY,
    material_name     TEXT NOT NULL,
    category          TEXT,
    uom               TEXT NOT NULL DEFAULT 'PCS',
    supply_type       TEXT NOT NULL
                      CHECK (supply_type IN ('purchase', 'make', 'outsource')),
    purchase_lt       REAL NOT NULL DEFAULT 0 CHECK (purchase_lt >= 0),
    production_lt     REAL NOT NULL DEFAULT 0 CHECK (production_lt >= 0),
    inspection_lt     REAL NOT NULL DEFAULT 0 CHECK (inspection_lt >= 0),
    logistics_lt      REAL NOT NULL DEFAULT 0 CHECK (logistics_lt >= 0),
    supplier_id       TEXT,
    unit_price        REAL NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'inactive')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- BOM：仅存直接父子关系；层级由 MRP 展开时递归计算
CREATE TABLE t_kadss_bom (
    bom_id              INTEGER PRIMARY KEY,
    parent_material_id  TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    child_material_id   TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    quantity_per        REAL NOT NULL CHECK (quantity_per > 0),
    unit                TEXT NOT NULL DEFAULT 'PCS',
    is_substitute       INTEGER NOT NULL DEFAULT 0 CHECK (is_substitute IN (0, 1)),
    is_phantom          INTEGER NOT NULL DEFAULT 0 CHECK (is_phantom IN (0, 1)),
    version             TEXT NOT NULL DEFAULT 'V1',
    effective_date      TEXT,
    CHECK (parent_material_id <> child_material_id),
    UNIQUE (parent_material_id, child_material_id, version)
);

CREATE INDEX idx_kadss_bom_parent ON t_kadss_bom (parent_material_id, version);
CREATE INDEX idx_kadss_bom_child ON t_kadss_bom (child_material_id);

-- 库存快照（按物料 + 快照日期，不区分批次 / 库位 / 保质期）
CREATE TABLE t_kadss_inventory (
    inventory_id      INTEGER PRIMARY KEY,
    material_id       TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    on_hand           REAL NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    in_transit        REAL NOT NULL DEFAULT 0 CHECK (in_transit >= 0),
    allocated         REAL NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    quality_hold      REAL NOT NULL DEFAULT 0 CHECK (quality_hold >= 0),
    snapshot_date     TEXT NOT NULL,
    UNIQUE (material_id, snapshot_date)
);

CREATE INDEX idx_kadss_inventory_date ON t_kadss_inventory (snapshot_date);

-- 采购订单（采购件交期偏差来源）
CREATE TABLE t_kadss_purchase_order (
    po_id             TEXT PRIMARY KEY,
    material_id       TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    qty               REAL NOT NULL CHECK (qty > 0),
    expected_arrival  TEXT,
    promised_lt       REAL CHECK (promised_lt IS NULL OR promised_lt >= 0),
    actual_lt         REAL CHECK (actual_lt IS NULL OR actual_lt >= 0),
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'received', 'cancelled')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_kadss_po_material ON t_kadss_purchase_order (material_id, status);

-- 需求订单：生产工单 / 客户订单 / 补货计划 / 维修备件需求 / 项目物料需求
CREATE TABLE t_kadss_demand_order (
    order_id                TEXT PRIMARY KEY,
    order_type              TEXT NOT NULL
                            CHECK (order_type IN ('生产工单', '客户订单', '补货计划', '维修备件需求', '项目物料需求')),
    material_id             TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    qty                     REAL NOT NULL CHECK (qty > 0),
    required_date           TEXT NOT NULL,
    actual_completion_date  TEXT,
    status                  TEXT NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open', 'closed', 'cancelled')),
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_kadss_demand_date ON t_kadss_demand_order (required_date, status);

-- 历史需求（日粒度；周聚合仅用于展示，不参与安全库存公式）
CREATE TABLE t_kadss_demand_history (
    history_id    INTEGER PRIMARY KEY,
    material_id   TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    demand_date   TEXT NOT NULL,
    demand_qty    REAL NOT NULL DEFAULT 0 CHECK (demand_qty >= 0),
    UNIQUE (material_id, demand_date)
);

CREATE INDEX idx_kadss_demand_history_date ON t_kadss_demand_history (demand_date);

-- ---------------------------------------------------------------
-- 结果表
-- ---------------------------------------------------------------

-- ABC-XYZ 分层结果
-- abc_class / xyz_class / combined_class 允许为空：样本不足的物料不强行分层（PRD US-2）
CREATE TABLE t_kadss_abc_xyz_result (
    run_id             TEXT NOT NULL,
    material_id        TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    abc_class          TEXT CHECK (abc_class IS NULL OR abc_class IN ('A', 'B', 'C')),
    xyz_class          TEXT CHECK (xyz_class IS NULL OR xyz_class IN ('X', 'Y', 'Z')),
    combined_class     TEXT,
    cv                 REAL NOT NULL DEFAULT 0 CHECK (cv >= 0),
    amount_share       REAL NOT NULL DEFAULT 0 CHECK (amount_share >= 0 AND amount_share <= 1),
    confidence_level   TEXT NOT NULL DEFAULT 'high'
                       CHECK (confidence_level IN ('high', 'low')),
    data_status        TEXT NOT NULL DEFAULT 'ok'
                       CHECK (data_status IN ('ok', 'insufficient_data')),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, material_id)
);

-- 动态安全库存结果（按服务水平分档）
CREATE TABLE t_kadss_safety_stock_result (
    run_id              TEXT NOT NULL,
    material_id         TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    service_level       INTEGER NOT NULL CHECK (service_level BETWEEN 80 AND 99),
    z_value             REAL NOT NULL CHECK (z_value > 0),
    lt_total            REAL NOT NULL CHECK (lt_total >= 0),
    lt_purchase         REAL NOT NULL DEFAULT 0 CHECK (lt_purchase >= 0),
    lt_production       REAL NOT NULL DEFAULT 0 CHECK (lt_production >= 0),
    lt_inspection       REAL NOT NULL DEFAULT 0 CHECK (lt_inspection >= 0),
    lt_logistics        REAL NOT NULL DEFAULT 0 CHECK (lt_logistics >= 0),
    lt_avg              REAL NOT NULL CHECK (lt_avg >= 0),
    sigma_lt            REAL NOT NULL DEFAULT 0 CHECK (sigma_lt >= 0),
    avg_demand          REAL NOT NULL CHECK (avg_demand >= 0),
    sigma_demand        REAL NOT NULL CHECK (sigma_demand >= 0),
    dynamic_ss          REAL NOT NULL CHECK (dynamic_ss >= 0),
    fixed_ss            REAL NOT NULL CHECK (fixed_ss >= 0),
    delta               REAL NOT NULL DEFAULT 0,
    suggested_qty       REAL NOT NULL DEFAULT 0 CHECK (suggested_qty >= 0),
    suggested_amount    REAL NOT NULL DEFAULT 0 CHECK (suggested_amount >= 0),
    lt_source           TEXT NOT NULL DEFAULT 'history'
                        CHECK (lt_source IN ('history', 'master_data')),
    confidence_level    TEXT NOT NULL DEFAULT 'high'
                        CHECK (confidence_level IN ('high', 'low')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, material_id, service_level)
);

-- BOM 齐套检查结果（订单 × 物料）
-- strategy：baseline 为默认口径；fixed / dynamic 为成本仿真所需的两种安全库存策略口径
CREATE TABLE t_kadss_kitting_result (
    run_id            TEXT NOT NULL,
    order_id          TEXT NOT NULL REFERENCES t_kadss_demand_order (order_id),
    material_id       TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    service_level     INTEGER NOT NULL CHECK (service_level BETWEEN 80 AND 99),
    strategy          TEXT NOT NULL DEFAULT 'baseline'
                      CHECK (strategy IN ('baseline', 'fixed', 'dynamic')),
    req_qty           REAL NOT NULL CHECK (req_qty >= 0),
    on_hand           REAL NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    in_transit        REAL NOT NULL DEFAULT 0 CHECK (in_transit >= 0),
    allocated         REAL NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    quality_hold      REAL NOT NULL DEFAULT 0 CHECK (quality_hold >= 0),
    safety_stock_deduct REAL NOT NULL DEFAULT 0 CHECK (safety_stock_deduct >= 0),
    available_qty     REAL NOT NULL,
    gap_qty           REAL NOT NULL DEFAULT 0 CHECK (gap_qty >= 0),
    gap_amount        REAL NOT NULL DEFAULT 0 CHECK (gap_amount >= 0),
    available_scope   TEXT NOT NULL DEFAULT 'on_hand+in_transit-allocated',
    has_substitute    INTEGER NOT NULL DEFAULT 0 CHECK (has_substitute IN (0, 1)),
    confidence_level  TEXT NOT NULL DEFAULT 'high'
                      CHECK (confidence_level IN ('high', 'low')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, order_id, material_id, service_level, strategy)
);

CREATE INDEX idx_kadss_kitting_order ON t_kadss_kitting_result (run_id, strategy, order_id);
CREATE INDEX idx_kadss_kitting_material ON t_kadss_kitting_result (run_id, material_id);

-- 订单齐套概率结果
CREATE TABLE t_kadss_kitting_probability (
    run_id                  TEXT NOT NULL,
    order_id                TEXT NOT NULL REFERENCES t_kadss_demand_order (order_id),
    kitting_status          TEXT NOT NULL DEFAULT '不齐套'
                            CHECK (kitting_status IN ('齐套', '不齐套', '数据不足')),
    on_time_probability     REAL CHECK (on_time_probability IS NULL OR (on_time_probability >= 0 AND on_time_probability <= 1)),
    risk_level              TEXT NOT NULL
                            CHECK (risk_level IN ('低风险', '中风险', '高风险', '数据不足')),
    gap_rows                INTEGER NOT NULL DEFAULT 0 CHECK (gap_rows >= 0),
    gap_amount              REAL NOT NULL DEFAULT 0 CHECK (gap_amount >= 0),
    sim_count               INTEGER NOT NULL DEFAULT 100 CHECK (sim_count BETWEEN 1 AND 100000),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, order_id)
);

-- 库存成本仿真结果（固定 vs 动态安全库存）
CREATE TABLE t_kadss_cost_simulation_result (
    run_id                 TEXT NOT NULL,
    material_id            TEXT NOT NULL REFERENCES t_kadss_material (material_id),
    strategy               TEXT NOT NULL CHECK (strategy IN ('fixed', 'dynamic')),
    service_level          INTEGER NOT NULL CHECK (service_level BETWEEN 80 AND 99),
    penalty_factor         REAL NOT NULL CHECK (penalty_factor > 0),
    avg_inventory_qty      REAL NOT NULL DEFAULT 0 CHECK (avg_inventory_qty >= 0),
    avg_inventory_value    REAL NOT NULL DEFAULT 0 CHECK (avg_inventory_value >= 0),
    holding_cost           REAL NOT NULL DEFAULT 0 CHECK (holding_cost >= 0),
    shortage_qty           REAL NOT NULL DEFAULT 0 CHECK (shortage_qty >= 0),
    shortage_days          INTEGER NOT NULL DEFAULT 0 CHECK (shortage_days >= 0),
    shortage_cost          REAL NOT NULL DEFAULT 0 CHECK (shortage_cost >= 0),
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, material_id, strategy, service_level)
);
