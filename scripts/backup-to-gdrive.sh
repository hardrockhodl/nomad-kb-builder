#!/usr/bin/env bash
# backup-to-gdrive.sh
# Tar output/ and logs/ to ~/, then upload both tarballs to Google Drive via rclone.
#
# Usage: ./scripts/backup-to-gdrive.sh
#        REMOTE=gdrive ./scripts/backup-to-gdrive.sh        # override remote name
#        REMOTE_PATH=nomad-kb-builder/ ./scripts/backup-to-gdrive.sh   # override target path
#
# Requires:
#   - rclone configured with a remote named "gdrive" (or set REMOTE)
#   - script runs from anywhere; uses REPO_ROOT relative to its own location

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Config
REMOTE="${REMOTE:-gdrive}"
REMOTE_PATH="${REMOTE_PATH:-nomad-kb-builder/}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

OUTPUT_TAR="$HOME/nomad-kb-output-${TIMESTAMP}.tar.gz"
LOGS_TAR="$HOME/nomad-kb-logs-${TIMESTAMP}.tar.gz"

# Color codes
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${CYAN}=== nomad-kb-builder backup ===${NC}"
echo "Source dir:   $REPO_ROOT"
echo "Output tar:   $OUTPUT_TAR"
echo "Logs tar:     $LOGS_TAR"
echo "Remote:       $REMOTE:$REMOTE_PATH"
echo ""

# ----------------------------------------------------------------------------
# Verify rclone remote
# ----------------------------------------------------------------------------
if ! command -v rclone >/dev/null 2>&1; then
    echo -e "${RED}rclone not installed. Install with: sudo apt install rclone${NC}"
    exit 1
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE}:"; then
    echo -e "${RED}rclone remote '$REMOTE' not configured.${NC}"
    echo "Configure with: rclone config"
    exit 1
fi

# ----------------------------------------------------------------------------
# Create tarballs
# ----------------------------------------------------------------------------
if [ -d "output" ]; then
    echo -e "${CYAN}Creating $OUTPUT_TAR ...${NC}"
    tar czf "$OUTPUT_TAR" output/
    size=$(du -h "$OUTPUT_TAR" | awk '{print $1}')
    echo -e "${GREEN}  Done: $size${NC}"
else
    echo -e "${YELLOW}No output/ directory, skipping.${NC}"
    OUTPUT_TAR=""
fi
echo ""

if [ -d "logs" ]; then
    echo -e "${CYAN}Creating $LOGS_TAR ...${NC}"
    tar czf "$LOGS_TAR" logs/
    size=$(du -h "$LOGS_TAR" | awk '{print $1}')
    echo -e "${GREEN}  Done: $size${NC}"
else
    echo -e "${YELLOW}No logs/ directory, skipping.${NC}"
    LOGS_TAR=""
fi
echo ""

# ----------------------------------------------------------------------------
# Upload
# ----------------------------------------------------------------------------
upload() {
    local file="$1"
    if [ -z "$file" ] || [ ! -f "$file" ]; then
        return
    fi
    echo -e "${CYAN}Uploading $(basename "$file") to ${REMOTE}:${REMOTE_PATH} ...${NC}"
    if rclone copy --progress "$file" "${REMOTE}:${REMOTE_PATH}"; then
        echo -e "${GREEN}  Uploaded.${NC}"
    else
        echo -e "${RED}  Upload failed.${NC}"
        return 1
    fi
    echo ""
}

upload "$OUTPUT_TAR"
upload "$LOGS_TAR"

# ----------------------------------------------------------------------------
# Verify upload
# ----------------------------------------------------------------------------
echo -e "${CYAN}Verifying upload...${NC}"
rclone ls "${REMOTE}:${REMOTE_PATH}" 2>/dev/null \
    | grep "$TIMESTAMP" \
    | while IFS= read -r line; do
        echo "  $line"
    done

echo ""
echo -e "${BOLD}${GREEN}=== Backup complete ===${NC}"
echo ""
echo "Files on Google Drive:"
[ -n "$OUTPUT_TAR" ] && echo "  ${REMOTE}:${REMOTE_PATH}$(basename "$OUTPUT_TAR")"
[ -n "$LOGS_TAR" ]   && echo "  ${REMOTE}:${REMOTE_PATH}$(basename "$LOGS_TAR")"
echo ""
echo "Local tarballs (you can delete these if upload verified):"
[ -n "$OUTPUT_TAR" ] && echo "  $OUTPUT_TAR"
[ -n "$LOGS_TAR" ]   && echo "  $LOGS_TAR"
echo ""
echo "To remove local tarballs:"
[ -n "$OUTPUT_TAR" ] && echo "  rm $OUTPUT_TAR"
[ -n "$LOGS_TAR" ]   && echo "  rm $LOGS_TAR"