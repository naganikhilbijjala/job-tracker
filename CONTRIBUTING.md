# Contributing

Thanks for helping out! Bug reports, ideas and pull requests are all welcome.

## Run it locally

You need Python 3.12 or newer. There is nothing to install.

```bash
git clone https://github.com/naganikhilbijjala/job-tracker.git
cd job-tracker
./run.sh        # or: python3 server.py   →  http://127.0.0.1:8765
```

Your local data goes in `data/`, which git ignores. To try the AI features, add an API key on the Profile tab. A free Gemini or Groq key works.

## How the project is built

| Path | What |
|---|---|
| `server.py` | All routes and the API (one WSGI app; Vercel runs `server:app`) |
| `database.py` | Local SQLite, or Turso when `TURSO_DATABASE_URL` is set |
| `ai.py` | AI providers and prompts |
| `scoring.py` | Resume keyword and format scoring |
| `autofill.py` | Matching application-form questions to profile facts and saved answers |
| `pdf.py` | Resume PDFs (one page, real text, no dependencies) |
| `tests/` | Automated tests (standard library `unittest`) |
| `mcp.py`, `mcp_bridge.py` | MCP server for Claude |
| `gdrive.py` | Google Drive backup |
| `public/` | The web app (plain HTML, CSS and JavaScript, no build step) |
| `templates/print.html` | The tailored resume / print-to-PDF page |
| `extension/` | Browser extension |

## Ground rules

- **Python standard library only.** No `pip install`, no frameworks. This keeps setup to one command and lets anyone deploy for free. If a feature really needs a package, open an issue to discuss it first.
- **No build step for the front end.** Plain JavaScript that runs as written.
- **Bump the `?v=` number** on `/static/…` links in the HTML pages when you change a CSS or JS file, so phones that cached the old file pick up the new one.
- **It has to work on a phone.** Check new screens at about 390px wide, and in dark mode.
- **The AI must never invent facts.** Resumes and answers come only from the user's profile. Keep that rule in any prompt you change.
- **Never commit personal data.** No real resumes, job data, API keys, tokens, or `.env` files. Use made-up examples in issues and pull requests too.
- Match the style of the code around your change, and keep pull requests focused on one thing.

## Before you open a pull request

1. Run the tests: `python3 -m unittest discover tests`. They cover sign-in, job limits, and that no user can reach another user's data. If you add a route that reads or writes a user's data, add it to `tests/test_multi_user.py`.
2. Run the app and try the feature you changed, including on a narrow window.
3. Check the browser console for errors.
4. If you changed the database schema, make sure an existing `data/tracker.db` still works. Tables are created with `CREATE TABLE IF NOT EXISTS`, so new columns on existing tables need an explicit migration (see `_migrate` in `database.py`).
5. Describe what you changed and how you tested it.

More tests are very welcome, especially for the parts the suite doesn't cover yet.

## Reporting security issues

Please don't open a public issue for a security problem. Use **Security → Report a vulnerability** on the GitHub repository so it can be fixed before details are public.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
