# ZEUS Fork Maintenance Guide

## 架構說明

本 repo 是 `NousResearch/hermes-agent` 的 ZEUS 自訂 fork。
ZEUS 在 upstream 基礎上加入了大量自訂模組，因此**無法直接升級 upstream**。

- **本機 repo**: `https://github.com/Eric0831/hermes-agent-zeus`
- **Upstream**: `https://github.com/NousResearch/hermes-agent`
- **工作 branch**: `zeus-fork-v39`
- **主要 remote**:
  - `origin` → Eric0831/hermes-agent-zeus（本機推送目標）
  - `upstream` → NousResearch/hermes-agent（只讀，cherry-pick 來源）

---

## 本機自訂模組清單（永不從 upstream 覆蓋）

### 🔴 核心架構（完全自訂）

| 模組 | 說明 |
|------|------|
| `brain/` (55 個檔案) | ZEUS 安全/政策/進化/記憶系統 |
| `run_agent.py` | AIAgent 核心（含 brain hooks） |
| `gateway/run.py` | Gateway 核心（含 ZEUS routing） |
| `hermes_cli/config.py` | 配置系統（含 ZEUS 配置項） |
| `model_tools.py` | 工具系統（含 brain 整合） |
| `agent/smart_model_routing.py` | ZEUS 模型路由（upstream 已刪除） |
| `gateway/runtime_metadata.py` | ZEUS 診斷模組（upstream 已刪除） |
| `toolsets.py` | toolset 定義（含 ZEUS tools） |

### 🟡 部分自訂（需謹慎 cherry-pick）

| 模組 | 說明 |
|------|------|
| `cron/scheduler.py` | ZEUS cron 修改（已修復 credential_pool） |
| `tools/` | 部分工具有 ZEUS 自訂 |
| `tests/` | 含 ZEUS 自訂模組測試 |

---

## 升級策略

### 絕對不做
- ❌ `git merge upstream/main`
- ❌ `git rebase upstream/main`
- ❌ 直接覆蓋任何核心架構檔案
- ❌ `hermes update`（套件升級同樣危險）

### 可以做（選擇性 cherry-pick）

```bash
# 1. 先掃描 upstream 新增 commits
bash scripts/zeus_upstream_sync.sh

# 2. 對 SAFE 類型的 commits 直接 cherry-pick
git cherry-pick <SHA>

# 3. 對 REVIEW 類型先確認
git show <SHA>
# 確認沒有碰到核心架構後再 cherry-pick

# 4. SKIP 類型永遠不要 cherry-pick
```

### 安全的 cherry-pick 類型
- ✅ 測試修復（只改 `tests/`）
- ✅ 文件更新（只改 `docs/`, `README`）
- ✅ CI/CD 配置（只改 `.github/`）
- ✅ 非核心 tools 的 bug fix（如 `tools/browser_tool.py`）
- ✅ 新增 platform adapter（`gateway/platforms/` 新檔案）

### 危險的 cherry-pick 類型
- ❌ 修改 `run_agent.py`
- ❌ 修改 `gateway/run.py`
- ❌ 修改 `brain/` 任何模組
- ❌ 修改 `hermes_cli/config.py`
- ❌ 刪除任何模組
- ❌ 修改 `model_tools.py`

---

## 定期維護流程

### 每週
```bash
cd ~/.hermes/hermes-agent
bash scripts/zeus_upstream_sync.sh
# 確認 SAFE commits → cherry-pick
# 確認 REVIEW commits → 人工判斷
```

### 每月
```bash
# 跑完整測試
source venv/bin/activate
python -m pytest tests/ -q

# 推送最新狀態到 fork
git push zeus zeus-fork-v39
```

### 重大版本
```bash
# 建立新 branch（如 upstream 發布 v0.11）
git checkout -b zeus-fork-v40
git push zeus zeus-fork-v40
```

---

## Git Remote 設定

```bash
# 確認 remote
git remote -v
# origin    https://github.com/Eric0831/hermes-agent-zeus.git
# upstream  https://github.com/NousResearch/hermes-agent.git

# fetch upstream（不合併）
git fetch upstream

# 推送到自己的 fork
git push zeus zeus-fork-v39
```

---

## 衝突歷史記錄

| 日期 | 衝突類型 | 處理方式 | 結果 |
|------|---------|---------|------|
| 2026-04-24 | `credential_pool` 參數衝突 | 移除不支援參數 | ✅ 修復 |
| 2026-04-24 | `Platform.FEISHU` 不存在 | 軟性載入 | ✅ 修復 |
| 2026-04-24 | cron footer 缺少 invisible note | 補上文字 | ✅ 修復 |
| 2026-04-24 | `brain/` 整個目錄 upstream 已刪除 | 保留本機版本 | ✅ 保留 |
| 2026-04-24 | `smart_model_routing` upstream 已刪除 | 保留本機版本 | ✅ 保留 |
| 2026-04-24 | `runtime_metadata` upstream 已刪除 | 保留本機版本 | ✅ 保留 |
