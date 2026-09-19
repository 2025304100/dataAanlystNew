---
name: "factor-acl-governance"
description: "Executes factor-domain anticorruption checklist: P1.1 Settings 12->scoring thin-facade, P1.2 train/freeze/activate via Facade+gates, P2 long-guard fixes, and P0 3-piece re-audit (frontend cross-domain / backend cross-domain / destructive regression). Invoke when continuing docs/p04-anticorruption-followup-checklist remaining P1/P2 items or any factor-center page needs native->scoring migration plus P0 anti-cross-domain re-verification."
---

# Factor ACL Governance

Factor-domain anticorruption standard workflow. Covers P1.1 Settings 12 native->scoring thin-facade migration, P1.2 FactorModelPage writes (train / freeze / activate) via Facade (with mandatory training gates), P2 long-guard 5 items, and P0 3-piece re-audit.

## 0. Entry files
- Handover checklist: docs/p04-anticorruption-followup-checklist.md
- Master plan (recommended order section 10, write-back section 11): docs/factors-refactor-plan.md
- Deliverables: app/services/factors/__facade__.py; app/api/routes/scoring_facade.py; frontend/src/api/client.ts; frontend/src/components/FactorModelSettings.tsx; frontend/src/components/factors/FactorModelPage.tsx
- Audits: scripts/audit-frontend-cross-domain.py; scripts/audit-backend-cross-domain.py
- Regressions: tmp/_p04_final_smoke.py; tmp/_p044_final_proof.py

## 1. Preflight (run first every time)
1. py_compile: python -m py_compile app/services/factors/__facade__.py app/api/routes/scoring_facade.py app/services/discovery_data_prep.py tests/conftest.py scripts/audit-frontend-cross-domain.py scripts/audit-backend-cross-domain.py tmp/_p04_final_smoke.py tmp/_p044_final_proof.py
2. tsc sanity (frontend dir): npx tsc --noEmit --target es2020 --module esnext --moduleResolution bundler --jsx preserve --skipLibCheck --esModuleInterop --allowSyntheticDefaultImports --strict false src/api/client.ts src/components/FactorModelSettings.tsx src/components/factors/FactorModelPage.tsx
   - Allow exactly 1 legacy error: "Property env does not exist on type ImportMeta" in i18n
3. Server probe: GET http://127.0.0.1:8000/api/v1/scoring/models?scope=any&limit=1 must 200

## 2. P1.1 Settings 12->scoring mapping
Replacements in FactorModelSettings.tsx:
- listFactorSets + typeof guards  -> scoringListFactorSetsAsFactor
- getFactorOverview              -> scoringGetOverviewAsFactor
- getFactorModels(undefined,20)  -> scoringGetFactorModelListAsFactor(20)
- listFactorPipelineTasks(5)     -> scoringListTasks("factor_pipeline", 5)
- getFactorPipelineTask(id)      -> scoringGetTask(id)
- getFactorPipelineEta(a,b)      -> scoringGetPipelineEta(a,b)
- createFactorPipelineTask       -> scoringCreateTask (+actor settings:pipeline + source_hint)
- updateFactorSystemConfig(flag) -> scoringUpdateSystemConfig(flag, "settings:toggle")
- initializeFactorWarehouse()    -> scoringInitializeWarehouse(false, "settings:init")
- cancelFactorPipelineTask(id)   -> scoringCancelTask(id)
- activateFactorModel            -> scoringActivateModel
- fallbackFactorModel            -> scoringFallbackToManual

Backend thin-facade functions required in __facade__.py: list_scoring_tasks, get_scoring_pipeline_eta, update_scoring_system_config (whitelist feature_enabled only), init_scoring_warehouse (idempotent when warehouse_available=true), activate_scoring_model (reuse runtime gates + audit), fallback_scoring_model.
Client.ts api block (top-level property): scoringListTasks / scoringGetPipelineEta / scoringUpdateSystemConfig / scoringInitializeWarehouse / scoringActivateModel / scoringFallbackToManual.

DTO shape adapters in client.ts module scope BEFORE the P1 UI Stitching comment:
- _scoringOverviewToFactorOverview -> { runtime, config, feature_enabled, health, factor_coverage }
- _scoringModelsToFactorModelList -> { runtime, items } with metrics{validation_ic,sample_count} filled + rejection_reason/weights/audit/activated_at compatibility
- _scoringFactorSetsToFactorSets -> label<->name bidirectional; n_members<->member_count
Then expose three as-factor wrappers inside api object: scoringGetOverviewAsFactor / scoringGetFactorModelListAsFactor / scoringListFactorSetsAsFactor.

## 3. P1.2 FactorModelPage writes
Backend invariant: def train_scoring_model(factorset_id, mode, actor) MUST call run_training_eligibility_gates(coverage>=0.70, IC in [0.01, 0.10], lookback_days=30) BEFORE training; any gate not passed -> raise ValueError -> HTTP 400 detail.
Replacements in FactorModelPage.tsx:
- getFactorModels -> scoringGetFactorModelListAsFactor; listFactorSets -> scoringListFactorSetsAsFactor("any",50)
- activateFactorModel -> scoringActivateModel with actor "factor_center:activate"
- fallbackFactorModel -> scoringFallbackToManual with actor "factor_center:fallback"
- freezeFactorSet   -> scoringFreezeFactorSet with actor "factor_center:freeze"
- trainFactorModel({full payload}) -> scoringTrainModel(trainTarget.id, trainMode, "factor_center:train")
Keep getFactorModel native (rich CRUD/weights/audit permissioned under P1.3 comments guard).

## 4. P2 long-guard 5 items
1. client.ts DO NOT USE header + per-method guards + B-class call sites PRECEDING-LINE guard "Factor-domain internal - DO NOT USE outside factor center"
2. DTO schema_version backend + frontend + 6 scoring* drift console.warn
3. tests/conftest.py _p0_hard_import_gate session autouse fixture; ANY forbidden import inside app/**/*.py -> pytest.fail listing first 50 lines
4. P2-4 ExternalDataSync UX semantics NO "因子" word; button/toast: "刷新评分特征 / 启动评分流水线"
5. P1-3 B-class factor CRUD NO migration; only call-site preceding guard comments

## 5. Post-migration re-aid triple (ALWAYS last)
1. Frontend: python scripts/audit-frontend-cross-domain.py  -> expect A_hits=0; B_unguarded_total=0
2. Backend:  python scripts/audit-backend-cross-domain.py   -> expect forbidden_total=0
3. Break:    python tmp/_p044_final_proof.py                -> expect VERDICT 9/9; leak_errors=0
RULE: Any single failure -> STOP feature advancement; fix boundary leakage first.

## 6. Patch script pattern (when >5 needles)
For >5 replacements or >3 files, ALWAYS write intermediate tmp/*.py via Set-Content, then execute. Avoid large multi-needle edits through direct Edit tool. Useful scaffold temp script names: tmp/_p11_survey.py, tmp/_p11_backend_patch.py, tmp/_p11_settings_patch.py, tmp/_p12_page_patch.py.

## 7. Conventions
- Scoring DTO fields: append defaults, NEVER remove legacy keys; bump schema_version if DTO shape changes; frontend drift warn fires automatically
- Frontend writes always use jsonBody option (RequestJsonOptions supports jsonBody?: unknown; executeRequestJson stringifies + sets Content-Type automatically)
- All thin-facade writes carry actor string; use scopes "settings:*", "factor_center:*", "ext_page:*"
- FactorModelSettings.tsx and factors/FactorModelPage.tsx are B-class; keep DO NOT USE guard comment at every native call site that still touches api.*Factor* methods
