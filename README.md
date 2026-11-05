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
