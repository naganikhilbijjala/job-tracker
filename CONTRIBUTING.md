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
| `mcp.py`, `mcp_bridge.py` | MCP server for Claude |
| `gdrive.py` | Google Drive backup |
| `public/` | The web app (plain HTML, CSS and JavaScript, no build step) |
| `templates/print.html` | The tailored resume / print-to-PDF page |
| `extension/` | Browser extension |

## Ground rules

- **Python standard library only.** No `pip install`, no frameworks. This keeps setup to one command and lets anyone deploy for free. If a feature really needs a package, open an issue to discuss it first.
- **No build step for the front end.** Plain JavaScript that runs as written.
- **It has to work on a phone.** Check new screens at about 390px wide, and in dark mode.
- **The AI must never invent facts.** Resumes and answers come only from the user's profile. Keep that rule in any prompt you change.
- **Never commit personal data.** No real resumes, job data, API keys, tokens, or `.env` files. Use made-up examples in issues and pull requests too.
- Match the style of the code around your change, and keep pull requests focused on one thing.

## Before you open a pull request

There's no automated test suite yet. Please:

1. Run the app and try the feature you changed, including on a narrow window.
2. Check the browser console for errors.
3. If you changed the database schema, make sure an existing `data/tracker.db` still works. Tables are created with `CREATE TABLE IF NOT EXISTS`, so new columns on existing tables need an explicit migration.
4. Describe what you changed and how you tested it.

Contributions to a test suite are very welcome.

## Reporting security issues

Please don't open a public issue for a security problem. Use **Security → Report a vulnerability** on the GitHub repository so it can be fixed before details are public.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
