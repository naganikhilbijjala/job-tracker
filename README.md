# ApplyStack

Use it at **[applystack.app](https://applystack.app)**: sign in with Google, free.

A job application tracker, similar to Teal. You can run it on your computer or host it for free on Vercel, so you can use it from your phone and any other device.

- **Board**: on a computer, drag cards between Saved → Applied → Interviewing → Offer / Rejected / Withdrawn. On a phone, swipe between columns and change status with a dropdown.
- **List**: a sortable table on desktop that becomes a card list on phones.
- Saves the **job description** with each job, so you still have it after the posting is taken down.
- Records **which resume** you sent to each job. Keeps a resume library, with optional **Google Drive** backup.
- Sets the applied date and a 7-day **follow-up reminder** when you mark a job Applied.
- **Visa sponsorship check**: reads each job description and tags jobs that say they don't sponsor visas (or need citizenship or a security clearance), so you can skip them. The job's panel quotes the sentence, and the extension warns you before you save. Turn it off on the Profile tab if you don't need sponsorship.
- **✨ AI assistant** on each job: tailored resumes (saved as PDF), skills lists, and answers for application forms, using your choice of AI provider. Each tailored resume gets a **score**: keyword match with the job, format checks, and an AI fact check against your profile. Plus an **MCP connection** so Claude can use your tracker directly.
- **Auto-fill** from a pasted link, a **browser extension** that saves a job in one click, and on Android, **Share → ApplyStack** from LinkedIn or any other app.
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
- **Android (Chrome):** ⋮ → **Add to Home screen** / **Install app**. After that, **ApplyStack appears in the Share menu**: in the LinkedIn app, tap Share on a job → ApplyStack, and it opens with the job auto-filled.

---

## Browser extension (one-click save)

On any job posting, click the extension icon or press **Alt+Shift+J**. It reads the title, company, location, salary and full description. Pick the status and the resume you used, then click Save. If you save a job that's already tracked, it fills in the blanks instead of making a duplicate.

Install it in Chrome:
1. Go to `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and choose the `extension/` folder.
2. Click the extension, then enter your tracker's address (shown on the **Resumes** tab) and your password.

It works with LinkedIn, Indeed, Workday, Greenhouse, Lever, Ashby, Glassdoor, and most company career pages. On an unusual site, **highlight the job description** before clicking, and it will use that text.

### Filling application forms

On an application form (LinkedIn Easy Apply, Greenhouse, Lever and most others), open the extension and click **✨ Fill empty fields**. It only touches empty fields, so it works alongside Simplify, and it never submits or clicks Next for you. Each answer comes from, in order:

1. **Your profile**: LinkedIn, GitHub, website, email, phone and name.
2. **Answers you saved**: the same question, or the same kind of question worded differently (one sponsorship answer covers "Do you require sponsorship?" and "Will you now or in the future need a visa?").
3. **The AI**, in one request, for the rest: yes/no experience questions, "How many years…", and "Why this company?". It uses only your profile and says "Yes" only when the profile shows it.

Filled fields are outlined: **green** from your profile or saved answers, **purple** written by the AI (check these). Salary, sponsorship, work authorization, start date, demographics and consent questions are never guessed; they're left for you. After answering them, click **Remember my answers** and they fill in on the next form. Review or edit saved answers on the **Profile** tab.

Reload the extension after updating (`chrome://extensions` → ↻) to get new features. For a form inside an embedded frame (some company sites embed Greenhouse), open the form on its own page first.

The **paste-a-link** auto-fill in the app itself works for Greenhouse, Lever, Ashby and most company career sites. LinkedIn usually blocks it, so use the extension or Android's Share menu for LinkedIn.

---

## ✨ AI assistant (tailored resumes, form answers)

1. **Profile tab:** paste your full master resume. Include everything (all roles, projects, skills, numbers), because the assistant picks what fits each job and only uses facts from here. Add standing instructions such as "one page, lead with backend work".
2. **Profile tab → AI provider:** choose Anthropic (Claude), OpenAI, Gemini, OpenRouter, Groq, or any OpenAI-compatible endpoint, then paste your API key. The models your key can use load into the list; pick one and click **Test**. Each provider keeps its own key, so you can switch between them without pasting it again. You pay the provider directly; a tailored resume costs a few cents. The key is stored in your tracker's database and is never sent back to the browser. **Free option:** a Google Gemini key, plus a free Groq key added with **Save as backup**. When the main provider is overloaded, the app retries briefly (about 10 seconds at most), then the backup answers. The step-by-step guide is at `/guides/free-ai-setup.html` on your site.
3. **On any job, open ✨ Assistant.** It knows your profile and that job's saved description. Use the quick actions (*Tailor resume*, *10 skills to add*, *Why this company?*, *Cover letter*, *How well do I match?*), or paste any question from an application form. Each job keeps its own chat history.
4. When it writes a resume, click **Open & save as PDF**. You can edit the page directly, then click **Download PDF**: a one-page file named like `Your Name Resume - Company.pdf`, ready to upload. On a phone it goes to Downloads, and it's first under **Recent** when an application asks for your resume. Tailored resumes stay listed on the job, with a **PDF ↓** link.
5. **Check the score** in the panel beside the resume (the **Score** button shows or hides it). Applicant-tracking systems don't really score resumes. They read the text and recruiters search it by keyword, so the score estimates both:
   - **Keywords (60%):** skills and phrases from the job description, split into *in the resume*, *in your profile but missing here* (easy wins), and *not in your profile* (real gaps: don't add these just for the score). A dot marks ones listed as requirements.
   - **Format (40%):** name and contact details, standard section headings, dates on every role in one format, bullets per role, measurable results, strong opening verbs, length, and no tables or images.
   - **AI review** (one AI request, on demand): a **fact check** that flags any claim your profile doesn't back up, plus a **recruiter's view** with a fit rating, strengths, and what to improve. It's marked out of date when you edit the resume.
   - **Revise in chat** sends all of the above, with the current resume, to the job's assistant for a corrected version.

## Use with Claude (MCP)

At your computer, you can use Claude itself with full access to your tracker: jobs, saved descriptions, profile, and past tailored resumes. Claude can also save new resumes back to the tracker. The **Profile tab** shows your connection token and copy-ready setup for each app:

- **Claude Code:** `claude mcp add --scope user --transport http applystack https://YOUR-APP.vercel.app/mcp --header "Authorization: Bearer TOKEN"`
- **claude.ai:** Settings → Connectors → Add custom connector → `https://YOUR-APP.vercel.app/mcp?key=TOKEN`
- **Claude Desktop:** add the JSON config from the Profile tab. It runs `mcp_bridge.py` from this folder.

Then ask, for example: *"Tailor my resume for the AssemblyAI job in my tracker and save it."* Saving returns the keyword and format check, and Claude can fix what it finds with `check_tailored_resume` and `update_tailored_resume`. Anyone with the token can read and edit your tracker, so keep it private. **New token** on the Profile tab cancels the old one.

---

## Google Drive backup for resumes (optional, free)

1. Create a project at https://console.cloud.google.com/projectcreate and enable the **Google Drive API**.
2. **OAuth consent screen** → External → add yourself under **Test users**.
3. **Credentials → Create credentials → OAuth client ID**:
   - **Hosted (Vercel):** choose **Web application**, and add the redirect URI `https://YOUR-APP.vercel.app/oauth2callback`. Put the client ID and secret in Vercel as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, then redeploy.
   - **Local:** choose **Desktop app**, download the JSON, and save it as `data/client_secret.json`.
4. Click **Connect Google Drive** on the Resumes tab.

Every resume you upload is then also copied to an **"ApplyStack Resumes"** folder in your Drive. The app uses the `drive.file` permission, so it can only see files it created. While the consent screen is in "Testing" mode, Google expires the connection every 7 days. To avoid that, click **Publish app** on the consent screen; an app that only you use doesn't need Google's review.

---

## Share it with friends (Google sign-in)

One deployment and one database can serve you and anyone you share the link with. Everyone signs in with Google and sees only their own jobs, resumes and chats.

1. **Google Cloud** (if you set up Drive backup above, reuse the same project and OAuth client):
   - Create a project at https://console.cloud.google.com/projectcreate.
   - **OAuth consent screen** → External. Sign-in only asks for name and email.
   - **Credentials → Create credentials → OAuth client ID** → **Web application**, with the redirect URI `https://YOUR-APP.vercel.app/oauth2callback`.
   - On the consent screen, click **Publish app**. While it's in "Testing" mode, only Google accounts you list as test users can sign in.
2. **Vercel → Settings → Environment Variables**, then redeploy:

   | Name | Value |
   |---|---|
   | `ADMIN_EMAIL` | Your Gmail address. This account is the admin. |
   | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | From step 1 |
   | `APP_PASSWORD` | Keep it. It still logs you in as the admin, and keeps you, your phone and the extension logged in. |

3. **Sign in with Google** using your `ADMIN_EMAIL` account. Everything you already had stays yours.
4. **Share the link.** Anyone who signs in with Google gets their own empty tracker straight away.

**Limits.** Each person can save up to 50 jobs (set `JOB_LIMIT` in Vercel to change the default). They don't see the number until they reach it; then adding a job tells them to ask you for more. The **Users** tab, which only you can see, shows everyone's jobs and resume storage. A red badge counts people at their limit. From there you can:
- **Change limit** for one person, raising it or lowering it.
- **Turn off** someone's access. This signs them out everywhere at once (browser, extension, Claude), and their data stays until you **Delete data**.

Each person uses their own AI key (Profile tab) and their own Claude/MCP token. For the browser extension, they click **Get connection code** on the Resumes tab and paste it in place of a password.

**Good to know:**
- As the owner of the database, you can technically read everyone's data, including the AI keys they save. Tell your friends.
- The **Users** tab shows how many jobs and how much resume storage each person uses. Turso's free plan includes 5 GB, which is far more than a group of friends needs.

## Run it on your computer instead

```bash
./run.sh       # http://127.0.0.1:8765, opens your browser
```
Locally, data is stored in `data/tracker.db` and resumes in `data/resumes/` (files you copy into that folder show up automatically). No password is needed, because only your own computer can reach it. Set `APP_PASSWORD` if you want one anyway. To use your Turso database locally, set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` before running.

Keyboard shortcuts: `n` adds a job, `/` searches, `Esc` closes the side panel.

Tests: `python3 -m unittest discover tests`. These cover Google sign-in, job limits, and that no user can reach another user's data.

## Contributing

This project is open source under the [MIT License](LICENSE). Bug reports, ideas and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

## Notes
- Resumes are limited to **4 MB** each, which is Vercel's upload limit.
- **Back up** with **Export CSV** on the List tab. Turso also keeps its own backups.
- Changing `APP_PASSWORD` logs out every device and the extension.

## Files

| Path | What |
|---|---|
| `server.py` | Web app + API (WSGI; Vercel runs `server:app`) |
| `database.py` | Local SQLite or Turso, plus resume file storage |
| `gdrive.py` | Google sign-in, plus Drive OAuth + upload |
| `ai.py` | AI providers (Anthropic + OpenAI-compatible), the assistant's prompt, and the AI resume review |
| `scoring.py` | Resume score: keyword match with the job description and format checks (no AI) |
| `sponsorship.py` | Reads what a job description says about visa sponsorship (no AI) |
| `autofill.py` | Matching application-form questions to profile facts and saved answers |
| `mcp.py`, `mcp_bridge.py` | MCP server for Claude, and the Claude Desktop bridge |
| `public/` | The web pages, icons, and home-screen app manifest; `public/guides/` holds the public how-to guides |
| `templates/print.html` | The tailored resume page (edit, score, download as PDF) |
| `pdf.py` | Resume PDFs with the standard library: layout, fit to one page, Helvetica text |
| `extension/` | Chrome/Firefox extension: save a job, fill application forms |
| `pyproject.toml` | Tells Vercel how to run the app |
