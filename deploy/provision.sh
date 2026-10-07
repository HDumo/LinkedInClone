#!/usr/bin/env bash
#
# provision.sh — one-time setup of a fresh Ubuntu/Debian server for LinkedClone.
#
#   git clone <your repo URL> && cd LinkedInClone
#   sudo ./deploy/provision.sh --domain example.com --email you@example.com
#   sudo /opt/linkedclone/deploy-release.sh install
#   sudo /opt/linkedclone/deploy-release.sh manage createsuperuser
#   # point your domain's DNS at this server, then:
#   sudo ./deploy/provision.sh --domain example.com --email you@example.com --tls
#
# Options:
#   --domain NAME   the site's address (required), e.g. example.com
#   --email ADDR    contact address for the Let's Encrypt certificate (required with --tls)
#   --no-www        do not also serve www.NAME
#   --sqlite        use a SQLite file instead of PostgreSQL (small trial servers only)
#   --tls           second step: get the certificate and switch the site to HTTPS only
#
# Safe to run again: it never overwrites an existing .env, database or password.
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/linkedclone}"
SERVICE_NAME="linkedclone"
SERVICE_USER="linkedclone"
DB_NAME="linkedclone"
DB_USER="linkedclone"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
NGINX_AVAILABLE="${NGINX_AVAILABLE:-/etc/nginx/sites-available}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled}"
NGINX_CONFD="${NGINX_CONFD:-/etc/nginx/conf.d}"
SKIP_APT="${SKIP_APT:-0}"   # tests only

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$HERE/.." && pwd)"

c_green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
c_yellow() { printf '\033[0;33m%s\033[0m\n' "$1"; }
c_red() { printf '\033[0;31m%s\033[0m\n' "$1"; }
die() { c_red "ERROR: $1"; exit 1; }

DOMAIN="" EMAIL="" WWW=1 SQLITE=0 TLS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="${2:-}"; shift 2 ;;
        --email) EMAIL="${2:-}"; shift 2 ;;
        --no-www) WWW=0; shift ;;
        --sqlite) SQLITE=1; shift ;;
        --tls) TLS=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) die "Unknown option '$1'. Run '$0 --help'." ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."
[ -n "$DOMAIN" ] || die "Give your site's address: sudo $0 --domain example.com --email you@example.com"
printf '%s' "$DOMAIN" | grep -qE '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' || die "'$DOMAIN' does not look like a domain name (example: example.com)."
[ -f "$REPO_DIR/manage.py" ] || die "Run this from inside a clone of the LinkedClone repository (manage.py not found next to deploy/)."
if [ "$TLS" = "1" ] && [ -z "$EMAIL" ]; then die "--tls needs --email (Let's Encrypt sends expiry warnings there)."; fi

if [ "$WWW" = "1" ]; then SERVER_NAMES="$DOMAIN www.$DOMAIN"; else SERVER_NAMES="$DOMAIN"; fi

fill() {  # fill TEMPLATE DEST
    sed -e "s#__APP_ROOT__#$APP_ROOT#g" -e "s#__DOMAIN__#$DOMAIN#g" -e "s#__SERVER_NAMES__#$SERVER_NAMES#g" "$1" > "$2"
}

# --- --tls: second step -------------------------------------------------------
if [ "$TLS" = "1" ]; then
    [ -f "$APP_ROOT/shared/.env" ] || die "Run '$0 --domain $DOMAIN --email $EMAIL' (without --tls) first."
    c_yellow "Requesting a certificate for: $SERVER_NAMES"
    c_yellow "(This only works once your DNS already points these names at this server.)"
    mkdir -p /var/www/certbot
    CERT_ARGS=(); for n in $SERVER_NAMES; do CERT_ARGS+=(-d "$n"); done
    if [ "$SKIP_APT" != "1" ]; then
        apt-get install -y -qq certbot > /dev/null
    fi
    certbot certonly --webroot -w /var/www/certbot "${CERT_ARGS[@]}" --email "$EMAIL" --agree-tos --non-interactive \
        || die "certbot could not issue a certificate. Check that DNS for $SERVER_NAMES points at this server and that ports 80 and 443 are open in your cloud firewall, then run this again."
    fill "$HERE/nginx.conf" "$NGINX_AVAILABLE/$SERVICE_NAME"
    nginx -t || die "The new nginx config failed validation; the site is unchanged."
    systemctl reload nginx
    origins=""; for n in $SERVER_NAMES; do origins="${origins:+$origins,}https://$n"; done
    python3 - "$APP_ROOT/shared/.env" "$origins" <<'PY'
import re, sys
path, origins = sys.argv[1:3]
s = open(path).read()
def setkey(s, key, val):
    if re.search(rf"^{key}=.*$", s, re.M):
        return re.sub(rf"^{key}=.*$", f"{key}={val}", s, flags=re.M)
    return s + f"\n{key}={val}\n"
s = setkey(s, "HTTPS_ONLY", "True")
s = setkey(s, "CSRF_TRUSTED_ORIGINS", origins)
open(path, "w").write(s)
PY
    systemctl is-active --quiet "$SERVICE_NAME" && systemctl restart "$SERVICE_NAME"
    # Let's Encrypt certificates last 90 days; the certbot package's own timer renews them.
    # A deploy hook makes nginx pick up the renewed certificate.
    mkdir -p /etc/letsencrypt/renewal-hooks/deploy
    printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
    chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
    c_green "HTTPS is on. Visit https://$DOMAIN/"
    exit 0
fi

# --- 1. Operating-system packages -------------------------------------------
if [ "$SKIP_APT" != "1" ]; then
    command -v apt-get > /dev/null 2>&1 || die "This script supports Ubuntu/Debian (apt-get not found)."
    if [ "$SQLITE" = "1" ]; then DB_LABEL="sqlite"; else DB_LABEL="postgresql"; fi
    c_yellow "Installing system packages (python, git, nginx, $DB_LABEL)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    PKGS=(python3 python3-venv python3-pip git nginx curl sqlite3 ca-certificates)
    [ "$SQLITE" = "1" ] || PKGS+=(postgresql)
    apt-get install -y -qq "${PKGS[@]}" > /dev/null
fi

# --- 2. Service user and folders --------------------------------------------
if ! id "$SERVICE_USER" > /dev/null 2>&1; then
    c_yellow "Creating the '$SERVICE_USER' system user the app will run as..."
    useradd --system --user-group --home-dir "$APP_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$APP_ROOT/releases" "$APP_ROOT/shared/media" "$APP_ROOT/backups" "$APP_ROOT/logs"
chmod 755 "$APP_ROOT" "$APP_ROOT/shared"            # nginx must be able to walk down to shared/media
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_ROOT/shared/media"
chmod 750 "$APP_ROOT/backups"

# --- 3. Database ------------------------------------------------------------
DB_URL=""
if [ "$SQLITE" != "1" ]; then
    if [ -f "$APP_ROOT/shared/.env" ] && grep -qE '^DATABASE_URL=.+' "$APP_ROOT/shared/.env"; then
        c_yellow "Database already configured in shared/.env — leaving it alone."
    else
        c_yellow "Creating the PostgreSQL database and user (CREATEDB lets the test suite run on the server)..."
        DB_PASS="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
        if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
            sudo -u postgres psql -qc "ALTER ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS' CREATEDB"
        else
            sudo -u postgres psql -qc "CREATE ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS' CREATEDB"
        fi
        sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
            || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
        DB_URL="postgres://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
    fi
fi

# --- 4. The shared .env (never overwritten) ---------------------------------
if [ -f "$APP_ROOT/shared/.env" ]; then
    c_yellow "shared/.env already exists — keeping it (edit it by hand to change settings)."
else
    c_yellow "Writing $APP_ROOT/shared/.env ..."
    origins=""; for n in $SERVER_NAMES; do origins="${origins:+$origins,}https://$n,http://$n"; done
    hosts="$(printf '%s' "$SERVER_NAMES" | tr ' ' ','),localhost,127.0.0.1"
    SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(60))')"
    python3 - "$REPO_DIR/.env.example" "$APP_ROOT/shared/.env" "$SECRET" "$hosts" "$origins" "$DB_URL" "$APP_ROOT/shared/db.sqlite3" "$SQLITE" "$DOMAIN" <<'PY'
import re, sys
src, dest, secret, hosts, origins, db_url, sqlite_path, sqlite, domain = sys.argv[1:10]
s = open(src).read()
def setkey(s, key, val):
    return re.sub(rf"^{key}=.*$", lambda m: f"{key}={val}", s, count=1, flags=re.M)
s = setkey(s, "DEBUG", "False")
s = setkey(s, "DJANGO_SECRET_KEY", secret)
s = setkey(s, "ALLOWED_HOSTS", hosts)
s = setkey(s, "CSRF_TRUSTED_ORIGINS", origins)
s = setkey(s, "HTTPS_ONLY", "False")  # flipped to True by `provision.sh --tls`
s = setkey(s, "DATABASE_URL", db_url)
if sqlite == "1":
    s = setkey(s, "SQLITE_PATH", sqlite_path)
s = setkey(s, "DEFAULT_FROM_EMAIL", f"LinkedClone <no-reply@{domain}>")
open(dest, "w").write(s)
PY
    chown "root:$SERVICE_USER" "$APP_ROOT/shared/.env"
    chmod 640 "$APP_ROOT/shared/.env"
fi
if [ "$SQLITE" = "1" ]; then
    touch "$APP_ROOT/shared/db.sqlite3"
    chown "$SERVICE_USER:$SERVICE_USER" "$APP_ROOT/shared/db.sqlite3"
    chmod 660 "$APP_ROOT/shared/db.sqlite3"
fi

# --- 5. deploy.conf and the deploy script -----------------------------------
ORIGIN_URL="$(git -C "$REPO_DIR" remote get-url origin 2> /dev/null || true)"
if [ ! -f "$APP_ROOT/deploy.conf" ]; then
    {
        echo "# Written by provision.sh. Edit if the repository address changes."
        if [ -n "$ORIGIN_URL" ]; then echo "REPO_URL=\"$ORIGIN_URL\""; fi
    } > "$APP_ROOT/deploy.conf"
fi
cp "$HERE/deploy-release.sh" "$APP_ROOT/deploy-release.sh"
chmod +x "$APP_ROOT/deploy-release.sh"

# --- 6. systemd + nginx -----------------------------------------------------
c_yellow "Installing the systemd service, nightly backup timer and nginx site..."
fill "$HERE/linkedclone.service" "$SYSTEMD_DIR/$SERVICE_NAME.service"
fill "$HERE/linkedclone-backup.service" "$SYSTEMD_DIR/$SERVICE_NAME-backup.service"
fill "$HERE/linkedclone-backup.timer" "$SYSTEMD_DIR/$SERVICE_NAME-backup.timer"
mkdir -p "$NGINX_AVAILABLE" "$NGINX_ENABLED" "$NGINX_CONFD" /var/www/certbot
cp "$HERE/nginx-limits.conf" "$NGINX_CONFD/linkedclone-limits.conf"
if [ ! -f "$NGINX_AVAILABLE/$SERVICE_NAME" ] || ! grep -q ssl_certificate "$NGINX_AVAILABLE/$SERVICE_NAME"; then
    fill "$HERE/nginx-http.conf" "$NGINX_AVAILABLE/$SERVICE_NAME"
fi
ln -sfn "$NGINX_AVAILABLE/$SERVICE_NAME" "$NGINX_ENABLED/$SERVICE_NAME"
rm -f "$NGINX_ENABLED/default"
if command -v nginx > /dev/null 2>&1; then
    nginx -t || die "nginx rejected its configuration — see the message above. Nothing was started."
fi
if command -v systemctl > /dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" > /dev/null 2>&1 || true
    systemctl enable --now "$SERVICE_NAME-backup.timer" > /dev/null 2>&1 || true
    systemctl reload nginx 2> /dev/null || systemctl restart nginx || true
fi

c_green "Server is provisioned. Nothing is running yet."
echo
echo "Next:"
echo "  1. sudo $APP_ROOT/deploy-release.sh install        # builds, tests and starts the site (a few minutes)"
echo "  2. sudo $APP_ROOT/deploy-release.sh manage createsuperuser"
echo "  3. Visit http://$DOMAIN/ (or http://THIS-SERVER-IP/ before DNS is ready)."
echo "  4. Point $SERVER_NAMES at this server, then turn on HTTPS:"
echo "       sudo $0 --domain $DOMAIN --email you@example.com --tls"
echo "Optional: add email credentials to $APP_ROOT/shared/.env so password-reset mail is delivered."
