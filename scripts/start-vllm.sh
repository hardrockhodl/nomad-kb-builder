#!/usr/bin/env bash
# start-vllm.sh
# Starts vLLM server with Qwen3.6-27B on GH200
# Usage: ./start-vllm.sh

set -eu

MODEL_PATH="$HOME/models/Qwen3.6-27B"
LOG_DIR="$HOME/vllm-logs"
PORT=8000

mkdir -p "$LOG_DIR"

# Verify model exists
if [ ! -d "$MODEL_PATH" ]; then
    echo "Error: Model not found at $MODEL_PATH"
    exit 1
fi

# Stop existing container if running
if sudo docker ps -a --format '{{.Names}}' | grep -q '^vllm-qwen$'; then
    echo "Stopping existing vllm-qwen container..."
    sudo docker stop vllm-qwen >/dev/null 2>&1 || true
    sudo docker rm vllm-qwen >/dev/null 2>&1 || true
fi

echo "Starting vLLM server with Qwen3.6-27B..."
echo "Model:   $MODEL_PATH"
echo "Port:    $PORT"
echo "Logs:    $LOG_DIR/vllm.log"
echo ""

sudo docker run -d \
    --name vllm-qwen \
    --gpus all \
    --ipc=host \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    -p ${PORT}:8000 \
    -v "$MODEL_PATH":/model \
    -v "$LOG_DIR":/logs \
    --restart unless-stopped \
    nvcr.io/nvidia/vllm:26.04-py3 \
    vllm serve /model \
        --served-model-name Qwen3.6-27B \
        --gpu-memory-utilization 0.85 \
        --max-model-len 32768 \
        --max-num-seqs 64 \
        --dtype bfloat16 \
        --host 0.0.0.0 \
        --port 8000 \
        --enable-prefix-caching

echo ""
echo "Container started. Watching startup logs..."
echo "Press Ctrl+C to stop watching (container keeps running)."
echo ""
sleep 3
sudo docker logs -f vllm-qwen
