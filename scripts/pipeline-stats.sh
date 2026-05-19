#!/usr/bin/env bash
# pipeline-stats.sh
# Print aggregated counts across all output/<doc>/kb-drafts/ subdirectories.
# Shows totals for unverified/skipped (Stage B) and PASS/FLAG/ERROR (Stage C).
#
# Usage: ./scripts/pipeline-stats.sh
#        ./scripts/pipeline-stats.sh /path/to/other/output

set -u

OUTPUT_DIR="${1:-output}"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

if [ ! -d "$OUTPUT_DIR" ]; then
    echo "Output directory not found: $OUTPUT_DIR"
    exit 1
fi

# Helper: count *.md (or *.skip.txt) files under a path pattern
count_files() {
    local pattern="$1"
    local name_glob="$2"
    find "$OUTPUT_DIR" -type d -name "$pattern" 2>/dev/null \
        | xargs -I{} find {} -name "$name_glob" 2>/dev/null \
        | wc -l
}

# Totals
total_unverified=$(count_files "unverified" "*.md")
total_skipped=$(count_files "skipped" "*.skip.txt")
total_pass=$(count_files "auto-approved" "*.md")
total_flag=$(count_files "needs-review" "*.md")
total_err=$(count_files "verification-errors" "*.md")

echo -e "${BOLD}${CYAN}=== Pipeline Stats ===${NC}"
echo "Source: $OUTPUT_DIR/"
echo ""

echo -e "${BOLD}Stage B (generate-kbs):${NC}"
echo "  KB drafts generated:  $total_unverified"
echo "  Sections skipped:     $total_skipped"
if [ "$total_unverified" -gt 0 ] || [ "$total_skipped" -gt 0 ]; then
    total_b=$((total_unverified + total_skipped))
    gen_pct=$(awk -v g="$total_unverified" -v t="$total_b" 'BEGIN {if (t > 0) printf "%.1f", 100 * g / t; else print "0.0"}')
    echo "  Generation rate:      ${gen_pct}%"
fi
echo ""

echo -e "${BOLD}Stage C (verify-kbs):${NC}"
echo -e "  ${GREEN}PASS  (auto-approved):${NC}  $total_pass"
echo -e "  ${YELLOW}FLAG  (needs-review):${NC}   $total_flag"
echo -e "  ERROR (verification):    $total_err"

if [ "$total_pass" -gt 0 ] || [ "$total_flag" -gt 0 ]; then
    verified=$((total_pass + total_flag + total_err))
    pass_pct=$(awk -v p="$total_pass" -v v="$verified" 'BEGIN {if (v > 0) printf "%.1f", 100 * p / v; else print "0.0"}')
    echo "  Pass rate:               ${pass_pct}%"
fi
echo ""

echo -e "${BOLD}Per PDF breakdown:${NC}"
printf "  %5s  %5s  %5s  %5s  %5s  %s\n" "GEN" "SKIP" "PASS" "FLAG" "ERR" "PDF"
echo "  --------------------------------------"

for d in "$OUTPUT_DIR"/*/; do
    [ -d "$d" ] || continue
    base=$(basename "$d")

    gen=$(find "$d/kb-drafts/unverified" -name "*.md" 2>/dev/null | wc -l)
    skip=$(find "$d/kb-drafts/skipped" -name "*.skip.txt" 2>/dev/null | wc -l)
    pass=$(find "$d/kb-drafts/auto-approved" -name "*.md" 2>/dev/null | wc -l)
    flag=$(find "$d/kb-drafts/needs-review" -name "*.md" 2>/dev/null | wc -l)
    err=$(find "$d/kb-drafts/verification-errors" -name "*.md" 2>/dev/null | wc -l)

    printf "  %5d  %5d  %5d  %5d  %5d  %s\n" "$gen" "$skip" "$pass" "$flag" "$err" "$base"
done

echo ""
echo "Done."