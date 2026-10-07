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
- **Notifications:** connection requests/accepts, likes, comments, shares, messages, endorsements, applications

## Run it

    pip install -r requirements.txt
    python manage.py migrate
    python manage.py seed_demo      # optional: alice/bob/carol/dave, password demo-pass-123
    python manage.py runserver

## Test

    python manage.py test

## Not built yet

Recommendations, group chat, image posts, email notifications, login history and rate limiting,
password reset emails, and production settings (this is a development configuration: `DEBUG=True`,
SQLite, hard-coded `SECRET_KEY`). Do not deploy as is.
