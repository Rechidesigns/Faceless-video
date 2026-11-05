# Faceless Shorts

Multi-user web app that turns a topic or a channel niche into vertical 1080x1920 shorts: AI script, neural voiceover,
stock footage and/or AI images, word-highlighted captions, optional music. It can post on a schedule to YouTube Shorts and
Instagram Reels, and send videos to TikTok drafts.

## Run locally
Double-click `run.bat`, then open http://localhost:8000 and **create an account**. The first account is the admin.
Locally it uses a SQLite file and a `blobs/` folder. Any data from the old single-user version (channels, videos) is
imported into that first account automatically.

## Deploy
See [DEPLOY.md](DEPLOY.md): Railway + Postgres + Storage Bucket, then YouTube / TikTok / Instagram app setup.

## Database migrations (Alembic)
Migrations run **automatically**:
- **Before every Railway deploy** (`railway.json → preDeployCommand: python -m app.migrations`). If a migration fails, the deploy stops and the previous version keeps running.
- **At app startup**, as a safety net. A Postgres lock makes concurrent runs wait for each other.

Databases created before migrations existed are detected and adopted at the baseline (`0001_initial`); nothing is recreated.

To change the schema:
1. Edit the models in `app/db.py`.
2. Generate a migration: `.venv\Scripts\alembic revision --autogenerate -m "describe the change"`
3. Review the new file in `migrations/versions/`. Autogenerate is a draft: check defaults, renames and data moves.
4. Apply it locally: `.venv\Scripts\python -m app.migrations` (or just restart the app).
5. Check nothing was missed: `.venv\Scripts\alembic check` should say "No new upgrade operations detected".
6. Commit and deploy. Railway applies it before the new version goes live.

Rules for safe deploys: add new columns as nullable or with a `server_default`, and never rename or drop a column
in the same deploy that stops using it. The old version keeps running against the migrated database until the new one is live.

## How it fits together
| Piece | Where |
|---|---|
| Users, sessions, channels, connected accounts, video records, settings | Postgres (`app/db.py`) |
| Video files | S3-compatible bucket (`app/storage.py`), served through short-lived signed links |
| Script / topics | Gemini → Groq fallback (`app/llm.py`) |
| Voice + word timings | edge-tts (`app/tts.py`) |
| Visuals | Pixabay/Pexels stock video, Cloudflare Workers AI images (`app/visuals.py`) |
| Captions + render | ASS captions, ffmpeg (`app/captions.py`, `app/render.py`) |
| Queue, retries, daily limits | `app/jobs.py` |
| Daily schedule per channel | `app/scheduler.py` |
| Publishing | `app/publish.py` |
| Accounts (email + password, argon2) | `app/auth.py` |
