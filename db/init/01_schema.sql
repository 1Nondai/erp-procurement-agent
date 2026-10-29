-- ============================================================
-- 明远实验装备 · 采购业务库 schema
-- ------------------------------------------------------------
-- 设计要点：
--   1. 业务数据全部落 PostgreSQL，独立 schema `erp`，与 LangGraph 的
--      checkpoint / store 表共用同一个库但互不干扰（前缀隔离，便于备份与授权）；
--   2. 金额统一 numeric(12,2)，禁止 float —— 浮点误差会直接变成对不上账的采购单；
--   3. 订单状态用 smallint + CHECK 约束而不是 enum：改状态值不用改类型、迁移简单；
--   4. 比价不另建冗余表：物料-供应商报价是"多对多 + 有效期"，
--      单表 UNIQUE(material_id, supplier_id) 足够，历史价由 price_date 保留；
--   5. 订单明细总价由后端计算并落库（subtotal），查询侧不再重复算，
--      避免"库里一套、报表另一套"。
-- ============================================================

CREATE SCHEMA IF NOT EXISTS erp;
COMMENT ON SCHEMA erp IS '明远实验装备 · ERP 采购业务库';

-- ------------------------------------------------------------
-- 供应商
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.supplier (
    id                serial PRIMARY KEY,
    code              varchar(16)  NOT NULL UNIQUE,          -- SUP-001
    name              varchar(128) NOT NULL,
    short_name        varchar(32)  NOT NULL,
    category          varchar(16)  NOT NULL,                 -- 主供品类：台面/钢材/五金/玻璃/水暖/电气/通风/涂装/辅材
    contact           varchar(32),
    phone             varchar(32),
    email             varchar(64),
    address           varchar(256),
    -- QSDC 四维评分（0-10）：Quality / Service / Delivery / Cost
    quality_score     numeric(3,1) NOT NULL DEFAULT 8.0 CHECK (quality_score BETWEEN 0 AND 10),
    service_score     numeric(3,1) NOT NULL DEFAULT 8.0 CHECK (service_score BETWEEN 0 AND 10),
    delivery_score    numeric(3,1) NOT NULL DEFAULT 8.0 CHECK (delivery_score BETWEEN 0 AND 10),
    cost_score        numeric(3,1) NOT NULL DEFAULT 8.0 CHECK (cost_score BETWEEN 0 AND 10),
    credit_rating     char(1)      NOT NULL DEFAULT 'B' CHECK (credit_rating IN ('A', 'B', 'C', 'D')),
    avg_delivery_days integer      NOT NULL DEFAULT 15,
    payment_terms     varchar(64)  NOT NULL DEFAULT '月结30天',
    status            smallint     NOT NULL DEFAULT 1,       -- 1=合作中 0=停用
    created_at        timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON COLUMN erp.supplier.credit_rating IS '信用等级：A 优先合作 / B 正常 / C 需预付款 / D 黑名单';
COMMENT ON COLUMN erp.supplier.payment_terms IS '账期约定，例如 月结30天 / 预付30%';

-- ------------------------------------------------------------
-- 物料（材料 / 外购件）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.material (
    id              serial PRIMARY KEY,
    code            varchar(16)   NOT NULL UNIQUE,           -- MAT-0001
    name            varchar(128)  NOT NULL,
    spec            varchar(160)  NOT NULL,                  -- 规格型号，如 1220×2440×13mm
    category        varchar(16)   NOT NULL,
    unit            varchar(8)    NOT NULL,                  -- 张/平方米/米/套/个/副/盒/支/kg
    brand           varchar(64),
    ref_price       numeric(12,2) NOT NULL,                  -- 参考单价（最近一次成交或市场价）
    safety_stock    numeric(12,2) NOT NULL DEFAULT 0,        -- 安全库存
    current_stock   numeric(12,2) NOT NULL DEFAULT 0,        -- 当前库存
    lead_time_days  integer       NOT NULL DEFAULT 15,       -- 采购提前期（天）
    status          smallint      NOT NULL DEFAULT 1,
    created_at      timestamptz   NOT NULL DEFAULT now()
);
COMMENT ON TABLE erp.material IS '物料主数据：实验台/通风柜的板材、钢材、五金、水暖、电气、涂装与辅材';

-- ------------------------------------------------------------
-- 物料-供应商报价（支撑比价）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.material_supplier (
    id            serial PRIMARY KEY,
    material_id   integer       NOT NULL REFERENCES erp.material(id) ON DELETE CASCADE,
    supplier_id   integer       NOT NULL REFERENCES erp.supplier(id) ON DELETE CASCADE,
    quoted_price  numeric(12,2) NOT NULL,
    moq           numeric(12,2) NOT NULL DEFAULT 1,          -- 最小起订量
    currency      char(3)       NOT NULL DEFAULT 'CNY',
    price_date    date          NOT NULL,
    valid_until   date,                                      -- 报价有效期（进口板材常为 15 天）
    is_preferred  boolean       NOT NULL DEFAULT false,      -- 优先供应商（同价优先下单）
    UNIQUE (material_id, supplier_id)
);
COMMENT ON TABLE erp.material_supplier IS '一家物料可有多家供应商报价；is_preferred 标记首选供应商';

-- ------------------------------------------------------------
-- 项目（工程单）：采购按项目归集，便于算单项目的材料成本
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.project (
    id              serial PRIMARY KEY,
    code            varchar(24)   NOT NULL UNIQUE,           -- LHK-2026-001
    name            varchar(128)  NOT NULL,
    customer        varchar(128)  NOT NULL,
    project_type    varchar(32)   NOT NULL,                  -- 整体实验室 / 实验台 / 通风柜 / 药品柜
    contract_amount numeric(14,2),
    status          smallint      NOT NULL DEFAULT 1,        -- 1=进行中 2=已交付 3=已关闭
    start_date      date,
    due_date        date
);

-- ------------------------------------------------------------
-- 采购订单
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.purchase_order (
    id                     serial PRIMARY KEY,
    order_no               varchar(32)   NOT NULL UNIQUE,
    supplier_id            integer       NOT NULL REFERENCES erp.supplier(id),
    project_id             integer       REFERENCES erp.project(id),   -- 可为空：通用备料
    status                 smallint      NOT NULL DEFAULT 1
        CHECK (status BETWEEN 1 AND 5),
    total_amount           numeric(14,2) NOT NULL DEFAULT 0,
    order_time             timestamptz   NOT NULL DEFAULT now(),
    expected_delivery_date date,
    actual_delivery_date   date,
    created_by             varchar(64)   NOT NULL DEFAULT 'system',
    remark                 text,
    created_at             timestamptz   NOT NULL DEFAULT now()
);
COMMENT ON COLUMN erp.purchase_order.status IS '1=待审核 2=已审核 3=采购中 4=已入库 5=已取消';

-- ------------------------------------------------------------
-- 采购订单明细
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.purchase_order_detail (
    id          serial PRIMARY KEY,
    order_id    integer       NOT NULL REFERENCES erp.purchase_order(id) ON DELETE CASCADE,
    material_id integer       NOT NULL REFERENCES erp.material(id),
    quantity    numeric(12,2) NOT NULL CHECK (quantity > 0),
    unit_price  numeric(12,2) NOT NULL CHECK (unit_price >= 0),
    subtotal    numeric(14,2) NOT NULL,
    remark      varchar(256)
);
COMMENT ON COLUMN erp.purchase_order_detail.subtotal IS '落库时由后端按 quantity*unit_price 计算，查询侧不再重算';

-- ------------------------------------------------------------
-- 出入库流水（库存预警之外，让"库存"这件事有账可查）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS erp.inventory_movement (
    id          serial PRIMARY KEY,
    material_id integer       NOT NULL REFERENCES erp.material(id) ON DELETE CASCADE,
    move_type   char(1)       NOT NULL CHECK (move_type IN ('I', 'O', 'A')),  -- 入库/出库/盘点调整
    quantity    numeric(12,2) NOT NULL,
    ref_type    varchar(16),                                 -- PO=采购入库 PR=生产领用
    ref_no      varchar(32),
    moved_at    timestamptz   NOT NULL DEFAULT now(),
    remark      varchar(256)
);

-- ------------------------------------------------------------
-- 索引：按查询路径建，不按"看起来重要"建
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_material_category      ON erp.material (category);
CREATE INDEX IF NOT EXISTS idx_material_name          ON erp.material (name);
CREATE INDEX IF NOT EXISTS idx_ms_material            ON erp.material_supplier (material_id);
CREATE INDEX IF NOT EXISTS idx_ms_supplier            ON erp.material_supplier (supplier_id);
CREATE INDEX IF NOT EXISTS idx_po_supplier            ON erp.purchase_order (supplier_id);
CREATE INDEX IF NOT EXISTS idx_po_project             ON erp.purchase_order (project_id);
CREATE INDEX IF NOT EXISTS idx_po_order_time          ON erp.purchase_order (order_time DESC);
CREATE INDEX IF NOT EXISTS idx_pod_order              ON erp.purchase_order_detail (order_id);
CREATE INDEX IF NOT EXISTS idx_pod_material           ON erp.purchase_order_detail (material_id);
CREATE INDEX IF NOT EXISTS idx_movement_material_time ON erp.inventory_movement (material_id, moved_at DESC);

-- ------------------------------------------------------------
-- 视图：把"比价"和"库存预警"的口径固化成一处，避免每个调用方各写一套
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW erp.v_material_price_compare AS
SELECT
    m.id                                        AS material_id,
    m.code,
    m.name                                      AS material_name,
    m.spec,
    m.unit,
    m.ref_price,
    s.id                                        AS supplier_id,
    s.short_name                                AS supplier_name,
    s.credit_rating,
    ms.quoted_price,
    ms.moq,
    ms.price_date,
    ms.valid_until,
    ms.is_preferred,
    ms.quoted_price - m.ref_price               AS diff_to_ref,
    ROUND((ms.quoted_price - m.ref_price) / NULLIF(m.ref_price, 0) * 100, 2) AS diff_pct,
    (ms.valid_until IS NOT NULL AND ms.valid_until < CURRENT_DATE) AS is_expired
FROM erp.material m
JOIN erp.material_supplier ms ON ms.material_id = m.id
JOIN erp.supplier s           ON s.id = ms.supplier_id
WHERE s.status = 1 AND m.status = 1;

CREATE OR REPLACE VIEW erp.v_stock_warning AS
SELECT
    m.id            AS material_id,
    m.code,
    m.name          AS material_name,
    m.spec,
    m.category,
    m.unit,
    m.current_stock,
    m.safety_stock,
    m.safety_stock - m.current_stock                       AS shortage,
    m.lead_time_days,
    ROUND((m.current_stock / NULLIF(m.safety_stock, 0)) * 100, 1) AS stock_ratio,
    CASE
        WHEN m.current_stock <= 0                    THEN 'OUT_OF_STOCK'
        WHEN m.current_stock < m.safety_stock * 0.5  THEN 'CRITICAL'
        ELSE 'LOW'
    END AS warning_level
FROM erp.material m
WHERE m.status = 1 AND m.current_stock < m.safety_stock
ORDER BY (m.safety_stock - m.current_stock) DESC;


-- ============================================================
-- 运维 schema：沙箱注册表
-- ------------------------------------------------------------
-- user_id → sandbox_id 的绑定关系。注意它**不是沙箱的真相**，只是索引：
-- 读到的 id 连不上（容器被清、机器重启）时会走"重建"路径并覆盖绑定，
-- 宁可多建一个沙箱，也不让用户卡住。
-- sandbox_registry.init() 会以 IF NOT EXISTS 幂等地建同样的表，
-- 这里保留一份是为了让"库里有哪些表"一眼可见。
-- ============================================================
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.sandbox_registry (
    user_id    TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sandbox_registry_sandbox
    ON ops.sandbox_registry (sandbox_id);
