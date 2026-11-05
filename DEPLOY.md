# Deploying to Railway + connecting YouTube, TikTok and Instagram

Do the steps in order. `YOUR-APP` below means your Railway domain, e.g. `faceless-production.up.railway.app`.

> **Tip:** after the first deploy, the admin can enter every API key on the app's **Settings** page instead of in
> Railway Variables. Database and bucket settings must be Railway Variables. The Settings page also shows the exact redirect
> URLs to copy and has a **Test all keys** button.
>
> **Multiple users:** until each platform reviews your app, only a few accounts can connect.
> - **TikTok:** unaudited apps are limited to 5 accounts.
> - **Instagram:** in development mode, only accounts added as testers can connect.
> - **YouTube:** shows an "unverified app" warning, with a 100-user cap, until Google verifies the app.
>
> Submit each app for review before opening the site to the public.

---

## 1. Deploy on Railway

> Railway has no permanent free plan: the Hobby plan is $5/month (that includes $5 of usage).
> Bucket storage is $0.015 per GB-month; a 45-second video is about 5–15 MB.

The project has three pieces: the **app** (this repo), a **Postgres** database (users, channels, video records)
and a **Storage Bucket** (the video files).

1. Put this folder in a **private** GitHub repo. `.gitignore` already keeps out `.env`, local data and videos.
2. Go to https://railway.com/new and choose **Deploy from GitHub repo**, then pick the repo. Railway uses the `Dockerfile`.
3. **Add Postgres:** in the project, click **+ Create → Database → PostgreSQL**.
4. **Add a bucket:** in the project, click **+ Create → Bucket**.
5. **Connect them to the app:** open the app service → **Variables** and add:

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `ENDPOINT` | `${{Bucket.ENDPOINT}}` (use your bucket service's name in place of `Bucket`) |
| `BUCKET` | `${{Bucket.BUCKET}}` |
| `ACCESS_KEY_ID` | `${{Bucket.ACCESS_KEY_ID}}` |
| `SECRET_ACCESS_KEY` | `${{Bucket.SECRET_ACCESS_KEY}}` |
| `REGION` | `${{Bucket.REGION}}` |
| `ADMIN_EMAIL` | **your email**: that account becomes the admin when it signs up |
| `PUBLIC_URL` | `https://YOUR-APP` (after step 6) |

   - **Shortcut:** on the bucket's **Credentials** tab you can also use Railway's "AWS SDK" preset. The app reads
     the standard `AWS_*` names too.
   - **Older buckets:** if the Credentials tab says the bucket needs path-style URLs, also add `S3_ADDRESSING_STYLE=path`.

6. **Get a URL:** go to the app service, then **Settings → Networking → Generate Domain**.
7. Open `https://YOUR-APP`, click **Create account**, and sign up with your `ADMIN_EMAIL`.
8. As admin, open **Settings**. It should show **Database: postgres · Video storage: bucket '…'**.
   - Paste your API keys there (Gemini, Groq, Pixabay, Cloudflare), or set them as Railway Variables.
   - Click **Test all keys**. The "Video storage" line confirms the bucket works.
   - Under **Sign-ups & limits**, choose whether new people can register, and how many videos each user may make per 24 hours (default 3).

> ⚠ If Settings says **sqlite / local folder** on Railway, the database or bucket variables are missing,
> and data will be lost on every redeploy. Fix the variables before inviting users.

No Railway volume is needed: everything permanent lives in Postgres and the bucket.

**Database migrations run automatically on every deploy.** `railway.json` sets the pre-deploy command
`python -m app.migrations`, which runs before the new version starts. If it fails, Railway stops the deploy and the
previous version keeps serving, so a broken migration never takes the site down. In the deploy's logs, look for
`Migrations OK: {... 'up_to_date': True}`. See README → *Database migrations* for how to add one.

**Background music:** put royalty-free tracks in `music/` and commit them. They're built into the image.

**Users:** anyone can register at `/login → Create account` while sign-ups are on. Passwords are stored as argon2 hashes.
The admin can see all users under **Settings → Users**, change a user's daily limit, make someone admin, or disable an account.
Email OTP verification is planned (the database already has an `email_verified` field).

---

## 2. YouTube auto-upload

### a. Create the Google app (one time, free)
1. Create a project: https://console.cloud.google.com/projectcreate
2. Enable the YouTube API: https://console.cloud.google.com/apis/library/youtube.googleapis.com, then click **Enable**.
3. Set up the consent screen: https://console.cloud.google.com/auth/overview, then **Get started**.
   - App name: anything (e.g. `Faceless Shorts`). User support email: yours.
   - Audience: **External**. Contact email: yours. Then **Create**.
4. On **Branding** (https://console.cloud.google.com/auth/branding), add these links:
   home page `https://YOUR-APP`, privacy policy `https://YOUR-APP/privacy`, terms `https://YOUR-APP/terms`.
5. On **Audience** (https://console.cloud.google.com/auth/audience), click **Publish app**, then **In production**.
   - **Why:** in "Testing" mode, Google signs you out every 7 days, and the daily automation would stop.
   - When you connect, Google will say *"Google hasn't verified this app"*. Click **Advanced**, then **Go to … (unsafe)**. That's expected for your own private app.
6. Create the client at https://console.cloud.google.com/auth/clients: **Create client**, then **Web application**.
   - Authorized redirect URIs: `https://YOUR-APP/oauth/youtube/callback`.
     For local testing, add `http://localhost:8000/oauth/youtube/callback` too.
   - Copy the **Client ID** and **Client secret** into Railway as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

### b. Connect a channel
In the app, open **Channels**, edit a channel, and tick **Post to YouTube Shorts**. Then click **Connect YouTube** and pick the
YouTube channel (Brand Account) that should get the videos. Each app channel can use a different YouTube channel.

### c. Important: uploads are Private until Google audits your app
For API projects that haven't been audited, YouTube forces every upload to **Private**, whatever visibility you choose.
- **Until the audit:** open YouTube Studio and switch each video to Public (one click).
- **To remove the limit:** apply for the free audit at https://support.google.com/youtube/contact/yt_api_form
  (choose "audit"; describe it as a tool that uploads your own videos to your own channels).

---

## 3. TikTok drafts

The app sends each video to your **TikTok inbox as a draft**. TikTok then notifies you, and you add the caption
(use **Copy caption + hashtags** in the app) and tap Post. Fully automatic public posting needs a TikTok
"Direct Post" audit, which isn't realistic for a personal tool.

1. Sign in at https://developers.tiktok.com and open **Manage apps** (https://developers.tiktok.com/apps/), then **Connect an app**.
2. Fill in the app details: icon, name, category, description.
   - Terms of Service URL: `https://YOUR-APP/terms`. Privacy Policy URL: `https://YOUR-APP/privacy`.
   - Platform: **Web**, website URL `https://YOUR-APP`.
3. **Add products:** **Login Kit** and **Content Posting API**. Leave "Direct Post" off.
   - Login Kit redirect URI: `https://YOUR-APP/oauth/tiktok/callback`
   - Scopes: `user.info.basic`, `video.upload`
4. **Quickest way to start: use Sandbox.** At the top of the app page, switch to **Sandbox** and create one.
   Under **Target users**, add your own TikTok account(s). Copy the **sandbox** Client key and Client secret into Railway as
   `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET`.
   - Alternatively, submit the app for review to use the Production keys.
5. In the app: **Channels**, then edit a channel, tick **Send to TikTok drafts**, and click **Connect TikTok**.

TikTok only accepts HTTPS redirect URLs, so **connect TikTok on the Railway URL, not on localhost**.

---

## 4. Instagram Reels (auto-post with caption)

Requirements: an Instagram **Business or Creator** account (Instagram app → Settings → Account type and tools →
Switch to professional account). No Facebook Page is needed.

1. Create a Meta app at https://developers.facebook.com/apps/creation/ and choose the use case
   **Manage messaging & content on Instagram**.
2. In the app, open **Instagram → API setup with Instagram login**. Copy the **Instagram app ID** and **Instagram app secret**
   into Settings (or Railway) as `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET`.
   (These are *not* the Facebook app ID/secret shown at the top of the dashboard.)
3. Under **Set up Instagram business login**, add the redirect URL `https://YOUR-APP/oauth/instagram/callback`.
4. **App roles → Roles → Add people → Instagram Tester**: add your Instagram account. Then accept the invite on
   instagram.com → Settings → Apps and websites → **Tester invites**.
   The app can stay in **Development mode**: that's enough to post to your own accounts, and no app review is needed.
5. In the app: **Channels** → edit a channel → tick **Post to Instagram Reels** → **Connect Instagram**.

How posting works: Instagram downloads the video from a private link that expires after 6 hours, then the app publishes it with
the title, description and hashtags as the caption. Instagram allows up to 100 API posts per account per 24 hours.
Sign-ins last 60 days, and the app renews them automatically.

---

## 5. Set up the automation
For each faceless channel: open **Channels**, then **New channel**.
1. Set the niche, guidelines, voice and visual style.
2. Under **Schedule**, tick it and set the times (e.g. `09:00` or `09:00, 18:00`) and your timezone.
3. Under **Publishing**, choose **Post automatically** or **Wait for my approval** per platform.
4. Optionally click **✨ Brainstorm 10 topics** and edit the queue. The AI refills it when it runs out and never repeats used topics.

Videos waiting for approval show **Post to YouTube**, **Send to TikTok** or **Post to Instagram** buttons in the Videos panel.
Every video also keeps **Download MP4** and **Copy caption + hashtags**, so you can always post by hand.

**Reliability:** if a scheduled video fails (for example, the network is down or an AI service is busy), it retries automatically
up to 3 times, 10 minutes apart. If more than a third of the scenes get no picture, the video is stopped and retried
rather than published mostly blank.
