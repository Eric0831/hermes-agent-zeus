# Fork Strategy — 2026-04-26 Decision

## 背景與現況

`Eric0831/hermes-agent-zeus` 從 `NousResearch/hermes-agent` fork 後，已經演變為一份
**獨立 codebase**，不再是「微調的 fork」：

| 對比項 | 數量 | 說明 |
|---|---|---|
| Upstream 有 / ZEUS 沒有 | **1410 檔** | bedrock_adapter、codex_responses_adapter、context_engine、credential_sources… |
| 兩邊都有但 ZEUS 已 modify | **498 檔** | 大量核心檔已演化 |
| ZEUS-only（brain/* 等自訂） | **194 檔** | ZEUS V40 brain ensemble + scripts |
| ZEUS 落後 upstream 的 commits | **3117** | 自 fork 後 |

## 結論：放棄「fork 同步」，定位為獨立 codebase

### 為什麼不再 sync？
1. **Cherry-pick 實測 5/5 全 conflict**（2026-04-26）— 即使選最小 / 最安全的 5 個 upstream commits，每個都因為 ZEUS 的檔案改動 / 刪除而衝突
2. **Rebase / merge 不可行** — 1410 個 missing 檔案 + 498 個已 modify 檔案 = 衝突量無解
3. **架構偏離本質** — ZEUS 加上 V40 brain 系統、cron scheduler 客製、memory v2、智慧路由等，跟 hermes-agent 的演進方向已分歧

### 新策略：**reference + self-implementation**
- ✅ Upstream 視為**參考資料**，不再嘗試自動同步
- ✅ 每月看 upstream 的 fix/security/feat 列表，識別「我們也有的問題」或「我們想要的功能」
- ✅ 對識別出的需求，**設計符合 ZEUS 架構的等價實作**，不複製 upstream 程式碼
- ✅ Commit 時用 `inspired-by: upstream:<sha> — <description>` 紀錄參考來源

## 工作流程

> **🚨 全流程禁止自動化**：upstream 永遠是 read-only 參考。每一步都需人工
> 確認後才執行。`zeus_upstream_watch.sh` 內建 TTY 檢查會拒絕在 cron / systemd
> timer / pipe 環境執行（除非顯式 `--yes` 旗標）。

### 月度 triage（**建議**每月第一個工作日，**人工觸發**）
**步驟 1 — 人工終端跑 watcher，產報告**
```bash
cd ~/.hermes/hermes-agent
bash scripts/zeus_upstream_watch.sh > docs/upstream-watch-$(date +%Y-%m).md
```

**步驟 2 — 人工 review 報告**（不自動）
- 看 🔒 SECURITY / ✨ FEAT / 🐛 FIX 三段
- 對每個有興趣的 commit，**人工**判斷是否跟 ZEUS 相關

**步驟 3 — 人工確認後**才能進入實作階段
- 對 confirmed 的需求開 task / issue
- **人工**讀 upstream 邏輯，**人工**設計 ZEUS-native 等價實作
- **不可** cherry-pick / merge / rebase 任何 upstream commit

### 緊急 backport（CVE / security）— 仍需人工
1. 監控 https://github.com/NousResearch/hermes-agent/security/advisories
2. 收到 advisory → **人工** review → 判斷是否影響 ZEUS 架構 → 如有，**人工**手寫 patch
3. 即使是 critical CVE，也不允許自動套用 upstream patch

### Self-driven updates（ZEUS 自有開發）
ZEUS 自己的 fixes/features 直接在主線開發，**人工** commit：
- `feat(zeus): <description>` — 新功能
- `fix(zeus): <description>` — 修補
- `inspired-by: upstream:<sha>` — 提及上游靈感（選用）

### 為什麼禁止自動化？
1. **架構分歧大** — 1410 / 498 / 194 檔案差異，cherry-pick 必衝突
2. **自動套會破** — 5/5 實測 upstream commits 全部 conflict 在 protected files
3. **ZEUS 客製需要 context** — 機械化 sync 會破壞 brain/* 等模組的設計意圖
4. **safety-by-design** — production 跑這份 working tree，無法承受意外改動

## Branch 結構（變更後）

```
main                                          ← canonical primary
                                                (前身 zeus-fork-v39，2026-04-26 重命名)
archive/main-pre-zeus-v39-2026-04-26          ← 凍結舊 main（落後 upstream 3117）
archive/cherry-picks-attempt-20260420         ← 凍結 4/20 cherry-pick 實驗
archive/origin-main-overwritten-2026-04-26    ← force push 前的 origin/main 備份
wip-v40-brain-incomplete                      ← V40 brain WIP 暫存
upstream/main                                 ← read-only reference，never merge
```

## PROTECTED_FILES 列表已過時

原 `scripts/zeus_upstream_sync.sh` 的 PROTECTED_FILES 在新策略下意義降低 —
所有 ZEUS codebase 都是「我們的」，不再有「保護 vs 可同步」的分類。
保留作為「歷史上跟 upstream 偏離最多的高風險檔案」記錄。

## 何時可能改回 fork 策略？

1. 如果 ZEUS 自有功能被 upstream 也實作（趨同）→ 重新評估
2. 如果 upstream 有重大架構改造（如 V2 protocol）我們也想用 → 視作 ground-up rewrite，不是 sync
3. 否則：保持獨立 codebase 路線
