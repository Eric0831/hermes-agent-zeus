#!/usr/bin/env bash
# ============================================================
# ZEUS Upstream Sync — cherry-pick 安全評估腳本
# 用途：掃描 upstream (NousResearch/hermes-agent) 新增 commit，
#       自動分類為 SAFE / REVIEW / SKIP
# 執行：bash scripts/zeus_upstream_sync.sh
# ============================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_REMOTE="upstream"
ZEUS_BRANCH="zeus-fork-v39"
REPORT_FILE="$REPO_DIR/docs/zeus_upstream_sync_$(date +%Y%m%d).md"

# 核心自訂檔案 — 這些永遠 SKIP
PROTECTED_FILES=(
    "brain/"
    "run_agent.py"
    "gateway/run.py"
    "hermes_cli/config.py"
    "model_tools.py"
    "agent/smart_model_routing.py"
    "gateway/runtime_metadata.py"
    "gateway/session_context.py"
    "toolsets.py"
)

echo "=============================================="
echo " ZEUS Upstream Sync Report — $(date '+%Y-%m-%d %H:%M')"
echo "=============================================="
echo ""

cd "$REPO_DIR"

# 取得最新 upstream
echo "📡 Fetching upstream..."
git fetch "$UPSTREAM_REMOTE" --quiet

# 找出本機落後 upstream 的 commits
UPSTREAM_COMMITS=$(git log HEAD..upstream/main --oneline 2>/dev/null | head -50)

if [ -z "$UPSTREAM_COMMITS" ]; then
    echo "✅ 本機與 upstream 同步，無需更新"
    exit 0
fi

echo "📋 Upstream 新增 $(git log HEAD..upstream/main --oneline | wc -l) 個 commits"
echo ""

# 分析每個 commit
SAFE=()
REVIEW=()
SKIP=()

while IFS= read -r line; do
    SHA=$(echo "$line" | awk '{print $1}')
    MSG=$(echo "$line" | cut -d' ' -f2-)

    # 取得此 commit 修改的檔案
    FILES=$(git diff-tree --no-commit-id -r --name-only "$SHA" 2>/dev/null || echo "")

    # 檢查是否碰到 PROTECTED_FILES
    IS_PROTECTED=false
    for pf in "${PROTECTED_FILES[@]}"; do
        if echo "$FILES" | grep -q "^$pf"; then
            IS_PROTECTED=true
            break
        fi
    done

    if $IS_PROTECTED; then
        SKIP+=("$SHA $MSG")
    elif echo "$FILES" | grep -qE "^(tests/|docs/|README|\.github/)"; then
        # 只改測試/文件，安全
        SAFE+=("$SHA $MSG")
    elif echo "$FILES" | grep -qE "^tools/" && ! echo "$FILES" | grep -qE "^(run_agent|model_tools|toolsets)"; then
        # 只改 tools/ 且不碰核心，需人工 review
        REVIEW+=("$SHA $MSG")
    elif echo "$MSG" | grep -qiE "^fix|^chore|^docs|^test|^ci"; then
        # 小修復類型，可能安全，但需 review
        REVIEW+=("$SHA $MSG")
    else
        SKIP+=("$SHA $MSG")
    fi

done <<< "$UPSTREAM_COMMITS"

echo "================================"
echo "✅ SAFE — 可直接 cherry-pick (${#SAFE[@]})"
echo "================================"
for c in "${SAFE[@]}"; do echo "  $c"; done

echo ""
echo "================================"
echo "⚠️  REVIEW — 需人工確認 (${#REVIEW[@]})"
echo "================================"
for c in "${REVIEW[@]}"; do echo "  $c"; done

echo ""
echo "================================"
echo "❌ SKIP — 碰到核心架構 (${#SKIP[@]})"
echo "================================"
for c in "${SKIP[@]}"; do echo "  $c"; done

echo ""
echo "================================"
echo "🎯 建議執行："
echo "================================"
if [ ${#SAFE[@]} -gt 0 ]; then
    echo ""
    echo "# SAFE commits — cherry-pick:"
    for c in "${SAFE[@]}"; do
        SHA=$(echo "$c" | awk '{print $1}')
        echo "git cherry-pick $SHA"
    done
fi
if [ ${#REVIEW[@]} -gt 0 ]; then
    echo ""
    echo "# REVIEW commits — 先 git show 再決定:"
    for c in "${REVIEW[@]}"; do
        SHA=$(echo "$c" | awk '{print $1}')
        echo "git show $SHA  # 確認後再 cherry-pick"
    done
fi

# 儲存報告
mkdir -p "$REPO_DIR/docs"
{
    echo "# ZEUS Upstream Sync Report — $(date '+%Y-%m-%d %H:%M')"
    echo ""
    echo "## SAFE (${#SAFE[@]})"
    for c in "${SAFE[@]}"; do echo "- $c"; done
    echo ""
    echo "## REVIEW (${#REVIEW[@]})"
    for c in "${REVIEW[@]}"; do echo "- $c"; done
    echo ""
    echo "## SKIP (${#SKIP[@]})"
    for c in "${SKIP[@]}"; do echo "- $c"; done
} > "$REPORT_FILE"

echo ""
echo "📄 報告已儲存：$REPORT_FILE"
