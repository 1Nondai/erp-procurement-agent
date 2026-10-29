-- 数据自检：这几条如果都能对上，说明种子数据是可用的（写进 db/verify.sql，评审者也能跑）
\echo '--- 1. 各表行数 ---'
SELECT 'supplier' AS t, count(*) FROM erp.supplier
UNION ALL SELECT 'material', count(*) FROM erp.material
UNION ALL SELECT 'material_supplier', count(*) FROM erp.material_supplier
UNION ALL SELECT 'project', count(*) FROM erp.project
UNION ALL SELECT 'purchase_order', count(*) FROM erp.purchase_order
UNION ALL SELECT 'purchase_order_detail', count(*) FROM erp.purchase_order_detail
UNION ALL SELECT 'inventory_movement', count(*) FROM erp.inventory_movement;

\echo '--- 2. 订单总额 = 明细合计（取消单除外）---'
SELECT count(*) AS inconsistent_orders
FROM erp.purchase_order o
WHERE o.status <> 5
  AND o.total_amount <> COALESCE((SELECT SUM(subtotal) FROM erp.purchase_order_detail d WHERE d.order_id = o.id), 0);

\echo '--- 3. 比价：理化板 / 冷轧板 1.0mm ---'
SELECT material_name, supplier_name, quoted_price, ref_price, diff_pct, is_preferred, is_expired
FROM erp.v_material_price_compare
WHERE material_name IN ('理化板台面', '冷轧钢板')
ORDER BY material_name, quoted_price;

\echo '--- 4. 库存预警 ---'
SELECT material_name, current_stock, safety_stock, shortage, warning_level
FROM erp.v_stock_warning;

\echo '--- 5. 按项目归集的采购额 ---'
SELECT p.code, p.name, count(o.id) AS orders, COALESCE(SUM(o.total_amount), 0) AS amount
FROM erp.project p
LEFT JOIN erp.purchase_order o ON o.project_id = p.id AND o.status <> 5
GROUP BY p.code, p.name
ORDER BY amount DESC;
