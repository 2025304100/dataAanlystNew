PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'etf', 'index')),
    market TEXT NOT NULL,
    board TEXT,
    industry TEXT,
    theme TEXT,
    is_st INTEGER NOT NULL DEFAULT 0 CHECK (is_st IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    listed_at DATE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_symbols_asset_market
    ON symbols (asset_type, market);

CREATE INDEX IF NOT EXISTS idx_symbols_theme
    ON symbols (theme);

CREATE TABLE IF NOT EXISTS watchlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    list_type TEXT NOT NULL CHECK (list_type IN ('watch', 'trade', 'eliminate', 'custom')),
    description TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    note TEXT,
    added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (watchlist_id, symbol_id),
    FOREIGN KEY (watchlist_id) REFERENCES watchlists (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_watchlist_items_watchlist
    ON watchlist_items (watchlist_id);

CREATE INDEX IF NOT EXISTS idx_watchlist_items_symbol
    ON watchlist_items (symbol_id);

CREATE TABLE IF NOT EXISTS daily_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    trade_date DATE NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    amount REAL,
    turnover_rate REAL,
    source TEXT NOT NULL DEFAULT 'akshare',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol_id, trade_date),
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_daily_bars_trade_date
    ON daily_bars (trade_date);

CREATE INDEX IF NOT EXISTS idx_daily_bars_symbol_trade_date
    ON daily_bars (symbol_id, trade_date DESC);

CREATE TABLE IF NOT EXISTS factors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('positive', 'negative', 'neutral')),
    status TEXT NOT NULL CHECK (status IN ('draft', 'testing', 'active', 'deprecated')),
    description TEXT,
    formula_expr TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS factor_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    factor_id INTEGER NOT NULL,
    trade_date DATE NOT NULL,
    raw_value REAL,
    normalized_value REAL,
    calc_batch_id TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol_id, factor_id, trade_date, calc_batch_id),
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE,
    FOREIGN KEY (factor_id) REFERENCES factors (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_factor_values_factor_trade_date
    ON factor_values (factor_id, trade_date);

CREATE INDEX IF NOT EXISTS idx_factor_values_symbol_trade_date
    ON factor_values (symbol_id, trade_date DESC);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER NOT NULL,
    trade_date DATE NOT NULL,
    quality_score REAL NOT NULL,
    quality_grade TEXT NOT NULL CHECK (quality_grade IN ('A', 'B', 'C', 'D')),
    timing_score REAL NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('start', 'accel', 'overheat', 'cooldown')),
    action TEXT NOT NULL CHECK (action IN ('open', 'buy_dip', 'hold', 'reduce', 'exit')),
    priority_score REAL NOT NULL,
    trend_score REAL,
    momentum_score REAL,
    volatility_score REAL,
    liquidity_score REAL,
    breadth_score REAL,
    event_score REAL,
    calc_batch_id TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (symbol_id, trade_date, calc_batch_id),
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scores_trade_date_priority
    ON scores (trade_date, priority_score DESC);

CREATE INDEX IF NOT EXISTS idx_scores_symbol_trade_date
    ON scores (symbol_id, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_scores_stage_action
    ON scores (stage, action);

CREATE TABLE IF NOT EXISTS scan_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('stock', 'etf', 'mixed')),
    markets TEXT,
    boards TEXT,
    filters_json TEXT,
    sort_mode TEXT,
    description TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL CHECK (account_type IN ('simulated', 'real')),
    total_capital REAL NOT NULL,
    investable_ratio REAL NOT NULL CHECK (investable_ratio >= 0 AND investable_ratio <= 1),
    cash_reserve_ratio REAL NOT NULL CHECK (cash_reserve_ratio >= 0 AND cash_reserve_ratio <= 1),
    currency TEXT NOT NULL DEFAULT 'CNY',
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portfolio_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    rule_name TEXT NOT NULL,
    max_single_position_pct REAL NOT NULL CHECK (max_single_position_pct >= 0 AND max_single_position_pct <= 1),
    max_sector_position_pct REAL NOT NULL CHECK (max_sector_position_pct >= 0 AND max_sector_position_pct <= 1),
    max_stock_position_pct REAL NOT NULL CHECK (max_stock_position_pct >= 0 AND max_stock_position_pct <= 1),
    max_etf_position_pct REAL NOT NULL CHECK (max_etf_position_pct >= 0 AND max_etf_position_pct <= 1),
    max_loss_per_trade_pct REAL NOT NULL CHECK (max_loss_per_trade_pct >= 0 AND max_loss_per_trade_pct <= 1),
    max_open_positions INTEGER NOT NULL CHECK (max_open_positions >= 0),
    stage_limits_json TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_portfolio_rules_portfolio
    ON portfolio_rules (portfolio_id, is_active);

CREATE TABLE IF NOT EXISTS signal_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    rule_name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'balanced',
    quality_tolerance REAL NOT NULL DEFAULT 12.0,
    timing_tolerance REAL NOT NULL DEFAULT 12.0,
    min_sample_count INTEGER NOT NULL DEFAULT 3,
    max_samples INTEGER NOT NULL DEFAULT 60,
    same_region INTEGER NOT NULL DEFAULT 1,
    same_asset_type INTEGER NOT NULL DEFAULT 1,
    same_stage INTEGER NOT NULL DEFAULT 1,
    same_action INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signal_rules_portfolio
    ON signal_rules (portfolio_id, is_active);

CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER,
    symbol TEXT,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT,
    event_type TEXT NOT NULL DEFAULT 'news',
    sentiment TEXT NOT NULL DEFAULT 'neutral',
    strength INTEGER NOT NULL DEFAULT 1,
    raw_score REAL NOT NULL DEFAULT 0,
    effective_score REAL NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT 'low',
    published_at TEXT,
    expires_at TEXT,
    raw_payload TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE,
    UNIQUE (symbol_id, title, published_at)
);

CREATE INDEX IF NOT EXISTS idx_news_events_symbol
    ON news_events (symbol_id, published_at);

CREATE TABLE IF NOT EXISTS news_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER,
    symbol_id INTEGER,
    scope TEXT NOT NULL,
    macro_score REAL NOT NULL DEFAULT 0,
    sector_score REAL NOT NULL DEFAULT 0,
    symbol_score REAL NOT NULL DEFAULT 0,
    message_score REAL NOT NULL DEFAULT 0,
    sentiment TEXT NOT NULL DEFAULT 'neutral',
    risk_level TEXT NOT NULL DEFAULT 'low',
    confidence REAL NOT NULL DEFAULT 0,
    positive_count INTEGER NOT NULL DEFAULT 0,
    negative_count INTEGER NOT NULL DEFAULT 0,
    risk_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE SET NULL,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_news_snapshots_symbol
    ON news_snapshots (symbol_id, created_at);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    latest_price REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    position_pct REAL NOT NULL DEFAULT 0 CHECK (position_pct >= 0 AND position_pct <= 1),
    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'etf', 'index')),
    theme TEXT,
    opened_at DATETIME,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (portfolio_id, symbol_id),
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_positions_portfolio
    ON positions (portfolio_id);

CREATE INDEX IF NOT EXISTS idx_positions_symbol
    ON positions (symbol_id);

CREATE TABLE IF NOT EXISTS cash_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('deposit', 'withdraw', 'buy', 'sell', 'fee', 'dividend', 'adjustment')),
    amount REAL NOT NULL,
    balance_after REAL NOT NULL,
    ref_type TEXT,
    ref_id INTEGER,
    note TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cash_ledger_portfolio_created
    ON cash_ledger (portfolio_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sim_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    limit_price REAL,
    submitted_price REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('filled', 'cancelled')),
    filled_quantity REAL NOT NULL DEFAULT 0,
    filled_price REAL NOT NULL DEFAULT 0,
    filled_amount REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    note TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    filled_at DATETIME,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sim_orders_portfolio_created
    ON sim_orders (portfolio_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sim_orders_symbol_created
    ON sim_orders (symbol_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    realized_pnl REAL,
    note TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES sim_orders (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sim_trades_portfolio_created
    ON sim_trades (portfolio_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sim_trades_symbol_created
    ON sim_trades (symbol_id, created_at DESC);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_id INTEGER,
    run_name TEXT NOT NULL,
    scope_snapshot TEXT NOT NULL,
    filters_snapshot TEXT,
    portfolio_id INTEGER,
    portfolio_rule_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    started_at DATETIME,
    finished_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (preset_id) REFERENCES scan_presets (id) ON DELETE SET NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE SET NULL,
    FOREIGN KEY (portfolio_rule_id) REFERENCES portfolio_rules (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_status_created
    ON scan_runs (status, created_at DESC);

CREATE TABLE IF NOT EXISTS scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    result_type TEXT NOT NULL CHECK (result_type IN ('quality', 'timing', 'executable')),
    rank_no INTEGER NOT NULL CHECK (rank_no >= 1),
    quality_score REAL,
    timing_score REAL,
    priority_score REAL,
    stage TEXT CHECK (stage IN ('start', 'accel', 'overheat', 'cooldown')),
    action TEXT CHECK (action IN ('open', 'buy_dip', 'hold', 'reduce', 'exit')),
    recommended_position_pct REAL CHECK (recommended_position_pct IS NULL OR (recommended_position_pct >= 0 AND recommended_position_pct <= 1)),
    is_sector_overweight INTEGER NOT NULL DEFAULT 0 CHECK (is_sector_overweight IN (0, 1)),
    is_asset_overweight INTEGER NOT NULL DEFAULT 0 CHECK (is_asset_overweight IN (0, 1)),
    reason_tags TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (scan_run_id) REFERENCES scan_runs (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scan_results_run_type_rank
    ON scan_results (scan_run_id, result_type, rank_no);

CREATE INDEX IF NOT EXISTS idx_scan_results_symbol_created
    ON scan_results (symbol_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trade_setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    score_id INTEGER NOT NULL,
    scan_run_id INTEGER,
    stage TEXT NOT NULL CHECK (stage IN ('start', 'accel', 'overheat', 'cooldown')),
    action TEXT NOT NULL CHECK (action IN ('open', 'buy_dip', 'hold', 'reduce', 'exit')),
    entry_min REAL,
    entry_max REAL,
    stop_loss REAL,
    target_price REAL,
    recommended_position_pct REAL NOT NULL CHECK (recommended_position_pct >= 0 AND recommended_position_pct <= 1),
    recommended_position_amount REAL NOT NULL CHECK (recommended_position_amount >= 0),
    risk_reward_ratio REAL,
    allow_add_position INTEGER NOT NULL DEFAULT 0 CHECK (allow_add_position IN (0, 1)),
    is_sector_overweight INTEGER NOT NULL DEFAULT 0 CHECK (is_sector_overweight IN (0, 1)),
    is_asset_overweight INTEGER NOT NULL DEFAULT 0 CHECK (is_asset_overweight IN (0, 1)),
    setup_reason TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE,
    FOREIGN KEY (score_id) REFERENCES scores (id) ON DELETE CASCADE,
    FOREIGN KEY (scan_run_id) REFERENCES scan_runs (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_trade_setups_portfolio_created
    ON trade_setups (portfolio_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_trade_setups_symbol_created
    ON trade_setups (symbol_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trade_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    trade_setup_id INTEGER NOT NULL,
    signal_type TEXT NOT NULL CHECK (signal_type IN ('open', 'add', 'hold', 'reduce', 'exit')),
    signal_level TEXT NOT NULL CHECK (signal_level IN ('info', 'warn', 'strong')),
    message TEXT NOT NULL,
    trigger_price REAL,
    status TEXT NOT NULL CHECK (status IN ('active', 'ignored', 'done')),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE,
    FOREIGN KEY (trade_setup_id) REFERENCES trade_setups (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_signals_portfolio_status
    ON trade_signals (portfolio_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS allocation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    related_run_type TEXT NOT NULL CHECK (related_run_type IN ('scan', 'setup', 'manual')),
    related_run_id INTEGER,
    total_position_pct REAL NOT NULL CHECK (total_position_pct >= 0 AND total_position_pct <= 1),
    stock_position_pct REAL NOT NULL CHECK (stock_position_pct >= 0 AND stock_position_pct <= 1),
    etf_position_pct REAL NOT NULL CHECK (etf_position_pct >= 0 AND etf_position_pct <= 1),
    cash_pct REAL NOT NULL CHECK (cash_pct >= 0 AND cash_pct <= 1),
    sector_exposure_json TEXT,
    position_count INTEGER NOT NULL CHECK (position_count >= 0),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_allocation_snapshots_portfolio_created
    ON allocation_snapshots (portfolio_id, created_at DESC);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    trade_setup_id INTEGER,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('watch', 'trade', 'review')),
    title TEXT NOT NULL,
    content TEXT,
    subjective_view TEXT,
    follow_system INTEGER NOT NULL DEFAULT 0 CHECK (follow_system IN (0, 1)),
    outcome TEXT,
    review_note TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios (id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols (id) ON DELETE CASCADE,
    FOREIGN KEY (trade_setup_id) REFERENCES trade_setups (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_entries_portfolio_created
    ON journal_entries (portfolio_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_journal_entries_symbol_created
    ON journal_entries (symbol_id, created_at DESC);
