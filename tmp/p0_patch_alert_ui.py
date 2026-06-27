from pathlib import Path
path = Path('frontend/src/components/InvestmentCenter.tsx')
text = path.read_text(encoding='utf-8')
anchor = '''  /* ════════════════════════════════════════════════════
     渲染：风险仪表盘
     ════════════════════════════════════════════════════ */
'''
fn = '''  const renderPriceAlertSection = () => {
    if (!detail) return null;
    return (
      <section className="ic__price-alerts">
        <div className="detail-card-head">
          <h4>{t("priceAlerts")}</h4>
          <Button size="small" onClick={() => setAlertSettingsOpen((v) => !v)}>{icText.alertSettings}</Button>
        </div>
        {alertSettingsOpen && (
          <div className="ic__settings-grid ic__settings-grid--alerts">
            <label><span>{icText.stopNearPct}</span><InputNumber size="small" min={0} max={50} value={alertSettings.stopNearPct} onChange={(v) => setAlertSettings((prev) => ({ ...prev, stopNearPct: Number(v ?? 3) }))} /></label>
            <label><span>{icText.targetNearPct}</span><InputNumber size="small" min={0} max={50} value={alertSettings.targetNearPct} onChange={(v) => setAlertSettings((prev) => ({ ...prev, targetNearPct: Number(v ?? 5) }))} /></label>
            <label><span>{icText.rsiOverbought}</span><InputNumber size="small" min={50} max={100} value={alertSettings.rsiOverbought} onChange={(v) => setAlertSettings((prev) => ({ ...prev, rsiOverbought: Number(v ?? 75) }))} /></label>
            <label><span>{icText.rsiOversold}</span><InputNumber size="small" min={0} max={50} value={alertSettings.rsiOversold} onChange={(v) => setAlertSettings((prev) => ({ ...prev, rsiOversold: Number(v ?? 25) }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableStopLoss}</span><Switch size="small" checked={alertSettings.enableStopLoss} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableStopLoss: checked }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableTarget}</span><Switch size="small" checked={alertSettings.enableTarget} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableTarget: checked }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableRsi}</span><Switch size="small" checked={alertSettings.enableRsi} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableRsi: checked }))} /></label>
            <label className="ic__switch-field"><span>{icText.enableMacd}</span><Switch size="small" checked={alertSettings.enableMacd} onChange={(checked) => setAlertSettings((prev) => ({ ...prev, enableMacd: checked }))} /></label>
          </div>
        )}
        {priceAlerts.length > 0 && (
          <div className="ic__alert-list">
            {priceAlerts.map((alert) => (
              <div key={alert.id} className={`ic__alert-item ic__alert--${alert.level}`}>
                <span className="ic__alert-icon">{alert.level === "danger" ? "!" : alert.level === "warning" ? "!" : "i"}</span>
                <div className="ic__alert-body">
                  <strong>{alert.message}</strong>
                  <span>{alert.detail}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    );
  };

'''
if anchor not in text:
    raise SystemExit('risk anchor not found')
text = text.replace(anchor, fn + anchor)
# Replace the old desktop inline alert section with a function call.
start = text.find('        {/* 价格预警栏 */}')
if start != -1:
    end = text.find('        {renderRiskDashboard()}', start)
    if end == -1:
        raise SystemExit('risk render after alert not found')
    text = text[:start] + '        {renderPriceAlertSection()}\n' + text[end:]
# Add alert settings to mobile path before risk dashboard if not already there.
text = text.replace('''        {renderRiskDashboard()}
        {renderTradePlan()}''', '''        {renderPriceAlertSection()}
        {renderRiskDashboard()}
        {renderTradePlan()}''', 1)
path.write_text(text, encoding='utf-8')
