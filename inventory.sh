#!/usr/bin/env bash
# gh200_inventory.sh
# Inventory script for Supermicro ARS-111GL-NHR (GH200 Grace Hopper)
# Usage: bash gh200_inventory.sh | tee inventory_$(date +%Y%m%d_%H%M).log

set -u

print_header() {
    echo ""
    echo "=============================================="
    echo "  $1"
    echo "=============================================="
}

run_or_skip() {
    # Run a command, print "saknas" if the binary is missing
    local cmd="$1"
    local bin
    bin=$(echo "$cmd" | awk '{print $1}')
    if command -v "$bin" >/dev/null 2>&1; then
        eval "$cmd"
    else
        echo "$bin saknas"
    fi
}

echo "GH200 Inventory Report"
echo "Generated: $(date)"
echo "Host: $(hostname)"

print_header "OS & Kernel"
grep -E "PRETTY_NAME|VERSION=" /etc/os-release 2>/dev/null
uname -a

print_header "CPU"
lscpu | grep -E "Model name|Architecture|^CPU\(s\):|Thread|Socket|MHz|NUMA"

print_header "Minne (free)"
free -h

print_header "Minne (dmidecode, kräver sudo)"
if sudo -n true 2>/dev/null; then
    sudo dmidecode -t memory 2>/dev/null | grep -E "Size:|Speed:|Type:" | head -30
else
    echo "Sudo kräver lösenord, hoppar dmidecode"
fi

print_header "Disk (lsblk)"
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,MODEL

print_header "Disk (df)"
df -h | grep -vE "tmpfs|udev|loop"

print_header "GPU (nvidia-smi)"
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi
    echo "---"
    nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv
else
    echo "nvidia-smi saknas"
fi

print_header "CUDA Toolkit"
if command -v nvcc >/dev/null 2>&1; then
    nvcc --version
else
    echo "nvcc saknas i PATH"
fi
echo "---"
echo "Innehåll i /usr/local/ som matchar cuda:"
ls /usr/local/ 2>/dev/null | grep -i cuda || echo "Inget cuda-bibliotek i /usr/local/"

print_header "NVLink / Grace Hopper topologi"
if command -v nvidia-smi >/dev/null 2>&1; then
    echo "NVLink status:"
    nvidia-smi nvlink --status 2>/dev/null | head -30
    echo "---"
    echo "Topologi:"
    nvidia-smi topo -m 2>/dev/null
else
    echo "nvidia-smi saknas, hoppar"
fi

print_header "Python"
run_or_skip "python3 --version"
echo "Sökväg: $(which python3 2>/dev/null || echo 'saknas')"
echo "---"
echo "Relevanta paket:"
if command -v pip3 >/dev/null 2>&1; then
    pip3 list 2>/dev/null | grep -iE "torch|transformers|vllm|accelerate|deepspeed|ollama|sentence-transformers|llama-cpp" || echo "Inga relevanta ML-paket hittade"
else
    echo "pip3 saknas"
fi

print_header "Container runtimes"
run_or_skip "docker --version"
echo "podman: $(command -v podman || echo 'saknas')"
echo "enroot: $(command -v enroot || echo 'saknas')"
echo "singularity: $(command -v singularity || echo 'saknas')"
echo "apptainer: $(command -v apptainer || echo 'saknas')"

print_header "NVIDIA Container Toolkit"
if command -v nvidia-container-cli >/dev/null 2>&1; then
    nvidia-container-cli --version | head -3
else
    echo "nvidia-container-cli saknas"
fi

print_header "Nätverk"
ip -br addr | grep -v "^lo "
echo "---"
echo -n "Internet-test (HTTP 200 betyder OK): "
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 https://www.google.com

print_header "Förinstallerade ML-verktyg"
echo "jupyter: $(command -v jupyter || echo 'saknas')"
echo "ollama: $(command -v ollama || echo 'saknas')"
echo "huggingface-cli: $(command -v huggingface-cli || echo 'saknas')"
echo "---"
echo "Innehåll i hemkatalog:"
ls -la ~/ 2>/dev/null | head -20
echo "---"
echo "Innehåll i /opt/:"
ls /opt/ 2>/dev/null || echo "Tomt eller saknas"

print_header "Diskutrymme för ML-arbete"
echo "Hemkatalog användning:"
du -sh ~/ 2>/dev/null
echo "---"
echo "Stora kataloger under /:"
df -h / /home /opt /var 2>/dev/null | grep -v "Filesystem"

print_header "GPU klocka och temperatur (snabbkoll)"
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory --format=csv
fi

print_header "Klar"
echo "Inventering slutförd: $(date)"
