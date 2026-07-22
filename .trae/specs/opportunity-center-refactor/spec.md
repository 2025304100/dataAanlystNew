# 机会中心、标的研究、策略组合交易改造 Spec

## Why
当前项目存在7个真实缺口：导航命名未对齐业务对象、观察池信息过少且与投资中心本地收藏重复、缺少持久化组合成员导致成员/持仓/候选混用、候选→观察→成员→持仓之间无统一流转服务和审计、自动交易与组合回测依赖"最新扫描结果"导致历史不可复现、订单/成交/回测缺少完整信号/规则/来源归因、大型前端组件和跨页面状态重复增加测试遗漏风险。

## What Changes
- **一级导航重构**：新增"机会中心"，投资中心退出一级导航改造为公共标的研究，当前观察池重命名为"组合交易"
- **候选池增强**：统一生命周期（new→reviewed→watched→portfolio→excluded/expired），补来源、有效期、关联状态
- **观察池重构**：从通用名单弹窗变为正式页面，增加来源、信号、标签、目标组合等上下文
- **组合成员新增**：新增 `portfolio_members` 表，解决成员/持仓/候选混用问题
- **统一流转服务**：新增 `opportunity_transitions.py`，候选→观察→成员→订单的幂等事务与审计
- **自动交易切换**：从临时扫描候选切换为有效 auto 成员，支持 manual/confirm/auto 三种执行模式
- **组合回测切换**：使用成员有效期和规则快照，不再依赖最新扫描临时集合
- **绩效归因**：按成员、执行模式、规则版本、候选来源分拆收益
- **功能开关**：OPPORTUNITY_CENTER_ENABLED、PORTFOLIO_MEMBERS_ENABLED 等分阶段启用
- **标的研究收口**：拆分 InvestmentCenter 为共享研究组件，迁出收藏/提醒/风控到对应模块

## Impact
- Affected specs: 无（首次改造）
- Affected code: 前端 9+ 组件（App.tsx, Discovery.tsx, InvestmentCenter.tsx, PortfolioWorkbench.tsx, Trading.tsx 等），后端 10+ 服务/模型（watchlist.py, portfolio.py, sim_account.py, auto_trade_task.py, portfolio_backtest.py 等）
- 新增: portfolio_members 模型, opportunity_transitions 服务, OpportunityCenter 前端组件, 标的研究共享壳层
- 不重写: 评分算法、扫描算法、回测引擎、模拟成交、因子仓库、