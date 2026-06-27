from pathlib import Path
path = Path('frontend/src/components/InvestmentCenter.tsx')
text = path.read_text(encoding='utf-8')
text = text.replace('''    // 1. 跌破止损
    if (stopLoss != null && lastClose <= stopLoss) {''', '''    // 1. 跌破止损
    if (alertSettings.enableStopLoss && stopLoss != null && lastClose <= stopLoss) {''')
text = text.replace('''    // 2. 接近止损（距离<3%）
    else if (stopLoss != null && lastClose > 0 && ((lastClose - stopLoss) / lastClose) < 0.03) {''', '''    // 2. 接近止损
    else if (alertSettings.enableStopLoss && stopLoss != null && lastClose > 0 && ((lastClose - stopLoss) / lastClose) < alertSettings.stopNearPct / 100) {''')
text = text.replace('''    if (targetPrice != null && lastClose >= targetPrice) {''', '''    if (alertSettings.enableTarget && targetPrice != null && lastClose >= targetPrice) {''')
text = text.replace('''    // 4. 接近目标（距离<5%）
    else if (targetPrice != null && lastClose > 0 && ((targetPrice - lastClose) / targetPrice) < 0.05) {''', '''    // 4. 接近目标
    else if (alertSettings.enableTarget && targetPrice != null && lastClose > 0 && ((targetPrice - lastClose) / targetPrice) < alertSettings.targetNearPct / 100) {''')
text = text.replace('''    if (lastRSI != null && lastRSI > 75) {''', '''    if (alertSettings.enableRsi && lastRSI != null && lastRSI > alertSettings.rsiOverbought) {''')
text = text.replace('''        detail: `RSI(${lastRSI.toFixed(1)}) 超过75，注意回调风险`,''', '''        detail: `RSI(${lastRSI.toFixed(1)}) 超过${alertSettings.rsiOverbought}，注意回调风险`,''')
text = text.replace('''    } else if (lastRSI != null && lastRSI < 25) {''', '''    } else if (alertSettings.enableRsi && lastRSI != null && lastRSI < alertSettings.rsiOversold) {''')
text = text.replace('''        detail: `RSI(${lastRSI.toFixed(1)}) 低于25，可能存在反弹机会`,''', '''        detail: `RSI(${lastRSI.toFixed(1)}) 低于${alertSettings.rsiOversold}，可能存在反弹机会`,''')
text = text.replace('''    if (chartData.macdSignals.length > 0) {''', '''    if (alertSettings.enableMacd && chartData.macdSignals.length > 0) {''')
text = text.replace('''  }, [chartData, scenarios, setup, detail?.bars]); // eslint-disable-line''', '''  }, [chartData, scenarios, setup, detail?.bars, alertSettings]); // eslint-disable-line''')
text = text.replace('''    const atrStopRef = lastATR != null ? lastATR * 2 : null;''', '''    const atrStopRef = lastATR != null ? lastATR * riskSettings.atrMultiplier : null;''')
text = text.replace('''    const concentrationLevel =
      positionPct >= 20 ? "high" : positionPct >= 10 ? "medium" : "low";''', '''    const concentrationLevel =
      positionPct >= riskSettings.concentrationHighPct ? "high" : positionPct >= riskSettings.concentrationMediumPct ? "medium" : "low";''')
text = text.replace('''      rrRatio, stopDistancePct, currentStopDist, atrStopRef,
      concentrationLevel, maxLossAmount, maxLossPerShare,
      reward, riskAmount, currentPrice, stop, target,''', '''      rrRatio, stopDistancePct, currentStopDist, atrStopRef,
      concentrationLevel, maxLossAmount, maxLossPerShare,
      reward, riskAmount, currentPrice, stop, target,
      maxLossLimitAmount: (ctx.workbench?.portfolio?.total_capital ?? 0) * (riskSettings.maxLossPct / 100),''')
text = text.replace('''  }, [setup, scenarios, entryPrice, quantity, chartData?.atr, detail?.bars]);''', '''  }, [setup, scenarios, entryPrice, quantity, chartData?.atr, detail?.bars, riskSettings, ctx.workbench?.portfolio?.total_capital]);''')
path.write_text(text, encoding='utf-8')
