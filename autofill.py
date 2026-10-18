"""Filling job application forms from the browser extension.

The extension sends the form's questions as {id, label, kind, options}. kind is one of
text, textarea, email, tel, url, number, date, select, radio, checkbox. Each question is
answered, in order, from:

1. a fact in the profile or account (LinkedIn, GitHub, website, email, phone, name),
2. an answer the person saved earlier (same question, or the same kind of question),
3. the AI, in one request for everything left, except questions only the person should
   answer (salary, sponsorship, demographics…), which are never guessed.
"""
import re

TEXT_KINDS = {"text", "email", "tel", "url", "number", "date"}
CHOICE_KINDS = {"select", "radio", "checkbox"}
KINDS = TEXT_KINDS | CHOICE_KINDS | {"textarea"}

# Short text fields that the profile or account answers. Checked in order.
FACTS = [
    ("linkedin", re.compile(r"linked\s*in", re.I)),
    ("github", re.compile(r"git\s*hub", re.I)),
    ("website", re.compile(r"website|portfolio|personal (?:site|page|url)|\bblog\b", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|\bcell\b", re.I)),
    ("first_name", re.compile(r"first name|given name|forename|preferred name", re.I)),
    ("last_name", re.compile(r"last name|surname|family name", re.I)),
    ("full_name", re.compile(r"^(?:your |full |legal )*name$", re.I)),
]

# Questions worded many ways that mean the same thing, so one saved answer covers them all.
CONCEPTS = [
    ("sponsorship", re.compile(r"sponsor", re.I)),
    ("work_authorization", re.compile(r"authori[sz]ed to work|work authori[sz]ation|legally (?:able|eligible|authori[sz]ed)|"
                                      r"right to work|eligible to work", re.I)),
    ("salary", re.compile(r"salary|compensation|pay expectation|desired pay|expected pay|pay range", re.I)),
    ("notice_period", re.compile(r"notice period", re.I)),
    ("start_date", re.compile(r"start date|when can you start|available to start|earliest (?:date|start)", re.I)),
    ("relocation", re.compile(r"relocat", re.I)),
    ("hear_about", re.compile(r"how did you (?:hear|find|learn)|where did you (?:hear|find)", re.I)),
    ("gender", re.compile(r"\bgender\b", re.I)),
    ("race", re.compile(r"\brace\b|ethnic", re.I)),
    ("hispanic", re.compile(r"hispanic|latin[oax]", re.I)),
    ("veteran", re.compile(r"veteran", re.I)),
    ("disability", re.compile(r"disab", re.I)),
    ("city", re.compile(r"^(?:current )?(?:city|location)\b|where are you (?:located|based)|current location", re.I)),
]

# Only the person can answer these. They're filled from saved answers, never by the AI.
PERSONAL = re.compile(
    r"sponsor|visa|authori[sz]ed to work|work authori[sz]ation|legally (?:able|eligible|authori)|right to work|"
    r"eligible to work|citizen|salary|compensation|pay (?:expectation|range|requirement)|desired pay|expected pay|"
    r"notice period|start date|when can you start|available to start|earliest (?:date|start)|relocat|clearance|"
    r"how did you (?:hear|find|learn)|referr|\bgender\b|\bsex\b|\brace\b|ethnic|hispanic|latin[oax]|veteran|disab|"
    r"pronoun|sexual orientation|transgender|birth|\bage\b|religio|social security|\bssn\b|criminal|convict|"
    r"background check|drug (?:test|screen)|signature|consent|agree|acknowledge|certify", re.I)

MAX_FIELDS = 150
MAX_SAVED = 500


def clean_label(label):
    label = re.sub(r"\s+", " ", str(label or "")).strip()
    return re.sub(r"\s*(?:\*|\(required\)|\(optional\))\s*$", "", label, flags=re.I).strip()[:500]


def question_key(label):
    """The question in a canonical form, so the same question from two forms matches."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", clean_label(label).lower())).strip()


def concept(label):
    return next((name for name, pattern in CONCEPTS if pattern.search(label)), None)


def fact(label, kind):
    if kind not in TEXT_KINDS or len(label) > 80:
        return None
    return next((name for name, pattern in FACTS if pattern.search(label)), None)


def is_personal(label):
    return bool(PERSONAL.search(label))


def company_specific(text, company):
    """Answers that mention this company (why do you want to work at X?) don't carry over to the next one."""
    company = (company or "").strip().lower()
    return len(company) >= 2 and company in str(text or "").lower()


def clean_fields(raw):
    fields = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or f.get("kind") not in KINDS:
            continue
        label = clean_label(f.get("label"))
        if not label:
            continue
        options = [str(o).strip()[:200] for o in f.get("options") or [] if str(o).strip()][:50]
        fields.append({"id": str(f.get("id", ""))[:40], "label": label, "kind": f["kind"], "options": options,
                       "value": str(f.get("value") or "")[:5000]})
    return fields[:MAX_FIELDS]


# ---------------------------------------------------------------- facts from the profile

def _url(found):
    found = found.rstrip("/.,;)")
    return found if found.startswith("http") else "https://" + found


def profile_facts(profile, email="", name=""):
    """Contact details from the profile text (and the signed-in account as a fallback)."""
    profile = profile or ""
    head = "\n".join(profile.strip().splitlines()[:12])  # contact details sit at the top of a resume
    no_emails = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", head)
    facts = {}
    if m := re.search(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w\-%.]+/?", profile, re.I):
        facts["linkedin"] = _url(m.group(0))
    if m := re.search(r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+/?(?![\w/])", profile, re.I):
        facts["github"] = _url(m.group(0))
    for m in re.finditer(r"https?://[^\s<>()\[\]|,]+|\b[\w-]+\.(?:dev|io|me|com|net|org|site|page)(?:/[^\s<>()|,]*)?",
                         no_emails, re.I):
        u = m.group(0)
        if not re.search(r"linkedin\.|github\.|@|gmail\.|outlook\.|yahoo\.|hotmail\.", u, re.I):
            facts["website"] = _url(u)
            break
    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", profile):
        facts["email"] = m.group(0).rstrip(".")
    elif email:
        facts["email"] = email
    if m := re.search(r"(?<![\w-])(\+?\(?\d[\d\s().-]{8,}\d)(?![\w-])", no_emails):
        facts["phone"] = re.sub(r"\s+", " ", m.group(1)).strip()
    full = name.strip()
    if not full:
        first = next((l for l in profile.splitlines() if l.strip()), "")
        first = re.sub(r"^#+\s*", "", first).strip()
        if re.fullmatch(r"[^\W\d_]+(?:[ '.-][^\W\d_]+){1,3}", first):
            full = first
    if full:
        parts = full.split()
        facts["full_name"] = full
        facts["first_name"] = parts[0]
        if len(parts) > 1:
            facts["last_name"] = parts[-1]
    return facts


# ---------------------------------------------------------------- matching answers to questions

YES = re.compile(r"^\s*(yes|y|true|i do|i am|i have|i will)\b", re.I)
NO = re.compile(r"^\s*(no|n|false|i do not|i don't|i am not|i have not|i will not)\b", re.I)


def pick_option(answer, options):
    """The option that answer means, or None. Choice fields only accept one of their options."""
    answer = str(answer or "").strip()
    if not answer:
        return None
    low = answer.lower()
    exact = [o for o in options if o.lower() == low]
    if exact:
        return exact[0]
    for pattern in (YES, NO):
        if pattern.match(answer):
            hits = [o for o in options if pattern.match(o)]
            if len(hits) == 1:
                return hits[0]
    contained = [o for o in options if o.lower() in low or low in o.lower()]
    return contained[0] if len(contained) == 1 else None


def fit_answer(answer, field):
    """Shape a stored answer for this field, or None if it doesn't fit."""
    answer = str(answer or "").strip()
    if not answer:
        return None
    if field["kind"] in ("select", "radio"):
        return pick_option(answer, field["options"])
    if field["kind"] == "checkbox":
        picked = [pick_option(part, field["options"]) for part in re.split(r"\s*;\s*", answer)]
        return "; ".join(p for p in picked if p) or None
    if field["kind"] == "number":
        m = re.search(r"\d+(?:\.\d+)?", answer.replace(",", ""))
        return m.group(0) if m else None
    return answer


def match(fields, saved, facts):
    """Answer what we can without the AI.

    saved: [{question, answer}] newest first. Returns (answers, rest): answers are
    {id, value, source}, rest are the fields still unanswered."""
    by_key = {}
    by_concept = {}
    for s in saved:
        by_key.setdefault(question_key(s["question"]), s["answer"])
        if c := concept(s["question"]):
            by_concept.setdefault(c, s["answer"])
    answers, rest = [], []
    for f in fields:
        value, source = None, None
        if (name := fact(f["label"], f["kind"])) and facts.get(name):
            value, source = facts[name], "profile"
        if value is None and (s := by_key.get(question_key(f["label"]))) is not None:
            value, source = fit_answer(s, f), "saved"
        if value is None and (c := concept(f["label"])) and c in by_concept:
            value, source = fit_answer(by_concept[c], f), "saved"
        if value is None:
            rest.append(f)
        else:
            answers.append({"id": f["id"], "value": value, "source": source})
    return answers, rest


def worth_saving(fields, saved, facts, company):
    """Answers the person filled in that are worth remembering for the next form."""
    known = {question_key(s["question"]): s["answer"] for s in saved}
    by_concept = {}
    for s in saved:
        if c := concept(s["question"]):
            by_concept.setdefault(c, s["answer"])
    out, seen = [], set()
    for f in fields:
        value = f["value"].strip()
        key = question_key(f["label"])
        if not value or key in seen or (fact(f["label"], f["kind"]) and value in facts.values()):
            continue
        if company_specific(f["label"], company) or (f["kind"] == "textarea" and company_specific(value, company)):
            continue
        seen.add(key)
        c = concept(f["label"])
        if known.get(key) == value or (key not in known and c and fit_answer(by_concept.get(c), f) == value):
            continue  # already covered, maybe by the same question worded differently
        out.append({"question": f["label"], "answer": value, "kind": f["kind"],
                    "status": "changed" if key in known else "new"})
    return out
