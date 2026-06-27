from pathlib import Path
path = Path('frontend/src/components/InvestmentCenter.tsx')
text = path.read_text(encoding='utf-8')
text = text.replace('''          <h3>{t("riskDashboard")}</h3>
          <div className="ic__risk-grid">''', '''          <div className="detail-card-head">
            <h3>{t("riskDashboard")}</h3>
            <Button size="small" onClick={() => setRiskSettingsOpen((v) => !v)}>{icText.riskSettings}</Button>
          </div>
          {riskSettingsOpen && (
            <div className="ic__settings-grid ic__settings-grid--compact">
              <label><span>{icText.atrMultiplier}</span><InputNumber size="small" min={0.5} max={5} step={0.5} value={riskSettings.atrMultiplier} onChange={(v) => setRiskSettings((prev) => ({ ...prev, atrMultiplier: Number(v ?? 2) }))} /></label>
              <label><span>{icText.mediumPosition}</span><InputNumber size="small" min={0} max={100} value={riskSettings.concentrationMediumPct} onChange={(v) => setRiskSettings((prev) => ({ ...prev, concentrationMediumPct: Number(v ?? 10) }))} /></label>
              <label><span>{icText.highPosition}</span><InputNumber size="small" min={0} max={100} value={riskSettings.concentrationHighPct} onChange={(v) => setRiskSettings((prev) => ({ ...prev, concentrationHighPct: Number(v ?? 20) }))} /></label>
              <label><span>{icText.maxLossPct}</span><InputNumber size="small" min={0} max={100} step={0.5} value={riskSettings.maxLossPct} onChange={(v) => setRiskSettings((prev) => ({ ...prev, maxLossPct: Number(v ?? 2) }))} /></label>
            </div>
          )}
          <div className="ic__risk-grid">''')
text = text.replace('''                {rm.atrStopRef != null && <span>2×ATR: {score(rm.atrStopRef)}</span>}''', '''                {rm.atrStopRef != null && <span>{riskSettings.atrMultiplier}×ATR: {score(rm.atrStopRef)}</span>}''')
text = text.replace('''                  percent={Math.min(100, (setup?.recommended_position_pct ?? 0) * 5)}''', '''                  percent={Math.min(100, ((setup?.recommended_position_pct ?? 0) * 100 / Math.max(riskSettings.concentrationHighPct, 1)) * 100)}''')
text = text.replace('''              <span className="ic__risk-value" style={{ color: rm.maxLossAmount > 0 ? "#b42318" : "#6b7280" }}>''', '''              <span className="ic__risk-value" style={{ color: rm.maxLossLimitAmount > 0 && rm.maxLossAmount > rm.maxLossLimitAmount ? "#b42318" : rm.maxLossAmount > 0 ? "#f59e0b" : "#6b7280" }}>''')
text = text.replace('''                <span>数量: {quantity}</span>''', '''                <span>数量: {quantity}</span>
                {rm.maxLossLimitAmount > 0 && <span>上限: {money(rm.maxLossLimitAmount)}</span>}''')
path.write_text(text, encoding='utf-8')
