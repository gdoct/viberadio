#!/usr/bin/env bash
#
# Deploy Vibe Radio to a Docker host over SSH.
#
#   ./deploy.sh                     sync source, rebuild, restart, verify
#   ./deploy.sh --first-run         everything below, for a host that has none of it yet
#   ./deploy.sh --firewall          open the app port and let containers reach the internet
#   ./deploy.sh --models            copy the Kokoro voice model (~340 MB, once)
#   ./deploy.sh --library           copy the media library and its track rows
#   ./deploy.sh --credentials       copy this machine's Claude Code login to the host
#
# Flags combine. Env overrides: HOST, PORT, REMOTE_DIR.
#
# The agents authenticate through a Claude Code login on the host. Either use
# --credentials, or log in on the host once the container is up:
#
#   ssh <host> 'cd <remote_dir> && docker compose exec viberadio claude'
#
set -euo pipefail

HOST="${HOST:-nuc-guido}"
PORT="${PORT:-6644}"
REMOTE_DIR="${REMOTE_DIR:-viberadio}"

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
do_firewall=0 do_models=0 do_library=0 do_credentials=0

for arg in "$@"; do
  case "$arg" in
    --first-run) do_firewall=1 do_models=1 do_library=1 do_credentials=1 ;;
    --firewall) do_firewall=1 ;;
    --models) do_models=1 ;;
    --library) do_library=1 ;;
    --credentials) do_credentials=1 ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Preparing $HOST:~/$REMOTE_DIR"
# These directories are bind-mounted, so the database, media library, voice model
# and run log all live on the host and outlive any container.
ssh "$HOST" "mkdir -p ~/$REMOTE_DIR/data/models ~/$REMOTE_DIR/data/media \
  ~/$REMOTE_DIR/logs ~/$REMOTE_DIR/backups ~/$REMOTE_DIR/claude-home/.claude"

if (( do_firewall )); then
  # firewalld ships a policy for traffic *into* containers but none for their
  # egress, so without this the agents cannot reach Claude or YouTube.
  step "Configuring firewall"
  ssh "$HOST" "
    set -e
    if ! sudo firewall-cmd --get-policies | grep -qw docker-egress; then
      sudo firewall-cmd --permanent --new-policy docker-egress
      sudo firewall-cmd --permanent --policy docker-egress --add-ingress-zone docker
      sudo firewall-cmd --permanent --policy docker-egress --add-egress-zone ANY
      sudo firewall-cmd --permanent --policy docker-egress --set-target ACCEPT
    fi
    sudo firewall-cmd --permanent --add-port=$PORT/tcp
    sudo firewall-cmd --reload
  "
fi

step "Syncing source"
# --delete prunes stale sources but leaves excluded paths alone; --delete-excluded
# would wipe the data, logs, backups and login directories on the host.
rsync -a --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' \
  --exclude '__pycache__' --exclude '.ruff_cache' \
  --exclude 'data' --exclude 'frontend/dist' --exclude 'claude-home' \
  --exclude 'logs' --exclude 'backups' \
  "$repo_dir/" "$HOST:$REMOTE_DIR/"

if (( do_models )); then
  step "Syncing Kokoro voice model"
  if [[ -f "$repo_dir/backend/data/models/kokoro-v1.0.onnx" ]]; then
    rsync -a --info=progress2 \
      "$repo_dir/backend/data/models/" "$HOST:$REMOTE_DIR/data/models/"
  else
    echo "No local model found — downloading on the host instead."
    ssh "$HOST" "
      cd ~/$REMOTE_DIR/data/models
      [ -f kokoro-v1.0.onnx ] || curl -fsSL -o kokoro-v1.0.onnx \
        https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
      [ -f voices-v1.0.bin ] || curl -fsSL -o voices-v1.0.bin \
        https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
    "
  fi
fi

if (( do_credentials )); then
  # Both machines then share one OAuth session, so a refresh on either can log
  # the other out. Logging in on the host directly avoids that.
  step "Copying Claude Code login"
  scp -q ~/.claude/.credentials.json "$HOST:$REMOTE_DIR/claude-home/.claude/.credentials.json"
  [[ -f ~/.claude.json ]] && scp -q ~/.claude.json "$HOST:$REMOTE_DIR/claude-home/.claude.json"
  ssh "$HOST" "chmod 600 ~/$REMOTE_DIR/claude-home/.claude/.credentials.json"
fi

# Build before stopping anything, so the station stays on air while the image is
# built and the swap is only as long as a restart.
step "Building image"
ssh "$HOST" "cd ~/$REMOTE_DIR && docker compose build"

step "Snapshotting database"
# Taken with the container stopped: SQLite's WAL and shm only make a consistent
# set alongside the database file when no writer is attached.
ssh "$HOST" "
  set -e
  cd ~/$REMOTE_DIR
  docker compose stop viberadio 2>/dev/null || true
  if [ -f data/viberadio.db ]; then
    dest=backups/\$(date +%Y%m%d-%H%M%S)
    mkdir -p \"\$dest\"
    cp -p data/viberadio.db \"\$dest/\"
    for extra in data/viberadio.db-wal data/viberadio.db-shm; do
      [ -f \"\$extra\" ] && cp -p \"\$extra\" \"\$dest/\"
    done
    echo \"saved \$dest\"
    # Keep the ten most recent snapshots; they are only a few hundred KB each.
    ls -1d backups/20*-* 2>/dev/null | sort | head -n -10 | xargs -r rm -rf
  else
    echo 'no database yet — nothing to snapshot'
  fi
"

step "Starting"
ssh "$HOST" "cd ~/$REMOTE_DIR && docker compose up -d"

if (( do_library )); then
  # After the container has created the database, so the import has a table to
  # insert into. Songs the agents download later land here on their own.
  step "Syncing media library"
  rsync -a --info=progress2 \
    "$repo_dir/backend/data/media/" "$HOST:$REMOTE_DIR/data/media/"

  step "Importing track rows"
  tracks="$(mktemp)"
  trap 'rm -f "$tracks"' EXIT
  python3 "$repo_dir/scripts/export_tracks.py" "$repo_dir/backend" "$tracks"
  scp -q "$tracks" "$HOST:$REMOTE_DIR/tracks.json"
  ssh "$HOST" "cd ~/$REMOTE_DIR && python3 scripts/import_tracks.py data tracks.json && rm -f tracks.json"
fi

step "Verifying"
for _ in $(seq 30); do
  if curl -fsS --max-time 5 "http://$HOST:$PORT/api/health" >/dev/null 2>&1; then
    curl -fsS "http://$HOST:$PORT/api/health"; echo
    echo "Vibe Radio is live at http://$HOST:$PORT"
    exit 0
  fi
  sleep 2
done

echo "Health check never passed. Recent logs:" >&2
ssh "$HOST" "cd ~/$REMOTE_DIR && docker compose logs --tail=40 viberadio" >&2
exit 1
