#!/usr/bin/env bash
# VOTEBOT-20: age out the new VoteBot's query logs (one JSONL file per day, written by the container to
# /opt/votebot-logs/queries). Compresses files older than COMPRESS_DAYS and deletes files older than DELETE_DAYS.
# Decision (Ramon, 2026-10-07): keep a year at most. The files hold what visitors typed, so nothing older is kept.
# The weekly quality report (VOTEBOT-19) reads the last 7 days uncompressed, which the defaults keep.
#
# Usage:  infrastructure/query-log-cleanup.sh [--dry-run]
#   --dry-run   print what would be compressed and deleted; change nothing (also: DRY_RUN=1)
# Env overrides: LOG_DIR (default /opt/votebot-logs/queries), COMPRESS_DAYS (7), DELETE_DAYS (365)
#
# Install (the operator, needs sudo; run-parts skips names with a dot, so no ".sh"):
#   sudo install -m 755 infrastructure/query-log-cleanup.sh /etc/cron.daily/votebot-query-log-cleanup
# Only touches *.jsonl and *.jsonl.gz directly inside LOG_DIR, never subdirectories. Age is the file's modification
# time: a day's file stops changing when the day ends, so today's file (still being appended to) is never touched.
set -uo pipefail

LOG_DIR="${LOG_DIR:-/opt/votebot-logs/queries}"
COMPRESS_DAYS="${COMPRESS_DAYS:-7}"
DELETE_DAYS="${DELETE_DAYS:-365}"
DRY_RUN="${DRY_RUN:-0}"
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

case "$COMPRESS_DAYS$DELETE_DAYS" in *[!0-9]*|"") echo "[query-log-cleanup] COMPRESS_DAYS and DELETE_DAYS must be whole numbers" >&2; exit 2 ;; esac
if [ "$COMPRESS_DAYS" -ge "$DELETE_DAYS" ]; then
  echo "[query-log-cleanup] COMPRESS_DAYS ($COMPRESS_DAYS) must be less than DELETE_DAYS ($DELETE_DAYS)" >&2; exit 2
fi
if [ -z "$LOG_DIR" ] || [ "$LOG_DIR" = "/" ] || [ ! -d "$LOG_DIR" ]; then
  echo "[query-log-cleanup] LOG_DIR is not a usable directory: '$LOG_DIR'" >&2; exit 2
fi

compressed=0; deleted=0; failed=0
# Paths are NUL-separated, so a name with a space or a newline is never split into two paths.
# Delete first, so an old file is not compressed just to be removed. -mtime +N means modified more than N whole
# days ago (N+1 days or more), so a file is compressed on its 8th day and deleted on its 366th.
while IFS= read -r -d '' f; do
  if [ "$DRY_RUN" = "1" ]; then printf 'would delete   %s\n' "$f"
  elif rm -f -- "$f"; then printf 'deleted        %s\n' "$f"
  else printf 'FAILED to delete %s\n' "$f" >&2; failed=$((failed + 1)); continue; fi
  deleted=$((deleted + 1))
done < <(find "$LOG_DIR" -maxdepth 1 -type f \( -name '*.jsonl' -o -name '*.jsonl.gz' \) -mtime +"$DELETE_DAYS" -print0 | sort -z)

while IFS= read -r -d '' f; do
  if [ -e "$f.gz" ]; then printf 'skipped        %s (%s.gz already exists)\n' "$f" "$f" >&2; continue; fi
  if [ "$DRY_RUN" = "1" ]; then printf 'would compress %s\n' "$f"
  elif gzip -n -- "$f"; then printf 'compressed     %s\n' "$f"
  else printf 'FAILED to compress %s\n' "$f" >&2; failed=$((failed + 1)); continue; fi
  compressed=$((compressed + 1))
done < <(find "$LOG_DIR" -maxdepth 1 -type f -name '*.jsonl' -mtime +"$COMPRESS_DAYS" ! -mtime +"$DELETE_DAYS" -print0 | sort -z)

echo "[query-log-cleanup] $([ "$DRY_RUN" = "1" ] && echo 'dry run: ')$compressed to compress, $deleted to delete in $LOG_DIR"
if [ "$failed" -gt 0 ]; then echo "[query-log-cleanup] $failed operation(s) FAILED" >&2; exit 1; fi
