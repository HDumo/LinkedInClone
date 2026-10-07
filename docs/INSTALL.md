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

Services: `linkedclone` (the app), `nginx` (the front door), `linkedclone-backup.timer` (nightly backup at about 03:15), `linkedclone-chores.timer` (daily housekeeping at about 04:20), `postgresql`.

## Security: what it does and how to run it

All of this is built in and on by default. Administrators and staff manage it at **`/security/`** (staff see a **Security** link in the top bar).

**For members**
- **Sign-in lockout.** After 3 wrong passwords a member sees "N attempts remaining"; the 4th locks the account for 1 minute, then 2, 3, 5, 10, 30 and 60 minutes for each further wrong try. It applies to `/admin/login/` too. Made-up usernames lock exactly like real ones, so the lockout can't be used to find out who has an account.
- **Unlock with a code.** "Locked out?" on the sign-in page emails a 6-digit code (valid 10 minutes, five tries, stored only as a hash). The page gives the same answer whether or not the account exists. Asking for or guessing codes is throttled with growing waits.
- **Password rules.** At least 10 characters, not common or all numbers, not too similar to the name or email, and (setting) not the current password or one of the last 5. Passwords are stored with Argon2. A password reset also signs the person out everywhere and clears a lockout.
- **Signed-in devices** (`/accounts/devices/`): see where you're signed in, sign out one device or all others. A sign-in from a new device or place adds a notice to Notifications. **Your sign-in activity** (`/accounts/activity/`) lists recent successes and failures.

**For staff and administrators** (`/security/`)
- **Sign-ins:** every attempt, successful or not, with address, device and (optionally) place; filter by name, address or result. The last 24 hours are summarised at the top.
- **Locked:** accounts with failed sign-ins, with an Unlock button.
- **People:** search members; require a new password, sign out everywhere, unlock, deactivate or reactivate. Only administrators can act on staff accounts, and nobody can deactivate themselves.
- **Visitors:** every address that has visited, with counts. Block or unblock addresses; administrators can also whitelist them. Addresses on the whitelist, your own address, and private or server addresses can never be blocked, and `/admin/` and `/healthz/` always stay reachable so nobody can lock themselves out.
- **Rules (administrators):** automatic rules such as "20 or more visits within 5 minutes" or "outside the US and 10 sign-in tries in 30 minutes". Tick the conditions you want, choose "block and alert", "block quietly" or "only alert", and use **Preview matches** to see who a rule would catch before saving. Alerts appear in administrators' Notifications.
- **Settings (administrators):** how long to keep sign-in history (default 365 days), the password-reuse rule, the whitelist, and whether staff can see Trending.
- **Trending:** page views, visitors, signed-in members, new members, top pages, where visitors came from, devices, and the most liked and commented posts, for 24 hours, 7, 30 or 90 days. It is recorded on the server (no tracking scripts), skips browsers that send "Do Not Track" or "Global Privacy Control", and can be switched off in Settings. Old page views are deleted after 90 days (adjustable).

**Locations (optional).** Country-based rules and "where did this sign-in come from" need a free offline database. Download it once and then about monthly:
`sudo /opt/linkedclone/deploy-release.sh manage update_ip_location_db`
(IP geolocation by DB-IP, CC BY 4.0.) Without it everything works; country rules simply never match, so no one is blocked by a missing database.

**Behind nginx.** The site trusts only the *last* address in `X-Forwarded-For`, which nginx sets, so visitors can't fake their address. Don't expose gunicorn (port 8000) to the internet; the provided firewall advice leaves it on 127.0.0.1 only.

## Email (password reset and unlock codes)

Out of the box, password-reset emails and unlock codes are written to the service log instead of being sent, so nothing breaks (but nobody receives them, so configure email before real members rely on them). To send real mail, put a provider's SMTP details in `shared/.env` (`EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`; any provider works) and restart the service. Until then, you can read a reset link with `sudo journalctl -u linkedclone -n 50`.

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
| A member says they're locked out | They can use **Locked out?** on the sign-in page, or you can unlock them in **Security → Locked**. |
| "Access denied." for someone | Their address is blocked. Find it in **Security → Visitors** (the block reason says whether a person or a rule did it) and unblock it, or whitelist it. |
| Everyone suddenly gets "Access denied." | A rule is too aggressive or your proxy hides real addresses. Turn the rule off in **Security → Rules** (rules never touch `/admin/`, so you can always sign in there). |
| A staff member can't reach pages and keeps landing on "change password" | An administrator required a new password. They must change it once; or clear it in **Security → People**. |
| Certificate step fails | DNS for the domain (and `www.`) must already point at this server, with ports 80 and 443 open. Run `--tls` again. |
| Browser warns about HTTPS right after step 2 | Expected until step 5. Use `http://` until HTTPS is on. |
| Lost the administrator password | `sudo /opt/linkedclone/deploy-release.sh manage changepassword USERNAME` |

## What is and isn't included

Included: sign-up and sign-in with throttling, lockout and unlock codes, sign-in history, password reset, change and reuse rules, forced password changes, signed-in devices, IP blocking with whitelist and automatic rules, a staff Security portal, Trending, profile photos with size and type checks, security headers, HTTPS with automatic renewal, HSTS, nightly backups, daily housekeeping, health check, friendly error pages, a robots.txt.

Not included: two-factor sign-in, email verification at sign-up, "view as this member" impersonation, data export and deletion requests, and the click and scroll tracking Pikes Peak has (it needs JavaScript, which this site deliberately doesn't ship). Also no monitoring service and no staging environment.
