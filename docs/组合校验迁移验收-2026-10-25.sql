-- 组合校验迁移验收，只读查询版；目标 MySQL 5.7。
-- 本文件没有 DDL/DML，不执行迁移、不清洗/删除数据、不重导历史状态。
-- 按 A→B→C 分块运行；不要跨过结构检查直接全文执行。
-- A/B：迁移前、迁移后、应用重启后各运行一次，保存完整结果及时间/代码commit。
-- C：仅确认0081已完成且相关列存在后运行；避免在迁移前引用不存在的id。
-- D：保存账务摘要和完整行；COUNT/SUM相等不证明无丢行/无字段变化。
-- E：在停写副本中，将已验证备份恢复到review_before、验收库命名review_after后核对。
--    如使用其它库名，只改变量；本文件不创建/恢复这些库，不执行动态SQL。
--    生成的SELECT需逐条执行并保存；不可把“成功生成SQL”当作已完成对账。

-- A0【必须首先运行】核对目标库、服务器、端口、版本、SQL_MODE。
-- 预期：与变更单一致；本轮仅允许新建专用副本codex_combination_<suffix>，
-- 既有gpfx_validation不能默认当一次性测试库，正式库仅在授权发布窗口验收。
-- 不一致立即停止后续操作。客户端预先指定连接库；本脚本不执行USE切库。
SELECT DATABASE() AS actual_database, @@hostname AS server_host,
       @@port AS server_port, @@version AS mysql_version, @@server_uuid AS server_uuid,
       @@tx_isolation AS isolation_level, @@default_storage_engine AS default_storage_engine,
       @@sql_mode AS sql_mode, @@session.time_zone AS session_time_zone,
       UTC_TIMESTAMP(6) AS captured_at_utc;
SELECT version_num FROM alembic_version;
-- 本次代码目标revision：wps_0024_083_combination_contracts；不以该值代替结构检查。

-- A1 结构快照：全库列，不只检查“多了id/改了DATETIME”。前后保存并比较。
SELECT TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
       COLUMN_DEFAULT, EXTRA, COLUMN_KEY, DATETIME_PRECISION,
       CHARACTER_SET_NAME, COLLATION_NAME, GENERATION_EXPRESSION
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, ORDINAL_POSITION;
SELECT TABLE_NAME, ENGINE, TABLE_COLLATION
FROM information_schema.TABLES
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'
ORDER BY TABLE_NAME;
-- 预期：涉及交易/审计/幂等的表为InnoDB；不得因重建丢表/旧列/精度/FK/业务默认。

-- A2 唯一性必须验证列组合和唯一属性，不能仅按索引名存在即通过。
SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME,
       SUB_PART, INDEX_TYPE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA=DATABASE()
  AND TABLE_NAME IN ('portfolio_members','idempotency_records',
                    'security_status_daily','sim_orders','scores')
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
-- 修订后0079/0083保留索引名idx_portfolio_members_active，但必须是三列条件唯一。
-- 两次启动后仍应看到唯一(portfolio_id,symbol_id,active_member_slot)，
-- 并核实GENERATION_EXPRESSION为effective_to IS NULL时返回1，否则NULL。
-- idempotency_records：id为自增PRIMARY；idempotency_key另有唯一索引，NON_UNIQUE=0。

-- A3 外键结构快照；MySQL5.7不应依赖CHECK来强制枚举取值。
SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME,
       k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
       r.UPDATE_RULE, r.DELETE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r
  ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME
 AND r.TABLE_NAME=k.TABLE_NAME
WHERE k.TABLE_SCHEMA=DATABASE() AND k.REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY k.TABLE_NAME,k.CONSTRAINT_NAME,k.ORDINAL_POSITION;

-- A4 幂等表分阶段结构检查：迁移前缺id可接受，迁移后必须满足完整契约。
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='idempotency_records'
ORDER BY ORDINAL_POSITION;
-- 修订后0081/0083必须保留resource_type/status旧列，并补默认和服务双写契约。
-- 预期旧新resource/entity映射可追溯；旧列不可删，不把空表或单次ORM SELECT当写入通过。
-- 新entity_type/entity_id/expire_at/executed_at需有可追溯映射，不能用空串/零日期假补。
-- 旧request_hash/response_json的nullable/default须与新业务写入契约核对。
SELECT COUNT(*) AS idempotency_rows_before_or_after FROM idempotency_records;
-- 0行只能说明这个时点没有存量，不证明迁移可处理非空表；发布必须停写防竞态。

-- A5 当前业务写入契约：旧DDL额外必填列不能被metadata建库测试掩盖。
-- review_status迁移后允许NULL，不能为普通手动下单伪造APPROVED；旧审核值逐行保持。
SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sim_orders' AND COLUMN_NAME='review_status';
-- 以下是待核对清单，不要求0行：正常ORM必填列仍应保留约束。
-- 必须对照当前写入模型，识别已停用但仍NOT NULL、无默认的旧额外列。
-- 仅对已确认停用列做保留旧值的前向补偿；不能删列、填0或编造关联账户。
SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,COLUMN_DEFAULT,EXTRA
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE()
  AND TABLE_NAME IN ('sim_orders','sim_trades','cash_ledger','positions','portfolios',
                    'portfolio_members','scores','strategy_execution_snapshots',
                    'backtest_runs','backtest_trades','backtest_execution_fills')
  AND IS_NULLABLE='NO' AND COLUMN_DEFAULT IS NULL AND EXTRA NOT LIKE '%auto_increment%'
ORDER BY TABLE_NAME,ORDINAL_POSITION;

-- B1 组合枚举精确检查：BINARY排除大小写不敏感collation与尾空格掩盖。
-- 预期0行；历史simulation等旧值先列入人工映射清单，不直接删除组合。
SELECT id,name,account_type,asset_scope,status_score,status_data,status_model,status_reconciliation
FROM portfolios
WHERE account_type IS NULL OR BINARY account_type NOT IN ('simulated','manual')
   OR asset_scope IS NULL OR BINARY asset_scope NOT IN ('stock','etf','mixed')
   OR status_score IS NULL OR BINARY status_score NOT IN
      ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED','MODEL_INACTIVE','RECONCILIATION_BLOCKED')
   OR status_data IS NULL OR BINARY status_data NOT IN
      ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED','MODEL_INACTIVE','RECONCILIATION_BLOCKED')
   OR status_model IS NULL OR BINARY status_model NOT IN
      ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED','MODEL_INACTIVE','RECONCILIATION_BLOCKED')
   OR status_reconciliation IS NULL OR BINARY status_reconciliation NOT IN
      ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED','MODEL_INACTIVE','RECONCILIATION_BLOCKED');
SELECT id,name,total_capital,investable_ratio,cash_reserve_ratio,auto_schedule_hour,auto_schedule_minute
FROM portfolios
WHERE name IS NULL OR LENGTH(TRIM(name))=0 OR total_capital IS NULL
   OR investable_ratio IS NULL OR cash_reserve_ratio IS NULL
   OR total_capital<0 OR investable_ratio<0 OR investable_ratio>1
   OR cash_reserve_ratio<0 OR cash_reserve_ratio>1
   OR auto_schedule_hour IS NULL OR auto_schedule_hour<20 OR auto_schedule_hour>23
   OR auto_schedule_minute IS NULL OR auto_schedule_minute<0 OR auto_schedule_minute>59;
-- 零资金合法；比例各自0..1，本轮不增加未经确认的总和约束。
-- 名称、币种、佣金、单票上限、benchmark长度及TEXT字节限制仍须API边界验收。

-- B2 成员状态、来源、执行模式和SCD2一致性；预期0行。
SELECT id,portfolio_id,symbol_id,status,execution_mode,source_type,effective_from,effective_to
FROM portfolio_members
WHERE portfolio_id IS NULL OR symbol_id IS NULL
   OR status IS NULL OR BINARY status NOT IN ('active','paused','archived')
   OR execution_mode IS NULL OR BINARY execution_mode NOT IN ('manual','confirm','auto')
   OR source_type IS NULL OR BINARY source_type NOT IN ('legacy_position','candidate','observation','manual')
   OR effective_from IS NULL
   OR (BINARY status='archived' AND effective_to IS NULL)
   OR (BINARY status IN ('active','paused') AND effective_to IS NOT NULL)
   OR effective_to<effective_from;
-- confirm属于存储枚举，但当前API能力未开放；不能以存储合法推断可启用。
SELECT portfolio_id,symbol_id,COUNT(*) AS current_row_count
FROM portfolio_members WHERE effective_to IS NULL
GROUP BY portfolio_id,symbol_id HAVING COUNT(*)>1;
-- SCD2区间按[effective_from,effective_to)检查；首尾相等不算重叠。
-- 若实现使用闭区间，则应另按实际消费代码复核边界，不能自行删除历史。
SELECT a.portfolio_id,a.symbol_id,a.id AS row_a,b.id AS row_b,
       a.effective_from AS a_from,a.effective_to AS a_to,
       b.effective_from AS b_from,b.effective_to AS b_to
FROM portfolio_members a JOIN portfolio_members b
  ON a.portfolio_id=b.portfolio_id AND a.symbol_id=b.symbol_id AND a.id<b.id
WHERE (a.effective_to IS NULL OR b.effective_from<a.effective_to)
  AND (b.effective_to IS NULL OR a.effective_from<b.effective_to);

-- B3 外键孤儿与订单归因，预期0行。
SELECT 'member' AS entity,m.id AS entity_id FROM portfolio_members m
LEFT JOIN portfolios p ON p.id=m.portfolio_id LEFT JOIN symbols s ON s.id=m.symbol_id
WHERE p.id IS NULL OR s.id IS NULL;
SELECT 'order' AS entity,o.id AS entity_id FROM sim_orders o
LEFT JOIN portfolios p ON p.id=o.portfolio_id LEFT JOIN symbols s ON s.id=o.symbol_id
LEFT JOIN portfolio_members m ON m.id=o.member_id
WHERE p.id IS NULL OR s.id IS NULL OR (o.member_id IS NOT NULL AND m.id IS NULL)
   OR (m.id IS NOT NULL AND (m.portfolio_id<>o.portfolio_id OR m.symbol_id<>o.symbol_id));
SELECT 'trade' AS entity,t.id AS entity_id FROM sim_trades t
LEFT JOIN sim_orders o ON o.id=t.order_id LEFT JOIN portfolios p ON p.id=t.portfolio_id
LEFT JOIN symbols s ON s.id=t.symbol_id
WHERE o.id IS NULL OR p.id IS NULL OR s.id IS NULL
   OR o.portfolio_id<>t.portfolio_id OR o.symbol_id<>t.symbol_id;
SELECT 'ledger' AS entity,l.id AS entity_id FROM cash_ledger l
LEFT JOIN portfolios p ON p.id=l.portfolio_id WHERE p.id IS NULL;
SELECT 'security_status' AS entity,d.id AS entity_id FROM security_status_daily d
LEFT JOIN symbols s ON s.id=d.symbol_id WHERE s.id IS NULL;

-- B4 历史状态结构/疑似时间异常。微秒为0不必然是坏数据，只能列入与原回执核验清单。
SELECT COLUMN_TYPE,DATETIME_PRECISION,IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='security_status_daily'
  AND COLUMN_NAME='source_updated_at';
-- 0082之后预期DATETIME_PRECISION=6；原秒级历史不会被扩精度自动恢复。
SELECT id,symbol_id,trade_date,status_source,source_updated_at,as_of_batch_id
FROM security_status_daily
WHERE symbol_id IS NULL OR trade_date IS NULL OR is_st IS NULL OR is_suspended IS NULL
   OR is_delisting_period IS NULL OR is_listed IS NULL
   OR status_source IS NULL OR LENGTH(TRIM(status_source))=0
   OR source_updated_at>UTC_TIMESTAMP(6)
   OR (as_of_batch_id IS NOT NULL AND
       (source_updated_at IS NULL OR MICROSECOND(source_updated_at)=0));
SELECT symbol_id,trade_date,COUNT(*) AS duplicates FROM security_status_daily
GROUP BY symbol_id,trade_date HAVING COUNT(*)>1;
-- 必须额外通过应用逐行验证_same_source、status_fingerprint与执行约束回执；
-- SQL无法仅凭DATETIME(6)、非空、微秒非零证明来源真实/涨跌停正确。

-- C【仅0081后且A4确认列齐全才执行】幂等PK/字段/JSON检查；预期0行。
SELECT id,idempotency_key,entity_type,request_hash,correlation_id,executed_at
FROM idempotency_records
WHERE id IS NULL OR idempotency_key IS NULL OR LENGTH(idempotency_key)=0
   OR entity_type IS NULL OR LENGTH(TRIM(entity_type))=0
   OR request_hash IS NULL OR LENGTH(request_hash)=0
   OR correlation_id IS NULL OR LENGTH(correlation_id)=0
   OR response_json IS NULL OR NOT JSON_VALID(response_json)
   OR executed_at IS NULL;
SELECT id,COUNT(*) AS duplicates FROM idempotency_records GROUP BY id HAVING COUNT(*)>1;
SELECT idempotency_key,COUNT(*) AS duplicates FROM idempotency_records
GROUP BY idempotency_key HAVING COUNT(*)>1;
-- A4显示resource_type/resource_id/expires_at仍存在时，另人工比对旧字段→新字段映射。
-- 不在本块直接引用旧字段，因为SQLite空表重建等路径会移除它们。

-- D 资金/订单摘要：前后两份均保存，只用于定位变化，绝不作为完整性证明。
SELECT portfolio_id,COUNT(*) AS ledger_rows,MIN(id) AS min_id,MAX(id) AS max_id,
       SUM(amount) AS sum_amount FROM cash_ledger GROUP BY portfolio_id ORDER BY portfolio_id;
SELECT portfolio_id,side,status,COUNT(*) AS order_rows,
       SUM(quantity) AS requested_quantity,SUM(filled_quantity) AS filled_quantity,
       SUM(filled_amount) AS filled_amount,SUM(fee) AS fees
FROM sim_orders GROUP BY portfolio_id,side,status ORDER BY portfolio_id,side,status;
SELECT portfolio_id,side,COUNT(*) AS trade_rows,SUM(quantity) AS quantity,
       SUM(amount) AS amount,SUM(fee) AS fees
FROM sim_trades GROUP BY portfolio_id,side ORDER BY portfolio_id,side;
SELECT portfolio_id,symbol_id,quantity,avg_cost FROM positions ORDER BY portfolio_id,symbol_id,id;
SELECT id FROM cash_ledger WHERE portfolio_id IS NULL OR amount IS NULL OR balance_after IS NULL;
SELECT id FROM sim_orders WHERE portfolio_id IS NULL OR symbol_id IS NULL OR side IS NULL
   OR quantity IS NULL OR submitted_price IS NULL OR status IS NULL;
SELECT id FROM sim_trades WHERE portfolio_id IS NULL OR symbol_id IS NULL OR order_id IS NULL
   OR quantity IS NULL OR price IS NULL OR amount IS NULL;

-- D2 保存完整行（无LIMIT）到受控证据文件；查询可能较大，应在停写副本运行。
SELECT * FROM portfolio_members ORDER BY id;
SELECT * FROM portfolios ORDER BY id;
SELECT * FROM idempotency_records ORDER BY idempotency_key;
SELECT * FROM security_status_daily ORDER BY id;
SELECT * FROM cash_ledger ORDER BY id;
SELECT * FROM sim_orders ORDER BY id;
SELECT * FROM sim_trades ORDER BY id;
-- 0080可触及全库；审计、基线、快照、候选、评分等其余被触及表也必须完整导出。

-- E 在隔离副本核对备份基线；仅SET会话变量，后续均只生成SELECT。
SET @before_schema='review_before';
SET @after_schema='review_after';
-- 预期这两库已由独立恢复演练建立且迁移区间停写；本文件不进行恢复。
SELECT TABLE_SCHEMA,COUNT(*) AS tables_present FROM information_schema.TABLES
WHERE TABLE_SCHEMA IN (@before_schema,@after_schema) GROUP BY TABLE_SCHEMA;
-- E1 旧表/旧列丢失：预期0行。新增列需另验有据回填。
SELECT b.TABLE_NAME AS missing_table
FROM information_schema.TABLES b LEFT JOIN information_schema.TABLES a
  ON a.TABLE_SCHEMA=@after_schema AND a.TABLE_NAME=b.TABLE_NAME
WHERE b.TABLE_SCHEMA=@before_schema AND b.TABLE_TYPE='BASE TABLE' AND a.TABLE_NAME IS NULL;
SELECT b.TABLE_NAME,b.COLUMN_NAME AS missing_old_column
FROM information_schema.COLUMNS b LEFT JOIN information_schema.COLUMNS a
  ON a.TABLE_SCHEMA=@after_schema AND a.TABLE_NAME=b.TABLE_NAME AND a.COLUMN_NAME=b.COLUMN_NAME
WHERE b.TABLE_SCHEMA=@before_schema AND a.COLUMN_NAME IS NULL;

-- E2 生成丢行/额外行核对SELECT：支持核心表单列id以及幂等旧键。
-- 必须实际执行输出的每条SELECT；停写迁移预期全0行。
SELECT CONCAT('SELECT b.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '` AS missing_key FROM `',REPLACE(@before_schema,'`','``'),'`.`',REPLACE(b.TABLE_NAME,'`','``'),
              '` b LEFT JOIN `',REPLACE(@after_schema,'`','``'),'`.`',REPLACE(b.TABLE_NAME,'`','``'),
              '` a ON a.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '`=b.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '` WHERE a.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,'` IS NULL;') AS missing_rows_sql,
       CONCAT('SELECT a.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '` AS added_key FROM `',REPLACE(@after_schema,'`','``'),'`.`',REPLACE(b.TABLE_NAME,'`','``'),
              '` a LEFT JOIN `',REPLACE(@before_schema,'`','``'),'`.`',REPLACE(b.TABLE_NAME,'`','``'),
              '` b ON b.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '`=a.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '` WHERE b.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,'` IS NULL;') AS added_rows_sql
FROM information_schema.TABLES b
WHERE b.TABLE_SCHEMA=@before_schema AND b.TABLE_TYPE='BASE TABLE'
  AND b.TABLE_NAME IN ('portfolios','portfolio_members','portfolio_candidates','idempotency_records',
                      'security_status_daily','cash_ledger','sim_orders','sim_trades','positions',
                      'decision_runs','data_governance_audit_events','scores');

-- E3 为上列核心表的每一个旧字段生成NULL安全比较，无GROUP_CONCAT长度截断风险。
-- 文本字段BINARY比较避免大小写/尾空格丢失未被发现；数值/日期按原类型<=>比较。
-- 新自增id不在旧表中，因此不会错误要求它与不存在的旧id一致。
-- 每条输出均须实际执行，预期0行；旧字段有批准变更时需逐键逐字段白名单及原始证据。
SELECT b.TABLE_NAME,b.COLUMN_NAME,
       CONCAT('SELECT b.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '` AS changed_key FROM `',REPLACE(@before_schema,'`','``'),'`.`',REPLACE(b.TABLE_NAME,'`','``'),
              '` b JOIN `',REPLACE(@after_schema,'`','``'),'`.`',REPLACE(b.TABLE_NAME,'`','``'),
              '` a ON a.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,
              '`=b.`',CASE WHEN b.TABLE_NAME='idempotency_records' THEN 'idempotency_key' ELSE 'id' END,'` WHERE NOT (',
              CASE WHEN b.DATA_TYPE IN ('char','varchar','tinytext','text','mediumtext','longtext','json') THEN 'BINARY ' ELSE '' END,
              'a.`',REPLACE(b.COLUMN_NAME,'`','``'),'` <=> ',
              CASE WHEN b.DATA_TYPE IN ('char','varchar','tinytext','text','mediumtext','longtext','json') THEN 'BINARY ' ELSE '' END,
              'b.`',REPLACE(b.COLUMN_NAME,'`','``'),'`);') AS full_column_compare_sql
FROM information_schema.COLUMNS b JOIN information_schema.COLUMNS a
  ON a.TABLE_SCHEMA=@after_schema AND a.TABLE_NAME=b.TABLE_NAME AND a.COLUMN_NAME=b.COLUMN_NAME
WHERE b.TABLE_SCHEMA=@before_schema
  AND b.TABLE_NAME IN ('portfolios','portfolio_members','portfolio_candidates','idempotency_records',
                      'security_status_daily','cash_ledger','sim_orders','sim_trades','positions',
                      'decision_runs','data_governance_audit_events','scores')
ORDER BY b.TABLE_NAME,b.ORDINAL_POSITION;
-- 对0080实际触及、未在列表内的表，同样按原主键生成丢行与全列比较。
-- 复合主键必须把所有主键列放入JOIN；不可挑一个非唯一列，否则会漏掉缺行/重复。
-- 最终通过：结构符合冻结契约，禁止变化的旧行/旧字段差异为0，新增字段有据映射，
-- 新旧主键集合一致；MySQL真实并发/API/导入回执/回测约束另按回归方案全部通过。

-- 0084/0085 追加验收：仅连接获准隔离库，以下均为只读语句。
SELECT DATABASE() AS actual_schema, VERSION() AS actual_version, version_num FROM alembic_version;
-- 当前预期 wps_0024_085_legacy_dates；旧 0083 通过不能代表当前 head 已验收。
SELECT TABLE_NAME,COLUMN_NAME,IS_NULLABLE,COLUMN_DEFAULT,DATA_TYPE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scheduled_tasks'
  AND COLUMN_NAME IN ('task_name','cron_expression');
-- 两列应允许 NULL 且无伪造默认；所有旧名称、cron、索引和关联仍须对照迁移前副本。

SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE,IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND (
 (TABLE_NAME='scores' AND COLUMN_NAME='trade_date') OR
 (TABLE_NAME='factor_model_runs' AND COLUMN_NAME IN ('train_start_date','train_end_date','validation_start_date','validation_end_date')) OR
 (TABLE_NAME='portfolios' AND COLUMN_NAME IN ('last_decision_trade_date','last_reconciled_trade_date','effective_start_date')) OR
 (TABLE_NAME='symbols' AND COLUMN_NAME='listed_at') OR
 (TABLE_NAME='backtest_runs' AND COLUMN_NAME IN ('start_date','end_date')) OR
 (TABLE_NAME='lhb_institution_trades' AND COLUMN_NAME='trade_date'))
ORDER BY TABLE_NAME,COLUMN_NAME;
-- 12 列应为 date。非法原文必须在任何 DDL 前拒绝，不能变成 NULL 或零日期。

-- 为实际存在的影子列生成核对 SQL；执行生成语句，changed_rows 必须为 0。
SELECT CONCAT('SELECT ',CHAR(39),TABLE_NAME,'.',COLUMN_NAME,CHAR(39),
 ' AS evidence_column, COUNT(*) AS changed_rows FROM `',TABLE_NAME,
 '` WHERE `',COLUMN_NAME,'` IS NOT NULL AND NOT (BINARY DATE_FORMAT(`',
 SUBSTRING(COLUMN_NAME,8,CHAR_LENGTH(COLUMN_NAME)-11),'`,',CHAR(39),'%Y-%m-%d',CHAR(39),
 ') <=> BINARY `',COLUMN_NAME,'`);') AS verify_original_text_sql
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA=DATABASE() AND LEFT(COLUMN_NAME,7)='legacy_' AND RIGHT(COLUMN_NAME,4)='_v85'
ORDER BY TABLE_NAME,COLUMN_NAME;
-- 影子 NULL 可能是原 NULL 或新增 native-date 行；不可倒填假原文。必须另核对迁移前原主键集合。

-- 以下 :model_id / :portfolio_id 使用参数化执行，取本轮实际 ID。
SELECT id,status,sample_count,data_cutoff_at,train_start_date,train_end_date,
 validation_start_date,validation_end_date FROM factor_model_runs WHERE id=:model_id;
SELECT weight_mode,factor_model_run_id,trade_date,COUNT(*) AS rows_count,
 SUM(published_at IS NULL) AS unpublished_count,SUM(model_alpha_score IS NULL) AS missing_alpha
FROM scores WHERE factor_model_run_id=:model_id GROUP BY weight_mode,factor_model_run_id,trade_date;
-- 100 标的夹具的 ridge 每日覆盖应为 100；shadow 不能正式准入。
SELECT client_order_key,COUNT(*) AS duplicate_count FROM sim_orders
WHERE client_order_key IS NOT NULL GROUP BY client_order_key HAVING COUNT(*)>1;
SELECT order_plan_id,portfolio_id,signal_date,execution_date,target_quantity,action
FROM decision_order_plans WHERE portfolio_id=:portfolio_id ORDER BY signal_date,order_plan_id;
SELECT id,client_order_key,quantity,filled_quantity,filled_price,fee,status FROM sim_orders
WHERE portfolio_id=:portfolio_id ORDER BY id;
-- 同一计划仅一个订单；T 日计划在 T+1 前不成交。
-- 拒绝前后按主键比较四张财务表的 SELECT *，只比较数量不足。
-- 普通刷新不得删除历史 Score 或 factor/target 批次；32 批次回归必须原列原行一致。
