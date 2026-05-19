#!/usr/bin/env bash
# stop-vllm.sh
# Stops the vLLM server container

set -u

if sudo docker ps --format '{{.Names}}' | grep -q '^vllm-qwen$'; then
    echo "Stopping vllm-qwen container..."
    sudo docker stop vllm-qwen
    echo "Done. Container is stopped but kept for inspection. Use 'sudo docker rm vllm-qwen' to remove it."
else
    echo "No vllm-qwen container is running."
fi
