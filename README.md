# LinkedClone

A LinkedIn-style professional network built with Django.

## Features

- **Accounts:** sign up, sign in/out
- **Profiles:** headline, about, location, photo, experience, education, skills, skill endorsements
- **Feed:** posts, likes, comments, shares; you see your own and your connections' posts
- **Network:** connection requests (send, accept, decline, remove), people search
- **Messaging:** one-to-one messages between connections, unread counts
- **Jobs:** post jobs, search, apply with a cover letter, applicant list for the poster, "my applications"
- **Companies:** company pages with their open jobs
- **Search:** people, jobs and companies from the nav bar
- **Security:** sign-in history, lockout with escalating waits, unlock-by-email-code, password reuse and forced-change rules, signed-in devices, IP blocking with a whitelist and automatic rules, staff **Security** portal, **Trending** (details in [docs/INSTALL.md](docs/INSTALL.md))
- **Notifications:** connection requests/accepts, likes, comments, shares, messages, endorsements, applications

## Run it on your computer

    cp .env.example .env            # then set DEBUG=True in .env
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py seed_demo      # optional: alice/bob/carol/dave, password demo-pass-123
    python manage.py runserver

## Put it on a server

One-time provisioning, one-command releases, instant rollback, nightly backups and HTTPS are built in: see **[docs/INSTALL.md](docs/INSTALL.md)**. The release tooling in `deploy/` is adapted from the Pikes Peak Clean deploy script.

## Test

    python manage.py test

## Not built yet

Recommendations, group chat, image posts, email verification at sign-up, two-factor sign-in, "view as member", data export/deletion requests.
