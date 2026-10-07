# Installing LinkedClone on a server

This takes about 20 minutes. You need:

- A server running **Ubuntu 22.04 or 24.04** (or Debian 12) that you can SSH into as a user with `sudo`. 1 GB of memory is enough.
- A **domain name** you control (you can start without DNS and add it later).
- In your cloud provider's firewall, **ports 22, 80 and 443 open**.

## Quick guide

```bash
# 1. Get the code onto the server (use the same URL you cloned with)
git clone https://github.com/HDumo/LinkedInClone.git
cd LinkedInClone

# 2. One-time server setup: packages, database, user, nginx, service
sudo ./deploy/provision.sh --domain example.com --email you@example.com

# 3. Build, test and start the site
sudo /opt/linkedclone/deploy-release.sh install

# 4. Create your administrator login
sudo /opt/linkedclone/deploy-release.sh manage createsuperuser

# 5. Point example.com and www.example.com at the server (DNS "A" records), wait until
#    http://example.com/ opens, then turn on HTTPS:
sudo ./deploy/provision.sh --domain example.com --email you@example.com --tls
```

Your site is now at `https://example.com/`. The administrator area is `/admin/`.

Small trial server? Add `--sqlite` to step 2 to skip PostgreSQL. Don't want `www.`? Add `--no-www`.
For a private repository, clone with a URL that works on the server (an SSH deploy key, or an `https://TOKEN@github.com/...` URL); the same address is saved in `/opt/linkedclone/deploy.conf` and reused for every update.

## Everyday commands

Run these from anywhere. All of them need `sudo`.

| What you want | Command |
|---|---|
| Release a new version | `deploy-release.sh deploy` then `deploy-release.sh promote` |
| Undo the last release | `deploy-release.sh rollback` |
| See what is live and the last result | `deploy-release.sh status` |
| Check on a deploy after your connection dropped | `deploy-release.sh logs` (add `-f` to follow) |
| Back up now | `deploy-release.sh backup` |
| Run any Django command | `deploy-release.sh manage <command>` |
| Free disk space | `deploy-release.sh cleanup` |

(Prefix each with `/opt/linkedclone/`.) **`deploy` never changes the live site.** It builds the new version in its own folder and runs all the tests. Only `promote` backs up the database, applies database changes and switches over, and it refuses a version that failed its tests.

Change a setting (domain, email, database)? Edit `/opt/linkedclone/shared/.env`, then `sudo systemctl restart linkedclone`. No redeploy is needed.

## How it is laid out

```
/opt/linkedclone/
  releases/<timestamp>/   every version, each with its own Python environment
  current  ->  releases/X the live one; nginx and the service always point here
  shared/.env             your one settings file
  shared/media/           profile photos (kept across every release)
  shared/data/            the SQLite database, only if you chose --sqlite
  backups/                database dumps taken before each promote and nightly at about 03:15
  logs/                   the full output of every deploy
  deploy-release.sh       the tool above
```

Services: `linkedclone` (the app), `nginx` (the front door), `linkedclone-backup.timer` (nightly backup), `postgresql`.

## Email (password reset)

Out of the box, password-reset emails are written to the service log instead of being sent, so nothing breaks. To send real mail, put a provider's SMTP details in `shared/.env` (`EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`; any provider works) and restart the service. Until then, you can read a reset link with `sudo journalctl -u linkedclone -n 50`.

## Backups

A backup is taken before every promote and every night, keeping the newest 15 of each. They are on the same disk as the site, so **copy `/opt/linkedclone/backups/` somewhere else regularly**.

Restore PostgreSQL: `zcat backups/FILE.sql.gz | sudo -u postgres psql linkedclone` (after dropping and recreating the empty database). Restore SQLite: stop the service, copy the file over `shared/data/db.sqlite3`, fix ownership with `sudo chown linkedclone:linkedclone shared/data/db.sqlite3`, start the service. Photos: `tar -xzf backups/media-FILE.tar.gz -C /opt/linkedclone/shared`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `deploy` says tests failed | Nothing live changed. Run `deploy-release.sh logs` and read the failing test. Fix it in the code and deploy again. |
| `promote` says "Refusing to promote an unverified release" | That deploy didn't finish (tests failed or the connection died). Run `logs <name>`, then `deploy` again. |
| "Another deploy-release.sh command is already running" | A deploy is in progress (`logs -f`). If sure none is, `sudo rm /opt/linkedclone/.deploy.lock`. |
| Health check failed after promote | `sudo journalctl -u linkedclone -n 50 --no-pager`. Roll back with `deploy-release.sh rollback`. |
| 400 "Bad Request" for everyone | Your domain isn't in `ALLOWED_HOSTS` in `shared/.env`. |
| Login or sign-up fails with a "Session expired" / CSRF page | `CSRF_TRUSTED_ORIGINS` is missing the address you're using (it needs the `https://`). Restart after editing. |
| Pages have no styling | Run `deploy-release.sh status`. If nothing is live, run `install`. Otherwise `promote` again; check `current/staticfiles` exists. |
| Profile photos give 404 | `sudo ls -ld /opt/linkedclone /opt/linkedclone/shared /opt/linkedclone/shared/media`; they must be readable by everyone. `deploy` repairs the media folder's permissions every time. |
| "Too many attempts" page | Sign-in is limited to 10 tries a minute, sign-up to 10 an hour and password reset to 5 an hour per address. Wait and retry. |
| Certificate step fails | DNS for the domain (and `www.`) must already point at this server, with ports 80 and 443 open. Run `--tls` again. |
| Browser warns about HTTPS right after step 2 | Expected until step 5. Use `http://` until HTTPS is on. |
| Lost the administrator password | `sudo /opt/linkedclone/deploy-release.sh manage changepassword USERNAME` |

## What is and isn't included

Included: sign-up and sign-in with throttling, password reset and change, profile photos with size and type checks, security headers, HTTPS with automatic renewal, HSTS, nightly backups, health check, friendly error pages, a robots.txt.

Not included yet: email verification at sign-up, login history, two-factor sign-in, a monitoring service, and a staging environment.
