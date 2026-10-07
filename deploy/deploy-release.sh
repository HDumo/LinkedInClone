#!/usr/bin/env bash
#
# deploy-release.sh — build, test, promote and roll back LinkedClone releases.
#
# Adapted from the script that deploys Pikes Peak Clean, and it keeps every
# lesson learned there. The layout is "releases directory + current symlink",
# so going live and going back are each a single instant switch, and a failed
# build never touches the live site.
#
#   /opt/linkedclone/
#     releases/<timestamp>/   one full checkout + venv + collected static per release
#     releases/<name>/.deploy_ok   written only after a fully clean build
#     shared/.env             the ONE config file, symlinked into every release
#     shared/media/           profile photos, symlinked into every release
#     shared/data/db.sqlite3  only when running on SQLite (no DATABASE_URL)
#     current -> releases/X   systemd and nginx always point HERE
#     backups/                database dump taken right before each migrate
#     logs/deploy-<name>.log  full output of each deploy (survives disconnects)
#     deploy.conf             REPO_URL etc., written by provision.sh
#     .previous_release       so `rollback` knows where to go
#     .deploy.lock            stops two runs overlapping
#
# USAGE (always run as root: sudo ./deploy-release.sh <command>)
#
#   install [branch]          First release on a freshly provisioned server:
#                             deploy, then promote. Use once.
#   deploy [branch-or-tag]    Clone, build, test. Detached, so a dropped SSH
#                             session cannot kill it. Touches NOTHING live:
#                             not the code, not the database.
#   promote [name] [--force]  Back up + migrate the database, switch `current`,
#                             restart, health check. Refuses a release whose
#                             deploy did not finish cleanly unless --force.
#   rollback                  Go back to what was live before the last promote
#                             (code only; see its output about the database).
#   status                    What is live, what is on disk, last deploy result.
#   logs [name] [-f]          Result and log of a deploy (default: the latest).
#   backup                    Database dump + photos archive into backups/.
#   manage <args...>          Run manage.py in the live release as the service
#                             user, e.g.  manage createsuperuser
#   cleanup [-y]              Delete every release except the live one and the
#                             rollback target; prune old backups.
#
# Failure modes this script is built around (each happened for real):
#   1. The shared .env made unreadable to the service user -> verified/fixed.
#   2. nginx pointing at old paths -> nginx.conf goes through current/ + shared/.
#   3. Migrating before tests pass -> migrate happens only in `promote`.
#   4. SQLite inside a release folder losing all data per deploy -> SQLITE_PATH
#      lives in shared/.
#   5. A deploy killed mid-run then promoted -> detached worker + .deploy_ok.
#   6. The script updating releases but not itself -> self-update after a
#      clean deploy.
#   7. Server-only test failures -> `test --noinput`, and the DB role has CREATEDB.
#   8. Upload folders unreadable by nginx -> permissions fixed every deploy.
#   9. Static files missing in the live release -> collectstatic is part of deploy.
#  10. A stale lock after a crash -> the error says how to clear it.
set -euo pipefail

# --------------------------------------------------------------------------
# Configuration (override any of these in $APP_ROOT/deploy.conf)
# --------------------------------------------------------------------------
APP_ROOT="${APP_ROOT:-/opt/linkedclone}"
REPO_URL="git@github.com:HDumo/LinkedInClone.git"
DEFAULT_BRANCH="main"
SERVICE_NAME="linkedclone"
HEALTH_CHECK_URL="http://127.0.0.1:8000/healthz/"
DB_NAME="linkedclone"
KEEP_BACKUPS=15
PYTHON="${PYTHON:-python3}"

# provision.sh writes the clone URL it was installed from, plus anything else
# you want to pin, into deploy.conf.
[ -f "$APP_ROOT/deploy.conf" ] && . "$APP_ROOT/deploy.conf"

RELEASES_DIR="$APP_ROOT/releases"
SHARED_DIR="$APP_ROOT/shared"
BACKUPS_DIR="$APP_ROOT/backups"
LOG_DIR="$APP_ROOT/logs"
CURRENT_LINK="$APP_ROOT/current"
PREV_STATE_FILE="$APP_ROOT/.previous_release"
LOCK_FILE="$APP_ROOT/.deploy.lock"

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
c_green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
c_yellow() { printf '\033[0;33m%s\033[0m\n' "$1"; }
c_red() { printf '\033[0;31m%s\033[0m\n' "$1"; }
die() { c_red "ERROR: $1"; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "Run this with sudo — it manages systemd, nginx, and files under $APP_ROOT."
    acquire_lock
}

# Advisory lock so two invocations can't step on each other (cleanup deleting
# a release while deploy is still cloning into it, say).
acquire_lock() {
    command -v flock > /dev/null 2>&1 || { c_yellow "flock not found — skipping the concurrent-run lock (a safety net, not required)."; return 0; }
    mkdir -p "$APP_ROOT"
    exec 200> "$LOCK_FILE"
    flock -n 200 || die "Another deploy-release.sh command is already running (lock: $LOCK_FILE). Wait for it to finish. If you are sure nothing is running (check: ps aux | grep deploy-release), remove the lock with: sudo rm $LOCK_FILE"
}

require_provisioned() {
    [ -d "$RELEASES_DIR" ] && [ -f "$SHARED_DIR/.env" ] \
        || die "$APP_ROOT is not set up yet. Run deploy/provision.sh first (see docs/INSTALL.md)."
}

health_check() {
    c_yellow "Checking the app actually responds..."
    local _
    for _ in $(seq 1 15); do
        if curl --fail --silent --max-time 5 "$HEALTH_CHECK_URL" > /dev/null; then
            c_green "Health check passed — the app is responding and can reach its database."
            return 0
        fi
        sleep 2
    done
    c_red "Health check FAILED — $HEALTH_CHECK_URL did not respond correctly after 30s."
    c_red "Check: sudo systemctl status $SERVICE_NAME"
    c_red "and:  sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
    exit 1
}

restart_service() {
    c_yellow "Restarting $SERVICE_NAME..."
    # `200>&-` closes our lock descriptor for this one command, so the
    # restarted service can never inherit (and keep holding) the deploy lock.
    systemctl restart "$SERVICE_NAME" 200>&-
}

# Reads a "Key=value" line out of a systemd unit file, if present.
unit_directive() {
    local unit_file="$1" key="$2"
    grep -oP "^${key}=\K.*" "$unit_file" 2>/dev/null | head -n1 || true
}

# The user/group $SERVICE_NAME runs as, read fresh from the unit file.
service_identity() {
    local unit_file="${SYSTEMD_DIR:-/etc/systemd/system}/${SERVICE_NAME}.service"
    [ -f "$unit_file" ] || { echo " "; return 0; }
    echo "$(unit_directive "$unit_file" User) $(unit_directive "$unit_file" Group)"
}

# Makes sure $1 (a file) is readable by user $2, fixing ownership if not.
ensure_readable_by() {
    local target_file="$1" service_user="$2" service_group="$3"
    [ -n "$service_user" ] || return 0
    if sudo -u "$service_user" test -r "$target_file" 2>/dev/null; then
        return 0
    fi
    c_yellow "'$service_user' (the user $SERVICE_NAME runs as) can't read $target_file yet — fixing ownership..."
    chown "$service_user":"${service_group:-$service_user}" "$target_file"
    chmod 640 "$target_file"
    if sudo -u "$service_user" test -r "$target_file" 2>/dev/null; then
        c_green "Fixed — '$service_user' can now read $target_file."
    else
        die "'$service_user' still can't read $target_file after chown+chmod 640. Check its group membership by hand."
    fi
}

# Makes directory $1 writable by the service user and its contents readable by
# everyone (nginx runs as another OS user and serves profile photos straight
# off disk; everything under media/ is public content). Always re-applies,
# recursively, so folders created earlier with a bad owner or umask get fixed.
ensure_writable_by() {
    local target_dir="$1" service_user="$2" service_group="$3"
    [ -n "$service_user" ] || return 0
    chown -R "$service_user":"${service_group:-$service_user}" "$target_dir" \
        || die "chown -R on $target_dir failed — see the error above."
    find "$target_dir" -type d -exec chmod u+rwx,go+rx {} + || die "chmod on $target_dir's folders failed."
    find "$target_dir" -type f -exec chmod u+rw,go+r {} + || die "chmod on $target_dir's files failed."
    if sudo -u "$service_user" test -w "$target_dir" 2>/dev/null; then
        c_green "'$service_user' can write to $target_dir; its contents are world-readable for nginx."
    else
        die "'$service_user' still can't write to $target_dir after chown -R + chmod. Check its group membership by hand."
    fi
}

# "postgres" if the shared .env sets DATABASE_URL, otherwise "sqlite" —
# mirrors config/settings.py exactly.
db_backend() {
    if grep -qE '^[[:space:]]*DATABASE_URL[[:space:]]*=[[:space:]]*[^[:space:]]' "$SHARED_DIR/.env" 2>/dev/null; then
        echo "postgres"
    else
        echo "sqlite"
    fi
}

# Value of KEY from the shared .env (empty if unset).
env_value() {
    grep -E "^[[:space:]]*$1[[:space:]]*=" "$SHARED_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' || true
}

sqlite_file() {
    local p; p="$(env_value SQLITE_PATH)"
    echo "${p:-$SHARED_DIR/data/db.sqlite3}"
}

# Run a command as the service user (so files it creates have the right owner).
as_service() {
    local u g; read -r u g <<< "$(service_identity)"
    if [ -n "$u" ]; then sudo -u "$u" "$@"; else "$@"; fi
}

# --------------------------------------------------------------------------
# _deploy_result — SUCCEEDED / STILL RUNNING / FAILED for a release name.
# --------------------------------------------------------------------------
_deploy_result() {
    local release_name="$1"
    local release_dir="$RELEASES_DIR/$release_name"
    local pidfile="$LOG_DIR/deploy-$release_name.pid"
    if [ -f "$release_dir/.deploy_ok" ]; then
        c_green "SUCCEEDED — built and tested cleanly ($(cat "$release_dir/.deploy_ok" 2>/dev/null))."
    elif [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
        c_yellow "STILL RUNNING (pid $(cat "$pidfile"))."
    else
        c_red "FAILED or interrupted before finishing — no .deploy_ok marker."
    fi
}

# --------------------------------------------------------------------------
# _self_update — keeps the installed $APP_ROOT/deploy-release.sh in sync with
# the copy in the release just built. Returns 3 when it changed something.
# --------------------------------------------------------------------------
_self_update() {
    local release_dir="$1"
    local installed_script="$APP_ROOT/deploy-release.sh"
    local release_script="$release_dir/deploy/deploy-release.sh"

    [ -f "$release_script" ] || return 0
    [ -f "$installed_script" ] || return 0
    cmp -s "$release_script" "$installed_script" && return 0

    if ! bash -n "$release_script" 2>/dev/null; then
        c_red "This release's deploy/deploy-release.sh has a syntax error —"
        c_red "leaving $installed_script as-is. Fix it before it can self-update."
        return 0
    fi

    c_yellow "This release's deploy-release.sh differs from the installed one — updating it"
    c_yellow "(a backup of the previous version is kept alongside it)."
    cp "$installed_script" "${installed_script}.bak.$(date +%Y%m%d%H%M%S)"
    # Write to a temp file and rename: this process was started from the
    # installed file, and a rename swaps the directory entry instead of
    # mutating the file under a running reader.
    cp "$release_script" "${installed_script}.new"
    chmod +x "${installed_script}.new"
    mv -f "${installed_script}.new" "$installed_script"
    c_green "Updated $installed_script. It takes effect from your NEXT command, not this one."
    return 3
}

# --------------------------------------------------------------------------
# _deploy_worker — the actual clone/build/test/collectstatic work. Launched
# detached by cmd_deploy; not meant to be run by hand.
# --------------------------------------------------------------------------
_deploy_worker() {
    local release_dir="$1" ref="$2"
    local release_name; release_name="$(basename "$release_dir")"

    # A deploy key created by provision.sh, if there is one.
    if [ -f /root/.ssh/linkedclone_deploy ]; then
        export GIT_SSH_COMMAND="ssh -i /root/.ssh/linkedclone_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    fi
    c_yellow "Cloning '$ref' from $REPO_URL into $release_dir ..."
    git clone --branch "$ref" --depth 1 "$REPO_URL" "$release_dir"

    c_yellow "Linking the shared .env into this release (your config is untouched)..."
    ln -s "$SHARED_DIR/.env" "$release_dir/.env"
    local SERVICE_USER SERVICE_GROUP
    read -r SERVICE_USER SERVICE_GROUP <<< "$(service_identity)"
    ensure_readable_by "$SHARED_DIR/.env" "$SERVICE_USER" "$SERVICE_GROUP"

    # Profile photos are shared across every release the same way .env is,
    # instead of each release getting its own empty folder.
    mkdir -p "$SHARED_DIR/media"
    ln -s "$SHARED_DIR/media" "$release_dir/media"
    ensure_writable_by "$SHARED_DIR/media" "$SERVICE_USER" "$SERVICE_GROUP"

    c_yellow "Creating a virtual environment and installing dependencies..."
    "$PYTHON" -m venv "$release_dir/venv"
    "$release_dir/venv/bin/pip" install --quiet --upgrade pip
    "$release_dir/venv/bin/pip" install --quiet -r "$release_dir/requirements.txt"

    cd "$release_dir"

    c_yellow "Running Django's production security checklist..."
    "$release_dir/venv/bin/python" manage.py check --deploy

    c_yellow "Checking your .env makes sense for a live server (domain, database, email)..."
    # As the service user, not root: it opens the database, and a SQLite file
    # created by root could not be written by the running service afterwards.
    as_service "$release_dir/venv/bin/python" manage.py check_install

    c_yellow "Running the test suite (against a throwaway test database — the live"
    c_yellow "database is never touched by deploy, only by promote)..."
    # --noinput: a stale test_<db> left by an interrupted run would otherwise
    # trigger a prompt this stdin-less worker can never answer (EOFError).
    if ! "$release_dir/venv/bin/python" manage.py test --noinput; then
        c_red "Tests FAILED. This release is built at $release_dir but will NOT go live."
        c_red "Nothing live was touched — not the code, not the database."
        c_red "Fix the issue, or delete this failed attempt with: sudo rm -rf $release_dir"
        exit 1
    fi

    c_yellow "Collecting static files..."
    "$release_dir/venv/bin/python" manage.py collectstatic --noinput > /dev/null

    local self_update_rc=0
    _self_update "$release_dir" || self_update_rc=$?
    if [ "$self_update_rc" = "3" ]; then
        c_red "=============================================================="
        c_red "IMPORTANT: deploy-release.sh itself was just updated (see above)."
        c_red "This deploy ran with the OLD copy from start to finish. If this"
        c_red "release ALSO changed deploy-release.sh, run 'sudo $0 deploy' once"
        c_red "more after promoting this one so the new script does the building."
        c_red "=============================================================="
    fi

    # Only a release that reaches here gets this marker; `promote` refuses
    # anything without it.
    date -u +%Y-%m-%dT%H:%M:%SZ > "$release_dir/.deploy_ok"

    c_green "Release '$release_name' built and tested successfully at:"
    c_green "  $release_dir"
    c_green "Nothing is live yet — code or database. When you're ready:"
    c_green "  sudo $0 promote"
}

# --------------------------------------------------------------------------
# deploy
# --------------------------------------------------------------------------
cmd_deploy() {
    require_root
    require_provisioned
    local ref="${1:-$DEFAULT_BRANCH}"
    local release_name
    release_name="$(date +%Y%m%d-%H%M%S)"
    local release_dir="$RELEASES_DIR/$release_name"
    mkdir -p "$LOG_DIR"
    local logfile="$LOG_DIR/deploy-$release_name.log"
    local pidfile="$LOG_DIR/deploy-$release_name.pid"

    # setsid detaches from the terminal's session entirely; nohup is extra
    # insurance against SIGHUP. fd 200 (our lock) is inherited, so the lock
    # covers the whole detached run.
    setsid nohup "$0" _deploy_worker "$release_dir" "$ref" < /dev/null > "$logfile" 2>&1 &
    local worker_pid=$!
    disown "$worker_pid" 2>/dev/null || true
    echo "$worker_pid" > "$pidfile"

    c_yellow "Deploying release '$release_name' in the background — a dropped"
    c_yellow "connection will NOT stop it. Log: $logfile"
    c_yellow "Reconnect any time and run '$0 logs' to check on it."
    echo

    tail -n +1 -f --pid="$worker_pid" "$logfile" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true

    # .deploy_ok is the sole source of truth for pass/fail: the worker's own
    # exit status doesn't reliably survive the setsid+nohup+tail chain.
    if [ -f "$release_dir/.deploy_ok" ]; then
        exit 0
    else
        echo
        c_red "Deploy did not finish cleanly — no .deploy_ok marker. Nothing live was touched."
        c_red "Full output: $0 logs $release_name"
        exit 1
    fi
}

# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------
cmd_logs() {
    mkdir -p "$LOG_DIR"
    local follow=0 release_name="" arg
    for arg in "$@"; do
        if [ "$arg" = "-f" ]; then follow=1; else release_name="$arg"; fi
    done

    local logfile
    if [ -n "$release_name" ]; then
        logfile="$LOG_DIR/deploy-$release_name.log"
        [ -f "$logfile" ] || die "No deploy log found for release '$release_name' at $logfile."
    else
        logfile="$(find "$LOG_DIR" -maxdepth 1 -name 'deploy-*.log' -printf '%T@ %p\n' 2>/dev/null \
            | sort -rn | head -n1 | cut -d' ' -f2-)"
        [ -n "$logfile" ] || die "No deploy logs found in $LOG_DIR yet — run '$0 deploy' first."
        release_name="$(basename "$logfile" .log | sed 's/^deploy-//')"
    fi

    echo "Release:  $release_name"
    echo "Log file: $logfile"
    printf "Result:   "
    _deploy_result "$release_name"
    echo

    if [ "$follow" = "1" ]; then
        tail -n 100 -f "$logfile"
    else
        tail -n 60 "$logfile"
        echo
        echo "(Showing the last 60 lines. Full log: $logfile — or '$0 logs $release_name -f' to follow live.)"
    fi
}

# --------------------------------------------------------------------------
# backup — database dump + photos archive
# --------------------------------------------------------------------------
_prune_backups() {
    local pattern
    for pattern in 'pre-promote-*.sql*' 'pre-promote-*.sqlite3' 'nightly-*.sql*' 'nightly-*.sqlite3' 'media-*.tar.gz'; do
        find "$BACKUPS_DIR" -maxdepth 1 -name "$pattern" -printf '%T@ %p\n' 2>/dev/null \
            | sort -rn | tail -n +$((KEEP_BACKUPS + 1)) | cut -d' ' -f2- \
            | while read -r old; do rm -f "$old"; c_green "Pruned old backup $old"; done
    done
}

cmd_backup() {
    require_root
    require_provisioned
    mkdir -p "$BACKUPS_DIR"
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    if [ "$(db_backend)" = "postgres" ]; then
        sudo -u postgres pg_dump "$DB_NAME" | gzip > "$BACKUPS_DIR/nightly-$stamp.sql.gz" \
            || die "Database backup failed. Check that 'sudo -u postgres pg_dump $DB_NAME' works on its own."
        c_green "Database saved to $BACKUPS_DIR/nightly-$stamp.sql.gz"
    else
        local db; db="$(sqlite_file)"
        [ -f "$db" ] || die "No SQLite database at $db yet — nothing to back up."
        if command -v sqlite3 > /dev/null 2>&1; then
            sqlite3 "$db" ".backup '$BACKUPS_DIR/nightly-$stamp.sqlite3'"
        else
            cp "$db" "$BACKUPS_DIR/nightly-$stamp.sqlite3"
        fi
        c_green "Database saved to $BACKUPS_DIR/nightly-$stamp.sqlite3"
    fi
    if [ -d "$SHARED_DIR/media" ] && [ -n "$(ls -A "$SHARED_DIR/media" 2>/dev/null)" ]; then
        tar -czf "$BACKUPS_DIR/media-$stamp.tar.gz" -C "$SHARED_DIR" media
        c_green "Photos saved to $BACKUPS_DIR/media-$stamp.tar.gz"
    fi
    _prune_backups
    c_green "Copy the files in $BACKUPS_DIR somewhere off this server too; a backup on the same disk does not survive losing the server."
}

# --------------------------------------------------------------------------
# promote — back up + migrate, then go live
# --------------------------------------------------------------------------
cmd_promote() {
    require_root
    require_provisioned
    local target_name="" force=0 arg
    for arg in "$@"; do
        if [ "$arg" = "--force" ]; then force=1; else target_name="$arg"; fi
    done
    local target_dir

    if [ -n "$target_name" ]; then
        target_dir="$RELEASES_DIR/$target_name"
        [ -d "$target_dir" ] || die "No release named '$target_name' in $RELEASES_DIR."
    else
        target_dir="$(find "$RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%T@ %p\n' \
            | sort -rn | head -n1 | cut -d' ' -f2-)"
        [ -n "$target_dir" ] || die "No releases found in $RELEASES_DIR. Run '$0 deploy' first."
    fi

    if [ ! -f "$target_dir/manage.py" ] || [ ! -x "$target_dir/venv/bin/python" ]; then
        die "'$target_dir' doesn't look like a release built by '$0 deploy' (missing manage.py or venv). Refusing to promote it."
    fi

    local current_target=""
    if [ -L "$CURRENT_LINK" ]; then
        current_target="$(readlink -f "$CURRENT_LINK")"
        if [ "$current_target" = "$(readlink -f "$target_dir")" ]; then
            c_yellow "'$target_dir' is already live. Nothing to do."
            exit 0
        fi
    else
        c_yellow "No release is live yet — this is the first promote on this server."
    fi

    # A release whose deploy failed or was interrupted has no marker.
    if [ ! -f "$target_dir/.deploy_ok" ]; then
        c_red "'$target_dir' has no .deploy_ok marker — its deploy either failed its"
        c_red "tests or was interrupted (e.g. a dropped connection) before finishing."
        c_red "Check what happened: $0 logs $(basename "$target_dir")"
        if [ "$force" != "1" ]; then
            die "Refusing to promote an unverified release. Re-run '$0 deploy' for a clean attempt, or pass --force if you are certain it is fine."
        fi
        c_yellow "Continuing anyway because --force was given."
    fi

    mkdir -p "$BACKUPS_DIR"
    local backup_file
    if [ "$(db_backend)" = "sqlite" ]; then
        local db; db="$(sqlite_file)"
        c_yellow "SQLite in use (no DATABASE_URL in shared/.env), database file: $db"
        c_yellow "Fine for a small site. For real traffic switch to PostgreSQL (docs/INSTALL.md)."
        # SQLite writes a journal NEXT TO the database, so the file and its
        # folder must both belong to the service user, whoever created them.
        local su sg; read -r su sg <<< "$(service_identity)"
        if [ -n "$su" ]; then
            mkdir -p "$(dirname "$db")"
            chown "$su":"${sg:-$su}" "$(dirname "$db")"
            [ -f "$db" ] && chown "$su":"${sg:-$su}" "$db"
        fi
        if [ -f "$db" ]; then
            backup_file="$BACKUPS_DIR/pre-promote-$(basename "$target_dir").sqlite3"
            c_yellow "Backing up the database to $backup_file..."
            cp "$db" "$backup_file"
        fi
    else
        backup_file="$BACKUPS_DIR/pre-promote-$(basename "$target_dir").sql.gz"
        c_yellow "Backing up the '$DB_NAME' database to $backup_file..."
        { sudo -u postgres pg_dump "$DB_NAME" | gzip > "$backup_file"; } \
            || die "Database backup failed — aborting BEFORE migrating or switching anything live. Check that 'sudo -u postgres pg_dump $DB_NAME' works on its own and that DB_NAME at the top of this script is right."
    fi

    c_yellow "Applying database migrations for the new release..."
    ( cd "$target_dir" && as_service "$target_dir/venv/bin/python" manage.py migrate --noinput ) \
        || die "Migration failed. The backup above is intact. Nothing live was switched — the site is still serving ${current_target:-nothing} unchanged."

    if [ -n "$current_target" ]; then
        c_yellow "Recording the current release as the rollback target..."
        echo "$current_target" > "$PREV_STATE_FILE"
    fi

    c_yellow "Switching 'current' to: $target_dir"
    ln -sfn "$target_dir" "$CURRENT_LINK"

    restart_service
    health_check
    c_green "Live: $target_dir"
    if [ -n "$current_target" ]; then
        c_green "If anything looks wrong: sudo $0 rollback"
        c_green "Once you're confident this release is good: sudo $0 cleanup"
    else
        c_green "The site is up. Create your first administrator:  sudo $0 manage createsuperuser"
    fi
}

# --------------------------------------------------------------------------
# install — first deploy + promote on a freshly provisioned server
# --------------------------------------------------------------------------
cmd_install() {
    require_provisioned
    if [ -L "$CURRENT_LINK" ]; then
        die "A release is already live ($(readlink -f "$CURRENT_LINK")). Use '$0 deploy' then '$0 promote' for updates."
    fi
    ( cmd_deploy "$@" ) || exit 1
    cmd_promote
}

# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------
cmd_rollback() {
    require_root
    [ -f "$PREV_STATE_FILE" ] || die "No previous release on record — nothing to roll back to."
    local prev_dir
    prev_dir="$(cat "$PREV_STATE_FILE")"
    [ -d "$prev_dir" ] || die "Recorded previous release '$prev_dir' no longer exists on disk."

    c_yellow "Rolling back to: $prev_dir"
    local current_target
    current_target="$(readlink -f "$CURRENT_LINK")"
    ln -sfn "$prev_dir" "$CURRENT_LINK"
    echo "$current_target" > "$PREV_STATE_FILE"

    restart_service
    health_check
    c_green "Rolled back. Live release is now: $prev_dir"
    c_red "Reminder: this reverted the CODE only. If the release you left ran new database"
    c_red "migrations, the schema was NOT rolled back. The backup taken before that migration"
    c_red "is in $BACKUPS_DIR if you need to restore data."
}

# --------------------------------------------------------------------------
# manage — run manage.py in the live release as the service user
# --------------------------------------------------------------------------
cmd_manage() {
    require_root
    [ -L "$CURRENT_LINK" ] || die "Nothing is live yet — run '$0 install' first."
    [ $# -gt 0 ] || die "Usage: $0 manage <command>, e.g.  $0 manage createsuperuser"
    cd "$CURRENT_LINK"
    as_service "$CURRENT_LINK/venv/bin/python" manage.py "$@"
}

# --------------------------------------------------------------------------
# cleanup — delete every release except the live one and the rollback target
# --------------------------------------------------------------------------
cmd_cleanup() {
    require_root
    [ -L "$CURRENT_LINK" ] || die "No release is live yet — nothing to clean up."
    local skip_confirm="${1:-}"
    local current_target keep_prev=""
    current_target="$(readlink -f "$CURRENT_LINK")"
    [ -f "$PREV_STATE_FILE" ] && keep_prev="$(readlink -f "$(cat "$PREV_STATE_FILE")" 2>/dev/null || true)"

    local to_delete=() dir
    for dir in "$RELEASES_DIR"/*/; do
        [ -d "$dir" ] || continue
        dir="${dir%/}"
        local real; real="$(readlink -f "$dir")"
        if [ "$real" != "$current_target" ] && [ "$real" != "$keep_prev" ]; then
            to_delete+=("$dir")
        fi
    done

    if [ ${#to_delete[@]} -eq 0 ]; then
        c_green "Nothing to clean up — only the live release${keep_prev:+ and the rollback target} exist."
    else
        echo "The following releases will be permanently deleted:"
        for dir in "${to_delete[@]}"; do
            echo "  - $dir ($(du -sh "$dir" 2>/dev/null | cut -f1))"
        done
        echo "Kept: live release $current_target${keep_prev:+ and rollback target $keep_prev}"
        if [ "$skip_confirm" != "-y" ]; then
            local confirm
            read -r -p "Delete these ${#to_delete[@]} release(s)? [y/N] " confirm
            [ "$confirm" = "y" ] || [ "$confirm" = "Y" ] || { echo "Cancelled."; exit 0; }
        fi
        for dir in "${to_delete[@]}"; do
            rm -rf "$dir"
            c_green "Deleted $dir"
        done
    fi
    [ -d "$BACKUPS_DIR" ] && _prune_backups
    c_green "Cleanup complete."
}

# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
cmd_status() {
    echo "App root:      $APP_ROOT"
    if [ -L "$CURRENT_LINK" ]; then
        echo "Live now:      $(readlink -f "$CURRENT_LINK")"
    else
        c_yellow "Live now:      nothing yet (run: sudo $0 install)"
    fi
    [ -f "$PREV_STATE_FILE" ] && echo "Rollback goes to: $(cat "$PREV_STATE_FILE")"
    [ -f "$SHARED_DIR/.env" ] && echo "Database:      $(db_backend)"
    echo
    local latest_log
    latest_log="$(find "$LOG_DIR" -maxdepth 1 -name 'deploy-*.log' -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | head -n1 | cut -d' ' -f2-)"
    if [ -n "$latest_log" ]; then
        local latest_release
        latest_release="$(basename "$latest_log" .log | sed 's/^deploy-//')"
        printf "Last deploy attempt (%s): " "$latest_release"
        _deploy_result "$latest_release"
        echo "  ('$0 logs' to see its output)"
    else
        c_yellow "Last deploy attempt: none recorded yet."
    fi
    echo
    echo "All releases on disk:"
    if [ -d "$RELEASES_DIR" ]; then
        local dir
        for dir in "$RELEASES_DIR"/*/; do
            [ -d "$dir" ] || continue
            dir="${dir%/}"
            local marker=" " tag=""
            if [ -L "$CURRENT_LINK" ] && [ "$(readlink -f "$dir")" = "$(readlink -f "$CURRENT_LINK")" ]; then
                marker="*"
            fi
            [ -f "$dir/.deploy_ok" ] || tag=" (unverified — no .deploy_ok; deploy never finished cleanly)"
            printf "  %s %-40s %s%s\n" "$marker" "$(basename "$dir")" "$(du -sh "$dir" 2>/dev/null | cut -f1)" "$tag"
        done
    fi
    echo
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        c_green "Service '$SERVICE_NAME' is running."
    else
        c_red "Service '$SERVICE_NAME' is NOT running."
    fi
}

# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
case "${1:-}" in
    install)   shift; cmd_install "$@" ;;
    deploy)    shift; cmd_deploy "$@" ;;
    promote)   shift; cmd_promote "$@" ;;
    rollback)  shift; cmd_rollback "$@" ;;
    cleanup)   shift; cmd_cleanup "$@" ;;
    status)    shift; cmd_status "$@" ;;
    logs)      shift; cmd_logs "$@" ;;
    backup)    shift; cmd_backup "$@" ;;
    manage)    shift; cmd_manage "$@" ;;
    # Internal — launched by cmd_deploy itself, detached.
    _deploy_worker) shift; _deploy_worker "$@" ;;
    -h|--help|"") sed -n '2,45p' "$0" ;;
    *) die "Unknown command '$1'. Run '$0 --help' for usage." ;;
esac
