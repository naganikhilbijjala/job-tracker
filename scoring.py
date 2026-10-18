"""Resume checks that need no AI: keyword match against the job description, and format.

Real applicant-tracking systems don't score resumes; they parse them and recruiters search
the text by keyword. So the score here approximates two things: would the parse go cleanly,
and would the resume turn up when a recruiter searches for this job's skills?
"""
import re
from collections import Counter

STOPWORDS = set("""
a about above across after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each either etc every few for from
further had has have having he her here hers him his how i if in into is it its itself just least
less like ll may me might more most much must my need needs no nor not now of off on once one only or
other our ours out over own per plus re same she should so some such than that the their them then
there these they this those through to too under until up upon us very via was we well were what when
where whether which while who whom why will with within without would yet you your yours
ability able across work working works worked job role roles team teams company candidate candidates
including include includes strong excellent good great new years year experience experienced
knowledge understanding skills skill required requirements preferred qualifications responsibilities
looking help join opportunity position environment etc using use used based related relevant equivalent
across best world day days time full part make makes making ensure ensures within highly demonstrated
proven related degree minimum least someone people across through closely across way ways across
""".split())

# Words that are capitalized in postings but aren't skills.
NOT_SKILLS = set("""
we you our your us the a an this that it they he she in on at for of to with and or if as is are be
about benefits salary pay equal opportunity employer eeo inc llc ltd corp team company remote hybrid
onsite monday tuesday wednesday thursday friday saturday sunday january february march april may june
july august september october november december usa united states north america responsibilities
requirements qualifications preferred required who what why how where when apply bonus perks
""".split())

# Common skills that are often written in lowercase, so capitalization alone won't catch them.
KNOWN_TERMS = [
    # software
    "python", "java", "javascript", "typescript", "golang", "go", "rust", "ruby", "php", "scala", "kotlin",
    "swift", "c++", "c#", "c", "r", "sql", "nosql", "html", "css", "bash", "shell scripting",
    "react", "angular", "vue", "node.js", "next.js", "django", "flask", "fastapi", "spring", "spring boot",
    ".net", "rails", "graphql", "rest", "rest apis", "grpc", "microservices", "distributed systems",
    "system design", "data structures", "algorithms", "object-oriented", "unit testing", "test automation",
    "ci/cd", "devops", "git", "linux", "docker", "kubernetes", "terraform", "ansible", "aws", "azure", "gcp",
    "google cloud", "serverless", "lambda", "postgresql", "mysql", "mongodb", "redis", "kafka", "spark",
    "hadoop", "airflow", "snowflake", "databricks", "bigquery", "dbt", "etl", "data pipelines",
    "data modeling", "data warehouse", "data analysis", "data visualization", "tableau", "power bi",
    "looker", "excel", "statistics", "machine learning", "deep learning", "nlp", "computer vision",
    "pytorch", "tensorflow", "scikit-learn", "pandas", "numpy", "llm", "llms", "generative ai",
    "a/b testing", "observability", "monitoring", "security", "networking", "mobile", "ios", "android",
    "frontend", "front-end", "backend", "back-end", "full stack", "full-stack", "api", "apis", "saas",
    "agile", "scrum", "kanban", "jira", "figma", "accessibility", "performance optimization",
    # business and general
    "project management", "product management", "program management", "stakeholder management",
    "roadmap", "roadmaps", "budgeting", "forecasting", "financial modeling", "salesforce", "crm",
    "seo", "sem", "content strategy", "copywriting", "customer success", "account management",
    "business development", "negotiation", "recruiting", "onboarding", "training", "mentoring",
    "leadership", "people management", "cross-functional", "communication", "presentation",
    "problem solving", "analytical", "strategic planning", "operations", "supply chain", "logistics",
    "procurement", "compliance", "risk management", "auditing", "accounting", "gaap", "quickbooks",
    "sap", "erp", "autocad", "solidworks", "revit", "matlab", "lean", "six sigma", "pmp",
]

# Different spellings of the same skill, normalized to one form.
ALIASES = {
    "js": "javascript", "ts": "typescript", "golang": "go", "k8s": "kubernetes", "postgres": "postgresql",
    "node": "node.js", "nodejs": "node.js", "react.js": "react", "reactjs": "react", "vue.js": "vue",
    "nextjs": "next.js", "amazon web services": "aws", "google cloud platform": "gcp", "google cloud": "gcp",
    "microsoft azure": "azure", "ml": "machine learning", "ai/ml": "machine learning",
    "natural language processing": "nlp", "large language models": "llm", "llms": "llm",
    "front-end": "frontend", "front end": "frontend", "back-end": "backend", "back end": "backend",
    "full-stack": "full stack", "fullstack": "full stack", "rest apis": "rest", "restful": "rest",
    "apis": "api", "ci / cd": "ci/cd", "continuous integration": "ci/cd", "sklearn": "scikit-learn",
    "power-bi": "power bi", "powerbi": "power bi", "roadmaps": "roadmap", "object oriented": "object-oriented",
    "oop": "object-oriented", "cross functional": "cross-functional", "a/b tests": "a/b testing",
}

# Where in a posting a line sits: requirements count more than nice-to-haves.
REQUIRED_HEAD = re.compile(r"requir|qualif|must|what you(?:'ll)? (?:need|bring)|you have|you bring|about you|skills", re.I)
PREFERRED_HEAD = re.compile(r"prefer|nice|bonus|plus|desired|ideal", re.I)
OTHER_HEAD = re.compile(r"benefit|perks|about us|who we are|compensation|salary|equal opportunity|eeo", re.I)

TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*[a-z0-9+#]|[a-z0-9]", re.I)


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # markdown links → their text
    text = re.sub(r"[*_`#>|]", " ", text)
    tokens = []
    for tok in TOKEN.findall(text):
        tokens.append(tok)
        if "-" in tok:  # "kafka-based" should also match "kafka"
            tokens.extend(["|", *tok.split("-"), "|"])
    return " " + " ".join(tokens) + " "


def canonical(term):
    term = term.lower().strip()
    return ALIASES.get(term, term)


def _variants(term):
    out = {term}
    out.update(k for k, v in ALIASES.items() if v == term)
    for t in list(out):
        if re.fullmatch(r"[a-z ]+", t) and len(t) > 3:
            out.update({t + "s", t + "es"} if not t.endswith("s") else {t[:-1]})
    return out


# Skills that are also ordinary words or letters only count when written like a skill: "C", "R", "Go".
SHORT_TERMS = {"c": r"C", "r": r"R", "go": r"Go|Golang|golang"}


def _short_term(raw, term):
    return re.search(rf"(?<![\w+#.&/-])(?:{SHORT_TERMS[term]})(?![\w+#&/-])", raw or "")


def contains(raw, norm_text, term):
    term = canonical(term)
    if term in SHORT_TERMS:
        return bool(_short_term(raw, term))
    return any(f" {v} " in norm_text for v in _variants(term))


def _sections(description):
    """Yield (weight, line) for each line of a posting, by the heading it falls under."""
    weight = 1.0
    for line in (description or "").splitlines():
        stripped = line.strip().strip("*#:_ ").strip()
        if not stripped:
            continue
        is_heading = len(stripped) < 60 and (line.strip().startswith("#") or line.strip().endswith(":")
                                              or (line.strip().startswith("**") and line.strip().endswith("**"))
                                              or not re.search(r"[.,;]", stripped) and len(stripped.split()) <= 6)
        if is_heading:
            if OTHER_HEAD.search(stripped):
                weight = 0.0
            elif PREFERRED_HEAD.search(stripped):
                weight = 0.6
            elif REQUIRED_HEAD.search(stripped):
                weight = 1.5
        yield weight, line


def extract_keywords(description, company="", limit=30):
    """The skills and phrases a recruiter would search for, most important first."""
    company_words = set(normalize(company).split())
    scores, required, counts, labels = Counter(), {}, Counter(), {}

    def add(raw, weight):
        term = canonical(raw)
        if term in seen:  # once per line, so "REST APIs" doesn't count three times
            return
        seen.add(term)
        if term not in labels:  # show it the way the posting writes it: "REST", "Node.js"
            m = re.search(rf"(?<!\w){re.escape(raw)}(?!\w)", line, re.I)
            labels[term] = m.group(0) if m and m.group(0) != m.group(0).lower() else term
        if not term or term in STOPWORDS or term in NOT_SKILLS or term in company_words or len(term) < 2 and term not in ("c", "r"):
            return
        counts[term] += 1
        scores[term] += weight
        required[term] = max(required.get(term, 0), weight)

    for weight, line in _sections(description):
        if not weight:
            continue
        seen = set()
        norm = normalize(line)
        for term in KNOWN_TERMS:
            if term in SHORT_TERMS:
                if _short_term(line, term):
                    add(term, weight)
            elif f" {term} " in norm:
                add(term, weight)
        for alias in ALIASES:
            if alias != "golang" and f" {alias} " in norm:
                add(alias, weight)
        # Technical-looking tokens: AWS, CI/CD, C#, Node.js, S3, OAuth...
        for tok in re.findall(r"(?<![\w])[A-Za-z][A-Za-z0-9]*(?:[+#./-][A-Za-z0-9+#]*)*", line):
            core = tok.rstrip(".")
            if (re.fullmatch(r"[A-Z]{2,6}s?", core) or re.search(r"[a-z][A-Z]", core)
                    or re.search(r"[A-Za-z][0-9]|[+#]|\.[a-z]", core)) and core.lower() not in NOT_SKILLS:
                add(core, weight)
        # Capitalized product/tool names in the middle of a sentence: Salesforce, Kubernetes, Figma.
        for m in re.finditer(r"(?<=[a-z,;(] )([A-Z][a-z]+(?: [A-Z][a-z]+)?)(?!\w|[+#.-]\w)", line):
            add(m.group(1), weight * 0.5)

    # Repeated two-word phrases ("data pipelines", "customer onboarding") are usually the job's core.
    bigrams = Counter()
    for weight, line in _sections(description):
        if not weight:
            continue
        words = normalize(line).split()
        for a, b in zip(words, words[1:]):
            if "|" not in (a, b) and a not in STOPWORDS and b not in STOPWORDS and not a.isdigit() and not b.isdigit() and len(a) > 2 and len(b) > 2:
                bigrams[f"{a} {b}"] += weight
    for phrase, w in bigrams.items():
        if w >= 2 and phrase not in scores and not any(p in NOT_SKILLS for p in phrase.split()):
            scores[phrase] += w * 0.8
            counts[phrase] += 2
            required[phrase] = 1.0

    # Drop single words already covered by a phrase we keep ("learning" under "machine learning").
    ranked = [t for t, s in scores.most_common() if s >= 0.55]
    kept = []
    for term in ranked:
        if any(term != k and f" {term} " in f" {k} " for k in kept):
            continue
        kept.append(term)
        if len(kept) >= limit:
            break
    return [{"term": t, "label": labels.get(t, t), "weight": round(min(scores[t], 4.5), 2), "required": required.get(t, 1) >= 1.5,
             "mentions": counts[t]} for t in kept]


def keyword_report(description, resume, profile, company=""):
    keywords = extract_keywords(description, company)
    if not keywords:
        return None
    resume_n, profile_n = normalize(resume), normalize(profile)
    matched, add, gaps = [], [], []
    for k in keywords:
        if contains(resume, resume_n, k["term"]):
            matched.append(k)
        elif contains(profile, profile_n, k["term"]):
            add.append(k)
        else:
            gaps.append(k)
    total = sum(k["weight"] for k in keywords)
    score = round(100 * sum(k["weight"] for k in matched) / total) if total else 0
    return {"score": score, "matched": matched, "in_profile": add, "gaps": gaps}


# ---------------------------------------------------------------- format

DATE = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{4}\b|\b\d{1,2}/\d{4}\b|"
                  r"\b(?:19|20)\d{2}\b|\bpresent\b|\bcurrent\b", re.I)
WEAK_OPENERS = re.compile(r"^(responsible for|worked on|helped( with)?|assisted( with)?|involved in|"
                          r"participated in|tasked with|duties included|in charge of)\b", re.I)


def _entries(lines, section_name):
    """Entries (### blocks) of one ## section: [{head, sub, bullets}]."""
    out, in_section, cur = [], False, None
    for line in lines:
        s = line.strip()
        if s.startswith("## "):
            in_section = section_name in s.lower()
            cur = None
        elif in_section and s.startswith("### "):
            cur = {"head": s[4:], "sub": "", "bullets": []}
            out.append(cur)
        elif in_section and cur is not None:
            if re.match(r"[-*•]\s+", s):
                cur["bullets"].append(re.sub(r"^[-*•]\s+", "", s))
            elif s and not cur["sub"] and not cur["bullets"]:
                cur["sub"] = s
    return out


def _date_style(text):
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{4}", text, re.I):
        return "Mon YYYY"
    if re.search(r"\b\d{1,2}/\d{4}\b", text):
        return "MM/YYYY"
    if re.search(r"\b(?:19|20)\d{2}\b", text):
        return "YYYY"
    return None


def format_report(resume):
    """Checks that the resume will parse cleanly and reads well. Each: {id, label, status, detail}."""
    text = resume or ""
    lines = text.splitlines()
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    sections = [l.strip()[3:].strip().lower() for l in lines if l.strip().startswith("## ")]
    checks = []

    def check(cid, label, status, detail=""):
        checks.append({"id": cid, "label": label, "status": status, "detail": detail})

    name = next((l.strip()[2:] for l in lines if l.strip().startswith("# ")), "")
    check("name", "Name at the top", "pass" if name else "fail",
          name or "Start with your name as a '# ' heading so parsers pick it up.")

    has_email = bool(re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text))
    has_phone = bool(re.search(r"(?:\+?\d[\d\s().-]{8,}\d)", text))
    missing = [w for w, ok in (("email", has_email), ("phone", has_phone)) if not ok]
    check("contact", "Email and phone", "pass" if not missing else "fail" if not has_email else "warn",
          "Both present." if not missing else f"Missing {' and '.join(missing)}. Recruiters can't contact you without them.")

    need = {"experience": ("experience", "employment", "work history"), "education": ("education",),
            "skills": ("skill",)}
    absent = [k for k, words in need.items() if not any(any(w in s for w in words) for s in sections)]
    unusual = [s for s in sections if not re.search(
        r"summary|profile|objective|skill|experience|employment|work|education|project|certific|award|"
        r"publication|volunteer|leadership|language|interest|activit|honor|research|course", s)]
    detail = "Missing: " + ", ".join(absent) + "." if absent else "Experience, Education and Skills found."
    if unusual:
        detail += f" Non-standard heading{'s' if len(unusual) > 1 else ''} ({', '.join(unusual)}) may not be recognized."
    check("sections", "Standard section headings", "fail" if "experience" in absent else
          "warn" if absent or unusual else "pass", detail)

    jobs = _entries(lines, "experience") or _entries(lines, "employment")
    undated = [j["head"].split("|")[0].strip() for j in jobs if not DATE.search(j["head"] + " " + j["sub"])]
    styles = {_date_style(j["head"] + " " + j["sub"]) for j in jobs} - {None}
    if not jobs:
        check("dates", "Dates on every role", "warn", "No roles found under Experience ('### Company' entries).")
    elif undated:
        check("dates", "Dates on every role", "fail", "No dates for: " + ", ".join(undated) + ".")
    elif len(styles) > 1:
        check("dates", "Dates on every role", "warn", "Mixed date formats (" + ", ".join(sorted(styles)) + "). Pick one.")
    else:
        check("dates", "Dates on every role", "pass", f"{len(jobs)} role{'s' if len(jobs) != 1 else ''}, consistent dates.")

    bullets = [b for j in jobs for b in j["bullets"]]
    thin = [j["head"].split("|")[0].strip() for j in jobs[:2] if len(j["bullets"]) < 2]
    long_ = [j["head"].split("|")[0].strip() for j in jobs if len(j["bullets"]) > 7]
    if thin or long_:
        parts = ([f"Recent role{'s' if len(thin) > 1 else ''} with under 2 bullets: {', '.join(thin)}."] if thin else []) + \
                ([f"Over 7 bullets: {', '.join(long_)}. Cut the weakest."] if long_ else [])
        check("bullets", "Bullets per role", "warn", " ".join(parts))
    elif jobs:
        check("bullets", "Bullets per role", "pass", "Recent roles have 2–7 bullets each.")

    if bullets:
        with_numbers = sum(bool(re.search(r"\d|%|\$", b)) for b in bullets)
        share = with_numbers / len(bullets)
        check("metrics", "Measurable results", "pass" if share >= 0.4 else "warn",
              f"{with_numbers} of {len(bullets)} bullets include a number."
              + ("" if share >= 0.4 else " Add real figures from your profile where you have them (never invent them)."))
        weak = [b for b in bullets if WEAK_OPENERS.search(b)]
        check("verbs", "Strong opening verbs", "pass" if not weak else "warn",
              "Bullets lead with action verbs." if not weak else
              f"{len(weak)} bullet{'s' if len(weak) > 1 else ''} open with phrases like “{WEAK_OPENERS.search(weak[0]).group(0)}”. "
              "Lead with what you did: Built, Led, Cut, Shipped…")
        long_bullets = [b for b in bullets if len(b.split()) > 40]
        if long_bullets:
            check("long-bullets", "Bullet length", "warn",
                  f"{len(long_bullets)} bullet{'s' if len(long_bullets) > 1 else ''} over 40 words. Aim for one or two lines.")

    words = len(re.findall(r"\w+", plain))
    check("length", "Length", "pass" if 300 <= words <= 850 else "warn",
          f"{words} words." + ("" if 300 <= words <= 850 else
                               " That's thin for most roles." if words < 300 else " Likely over one page; trim older roles."))

    risky = []
    if re.search(r"^\s*\|.*\|\s*$\n^\s*\|?\s*:?-{3,}", text, re.M):
        risky.append("a table")
    if re.search(r"!\[[^\]]*\]\(", text):
        risky.append("an image")
    if re.search(r"<[a-z][^>]*>", text, re.I):
        risky.append("HTML tags")
    if re.search(r"[☀-➿\U0001F300-\U0001FAFF]", text):
        risky.append("emoji or symbols")
    check("parse", "Plain, parseable text", "pass" if not risky else "warn",
          "No tables, images or graphics." if not risky else
          f"Contains {', '.join(risky)}. Many applicant-tracking systems drop or garble these.")

    points = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    score = round(100 * sum(points[c["status"]] for c in checks) / len(checks))
    return {"score": score, "checks": checks, "words": words}


def score_resume(resume, description="", profile="", company=""):
    fmt = format_report(resume)
    kw = keyword_report(description, resume, profile, company) if (description or "").strip() else None
    overall = round(0.6 * kw["score"] + 0.4 * fmt["score"]) if kw else fmt["score"]
    return {"score": overall, "keywords": kw, "format": fmt}
