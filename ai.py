"""AI providers for the job assistant (stdlib HTTP, so the app stays install-free).

Claude goes through the Anthropic Messages API. Everything else goes through the
OpenAI-style chat completions API, which OpenAI, Gemini, OpenRouter, Groq and
local servers such as Ollama all accept.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

import scoring

# keys_url: where to create a key, linked from the settings panel.
PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "model": "claude-opus-5-5",
                  "keys_url": "https://console.anthropic.com/settings/keys"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "",
               "keys_url": "https://platform.openai.com/api-keys"},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "",
               "keys_url": "https://aistudio.google.com/apikey"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "",
                   "keys_url": "https://openrouter.ai/keys"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": "",
             "keys_url": "https://console.groq.com/keys"},
    "custom": {"label": "Other (OpenAI-compatible)", "base_url": "", "model": "", "keys_url": ""},
}
# Claude models that accept server-side fallback when their safety classifiers decline.
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
TIMEOUT = 280  # Vercel stops a function at 300 s
# Sent with every request. Some providers sit behind Cloudflare, which blocks Python's default
# "Python-urllib" identifier with "error code: 1010" (Groq does).
USER_AGENT = "ApplyStack/1.0 (+https://github.com/naganikhilbijjala/job-tracker)"
# When a provider is overloaded: retry after these waits, but never keep at one provider for longer
# than RETRY_WINDOW, and stop everything (backup included) within TOTAL_BUDGET.
RETRY_WAITS = (3, 8)
RETRY_WINDOW = 45
TOTAL_BUDGET = 240
BACKUP_MIN_TIME = 30  # not worth starting the backup with less time than this left


class AIError(Exception):
    def __init__(self, message, retry=False, final=False):
        super().__init__(message)
        self.retry = retry  # a moment later it may work (the provider said it's overloaded)
        self.final = final  # no point asking another provider (e.g. the model declined the request)


_answered = threading.local()


def answered_by_backup():
    """The backup provider's name if it answered the last complete() on this thread, else ""."""
    return getattr(_answered, "backup", "")


def answered_with():
    """Which provider and model wrote the last complete() reply on this thread, e.g. "Groq · gpt-oss-120b"."""
    return getattr(_answered, "model", "")


def _error_message(raw):
    """Pull a readable message out of an error body. Providers disagree on the shape:
    Anthropic/OpenAI send {"error": {...}}, Gemini sends [{"error": {...}}]."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip()[:400]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or err)[:400]
        return str(err)[:400]
    return str(data)[:400]


def _blocked(code, detail):
    """A firewall in front of the provider refused the request, before the key was even checked."""
    return (code == 403 and "1010" in detail) or "cloudflare" in detail.lower()


BLOCKED = ("The provider's firewall blocked the request ({detail}). This isn't your key; "
           "try again later, and if it keeps happening, please report it on ApplyStack's GitHub.")


def _post(url, headers, body, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"User-Agent": USER_AGENT, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        detail = _error_message(e.read().decode("utf-8", "replace"))
        if _blocked(e.code, detail):
            raise AIError(BLOCKED.format(detail=detail)) from None
        if e.code in (401, 403):
            raise AIError(f"The provider rejected the API key ({e.code}): {detail}") from None
        if e.code == 429:  # quota: retrying straight away won't help, but a backup provider can
            raise AIError(f"Rate limited or out of free quota — wait a minute and try again. ({detail})") from None
        if e.code in (500, 502, 503, 504, 529):
            raise AIError(f"The provider is overloaded right now ({e.code}) — try again shortly. ({detail})",
                          retry=True) from None
        raise AIError(f"Provider error {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        # Not retried: an unreachable or timed-out provider rarely recovers within seconds (the backup may help).
        raise AIError(f"Couldn't reach the provider: {getattr(e, 'reason', e)}") from None


def _anthropic(config, system, messages, max_tokens, timeout=TIMEOUT):
    model = config["model"]
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    headers = {"x-api-key": config["api_key"], "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    if model in CLAUDE_FALLBACK_MODELS:
        # If the model's classifiers decline, Anthropic re-runs the request on its recommended fallback.
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    base = (config.get("base_url") or PROVIDERS["anthropic"]["base_url"]).rstrip("/")
    data = _post(base + "/v1/messages", headers, body, timeout)
    if data.get("stop_reason") == "refusal":
        category = (data.get("stop_details") or {}).get("category")
        raise AIError("Claude declined this request" + (f" ({category})" if category else "") + ". Try rephrasing.",
                      final=True)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if data.get("stop_reason") == "max_tokens":
        text += "\n\n_(The answer was cut off — ask it to continue.)_"
    return text


def _openai_compatible(config, system, messages, max_tokens, timeout=TIMEOUT):
    base = (config.get("base_url") or PROVIDERS.get(config["provider"], {}).get("base_url") or "").rstrip("/")
    if not base:
        raise AIError("Set the provider's base URL on the Profile tab.")
    headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}
    body = {"model": config["model"], "messages": [{"role": "system", "content": system}, *messages]}
    data = _post(base + "/chat/completions", headers, body, timeout)
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError):
        raise AIError(f"Unexpected response from the provider: {str(data)[:300]}") from None
    text = (choice.get("message") or {}).get("content") or ""
    if choice.get("finish_reason") == "length":
        text += "\n\n_(The answer was cut off — ask it to continue.)_"
    return text.strip()


# Models that can't chat (embeddings, speech, images, video…), hidden from the picker.
NOT_CHAT = re.compile(r"embed|tts|audio|speech|transcri|whisper|image|imagen|dall-e|veo|aqa|moderation|rerank", re.I)


def list_models(config):
    """Model IDs the key can use, for the settings dropdown."""
    if not config or not config.get("api_key"):
        raise AIError("Add your API key to load the models.")
    provider = config["provider"]
    if provider == "anthropic":
        base = (config.get("base_url") or PROVIDERS["anthropic"]["base_url"]).rstrip("/")
        url = base + "/v1/models?limit=100"
        headers = {"x-api-key": config["api_key"], "anthropic-version": "2023-06-01"}
    else:
        base = (config.get("base_url") or PROVIDERS.get(provider, {}).get("base_url") or "").rstrip("/")
        if not base:
            raise AIError("Set the provider's base URL first.")
        url = base + "/models"
        headers = {"Authorization": f"Bearer {config['api_key']}"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        detail = _error_message(e.read().decode("utf-8", "replace"))
        if _blocked(e.code, detail):
            raise AIError(BLOCKED.format(detail=detail)) from None
        if e.code in (401, 403):
            label = PROVIDERS.get(provider, {}).get("label", "The provider")
            raise AIError(f"{label} didn't accept this API key. Check it's a key for {label}. ({detail})") from None
        if e.code == 404 and provider == "custom":
            raise AIError("This server doesn't list its models.") from None
        raise AIError(f"Couldn't list models ({e.code}): {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise AIError(f"Couldn't reach the provider: {getattr(e, 'reason', e)}") from None
    models = []
    for m in data.get("data") or data.get("models") or []:
        model_id = (m.get("id") or m.get("name") or "").removeprefix("models/")  # Gemini prefixes IDs
        if model_id and not NOT_CHAT.search(model_id):
            models.append({"id": model_id, "name": m.get("display_name") or m.get("displayName") or ""})
    return sorted(models, key=lambda m: m["id"])


def _label(config):
    return PROVIDERS.get(config.get("provider"), {}).get("label") or config.get("provider") or "The provider"


def _attempts(config, system, messages, max_tokens, deadline, tries):
    """Ask one provider, retrying while it's overloaded, but only within RETRY_WINDOW."""
    started = time.monotonic()
    for attempt in range(tries):
        timeout = max(5, min(TIMEOUT, deadline - time.monotonic()))
        try:
            if config["provider"] == "anthropic":
                return _anthropic(config, system, messages, max_tokens, timeout)
            return _openai_compatible(config, system, messages, max_tokens, timeout)
        except AIError as e:
            wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
            if (not e.retry or attempt == tries - 1 or time.monotonic() + wait - started > RETRY_WINDOW
                    or time.monotonic() + wait > deadline):
                raise
            time.sleep(wait)


def complete(config, system, messages, max_tokens=16000, allow_backup=True, budget=TOTAL_BUDGET):
    """Send a conversation (list of {role, content}) and return the reply text.

    If config has a "fallback" (another provider's settings), allow_backup is on and the chosen
    provider fails, the fallback answers instead; answered_by_backup() then names it.
    answered_with() names the provider and model that answered."""
    _answered.backup = _answered.model = ""
    if not config or not config.get("api_key"):
        raise AIError("Add an AI provider and API key on the Profile tab first.")
    if not config.get("model"):
        raise AIError("Choose a model on the Profile tab first.")
    deadline = time.monotonic() + budget
    try:
        text = _attempts(config, system, messages, max_tokens, deadline, tries=3)
    except AIError as first:
        backup = config.get("fallback") or {}
        if first.final or not allow_backup or not (backup.get("api_key") and backup.get("model")) \
                or deadline - time.monotonic() < BACKUP_MIN_TIME:
            raise
        try:
            text = _attempts(backup, system, messages, max_tokens, deadline, tries=2)
        except AIError as second:
            raise AIError(f"{_label(config)}: {first}\nBackup ({_label(backup)}): {second}") from None
        _answered.backup = _label(backup)
        _answered.model = f"{_label(backup)} · {backup['model']}"
        return text
    _answered.model = f"{_label(config)} · {config['model']}"
    return text


# The layout the tracker's resume page renders (dates right-aligned, bold skill labels…).
# Shared with the MCP tools so resumes from Claude look the same.
RESUME_FORMAT = """Resume format (Markdown, single column, no tables or images, so applicant-tracking systems can read it):

# Full Name
City, ST | phone | email | [LinkedIn](https://full-url) | [GitHub](https://full-url)

## Summary
Two or three sentences about the candidate: what they do, years of experience, the skills from their profile that matter most for this job, and one concrete achievement.

## Skills
- **Category:** skill, skill, skill

## Experience
### Company Name | City, ST
**Job Title** | Mon YYYY – Mon YYYY
- Achievement starting with a strong verb; include the numbers the profile gives

## Education
### School Name | City, ST
**Degree, Major** | Mon YYYY – Mon YYYY

## Projects
### Project Name | Tech, Tech
- What it does and the result

Write like a person, not like marketing copy: plain, specific wording and real numbers. The summary describes the candidate; it never names the company or the job being applied to, and never states a goal ("seeking", "looking to", "eager to contribute to"): recruiters read both as AI-written. Avoid clichés such as "proven/strong track record", "results-driven", "passionate", "leverage", "spearheaded", "synergy", "cutting-edge", "seamless" and "user-focused solutions". Use ordinary hyphens (-), not special dash or hyphen characters, except the en dash in date ranges.

Rules: the resume is sent to employers exactly as written, so it holds only the resume itself: never notes, explanations, or a summary of what you changed or emphasized (put those outside, after it). Use exactly these heading levels and the " | " separators; "Present" for current roles; most recent first; only include contact details and links that appear in the profile (write full URLs); skip sections the profile has nothing for; aim for one page, about 3–5 bullets for recent roles and fewer for older ones. The resume page scales the text (9–11 pt) and spacing to fill exactly one page, so when asked to make it fit, or told it is some number of lines over or under, change about that many lines and no more."""


# What a model sometimes writes into a resume about the resume: "Here's your tailored resume:",
# "The resume emphasizes React… while preserving all original dates", "Note: …".
_COMMENTARY = re.compile(
    r"\b(?:the (?:above |updated |tailored |revised )?(?:resume|cv)|this (?:resume|cv|version|draft)|"
    r"(?:resume|cv) (?:is|has been|was) (?:tailored|updated|revised)|tailored (?:resume|version|to (?:this|the) (?:job|role|position))|"
    r"here(?:'s| is) (?:your|the|a)|i(?:'ve| have)? (?:kept|added|removed|made|focused|updated|adjusted|reordered|"
    r"emphasi[sz]ed|highlighted|tailored|rewrote|reworded|included|preserved)|emphasi[sz](?:es|ed|ing) (?:your|the) |what i changed|"
    r"while preserving|preserv(?:es|ed|ing) (?:all )?(?:the )?original|original (?:dates|titles|resume|wording)|"
    r"changes (?:made|i made)|let me know|feel free|answer was cut off|keywords? from the (?:job|posting|description))\b",
    re.I)
# Lines that are part of the resume's structure: headings, bullets, "Title | Dates" rows.
_STRUCTURE = re.compile(r"^\s*(?:#{1,3}\s|[-*•]\s|\*\*.+\*\*|.+\s\|\s.+)")


# A summary sentence that states a goal is an outdated objective statement, and reads as AI-written.
_OBJECTIVE = re.compile(r"^(?:i am |i'm )?(?:seeking|looking|eager|excited|hoping|aiming|keen|ready|motivated)\b"
                        r"|\b(?:seeking|looking|eager|excited|hoping) to (?:join|contribute|bring|apply|help|leverage)\b", re.I)
_SUMMARY_HEADING = re.compile(r"^##\s+(?:summary|profile|professional summary|overview|about(?: me)?)\s*$", re.I)


def _clean_summary(lines, company):
    """Drop summary sentences that name the target company or state an objective."""
    name = re.sub(r"[,.]?\s+(?:inc|llc|ltd|corp|co|gmbh|plc)\.?$", "", (company or "").strip(), flags=re.I)
    # Case-sensitive, so a company named after a word (Scale, Block, Ramp) doesn't catch "systems that scale".
    mentions = re.compile(rf"\b{re.escape(name)}(?:'s|’s)?\b") if len(name) >= 2 else None
    in_summary = False
    for i, line in enumerate(lines):
        if line.startswith("#"):
            in_summary = bool(_SUMMARY_HEADING.match(line))
            continue
        if not in_summary or not line.strip() or _STRUCTURE.match(line):
            continue
        sentences = re.split(r"(?<=[.!?])\s+", line.strip())
        kept = [x for x in sentences if not _OBJECTIVE.search(x) and not (mentions and mentions.search(x))]
        if kept and len(kept) < len(sentences):  # never empty the summary entirely
            lines[i] = " ".join(kept)
    return lines


def _plain(text):
    """Ordinary hyphens and spaces: some models write non-breaking hyphens, narrow no-break spaces and "25 %"."""
    text = re.sub("[\u2010\u2011\u2012]", "-", str(text or ""))
    text = re.sub("[\u00a0\u2009\u202f]", " ", text)
    return re.sub(r"(?<=\d) +%", "%", text)


def clean_resume(text, company=""):
    """The resume alone: drops a model's commentary before the name heading or after the last section,
    stray code fences and note/quote lines, summary sentences naming the target company or stating an
    objective, and unusual hyphen characters. Headings, bullets and "Title | Dates" rows are never removed."""
    text = _plain(text)
    lines = [l for l in text.replace("\r\n", "\n").split("\n")
             if not re.match(r"^\s*```", l)                              # stray fences
             and not re.match(r"^\s*>", l)                               # blockquotes (never in a resume)
             and not re.match(r"^\s*[*_(]*\s*notes?\s*[:*]", l, re.I)]  # "Note: …", "**Note:** …"
    first = next((i for i, l in enumerate(lines) if re.match(r"^#\s", l)), None)
    if first:  # "Here's your tailored resume:" before the name
        lines = lines[first:]
    # A "## Notes" / "## Changes made" section at the end isn't part of the resume.
    for i, l in enumerate(lines):
        if re.match(r"^#{2,3}\s+(?:notes?|changes|what i changed|tailoring notes|summary of changes)\b", l, re.I):
            lines = lines[:i]
            break
    # In the last section, a plain (non-bullet, non-heading) line that talks about the resume starts the
    # commentary; it and everything after it go ("The resume emphasizes…", "Changes made:" + a list).
    heading = max((i for i, l in enumerate(lines) if re.match(r"^#{1,3}\s", l)), default=0)
    for i in range(heading + 1, len(lines)):
        if lines[i].strip() and not _STRUCTURE.match(lines[i]) and _COMMENTARY.search(lines[i]):
            lines = lines[:i]
            break
    while lines and re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})?\s*", lines[-1]):  # trailing rules and blanks
        lines.pop()
    lines = _clean_summary(lines, company)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


COVER_FORMAT = """Cover letter format (it becomes a PDF exactly as written):
```cover
# Full Name
City, ST | phone | email | linkedin.com/in/handle

Dear Hiring Manager,

Two to four short paragraphs.

Sincerely,
Full Name
```
- Name and contact details come from the profile; leave out any the profile doesn't have. The app adds \
the date, so don't write one.
- No placeholders like [Company Address] or [Your Name], no company address block, no subject line.
- Plain, specific writing grounded in the profile. No clichés ("I am excited to apply", "passionate", \
"proven track record", "I believe I would be a great fit")."""

_COVER_BLOCK = re.compile(r"```cover[^\n]*\n([\s\S]*?)(?:```|$)")
_PLACEHOLDER = re.compile(r"^\s*\[[^\]]*\]\s*$|^\s*(?:date|\[date\])\s*:?\s*$", re.I)
_LETTER_NOTE = re.compile(r"^\s*(?:>\s*)?(?:\*\*|_)?(?:note|notes|tip)s?\b\s*:", re.I)


def cover_letter(reply):
    """The letter inside a reply's ```cover block, without placeholder or note lines ("" when there's none)."""
    m = _COVER_BLOCK.search(str(reply or ""))
    if not m:
        return ""
    lines = [line for line in _plain(m.group(1)).strip().split("\n")
             if not _PLACEHOLDER.match(line) and not _LETTER_NOTE.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_RESUME_BLOCK = re.compile(r"```resume[^\n]*\n([\s\S]*?)(```|$)")


def resume_problems(reply, company=""):
    """What's wrong with the resume in an assistant reply, as instructions a model can act on.
    Empty if the reply has no ```resume block, or the resume is fine. Same checks for every model."""
    found = _RESUME_BLOCK.search(reply or "")
    if not found:
        return []
    body, closed = found.group(1), bool(found.group(2))
    problems = [] if closed else ["The ```resume block isn't closed. End it with ``` on its own line."]
    cleaned = clean_resume(body, company)
    norm = lambda l: _plain(l).strip()
    kept = {norm(l) for l in cleaned.split("\n")}
    for line in body.split("\n"):
        if norm(line) and norm(line) not in kept and not line.lstrip().startswith("```"):
            problems.append(f'Not resume content, so it must not be inside the block (a note about the resume, a goal '
                            f'statement, or the target company named in the summary): "{norm(line)[:160]}"')
    for check in scoring.format_report(cleaned)["checks"]:
        if check["status"] == "fail":
            problems.append(f"{check['label']}: {check['detail']}")
    return problems


SYSTEM_PROMPT = """You are a job-application assistant working for one candidate. You help them \
tailor their resume, answer questions on application forms, and prepare for this specific job.

How to work:
- Base everything on the candidate profile below. Never invent employers, job titles, dates, \
degrees, certifications, metrics, or skills the profile doesn't support. If the job wants \
something the profile doesn't show, say so plainly and suggest an honest way to address it.
- You may reword, reorder, and emphasize real experience to fit the job, and use the job \
description's terminology where it truthfully applies.
- Answers to application-form questions should be ready to paste: no preamble, no "Here is…".
- Follow the candidate's own instructions below unless they conflict with honesty.
- When asked for a full resume, put the complete resume inside one fenced block that opens with ```resume and closes with ```, in the resume format below. Always close the block. Put any notes for the candidate (what you changed, what you emphasized, gaps) after the closing ```, never inside the block: everything inside goes straight onto the PDF that employers see.
- When asked for a cover letter, put the complete letter inside one fenced block that opens with ```cover and closes with ```, in the cover letter format below, with any notes after the closing ```.

{resume_format}

{cover_format}

<candidate_instructions>
{instructions}
</candidate_instructions>

<candidate_profile>
{profile}
</candidate_profile>

<job>
Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Link: {url}
Resume they sent (if any): {resume}

{description}
</job>"""


def system_prompt(profile, instructions, job, resume_label=""):
    return SYSTEM_PROMPT.format(
        resume_format=RESUME_FORMAT, cover_format=COVER_FORMAT,
        instructions=instructions.strip() or "(none)",
        profile=profile.strip() or "(The candidate hasn't filled in a profile yet. Tell them to add "
                                   "one on the Profile tab before you write a resume, and don't make one up.)",
        title=job.get("title") or "", company=job.get("company") or "",
        location=job.get("location") or "", salary=job.get("salary") or "",
        url=job.get("url") or "", resume=resume_label or "none recorded",
        description=job.get("description") or "(No job description saved for this job.)",
    )


REVIEW_PROMPT = """You review a tailored resume before a candidate sends it. Do two jobs and reply with \
JSON only (no Markdown fence, no other text).

1. Fact check. Compare every claim in the resume (employers, titles, dates, degrees, \
certifications, skills, tools, numbers, scope such as "led a team of 8") with the candidate \
profile. List each claim the profile does not support, or that overstates what it says. \
Rewording and emphasis are fine; new facts, inflated numbers, and skills the profile never \
mentions are not. If everything is supported, return an empty list.

2. Recruiter's view. Read the resume as the recruiter for this job would on a first, quick pass.

JSON shape:
{{
  "unsupported": [{{"claim": "the resume's words, quoted", "problem": "what the profile actually says, or that it says nothing", "fix": "an honest rewrite, or 'remove'"}}],
  "fit": 1-10 (how strong a match the resume presents for this job),
  "verdict": "one sentence a recruiter would think after a 10-second skim",
  "strengths": ["up to 3 short points"],
  "weaknesses": [{{"issue": "up to 3 biggest problems for this job", "fix": "a concrete change that stays true to the profile"}}]
}}

<candidate_profile>
{profile}
</candidate_profile>

<job>
Title: {title}
Company: {company}

{description}
</job>"""


def _json_object(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AIError("The AI's reply wasn't in the expected format. Try again.")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        raise AIError("The AI's reply wasn't in the expected format. Try again.") from None


def review_resume(config, profile, job, resume):
    """Fact-check a resume against the profile and give a recruiter's first impression."""
    if not profile.strip():
        raise AIError("Fill in your Profile tab first. The fact check compares the resume against it.")
    system = REVIEW_PROMPT.format(profile=profile.strip(), title=job.get("title") or "",
                                  company=job.get("company") or "",
                                  description=job.get("description") or "(No job description saved.)")
    data = _json_object(complete(config, system, [{"role": "user", "content": f"<resume>\n{resume}\n</resume>"}],
                                 max_tokens=8000))
    as_list = lambda v: v if isinstance(v, list) else []
    try:
        fit = max(1, min(10, int(data.get("fit"))))
    except (TypeError, ValueError):
        fit = None
    return {
        "unsupported": [{k: str(c.get(k, "")) for k in ("claim", "problem", "fix")}
                        for c in as_list(data.get("unsupported")) if isinstance(c, dict) and c.get("claim")],
        "fit": fit,
        "verdict": str(data.get("verdict") or ""),
        "strengths": [str(s) for s in as_list(data.get("strengths"))][:3],
        "weaknesses": [{k: str(w.get(k, "")) for k in ("issue", "fix")}
                       for w in as_list(data.get("weaknesses")) if isinstance(w, dict) and w.get("issue")][:3],
    }


FORM_PROMPT = """You fill in a job application form for the candidate, using only facts from their profile.

Rules:
- Never invent experience, skills, numbers, employers, dates or credentials. If the profile doesn't support an answer, use null.
- Yes/no questions about experience or skills: "Yes" only when the profile clearly shows it, otherwise null so the candidate decides. Never answer "No" on their behalf.
- When a question lists options, answer with one option exactly as written, or null.
- "How many years" questions: count from the dates in the profile, round down, and answer with just the number. null if the profile has no dates for it.
- Open questions (why this company, what excites you, tell us about…): 2–4 sentences in the first person, specific to this job and grounded in the profile. Ready to paste: no greeting, sign-off or placeholders.
- Short text fields: a short answer, not a sentence, unless the question asks for one.
- Stay consistent with the candidate's earlier answers, and follow their instructions where they apply.

Reply with only a JSON object mapping every question id to its answer or null, like {{"q1": "Yes", "q2": null}}.

<candidate_profile>
{profile}
</candidate_profile>

<candidate_instructions>
{instructions}
</candidate_instructions>

<earlier_answers>
{saved}
</earlier_answers>

<job>
Title: {title}
Company: {company}

{description}
</job>"""


def answer_form(config, profile, instructions, job, questions, saved=()):
    """Answer application questions [{id, label, kind, options}] in one request. Returns {id: answer}."""
    if not profile.strip():
        raise AIError("Fill in your Profile tab first. Answers come only from it.")
    system = FORM_PROMPT.format(
        profile=profile.strip(), instructions=instructions.strip() or "(none)",
        saved="\n".join(f"Q: {s['question']}\nA: {s['answer']}" for s in saved[:60]) or "(none)",
        title=job.get("title") or "", company=job.get("company") or "",
        description=(job.get("description") or "(No job description available.)")[:15000])
    listed = [{"id": q["id"], "question": q["label"], "type": q["kind"], **({"options": q["options"]} if q["options"] else {})}
              for q in questions]
    data = _json_object(complete(config, system, [{"role": "user", "content": json.dumps(listed, ensure_ascii=False)}],
                                 max_tokens=6000))
    return {str(k): str(v).strip() for k, v in data.items() if v not in (None, "") and str(v).strip().lower() != "null"}
