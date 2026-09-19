// WP5.1：标的研究搜索头部
//
// 设计说明：
// - 任务清单 #2：搜索框 + 快捷标的 + 搜索历史
// - 从 InvestmentCenter.tsx 抽出搜索框、搜索结果下拉、搜索历史下拉、快捷标的 chips
// - 状态（searchQuery/searchResults/searchHistory/favorites/quickSymbols）留在 Shell
// - 收藏切换在移动端和桌面端都有，逻辑统一通过 onToggleFavorite 透传
//
// 关键约束：
// - 不重写搜索逻辑（debounce/API 调用留在 Shell 的 useEffect）
// - 不删除 ic_favorites / ic_search_history（WP5.2 才进入只读回退期）
// - 收藏切换走 onToggleFavorite，由 Shell 决定走后端观察池还是本地回退
import { Input } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { t } from "../../i18n";
import type { SymbolSearchHeaderProps } from "./types";

/**
 * 标的搜索头部：搜索框 + 搜索结果下拉 + 搜索历史 + 快捷标的 chips。
 * - 移动端：紧凑布局（顶部 search-bar）
 * - 桌面端：大尺寸搜索框 + 独立 quick-bar 行
 */
export default function SymbolSearchHeader({
  isMobile,
  searchQuery,
  searchResults,
  searchHistory,
  favorites,
  quickSymbols,
  activeSymbolId,
  onSearchQueryChange,
  onSelectSymbol,
  onToggleFavorite,
}: SymbolSearchHeaderProps) {
  // 搜索结果下拉（移动端与桌面端共用）
  const renderSearchDropdown = () => {
    if (searchResults.length === 0) return null;
    return (
      <div className="ic__search-dropdown">
        {searchResults.map((item) => (
          <button
            type="button"
            key={item.id}
            className="ic__search-result-item"
            onClick={() => onSelectSymbol(item.id, item)}
          >
            <span
              className={`ic__fav-star${favorites.has(item.id) ? " active" : ""}`}
              onClick={(e) => {
                e.stopPropagation();
                onToggleFavorite(item.id);
              }}
            >
              {favorites.has(item.id) ? "★" : "☆"}
            </span>
            <span className="symbol-code">{item.symbol}</span>
            <span className="symbol-name">{item.name}</span>
          </button>
        ))}
      </div>
    );
  };

  // 搜索历史下拉（无搜索词时显示）
  const renderSearchHistory = () => {
    if (searchQuery || searchHistory.length === 0) return null;
    return (
      <div className="ic__search-dropdown ic__search-history">
        <div className="ic__history-header">{t("searchHistory")}</div>
        {searchHistory.map((item) => (
          <button
            type="button"
            key={item.symbol_id}
            className="ic__search-result-item"
            onClick={() => onSelectSymbol(item.symbol_id, item)}
          >
            <span
              className={`ic__fav-star${favorites.has(item.symbol_id) ? " active" : ""}`}
              onClick={(e) => {
                e.stopPropagation();
                onToggleFavorite(item.symbol_id);
              }}
            >
              {favorites.has(item.symbol_id) ? "★" : "☆"}
            </span>
            <span className="symbol-code">{item.symbol}</span>
            <span className="symbol-name">{item.name}</span>
          </button>
        ))}
      </div>
    );
  };

  if (isMobile) {
    return (
      <>
        <div className="ic__search-bar">
          <Input
            prefix={<SearchOutlined style={{ color: "var(--muted)" }} />}
            placeholder={t("searchSymbolPlaceholder")}
            allowClear
            value={searchQuery}
            onChange={(e) => onSearchQueryChange(e.target.value)}
            onPressEnter={() => {
              if (searchResults.length > 0) onSelectSymbol(searchResults[0].id, searchResults[0]);
            }}
          />
          {renderSearchDropdown()}
          {renderSearchHistory()}
        </div>
        {quickSymbols.length > 0 && !searchQuery && (
          <div className="ic__quick-chips">
            {quickSymbols.slice(0, 8).map((item) => (
              <button
                type="button"
                key={item.symbol_id}
                className={`ic__chip${activeSymbolId === item.symbol_id ? " active" : ""}`}
                onClick={() => onSelectSymbol(item.symbol_id)}
              >
                {item.symbol} {item.name}
              </button>
            ))}
          </div>
        )}
      </>
    );
  }

  // 桌面端布局
  return (
    <>
      <header className="ic__top-search">
        <Input
          prefix={<SearchOutlined style={{ color: "var(--muted)", fontSize: 15 }} />}
          placeholder={t("searchSymbolPlaceholder")}
          allowClear
          size="large"
          value={searchQuery}
          onChange={(e) => onSearchQueryChange(e.target.value)}
          onPressEnter={() => {
            if (searchResults.length > 0) onSelectSymbol(searchResults[0].id, searchResults[0]);
          }}
          className="ic__top-input"
        />
        {renderSearchDropdown()}
        {renderSearchHistory()}
      </header>

      <div className="ic__toolbar">
        {!searchQuery && quickSymbols.length > 0 && (
          <nav className="ic__quick-bar">
            {quickSymbols.map((item) => (
              <button
                type="button"
                key={item.symbol_id}
                className={`ic__chip${activeSymbolId === item.symbol_id ? " active" : ""}`}
                onClick={() => onSelectSymbol(item.symbol_id)}
              >
                {item.symbol} {item.name}
              </button>
            ))}
          </nav>
        )}
      </div>
    </>
  );
}
