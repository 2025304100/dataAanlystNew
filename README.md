# 个人量化工作台

## 测试

本项目采用四层测试体系，详见 [docs/test-architecture.md](docs/test-architecture.md)。

### 快速运行

```bash
# 后端白盒测试（无需后端运行）
pytest -m whitebox

# 前端测试
cd frontend && npm test

# 后端黑盒测试（需先启动后端）
python -m uvicorn app.main:app --port 8000 &
pytest -m blackbox

# E2E 测试（需前后端 + Playwright）
pytest tests/e2e/ -m e2e
```

### 测试分层

| 层级 | 命令 | 用例数 | 说明 |
|------|------|--------|------|
| 白盒单元 | `pytest -m whitebox` | ~260 | 直接调用服务层/路由，无需后端 |
| 前端组件 | `cd frontend && npm test` | ~216 | Vitest + Testing Library |
| 黑盒集成 | `pytest -m blackbox` | ~47 | httpx 直连后端 |
| E2E | `pytest tests/e2e/ -m e2e` | ~27 | Playwright 浏览器 |
| UAT 手动 | 见 [docs/uat-checklist.md](docs/uat-checklist.md) | 167 项 | 人工执行 |

### CI 自动化

- 配置：`.github/workflows/test.yml`
- pre-commit hook：`.pre-commit-config.yaml`（提交前自动跑白盒 + 前端测试）
