#!/usr/bin/env bash
# vllm-bench.sh
# Measure single-stream and batch throughput against the local vLLM server.
# Tells us where the bottleneck is before we start optimizing.
#
# Usage:  ./vllm-bench.sh
#         BATCH_SIZE=64 ./vllm-bench.sh      # override batch size
#         WARM_RUNS=2 ./vllm-bench.sh        # number of warmup rounds before measuring

set -eu

VLLM_HOST="${VLLM_HOST:-http://localhost:8000}"
MODEL="${MODEL:-Qwen3.6-27B}"
BATCH_SIZE="${BATCH_SIZE:-32}"
WARM_RUNS="${WARM_RUNS:-1}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Single-stream test prompt (medium length, 500-word target)
SINGLE_PROMPT='Write 500 words about OSPF routing protocol, including LSA types, area types, and how SPF calculation works.'
SINGLE_MAX_TOKENS=1500

# Batch test prompts (multiple distinct topics so prefix cache hits matter less)
BATCH_PROMPTS=(
    "Write 300 words about BGP path selection algorithm."
    "Write 300 words about EIGRP DUAL algorithm."
    "Write 300 words about VLAN trunking with 802.1Q."
    "Write 300 words about Spanning Tree Protocol convergence."
    "Write 300 words about HSRP and VRRP differences."
    "Write 300 words about MPLS label distribution."
    "Write 300 words about IPSec phase 1 and phase 2."
    "Write 300 words about BGP route reflectors."
)
BATCH_MAX_TOKENS=800

# Color codes for output
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo -e "${CYAN}=== vLLM Benchmark ===${NC}"
echo "Host:       $VLLM_HOST"
echo "Model:      $MODEL"
echo "Batch size: $BATCH_SIZE"
echo "Warm runs:  $WARM_RUNS"
echo ""

# Verify server is reachable
if ! curl -sf "${VLLM_HOST}/health" >/dev/null 2>&1; then
    echo "Server not reachable at $VLLM_HOST" >&2
    exit 1
fi

# Helper: send one request, return (latency_seconds, completion_tokens)
single_request() {
    local prompt="$1"
    local max_tokens="$2"
    local outfile="$3"
    local start end tokens latency

    start=$(date +%s.%N)
    curl -sS "${VLLM_HOST}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$(jq -n \
            --arg model "$MODEL" \
            --arg prompt "$prompt" \
            --argjson max_tokens "$max_tokens" \
            '{
                model: $model,
                messages: [{role: "user", content: $prompt}],
                max_tokens: $max_tokens,
                temperature: 0.1,
                chat_template_kwargs: {enable_thinking: false}
            }')" > "$outfile"
    end=$(date +%s.%N)

    latency=$(echo "$end $start" | awk '{printf "%.3f", $1 - $2}')
    tokens=$(jq -r '.usage.completion_tokens // 0' < "$outfile")
    echo "$latency $tokens"
}

# Warmup
echo -e "${YELLOW}Warmup ($WARM_RUNS rounds, results discarded)...${NC}"
for i in $(seq 1 "$WARM_RUNS"); do
    single_request "$SINGLE_PROMPT" 200 "$TMP_DIR/warmup_${i}.json" >/dev/null
done

echo ""
echo -e "${CYAN}--- Test 1: Single-stream latency ---${NC}"
echo "One request at a time, measure tokens/sec per stream."
echo ""

read -r latency tokens <<< "$(single_request "$SINGLE_PROMPT" "$SINGLE_MAX_TOKENS" "$TMP_DIR/single.json")"
single_tps=$(echo "$tokens $latency" | awk '{printf "%.1f", $1 / $2}')

echo "Latency:         ${latency}s"
echo "Tokens emitted:  ${tokens}"
echo -e "${GREEN}Tokens/sec:      ${single_tps}${NC}"
echo ""

echo -e "${CYAN}--- Test 2: Batch throughput ($BATCH_SIZE requests) ---${NC}"
echo "Fire $BATCH_SIZE requests in parallel, measure aggregate throughput."
echo ""

# Build batch by cycling prompts
batch_start=$(date +%s.%N)
for i in $(seq 0 $((BATCH_SIZE - 1))); do
    prompt="${BATCH_PROMPTS[$((i % ${#BATCH_PROMPTS[@]}))]}"
    (
        curl -sS "${VLLM_HOST}/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -d "$(jq -n \
                --arg model "$MODEL" \
                --arg prompt "$prompt" \
                --argjson max_tokens "$BATCH_MAX_TOKENS" \
                '{
                    model: $model,
                    messages: [{role: "user", content: $prompt}],
                    max_tokens: $max_tokens,
                    temperature: 0.1,
                    chat_template_kwargs: {enable_thinking: false}
                }')" > "$TMP_DIR/batch_${i}.json"
    ) &
done
wait
batch_end=$(date +%s.%N)

batch_elapsed=$(echo "$batch_end $batch_start" | awk '{printf "%.3f", $1 - $2}')
batch_total_tokens=$(jq -s 'map(.usage.completion_tokens // 0) | add' "$TMP_DIR"/batch_*.json)
batch_tps=$(echo "$batch_total_tokens $batch_elapsed" | awk '{printf "%.1f", $1 / $2}')
batch_per_req_avg=$(echo "$batch_elapsed $BATCH_SIZE" | awk '{printf "%.3f", $1 / $2}')

echo "Total elapsed:        ${batch_elapsed}s"
echo "Avg per request:      ${batch_per_req_avg}s"
echo "Total output tokens:  ${batch_total_tokens}"
echo -e "${GREEN}Aggregate tokens/sec: ${batch_tps}${NC}"
echo ""

# Speedup
speedup=$(echo "$batch_tps $single_tps" | awk '{printf "%.1fx", $1 / $2}')
echo -e "${YELLOW}Batch speedup over single-stream: ${speedup}${NC}"
echo ""

# Quick verdict
echo -e "${CYAN}--- Verdict ---${NC}"
expected_single=50
expected_batch_min=400

if (( $(echo "$single_tps < $expected_single" | bc -l) )); then
    echo "Single-stream is lower than expected (${single_tps} < ${expected_single} tok/s)."
    echo "  This suggests model or kernel inefficiency. Consider FP8 quantization."
else
    echo "Single-stream is within expected range."
fi

if (( $(echo "$batch_tps < $expected_batch_min" | bc -l) )); then
    echo "Batch aggregate is lower than expected (${batch_tps} < ${expected_batch_min} tok/s)."
    echo "  Possible fixes:"
    echo "  - Bump --max-num-seqs"
    echo "  - Lower --max-model-len to free KV cache"
    echo "  - Try FP8 quantization (--quantization fp8)"
else
    echo "Batch aggregate is within expected range."
fi

echo ""
echo "Done."