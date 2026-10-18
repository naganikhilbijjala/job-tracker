"""AI providers for the job assistant (stdlib HTTP, so the app stays install-free).

Claude goes through the Anthropic Messages API. Everything else goes through the
OpenAI-style chat completions API, which OpenAI, Gemini, OpenRouter, Groq and
local servers such as Ollama all accept.
"""
import json
import re
import time
import urllib.error
import urllib.request

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


class AIError(Exception):
    pass


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


def _post(url, headers, body):
    req_body = json.dumps(body).encode()
    for attempt in range(2):
        req = urllib.request.Request(url, data=req_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            detail = _error_message(e.read().decode("utf-8", "replace"))
            if e.code in (500, 502, 503, 504, 529) and attempt == 0:
                time.sleep(2)  # providers are sometimes briefly overloaded; one retry usually works
                continue
            if e.code in (401, 403):
                raise AIError(f"The provider rejected the API key ({e.code}): {detail}") from None
            if e.code == 429:
                raise AIError(f"Rate limited or out of free quota — wait a minute and try again. ({detail})") from None
            if e.code in (500, 502, 503, 504, 529):
                raise AIError(f"The provider is overloaded right now ({e.code}) — try again shortly. ({detail})") from None
            raise AIError(f"Provider error {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise AIError(f"Couldn't reach the provider: {getattr(e, 'reason', e)}") from None


def _anthropic(config, system, messages, max_tokens):
    model = config["model"]
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    headers = {"x-api-key": config["api_key"], "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    if model in CLAUDE_FALLBACK_MODELS:
        # If the model's classifiers decline, Anthropic re-runs the request on its recommended fallback.
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    base = (config.get("base_url") or PROVIDERS["anthropic"]["base_url"]).rstrip("/")
    data = _post(base + "/v1/messages", headers, body)
    if data.get("stop_reason") == "refusal":
        category = (data.get("stop_details") or {}).get("category")
        raise AIError("Claude declined this request" + (f" ({category})" if category else "") + ". Try rephrasing.")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if data.get("stop_reason") == "max_tokens":
        text += "\n\n_(The answer was cut off — ask it to continue.)_"
    return text


def _openai_compatible(config, system, messages, max_tokens):
    base = (config.get("base_url") or PROVIDERS.get(config["provider"], {}).get("base_url") or "").rstrip("/")
    if not base:
        raise AIError("Set the provider's base URL on the Profile tab.")
    headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}
    body = {"model": config["model"], "messages": [{"role": "system", "content": system}, *messages]}
    data = _post(base + "/chat/completions", headers, body)
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
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        detail = _error_message(e.read().decode("utf-8", "replace"))
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


def complete(config, system, messages, max_tokens=16000):
    """Send a conversation (list of {role, content}) and return the reply text."""
    if not config or not config.get("api_key"):
        raise AIError("Add an AI provider and API key on the Profile tab first.")
    if not config.get("model"):
        raise AIError("Choose a model on the Profile tab first.")
    if config["provider"] == "anthropic":
        return _anthropic(config, system, messages, max_tokens)
    return _openai_compatible(config, system, messages, max_tokens)


# The layout the tracker's resume page renders (dates right-aligned, bold skill labels…).
# Shared with the MCP tools so resumes from Claude look the same.
RESUME_FORMAT = """Resume format (Markdown, single column, no tables or images, so applicant-tracking systems can read it):

# Full Name
City, ST | phone | email | [LinkedIn](https://full-url) | [GitHub](https://full-url)

## Summary
Two or three sentences aimed at this job.

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

Rules: use exactly these heading levels and the " | " separators; "Present" for current roles; most recent first; only include contact details and links that appear in the profile (write full URLs); skip sections the profile has nothing for; aim for one page, about 3–5 bullets for recent roles and fewer for older ones."""


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
- When asked for a full resume, put the complete resume inside one fenced block that opens with ```resume and closes with ```, in the resume format below. Put any notes for the candidate after the block, not inside it.

{resume_format}

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
        resume_format=RESUME_FORMAT,
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
        raise AIError("The AI's review wasn't in the expected format. Try again.")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        raise AIError("The AI's review wasn't in the expected format. Try again.") from None


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
