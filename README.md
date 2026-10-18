# Job Tracker

A job application tracker, similar to Teal. You can run it on your computer or host it for free on Vercel, so you can use it from your phone and any other device.

- **Board**: on a computer, drag cards between Saved → Applied → Interviewing → Offer / Rejected / Withdrawn. On a phone, swipe between columns and change status with a dropdown.
- **List**: a sortable table on desktop that becomes a card list on phones.
- Saves the **job description** with each job, so you still have it after the posting is taken down.
- Records **which resume** you sent to each job. Keeps a resume library, with optional **Google Drive** backup.
- Sets the applied date and a 7-day **follow-up reminder** when you mark a job Applied.
- **Auto-fill** from a pasted link, a **browser extension** that saves a job in one click, and on Android, **Share → Job Tracker** from LinkedIn or any other app.
- CSV export.

It uses only the Python standard library, with no packages to install.

---

## Put it online (free): Vercel + Turso

Vercel runs the app. Turso, a free hosted SQLite database, stores your jobs and resumes. Vercel itself has no permanent storage, so the database has to live elsewhere.

### 1. Create the database (Turso)
1. Sign up at **https://turso.tech** (signing in with GitHub is easiest).
2. In the dashboard, **Create Database**. Any name works, e.g. `job-tracker`. Pick the region closest to you.
3. Open the database and copy its **URL**, which looks like `libsql://job-tracker-yourname.turso.io`.
4. Click **Create Token** (read & write, no expiry) and copy it.

### 2. Put the code on GitHub
Create an **empty private repo** at https://github.com/new (e.g. `job-tracker`), then run:
```bash
cd ~/Documents/job-tracker
git init -b main
git add .
git commit -m "Job tracker"
git remote add origin https://github.com/YOUR-USERNAME/job-tracker.git
git push -u origin main
```
`data/` (your local database and resumes) is in `.gitignore`, so none of your local data gets uploaded.

### 3. Deploy on Vercel
1. Sign up at **https://vercel.com** with GitHub (choose the free **Hobby** plan).
2. **Add New → Project**, then import your `job-tracker` repo.
3. Under **Environment Variables**, add:

   | Name | Value |
   |---|---|
   | `APP_PASSWORD` | A long password you'll log in with |
   | `TURSO_DATABASE_URL` | The `libsql://…` URL from step 1 |
   | `TURSO_AUTH_TOKEN` | The token from step 1 |
4. Click **Deploy**. You'll get an address like `https://job-tracker-yourname.vercel.app`.

From then on, every `git push` redeploys automatically. Your data stays in Turso between deploys.

### 4. On your phone
Open your Vercel address and log in. You stay logged in for 6 months. Then:
- **iPhone (Safari):** Share → **Add to Home Screen**.
- **Android (Chrome):** ⋮ → **Add to Home screen** / **Install app**. After that, **Job Tracker appears in the Share menu**: in the LinkedIn app, tap Share on a job → Job Tracker, and it opens with the job auto-filled.

---

## Browser extension (one-click save)

On any job posting, click the extension icon or press **Alt+Shift+J**. It reads the title, company, location, salary and full description. Pick the status and the resume you used, then click Save. If you save a job that's already tracked, it fills in the blanks instead of making a duplicate.

Install it in Chrome:
1. Go to `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and choose the `extension/` folder.
2. Click the extension, then enter your tracker's address (shown on the **Resumes** tab) and your password.

It works with LinkedIn, Indeed, Workday, Greenhouse, Lever, Ashby, Glassdoor, and most company career pages. On an unusual site, **highlight the job description** before clicking, and it will use that text.

The **paste-a-link** auto-fill in the app itself works for Greenhouse, Lever, Ashby and most company career sites. LinkedIn usually blocks it, so use the extension or Android's Share menu for LinkedIn.

---

## Google Drive backup for resumes (optional, free)

1. Create a project at https://console.cloud.google.com/projectcreate and enable the **Google Drive API**.
2. **OAuth consent screen** → External → add yourself under **Test users**.
3. **Credentials → Create credentials → OAuth client ID**:
   - **Hosted (Vercel):** choose **Web application**, and add the redirect URI `https://YOUR-APP.vercel.app/oauth2callback`. Put the client ID and secret in Vercel as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, then redeploy.
   - **Local:** choose **Desktop app**, download the JSON, and save it as `data/client_secret.json`.
4. Click **Connect Google Drive** on the Resumes tab.

Every resume you upload is then also copied to a **"Job Tracker Resumes"** folder in your Drive. The app uses the `drive.file` permission, so it can only see files it created. While the consent screen is in "Testing" mode, Google expires the connection every 7 days. To avoid that, click **Publish app** on the consent screen; an app that only you use doesn't need Google's review.

---

## Run it on your computer instead

```bash
./run.sh       # http://127.0.0.1:8765, opens your browser
```
Locally, data is stored in `data/tracker.db` and resumes in `data/resumes/` (files you copy into that folder show up automatically). No password is needed, because only your own computer can reach it. Set `APP_PASSWORD` if you want one anyway. To use your Turso database locally, set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` before running.

Keyboard shortcuts: `n` adds a job, `/` searches, `Esc` closes the side panel.

## Notes
- Resumes are limited to **4 MB** each, which is Vercel's upload limit.
- **Back up** with **Export CSV** on the List tab. Turso also keeps its own backups.
- Changing `APP_PASSWORD` logs out every device and the extension.

## Files

| Path | What |
|---|---|
| `server.py` | Web app + API (WSGI; Vercel runs `server:app`) |
| `database.py` | Local SQLite or Turso, plus resume file storage |
| `gdrive.py` | Google Drive OAuth + upload |
| `public/` | The web pages, icons, and home-screen app manifest |
| `extension/` | Chrome/Firefox "Save job" extension |
| `pyproject.toml` | Tells Vercel how to run the app |
