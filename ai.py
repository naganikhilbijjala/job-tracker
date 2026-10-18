"""AI providers for the job assistant (stdlib HTTP, so the app stays install-free).

Claude goes through the Anthropic Messages API. Everything else goes through the
OpenAI-style chat completions API, which OpenAI, Gemini, OpenRouter, Groq and
local servers such as Ollama all accept.
"""
import json
import urllib.error
import urllib.request

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com", "model": "claude-opus-5-5"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": ""},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": ""},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": ""},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": ""},
    "custom": {"label": "Other (OpenAI-compatible)", "base_url": "", "model": ""},
}
# Claude models that accept server-side fallback when their safety classifiers decline.
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
TIMEOUT = 280  # Vercel stops a function at 300 s


class AIError(Exception):
    pass


def _post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
            detail = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) \
                else detail.get("error") or detail.get("message") or raw
        except ValueError:
            detail = raw
        if e.code in (401, 403):
            raise AIError(f"The provider rejected the API key ({e.code}): {detail}") from None
        if e.code == 429:
            raise AIError(f"Rate limited or out of credits: {detail}") from None
        raise AIError(f"Provider error {e.code}: {str(detail)[:400]}") from None
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


def complete(config, system, messages, max_tokens=16000):
    """Send a conversation (list of {role, content}) and return the reply text."""
    if not config or not config.get("api_key"):
        raise AIError("Add an AI provider and API key on the Profile tab first.")
    if not config.get("model"):
        raise AIError("Choose a model on the Profile tab first.")
    if config["provider"] == "anthropic":
        return _anthropic(config, system, messages, max_tokens)
    return _openai_compatible(config, system, messages, max_tokens)


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
- When asked for a full resume, put the complete resume in Markdown inside one fenced block \
that opens with ```resume and closes with ```. Start with "# Full Name", then a contact line, \
then sections such as "## Summary", "## Experience", "## Skills", "## Education", with "- " \
bullets. Keep it single-column with no tables or images so applicant-tracking systems can \
read it. Put any notes for the candidate after the block, not inside it.

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
        instructions=instructions.strip() or "(none)",
        profile=profile.strip() or "(The candidate hasn't filled in a profile yet. Tell them to add "
                                   "one on the Profile tab before you write a resume, and don't make one up.)",
        title=job.get("title") or "", company=job.get("company") or "",
        location=job.get("location") or "", salary=job.get("salary") or "",
        url=job.get("url") or "", resume=resume_label or "none recorded",
        description=job.get("description") or "(No job description saved for this job.)",
    )
