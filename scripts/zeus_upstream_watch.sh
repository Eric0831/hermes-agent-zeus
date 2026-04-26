#!/usr/bin/env bash
# ============================================================
# ZEUS Upstream Watcher — 參考用，不再嘗試 sync
# 用途：列出 upstream 最近的 commits 分類後讓人工 triage
# 替代：scripts/zeus_upstream_sync.sh（已棄用，因 cherry-pick 全衝突）
# ============================================================
#
# 策略背景見 docs/FORK_STRATEGY.md
#
# 用法：
#   bash scripts/zeus_upstream_watch.sh            # 預設看最近 100 commits
#   bash scripts/zeus_upstream_watch.sh 200        # 看最近 200 commits
#   bash scripts/zeus_upstream_watch.sh > docs/upstream-watch-$(date +%Y-%m).md
#
# 輸出分類：
#   🔒 SECURITY     — 含 security/CVE/sec 關鍵字
#   🐛 FIX          — fix( 開頭
#   ✨ FEAT         — feat( 開頭（潛在我們也想要的功能）
#   📚 DOC/TEST     — docs/test/ci/chore（通常可忽略）
#   ❓ OTHER        — 其他（refactor、merge 等）

set +e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LIMIT="${1:-100}"

cd "$REPO_DIR"

echo "========================================================"
echo " ZEUS Upstream Watch — $(date '+%Y-%m-%d %H:%M')"
echo " Range: HEAD..upstream/main (latest $LIMIT commits)"
echo "========================================================"
echo

git fetch upstream --quiet 2>/dev/null

TOTAL_BEHIND=$(git log HEAD..upstream/main --oneline 2>/dev/null | wc -l)
echo "📊 Total upstream commits we don't have: $TOTAL_BEHIND"
echo

# 分類
SECURITY=()
FIX=()
FEAT=()
DOC=()
OTHER=()

while IFS= read -r line; do
    SHA=$(echo "$line" | awk '{print $1}')
    MSG=$(echo "$line" | cut -d' ' -f2-)

    if echo "$MSG" | grep -qiE "security|cve|sec\(|vulnerab"; then
        SECURITY+=("$SHA $MSG")
    elif echo "$MSG" | grep -qE "^fix\("; then
        FIX+=("$SHA $MSG")
    elif echo "$MSG" | grep -qE "^feat\("; then
        FEAT+=("$SHA $MSG")
    elif echo "$MSG" | grep -qE "^(docs|test|chore|ci)\("; then
        DOC+=("$SHA $MSG")
    else
        OTHER+=("$SHA $MSG")
    fi
done < <(git log HEAD..upstream/main --oneline 2>/dev/null | head -"$LIMIT")

echo "## 🔒 SECURITY (${#SECURITY[@]}) — review immediately"
echo
for c in "${SECURITY[@]}"; do echo "- $c"; done
[ ${#SECURITY[@]} -eq 0 ] && echo "_(none in this batch)_"
echo

echo "## ✨ FEAT (${#FEAT[@]}) — evaluate if we want similar"
echo
for c in "${FEAT[@]}"; do echo "- $c"; done
echo

echo "## 🐛 FIX (${#FIX[@]}) — check if we have same bug"
echo
for c in "${FIX[@]}"; do echo "- $c"; done
echo

echo "## ❓ OTHER (${#OTHER[@]}) — refactors / merges / misc"
echo
for c in "${OTHER[@]}"; do echo "- $c"; done
echo

echo "## 📚 DOC/TEST/CHORE (${#DOC[@]}) — usually safe to ignore"
echo
echo "_(${#DOC[@]} commits — see git log if needed)_"
echo

echo "---"
echo
echo "## 下一步"
echo
echo "1. Review SECURITY — 全部（必看）"
echo "2. Review FEAT/FIX — 挑跟 ZEUS 架構相關的，加進 backport queue"
echo "3. **不要 cherry-pick** — 讀 upstream 邏輯，在 ZEUS codebase 寫等價實作"
echo "4. Commit 時加 'inspired-by: upstream:<sha>' 標記"
echo
echo "策略詳見 docs/FORK_STRATEGY.md"
