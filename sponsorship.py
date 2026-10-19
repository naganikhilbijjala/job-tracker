"""Does a job posting say whether it sponsors work visas? (No AI: plain pattern matching.)

Returns one of:
  "no"     the posting says it won't sponsor, or needs citizenship / a security clearance
  "yes"    the posting says it sponsors (or welcomes OPT/CPT, H-1B transfers…)
  "mixed"  it says both, e.g. "we can't sponsor new H-1Bs but will transfer existing ones"
  ""       it doesn't say

It errs toward "" rather than guessing: a posting that only asks you to be "authorized to work
in the US" isn't marked "no", because many such companies do sponsor.
"""
import re

# A sentence is only considered if it's about visas or work authorization at all.
CONTEXT = re.compile(r"visa|sponsor|h-?1b|immigration|work authori[sz]|employment authori[sz]|green card|"
                     r"\bopt\b|\bcpt\b|citizen|clearance|permanent resident", re.I)

NEG = r"(?:not|unable|cannot|can't|won't|will not|do not|does not|don't|doesn't|no longer|isn't|aren't|" \
      r"is not|are not|never|nor)"
NO = [re.compile(p, re.I) for p in (
    rf"\b{NEG}\b[^.;:\n]{{0,60}}?\bsponsor",                       # we are unable to sponsor / will not provide sponsorship
    r"\bsponsor\w*\b[^.;\n]{0,40}?\b(?:not|unavailable|isn't|no)\b",  # sponsorship is not available / will not be provided
    r"\bno\s+(?:\w+[\s/-]+){0,2}?sponsor",                          # no visa sponsorship / No H-1B sponsorship
    r"\bwithout\s+(?:\w+[\s/-]+){0,5}?sponsor",                     # authorized to work without sponsorship
    rf"\b{NEG}\b[^.;\n]{{0,40}}?\b(?:opt|cpt|h-?1b|visas?)\b",      # not open to OPT/CPT / cannot support H-1B
    r"\b(?:u\.?s\.?|united states|american)\s+citizen(?:s|ship)?\b[^.;\n]{0,30}?\b(?:only|required|is required|mandatory)",
    r"\bmust\s+(?:be|hold)\s+(?:a\s+)?(?:u\.?s\.?\s+|united states\s+)?(?:citizen|green card|permanent resident)",
    r"\bonly\s+(?:\w+\s+){0,2}?(?:citizens|green card holders|permanent residents)",
    r"\b(?:citizens|green card holders)\s+only\b",
    r"\b(?:security|secret|top secret|ts/sci|public trust)\s+clearance\b[^.;\n]{0,40}?\b(?:required|must|necessary|needed)",
    r"\b(?:requires?|must (?:have|hold|obtain)|ability to obtain)\b[^.;\n]{0,30}?\b(?:security|secret|ts/sci)\s+clearance",
    r"\bsponsor\w*\s*[:\-–]\s*(?:no|none|n/a|not available)\b",       # Visa sponsorship: No
)]
YES = [re.compile(p, re.I) for p in (
    # "we … sponsor" only counts when the sentence is about visas or candidates, not "executive sponsors".
    r"\b(?:will|can|could|may|able to|happy to|willing to|open to|glad to|we|company|employer)\s+(?:\w+\s+){0,2}?"
    r"sponsor(?!ship)\w*\b(?=[^.;\n]*\b(?:visas?|h-?1b|immigration|green cards?|candidates?|applicants?|hires?)\b)",
    r"\bsponsorship\s+(?:\w+\s+){0,2}?(?:is\s+|are\s+|may be\s+|will be\s+|can be\s+)?(?:available|offered|provided|possible|supported|considered)",
    r"\b(?:offers?|provides?|providing|offering|supports?)\s+(?:\w+[\s/-]+){0,2}?sponsorship",
    r"\b(?:visa|h-?1b|immigration)\s+(?:\w+\s+){0,2}?(?:support|sponsorship|assistance)\s+(?:is\s+)?(?:available|offered|provided)",
    r"\bh-?1b\s+(?:transfers?|sponsorship)\b(?![^.;\n]{0,30}\b(?:not|unavailable)\b)",
    r"\btransfer\w*\s+(?:\w+\s+){0,2}?(?:h-?1b|visas?)\b",                 # can transfer existing H-1B visas
    r"\b(?:opt|cpt|stem opt)\b[^.;\n]{0,40}?\b(?:welcome|welcomed|accepted|eligible|encouraged|considered)",
    r"\bsponsor\w*\s*[:\-–]\s*(?:yes|available)\b",                   # Visa sponsorship: Yes
)]


def _sentences(text):
    # A full stop ends a sentence only after a lowercase word, so "U.S. citizen" stays whole.
    return [s.strip() for s in re.split(r"(?<=[a-z0-9)][.!?])\s+|[\n;•]+", text) if s.strip()]


def check(description):
    """{"status": "no" | "yes" | "mixed" | "", "quote": the sentence it's based on}."""
    text = (description or "").replace("’", "'").replace(" ", " ")
    no_quote = yes_quote = ""
    for sentence in _sentences(text):
        if not CONTEXT.search(sentence):
            continue
        if any(p.search(sentence) for p in NO):
            no_quote = no_quote or sentence
        elif any(p.search(sentence) for p in YES):
            yes_quote = yes_quote or sentence
    status = "mixed" if no_quote and yes_quote else "no" if no_quote else "yes" if yes_quote else ""
    quote = no_quote or yes_quote
    return {"status": status, "quote": quote[:280] + ("…" if len(quote) > 280 else "")}
