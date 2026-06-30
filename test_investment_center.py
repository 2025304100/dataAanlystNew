#!/usr/bin/env python3
"""
投资中心全面验收测试脚本
测试所有 P0/P1/P2 优化功能
"""

import json
import requests
import time
from datetime import datetime, timedelta

BASE_URL = "http://localhost:8000"
LOG_FILE = r"D:\ai_project\dataAanlystNew\test_investment_center.log"

def log_test(test_id, hypothesis_id, message, data=None):
    """写入测试日志"""
    log_entry = {
        "sessionId": "00b0c2cf-1efd-44e2-b928-e839c810dedf",
        "id": f"log_{int(time.time()*1000)}_{test_id}",
        "timestamp": int(time.time() * 1000),
        "location": f"test_investment_center.py:{test_id}",
        "message": message,
        "data": data or {},
        "hypothesisId": hypothesis_id
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

def test_h1_trade_plan_edit():
    """H1: 测试交易计划编辑功能"""
    print("\n=== H1: 测试交易计划参数可编辑 ===")
    
    # 1. 获取默认portfolio和symbol
    try:
        resp = requests.get(f"{BASE_URL}/api/dashboard/workbench", timeout=5)
        log_test("H1_001", "H1", "获取工作台数据", {
            "status": resp.status_code,
            "success": resp.ok
        })
        
        if not resp.ok:
            log_test("H1_002", "H1", "工作台API失败", {"error": resp.text})
            return False
            
        workbench = resp.json()
        portfolio_id = workbench.get("portfolio", {}).get("id")
        
        # 获取一个测试标的
        symbols = workbench.get("latest_scores", [])
        if not symbols:
            log_test("H1_003", "H1", "没有可用的评分标的", {})
            return False
            
        symbol_id = symbols[0]["symbol_id"]
        log_test("H1_004", "H1", "选择测试标的", {
            "portfolio_id": portfolio_id,
            "symbol_id": symbol_id,
            "symbol": symbols[0].get("symbol")
        })
        
        # 2. 生成交易计划
        resp = requests.post(
            f"{BASE_URL}/api/trade-setups/generate",
            json={
                "portfolio_id": portfolio_id,
                "symbol_id": symbol_id
            },
            timeout=10
        )
        log_test("H1_005", "H1", "生成默认交易计划", {
            "status": resp.status_code,
            "success": resp.ok
        })
        
        if not resp.ok:
            log_test("H1_006", "H1", "生成计划失败", {"error": resp.text})
            return False
            
        setup = resp.json()
        log_test("H1_007", "H1", "默认交易计划", {
            "setup_id": setup.get("id"),
            "entry_min": setup.get("entry_min"),
            "entry_max": setup.get("entry_max"),
            "stop_loss": setup.get("stop_loss"),
            "target_price": setup.get("target_price"),
            "position_pct": setup.get("recommended_position_pct"),
            "field_sources": setup.get("field_sources")
        })
        
        # 3. 手动覆盖参数测试
        original_entry_min = setup.get("entry_min")
        manual_entry_min = original_entry_min * 0.95 if original_entry_min else 10.0
        
        resp = requests.post(
            f"{BASE_URL}/api/trade-setups/generate",
            json={
                "portfolio_id": portfolio_id,
                "symbol_id": symbol_id,
                "overrides": {
                    "entry_min": manual_entry_min,
                    "recommended_position_pct": 0.08  # 8%
                }
            },
            timeout=10
        )
        log_test("H1_008", "H1", "使用手动覆盖重新生成计划", {
            "status": resp.status_code,
            "success": resp.ok
        })
        
        if not resp.ok:
            log_test("H1_009", "H1", "覆盖参数失败", {"error": resp.text})
            return False
            
        manual_setup = resp.json()
        log_test("H1_010", "H1", "手动覆盖后的计划", {
            "setup_id": manual_setup.get("id"),
            "entry_min": manual_setup.get("entry_min"),
            "manual_entry_min_expected": manual_entry_min,
            "entry_min_match": abs(manual_setup.get("entry_min", 0) - manual_entry_min) < 0.01,
            "position_pct": manual_setup.get("recommended_position_pct"),
            "field_sources": manual_setup.get("field_sources")
        })
        
        # 验证field_sources标记
        field_sources = manual_setup.get("field_sources", {})
        is_manual_marked = field_sources.get("entry_min") == "manual"
        log_test("H1_011", "H1", "字段来源标记验证", {
            "entry_min_source": field_sources.get("entry_min"),
            "is_manual_marked": is_manual_marked,
            "expected": "manual"
        })
        
        print(f"✓ H1测试完成: 手动覆盖={'成功' if is_manual_marked else '失败'}")
        return is_manual_marked
        
    except Exception as e:
        log_test("H1_999", "H1", "H1测试异常", {"error": str(e)})
        print(f"✗ H1测试异常: {e}")
        return False

def test_h3_price_alerts():
    """H3: 测试价格预警系统"""
    print("\n=== H3: 测试价格预警阈值自定义 ===")
    
    try:
        # 获取标的详情（包含最新价格和交易计划）
        resp = requests.get(f"{BASE_URL}/api/dashboard/workbench", timeout=5)
        if not resp.ok:
            log_test("H3_001", "H3", "获取工作台失败", {})
            return False
            
        workbench = resp.json()
        scores = workbench.get("latest_scores", [])
        if not scores:
            log_test("H3_002", "H3", "没有评分数据", {})
            return False
            
        # 前端价格预警逻辑在 InvestmentCenter.tsx 中
        # 这里验证后端数据完整性
        symbol = scores[0]
        log_test("H3_003", "H3", "检查预警所需数据", {
            "symbol_id": symbol.get("symbol_id"),
            "symbol": symbol.get("symbol"),
            "has_quality_score": "quality_score" in symbol,
            "has_timing_score": "timing_score" in symbol,
            "stage": symbol.get("stage"),
            "action": symbol.get("action")
        })
        
        # 获取K线数据（用于价格预警计算）
        symbol_id = symbol["symbol_id"]
        resp = requests.get(f"{BASE_URL}/api/symbols/{symbol_id}/bars?limit=100", timeout=5)
        log_test("H3_004", "H3", "获取K线数据", {
            "status": resp.status_code,
            "success": resp.ok
        })
        
        if not resp.ok:
            log_test("H3_005", "H3", "K线数据获取失败", {})
            return False
            
        bars = resp.json()
        if not bars:
            log_test("H3_006", "H3", "K线数据为空", {})
            return False
            
        latest_bar = bars[-1]
        log_test("H3_007", "H3", "最新K线数据", {
            "trade_date": latest_bar.get("trade_date"),
            "close": latest_bar.get("close"),
            "high": latest_bar.get("high"),
            "low": latest_bar.get("low"),
            "volume": latest_bar.get("volume")
        })
        
        # 验证预警计算所需的完整数据
        has_complete_data = (
            latest_bar.get("close") is not None and
            latest_bar.get("high") is not None and
            latest_bar.get("low") is not None
        )
        
        log_test("H3_008", "H3", "预警数据完整性", {
            "has_complete_data": has_complete_data,
            "can_calculate_alerts": has_complete_data
        })
        
        print(f"✓ H3测试完成: 预警数据={'完整' if has_complete_data else '不完整'}")
        return has_complete_data
        
    except Exception as e:
        log_test("H3_999", "H3", "H3测试异常", {"error": str(e)})
        print(f"✗ H3测试异常: {e}")
        return False

def test_h5_backtest_engine():
    """H5: 测试回测引擎"""
    print("\n=== H5: 测试轻量回测引擎 ===")
    
    try:
        # 1. 获取portfolio和symbol
        resp = requests.get(f"{BASE_URL}/api/dashboard/workbench", timeout=5)
        if not resp.ok:
            log_test("H5_001", "H5", "获取工作台失败", {})
            return False
            
        workbench = resp.json()
        portfolio_id = workbench.get("portfolio", {}).get("id")
        symbols = workbench.get("latest_scores", [])
        
        if not symbols:
            log_test("H5_002", "H5", "没有可用标的", {})
            return False
            
        symbol_id = symbols[0]["symbol_id"]
        log_test("H5_003", "H5", "准备回测", {
            "portfolio_id": portfolio_id,
            "symbol_id": symbol_id,
            "symbol": symbols[0].get("symbol")
        })
        
        # 2. 配置回测参数
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        
        backtest_config = {
            "portfolio_id": portfolio_id,
            "symbol_ids": [symbol_id],
            "start_date": start_date,
            "end_date": end_date,
            "run_name": "验收测试回测",
            "rule_config": {
                "buy_conditions": {
                    "quality_score_min": 60,
                    "timing_score_min": 55,
                    "stages": ["start", "accel"],
                    "actions": ["open", "hold", "buy_dip"]
                },
                "sell_conditions": {
                    "take_profit_pct": 0.15,
                    "stop_loss_pct": 0.08,
                    "max_hold_days": 30
                },
                "position_config": {
                    "type": "fixed_pct",
                    "value": 0.05,
                    "max_positions": 5
                }
            },
            "cost_config": {
                "commission_rate": 0.0003,
                "min_commission": 5.0,
                "stamp_tax_rate": 0.001,
                "slippage_rate": 0.001
            }
        }
        
        log_test("H5_004", "H5", "回测配置", backtest_config)
        
        # 3. 执行回测
        resp = requests.post(
            f"{BASE_URL}/api/backtest/run",
            json=backtest_config,
            timeout=60
        )
        
        log_test("H5_005", "H5", "回测执行结果", {
            "status": resp.status_code,
            "success": resp.ok
        })
        
        if not resp.ok:
            log_test("H5_006", "H5", "回测执行失败", {"error": resp.text})
            return False
            
        result = resp.json()
        run_id = result.get("id")
        log_test("H5_007", "H5", "回测创建成功", {
            "run_id": run_id,
            "status": result.get("status")
        })
        
        # 4. 获取回测详情
        resp = requests.get(f"{BASE_URL}/api/backtest/runs/{run_id}", timeout=10)
        log_test("H5_008", "H5", "获取回测详情", {
            "status": resp.status_code,
            "success": resp.ok
        })
        
        if not resp.ok:
            log_test("H5_009", "H5", "获取详情失败", {})
            return False
            
        run_detail = resp.json()
        log_test("H5_010", "H5", "回测详细结果", {
            "run_id": run_detail.get("id"),
            "status": run_detail.get("status"),
            "total_return_pct": run_detail.get("total_return_pct"),
            "max_drawdown_pct": run_detail.get("max_drawdown_pct"),
            "sharpe_ratio": run_detail.get("sharpe_ratio"),
            "win_rate": run_detail.get("win_rate"),
            "trade_count": run_detail.get("trade_count"),
            "has_trades": len(run_detail.get("trades", [])) > 0,
            "has_summary": run_detail.get("summary") is not None
        })
        
        # 验证回测完整性
        is_complete = (
            run_detail.get("status") in ["completed", "running"] and
            run_detail.get("trade_count") is not None
        )
        
        print(f"✓ H5测试完成: 回测={'成功' if is_complete else '失败'}")
        return is_complete
        
    except Exception as e:
        log_test("H5_999", "H5", "H5测试异常", {"error": str(e)})
        print(f"✗ H5测试异常: {e}")
        return False

def test_h6_technical_indicators():
    """H6: 测试技术指标"""
    print("\n=== H6: 测试技术指标显示 ===")
    
    try:
        # 获取标的K线数据
        resp = requests.get(f"{BASE_URL}/api/dashboard/workbench", timeout=5)
        if not resp.ok:
            log_test("H6_001", "H6", "获取工作台失败", {})
            return False
            
        workbench = resp.json()
        symbols = workbench.get("latest_scores", [])
        if not symbols:
            log_test("H6_002", "H6", "没有标的数据", {})
            return False
            
        symbol_id = symbols[0]["symbol_id"]
        
        # 获取足够多的K线数据用于指标计算
        resp = requests.get(f"{BASE_URL}/api/symbols/{symbol_id}/bars?limit=100", timeout=5)
        log_test("H6_003", "H6", "获取K线数据", {
            "status": resp.status_code,
            "success": resp.ok,
            "symbol_id": symbol_id
        })
        
        if not resp.ok:
            log_test("H6_004", "H6", "K线获取失败", {})
            return False
            
        bars = resp.json()
        if len(bars) < 50:
            log_test("H6_005", "H6", "K线数据不足", {"count": len(bars)})
            return False
            
        log_test("H6_006", "H6", "K线数据统计", {
            "total_bars": len(bars),
            "date_range": f"{bars[0].get('trade_date')} to {bars[-1].get('trade_date')}",
            "has_ohlcv": all(
                b.get("open") and b.get("high") and b.get("low") and 
                b.get("close") and b.get("volume")
                for b in bars[-10:]
            )
        })
        
        # 前端指标在 indicators.ts 中计算
        # 这里验证后端提供的数据完整性
        latest_bars = bars[-10:]
        data_quality = {
            "has_complete_ohlcv": all(
                b.get("open") and b.get("high") and b.get("low") and 
                b.get("close") and b.get("volume")
                for b in latest_bars
            ),
            "price_range": {
                "min": min(b.get("low", 0) for b in latest_bars),
                "max": max(b.get("high", 0) for b in latest_bars)
            },
            "volume_range": {
                "min": min(b.get("volume", 0) for b in latest_bars),
                "max": max(b.get("volume", 0) for b in latest_bars)
            }
        }
        
        log_test("H6_007", "H6", "技术指标数据质量", data_quality)
        
        # 验证数据可用于指标计算
        can_calculate = (
            data_quality["has_complete_ohlcv"] and
            data_quality["price_range"]["max"] > 0 and
            data_quality["volume_range"]["max"] > 0
        )
        
        print(f"✓ H6测试完成: 指标数据={'可用' if can_calculate else '不可用'}")
        return can_calculate
        
    except Exception as e:
        log_test("H6_999", "H6", "H6测试异常", {"error": str(e)})
        print(f"✗ H6测试异常: {e}")
        return False

def main():
    """执行所有测试"""
    print("=" * 60)
    print("投资中心全面验收测试")
    print("=" * 60)
    
    # 清空日志文件
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")
    
    log_test("MAIN_001", "MAIN", "开始验收测试", {
        "timestamp": datetime.now().isoformat(),
        "base_url": BASE_URL
    })
    
    # 测试系统健康状态
    try:
        resp = requests.get(f"{BASE_URL}/health", timeout=5)
        log_test("MAIN_002", "MAIN", "系统健康检查", {
            "status": resp.status_code,
            "healthy": resp.ok
        })
        if not resp.ok:
            print("✗ 系统健康检查失败，终止测试")
            return
    except Exception as e:
        log_test("MAIN_003", "MAIN", "系统不可用", {"error": str(e)})
        print(f"✗ 系统不可用: {e}")
        return
    
    # 执行各项测试
    results = {}
    
    results["H1_交易计划编辑"] = test_h1_trade_plan_edit()
    results["H3_价格预警"] = test_h3_price_alerts()
    results["H5_回测引擎"] = test_h5_backtest_engine()
    results["H6_技术指标"] = test_h6_technical_indicators()
    
    # 输出测试结果
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test_name, result in results.items():
        status = "✓ 通过" if result else "✗ 失败"
        print(f"{status}: {test_name}")
    
    print(f"\n总计: {passed}/{total} 通过")
    
    log_test("MAIN_999", "MAIN", "测试完成", {
        "results": results,
        "passed": passed,
        "total": total,
        "pass_rate": f"{passed/total*100:.1f}%"
    })
    
    print(f"\n日志文件: {LOG_FILE}")

if __name__ == "__main__":
    main()