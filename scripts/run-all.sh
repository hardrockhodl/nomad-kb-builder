#!/usr/bin/env bash
# run-all.sh
# Run the full kb-builder pipeline on every PDF in input/.
#
# Stages:
#   1. Clean output/
#   2. Stage A: Extract (Step 1) + Detect sections (Step 2) on all PDFs sequentially
#   3. Stage B: Generate KBs (Step 3) on all PDFs
#   4. Stage C: Verify KBs (Step 4) on all PDFs
#
# Each stage is timed independently. Final report shows totals.
#
# Usage: ./scripts/run-all.sh
#        SKIP_CLEAN=1 ./scripts/run-all.sh                   # don't wipe output/ first
#        STAGE=A ./scripts/run-all.sh                        # run only Stage A
#        STAGE=BC ./scripts/run-all.sh                       # run only Stages B and C
#        PLATFORM_OVERRIDE=ios-xr ./scripts/run-all.sh       # force platform on all generated KBs

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

INPUT_DIR="input"
OUTPUT_DIR="output"
LOG_DIR="logs/run-all-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"

# Color codes
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

STAGE="${STAGE:-ABC}"
SKIP_CLEAN="${SKIP_CLEAN:-0}"
PLATFORM_OVERRIDE="${PLATFORM_OVERRIDE:-}"

# Ensure venv is active
if [ -z "${VIRTUAL_ENV:-}" ]; then
    echo -e "${RED}Error: no Python venv active. Run 'source venv/bin/activate' first.${NC}"
    exit 1
fi

# Ensure vLLM backend is selected
export LLM_BACKEND="vllm"

# Verify vLLM is reachable before starting
echo -e "${CYAN}Verifying vLLM server reachable...${NC}"
if ! curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    echo -e "${RED}vLLM server not reachable on http://localhost:8000${NC}"
    echo "Start it with: ./scripts/start-vllm.sh"
    exit 1
fi
echo -e "${GREEN}OK${NC}"
echo ""

# Collect PDF list
mapfile -t PDFS < <(find "$INPUT_DIR" -maxdepth 1 -name "*.pdf" -type f | sort)
if [ "${#PDFS[@]}" -eq 0 ]; then
    echo -e "${RED}No PDFs found in $INPUT_DIR/${NC}"
    exit 1
fi

echo -e "${BOLD}Pipeline run starting${NC}"
echo "  PDFs found: ${#PDFS[@]}"
echo "  Log dir:    $LOG_DIR"
echo "  Stages:     $STAGE"
if [ -n "$PLATFORM_OVERRIDE" ]; then
    echo -e "  Platform override:    ${YELLOW}$PLATFORM_OVERRIDE${NC}"
fi
echo ""

# ----------------------------------------------------------------------------
# Cleanup
# ----------------------------------------------------------------------------
if [ "$STAGE" = "ABC" ] && [ "$SKIP_CLEAN" = "0" ]; then
    echo -e "${CYAN}Cleaning output/ directory...${NC}"
    if [ -d "$OUTPUT_DIR" ]; then
        rm -rf "${OUTPUT_DIR:?}"/*
        echo "  Removed previous output."
    else
        mkdir -p "$OUTPUT_DIR"
    fi
    echo ""
fi

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

# Format seconds as "Hh Mm Ss" or "Mm Ss" or "Ss"
fmt_duration() {
    local sec="$1"
    awk -v s="$sec" 'BEGIN {
        h = int(s/3600); s -= h*3600
        m = int(s/60);   s -= m*60
        if (h > 0) printf "%dh %dm %.1fs", h, m, s
        else if (m > 0) printf "%dm %.1fs", m, s
        else printf "%.1fs", s
    }'
}

# Stage tracking
declare -A STAGE_STARTS
declare -A STAGE_ENDS

stage_start() {
    STAGE_STARTS[$1]=$(date +%s.%N)
    echo -e "${BOLD}${CYAN}=== Stage $1: $2 ===${NC}"
    echo ""
}

stage_end() {
    local name="$1"
    STAGE_ENDS[$name]=$(date +%s.%N)
    local elapsed
    elapsed=$(awk -v a="${STAGE_STARTS[$name]}" -v b="${STAGE_ENDS[$name]}" 'BEGIN {print b - a}')
    echo ""
    echo -e "${GREEN}Stage $name complete in $(fmt_duration "$elapsed")${NC}"
    echo ""
}

# Strip path and .pdf extension, get the basename used for output dir
output_basename() {
    local pdf="$1"
    basename "$pdf" .pdf
}

# ----------------------------------------------------------------------------
# Stage A: Extract + Detect sections
# ----------------------------------------------------------------------------
run_stage_a() {
    stage_start "A" "Extract + Detect sections (all PDFs)"
    local pdf basename log
    local idx=0
    local total=${#PDFS[@]}

    for pdf in "${PDFS[@]}"; do
        idx=$((idx + 1))
        basename=$(output_basename "$pdf")
        log="$LOG_DIR/A-${idx}-${basename}.log"

        echo -e "${YELLOW}[A $idx/$total]${NC} $basename"

        # Step 1: extract
        if ! python kb_builder.py extract "$pdf" >> "$log" 2>&1; then
            echo -e "  ${RED}EXTRACT FAILED${NC} - see $log"
            continue
        fi

        # Step 2: detect-sections
        if ! python kb_builder.py detect-sections "$OUTPUT_DIR/$basename" >> "$log" 2>&1; then
            echo -e "  ${RED}DETECT-SECTIONS FAILED${NC} - see $log"
            continue
        fi

        # Pull key stats from log
        local sections
        sections=$(grep -oE "Total H[23] sections.*[0-9]+" "$log" 2>/dev/null | head -1 || echo "?")
        echo "  done"
    done

    stage_end "A"
}

# ----------------------------------------------------------------------------
# Stage B: Generate KBs
# ----------------------------------------------------------------------------
run_stage_b() {
    stage_start "B" "Generate KBs (vLLM, parallel)"
    local pdf basename log
    local idx=0
    local total=${#PDFS[@]}

    for pdf in "${PDFS[@]}"; do
        idx=$((idx + 1))
        basename=$(output_basename "$pdf")
        log="$LOG_DIR/B-${idx}-${basename}.log"

        if [ ! -f "$OUTPUT_DIR/$basename/sections.json" ]; then
            echo -e "${YELLOW}[B $idx/$total]${NC} $basename - ${RED}SKIP (no sections.json)${NC}"
            continue
        fi

        echo -e "${YELLOW}[B $idx/$total]${NC} $basename"

        local pdf_start pdf_end pdf_elapsed
        local -a gen_cmd
        gen_cmd=(python kb_builder.py generate-kbs "$OUTPUT_DIR/$basename")
        if [ -n "$PLATFORM_OVERRIDE" ]; then
            gen_cmd+=(--platform-override "$PLATFORM_OVERRIDE")
        fi

        pdf_start=$(date +%s.%N)
        if ! "${gen_cmd[@]}" >> "$log" 2>&1; then
            echo -e "  ${RED}GENERATE FAILED${NC} - see $log"
            continue
        fi
        pdf_end=$(date +%s.%N)
        pdf_elapsed=$(awk -v a="$pdf_start" -v b="$pdf_end" 'BEGIN {print b - a}')

        local generated skipped errored
        generated=$(grep -oE "Generated[[:space:]]+[0-9]+" "$log" | tail -1 | awk '{print $2}')
        skipped=$(grep -oE "Skipped[[:space:]]+[0-9]+" "$log" | tail -1 | awk '{print $2}')
        errored=$(grep -oE "Errored[[:space:]]+[0-9]+" "$log" | tail -1 | awk '{print $2}')
        echo "  generated=${generated:-?} skipped=${skipped:-?} errored=${errored:-?} (wall=$(fmt_duration "$pdf_elapsed"))"
    done

    stage_end "B"
}

# ----------------------------------------------------------------------------
# Stage C: Verify KBs
# ----------------------------------------------------------------------------
run_stage_c() {
    stage_start "C" "Verify KBs (vLLM, parallel)"
    local pdf basename log
    local idx=0
    local total=${#PDFS[@]}

    for pdf in "${PDFS[@]}"; do
        idx=$((idx + 1))
        basename=$(output_basename "$pdf")
        log="$LOG_DIR/C-${idx}-${basename}.log"

        if [ ! -d "$OUTPUT_DIR/$basename/kb-drafts/unverified" ]; then
            echo -e "${YELLOW}[C $idx/$total]${NC} $basename - ${RED}SKIP (no unverified/)${NC}"
            continue
        fi

        echo -e "${YELLOW}[C $idx/$total]${NC} $basename"

        local pdf_start pdf_end pdf_elapsed
        pdf_start=$(date +%s.%N)
        if ! python kb_builder.py verify-kbs "$OUTPUT_DIR/$basename" >> "$log" 2>&1; then
            echo -e "  ${RED}VERIFY FAILED${NC} - see $log"
            continue
        fi
        pdf_end=$(date +%s.%N)
        pdf_elapsed=$(awk -v a="$pdf_start" -v b="$pdf_end" 'BEGIN {print b - a}')

        local passed flagged
        passed=$(grep -oE "PASS \(auto-approved\)[[:space:]]+[0-9]+" "$log" | tail -1 | awk '{print $NF}')
        flagged=$(grep -oE "FLAG \(needs-review\)[[:space:]]+[0-9]+" "$log" | tail -1 | awk '{print $NF}')
        echo "  passed=${passed:-?} flagged=${flagged:-?} (wall=$(fmt_duration "$pdf_elapsed"))"
    done

    stage_end "C"
}

# ----------------------------------------------------------------------------
# Run selected stages
# ----------------------------------------------------------------------------
RUN_START=$(date +%s.%N)

case "$STAGE" in
    *A*) run_stage_a ;;
esac

case "$STAGE" in
    *B*) run_stage_b ;;
esac

case "$STAGE" in
    *C*) run_stage_c ;;
esac

RUN_END=$(date +%s.%N)
RUN_TOTAL=$(awk -v a="$RUN_START" -v b="$RUN_END" 'BEGIN {print b - a}')

# ----------------------------------------------------------------------------
# Final report
# ----------------------------------------------------------------------------
echo -e "${BOLD}${GREEN}=== Pipeline complete ===${NC}"
echo ""
echo "Total wall-clock time: $(fmt_duration "$RUN_TOTAL")"
echo "Logs:                  $LOG_DIR/"
echo ""
echo -e "${BOLD}Aggregated stats:${NC}"

if [ -d "$OUTPUT_DIR" ]; then
    local total_kbs total_skipped total_pass total_flag
    total_kbs=$(find "$OUTPUT_DIR" -type d -name "unverified" 2>/dev/null | xargs -I{} find {} -name "*.md" 2>/dev/null | wc -l)
    total_skipped=$(find "$OUTPUT_DIR" -type d -name "skipped" 2>/dev/null | xargs -I{} find {} -name "*.skip.txt" 2>/dev/null | wc -l)
    total_pass=$(find "$OUTPUT_DIR" -type d -name "auto-approved" 2>/dev/null | xargs -I{} find {} -name "*.md" 2>/dev/null | wc -l)
    total_flag=$(find "$OUTPUT_DIR" -type d -name "needs-review" 2>/dev/null | xargs -I{} find {} -name "*.md" 2>/dev/null | wc -l)

    echo "  KB drafts generated:    $total_kbs"
    echo "  Sections skipped:       $total_skipped"
    echo "  Auto-approved (Step 4): $total_pass"
    echo "  Needs-review (Step 4):  $total_flag"
fi

echo ""
echo "Done."