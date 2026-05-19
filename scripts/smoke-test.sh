#!/usr/bin/env bash
# smoke-test.sh
# Verify vLLM server is alive and responding

set -u

PORT=8000
URL="http://localhost:${PORT}"

echo "=== Health check ==="
curl -s "${URL}/health" || echo "Server not responding on ${URL}"

echo ""
echo "=== Available models ==="
curl -s "${URL}/v1/models" | python3 -m json.tool

echo ""
echo "=== Chat completion test ==="
curl -s "${URL}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{
        "model": "Qwen3.6-27B",
        "messages": [
            {"role": "system", "content": "You are a network engineering expert."},
            {"role": "user", "content": "In one sentence: what does OSPF stand for?"}
        ],
        "max_tokens": 64,
        "temperature": 0.1
    }' | python3 -m json.tool
