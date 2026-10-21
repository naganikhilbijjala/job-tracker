"""Resume PDFs, made with the standard library only.

Turns the tracker's resume Markdown (ai.RESUME_FORMAT) into a US Letter PDF. It uses the
PDF standard Helvetica fonts: nothing is embedded, files stay small (a few KB), and the
text is real text that applicant-tracking systems read. The layout follows the resume page
(templates/print.html), including its fit-to-one-page dial, so the download matches what
you saw there.
"""
import re
import unicodedata
import zlib

PAGE_W, PAGE_H = 612, 792      # US Letter, in points
MARGIN_X, MARGIN_Y = 36, 28.8  # 0.5in sides, 0.4in top and bottom, as on the resume page
CONTENT_W = PAGE_W - 2 * MARGIN_X
LIMIT = PAGE_H - 2 * MARGIN_Y - 3  # a little slack, like the page's SAFETY_PX
INK, MUTED, RULE = (0.067, 0.078, 0.094), (0.294, 0.333, 0.388), (0.604, 0.639, 0.682)
FONTS = {"regular": "Helvetica", "bold": "Helvetica-Bold", "italic": "Helvetica-Oblique",
         "bolditalic": "Helvetica-BoldOblique"}
FONT_IDS = {style: f"F{i + 1}" for i, style in enumerate(FONTS)}
SIZES = {"tight": 0.2, "compact": 0.5, "normal": 0.8}  # same dial positions as the resume page
ASCENT, CONTENT = 0.718, 0.925  # Helvetica ascender, and ascender + descender, per point of size


def fit_style(t):
    """One dial for the layout, from 0 (9 pt, tight) to 1 (11 pt, roomy). Matches fitStyle in print.html."""
    lerp = lambda a, b: a + (b - a) * t
    return {"fs": lerp(9, 11), "lh": lerp(1.2, 1.38), "gap": lerp(0.5, 1.4)}


# ---------------------------------------------------------------- text

FALLBACK = {"\u2192": "->", "\u2190": "<-", "\u2713": "", "\u2714": "", "\u2265": ">=", "\u2264": "<=", "\u00d7": "x",
            "\u2212": "-", "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u2011": "-", "\u2010": "-",
            # letters with no plain-letter decomposition
            "\u0141": "L", "\u0142": "l", "\u0110": "D", "\u0111": "d", "\u0126": "H", "\u0127": "h", "\u0131": "i"}


def win(text):
    """Text as WinAnsi bytes (the standard fonts' encoding), with close stand-ins for anything outside it."""
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1252")
            continue
        except UnicodeEncodeError:
            pass
        alt = FALLBACK.get(ch)
        if alt is None:
            alt = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        try:
            out += alt.encode("cp1252")
        except UnicodeEncodeError:
            # Symbols and emoji the font can't draw (↗, ★, 🚀) are decoration: leave them out. A letter
            # it can't draw stays visible as "?", so a name is never silently changed.
            kind = unicodedata.category(ch)[0]
            out += b" " if kind == "Z" else b"" if kind in "SPCM" else b"?"
    return bytes(out)


def width(data, style, size, spacing=0.0):
    w = WIDTHS[style]
    return sum(w[b - 32] if b >= 32 else 0 for b in data) * size / 1000 + spacing * len(data)


INLINE = re.compile(r"\*\*(.+?)\*\*|(?<![*\w])\*([^*\s][^*]*?)\*(?!\*)|\[([^\]]+)\]\((https?://[^\s)]+)\)|`([^`]+)`"
                    r"|(https?://[^\s<)]+)")


def runs(text, base="regular", color=INK, flatten=None):
    """Markdown inline text as [(text, style, color, link)]. flatten forces one style (e.g. italic sub-rows)."""
    def combine(extra):
        if flatten:
            return flatten
        b = "bold" in (base, extra) or base == "bolditalic"
        i = "italic" in (base, extra) or base == "bolditalic"
        return "bolditalic" if b and i else "bold" if b else "italic" if i else "regular"

    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], combine(None), color, None))
        bold, em, label, url, code, bare = m.groups()
        if bold is not None:
            out.append((bold, combine("bold"), color, None))
        elif em is not None:
            out.append((em, combine("italic"), color, None))
        elif label is not None:
            out.append((label, combine(None), color, url))
        elif code is not None:
            out.append((code, combine(None), color, None))
        else:
            out.append((bare, combine(None), color, bare))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], combine(None), color, None))
    return [r for r in out if r[0]]


def wrap(parts, size, max_w, spacing=0.0):
    """Lay runs out in lines no wider than max_w. Returns [(width, [(x, bytes, style, color, link)])]."""
    lines, line, x = [], [], 0.0
    for text, style, color, link in parts:
        for piece in re.findall(r"\S+|\s+", text):
            data = win(" " if piece.isspace() else piece)
            w = width(data, style, size, spacing)
            if piece.isspace():
                if line:
                    line.append((x, data, style, color, link))
                    x += w
                continue
            if line and x + w > max_w:
                while line and line[-1][1] == b" ":
                    x = line.pop()[0]
                lines.append((x, line))
                line, x = [], 0.0
            line.append((x, data, style, color, link))
            x += w
    while line and line[-1][1] == b" ":
        x = line.pop()[0]
    if line:
        lines.append((x, line))
    return [(w, _merge(segs)) for w, segs in lines]


def _merge(segs):
    """Neighbouring words in the same style become one piece of text, so parsers read whole phrases."""
    out = []
    for seg in segs:
        if out and out[-1][2:] == seg[2:]:
            out[-1] = (out[-1][0], out[-1][1] + seg[1], *seg[2:])
        else:
            out.append(seg)
    return out


# ---------------------------------------------------------------- resume structure

DATEISH = re.compile(r"\b(19|20)\d{2}\b|\bpresent\b|\bcurrent\b|\bexpected\b", re.I)


def split_row(text, right_if_no_date):
    """'Left | Right' rows with the date part pushed right. Matches splitRow in markdown.js."""
    parts = re.split(r"\s+\|\s+", text)
    if len(parts) < 2:
        return text, ""
    i = next((k for k, p in enumerate(parts) if k > 0 and DATEISH.search(p)), -1)
    if i == -1 and right_if_no_date:
        i = len(parts) - 1
    if i == -1:
        return text, ""
    right = parts.pop(i)
    return " | ".join(parts), right


def parse(src):
    """Resume Markdown as blocks, following renderResume in markdown.js."""
    lines = str(src or "").replace("\r\n", "\n").split("\n")
    blocks, section, para = [], "", []
    in_list = in_entry = after_head = False

    def flush():
        nonlocal para
        if para:
            blocks.append(("para", para))
        para = []

    def end_list():
        nonlocal in_list
        if in_list:
            blocks.append(("list_end",))
        in_list = False

    def end_entry():
        nonlocal in_entry
        flush()
        end_list()
        if in_entry:
            blocks.append(("entry_end",))
        in_entry = False

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if m := re.match(r"#\s+(.*)", line):
            end_entry()
            contact = []
            while i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].strip().startswith("#"):
                i += 1
                contact += [c.strip() for c in re.split(r"\s+[|·•]\s+", lines[i]) if c.strip()]
            blocks.append(("header", m.group(1), contact))
        elif m := re.match(r"##\s+(.*)", line):
            end_entry()
            section = m.group(1).lower()
            blocks.append(("h2", m.group(1)))
        elif m := re.match(r"###\s+(.*)", line):
            end_entry()
            blocks.append(("entry_head", *split_row(m.group(1), True)))
            in_entry = after_head = True
            i += 1
            continue
        elif m := re.match(r"[-*•]\s+(.*)", line):
            flush()
            skills = "skill" in section
            if not in_list:
                blocks.append(("list_start", skills))
                in_list = True
            text = m.group(1)
            if skills and not text.startswith("**"):
                text = re.sub(r"^([^:*]{2,40}):\s*", r"**\1:** ", text)
            blocks.append(("item", text, skills))
        elif re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line):
            flush()
            end_list()
            blocks.append(("hr",))
        elif not line:
            flush()
            end_list()
        elif after_head and not in_list:
            blocks.append(("entry_sub", *split_row(line, False)))
        else:
            end_list()
            para.append(line)
        after_head = False
        i += 1
    end_entry()
    return blocks


# ---------------------------------------------------------------- layout

def layout(blocks, t, extra_gap=0.0):
    """Place every line. Returns (pages, height of the last page) where pages are lists of draw ops."""
    s = fit_style(t)
    fs, lh, gap = s["fs"], s["lh"], s["gap"] + extra_gap
    pages, y, pending = [[]], 0.0, 0.0

    def space(top=0.0):
        nonlocal y, pending
        y += max(pending, top)
        pending = 0.0

    def room(h):
        nonlocal y
        if y + h > LIMIT and y > 0:  # a new page; only happens when it can't fit on one
            pages.append([])
            y = 0.0

    def put(lines, size, line_h, x0=0.0, align="left", spacing=0.0):
        """Place lines; returns (page, baseline) of the first one."""
        nonlocal y
        first = None
        for w, segs in lines:
            room(line_h)
            base = y + (line_h - CONTENT * size) / 2 + ASCENT * size
            first = first or (pages[-1], base)
            dx = x0 + ((CONTENT_W - x0 - w) / 2 if align == "center" else 0)
            for x, data, style, color, link in segs:
                pages[-1].append(("text", dx + x, base, data, style, size, color, spacing, link))
            y += line_h
        return first

    def row(left, right, left_style, flatten=None):
        nonlocal y
        size, line_h = fs, fs * lh
        right_parts = runs(right, "regular", MUTED) if right else []
        right_lines = wrap(right_parts, size, CONTENT_W) if right else []
        rw = right_lines[0][0] if right_lines else 0
        left_lines = wrap(runs(left, left_style, INK, flatten), size, CONTENT_W - rw - (12 if rw else 0))
        room(line_h)
        top = y
        put(left_lines or [(0, [])], size, line_h)
        if right_lines:
            base = top + (line_h - CONTENT * size) / 2 + ASCENT * size
            for x, data, style, color, link in right_lines[0][1]:
                pages[-1].append(("text", CONTENT_W - rw + x, base, data, style, size, color, 0, link))

    list_skills = False
    for b in blocks:
        kind = b[0]
        if kind == "header":
            _, name, contact = b
            space()
            size = fs * 2.05
            put(wrap(runs(name, "bold"), size, CONTENT_W), size, size * 1.1, align="center")
            y += 3
            if contact:
                csize = fs * 0.95
                parts = []
                for k, item in enumerate(contact):
                    if k:
                        parts.append((" | ", "regular", RULE, None))
                    parts += [(txt, st, MUTED, link) for txt, st, _, link in runs(item, "regular", MUTED)]
                put(wrap(parts, csize, CONTENT_W), csize, csize * lh, align="center")
            pending = 8 * gap
        elif kind == "h2":
            space(10 * gap)
            size = fs * 1.02
            put(wrap(runs(b[1].upper(), "bold"), size, CONTENT_W, spacing=1), size, size * lh, spacing=1)
            y += 1.5
            pages[-1].append(("rule", y + 0.45, 0.9, INK))
            y += 0.9
            pending = 4 * gap
        elif kind == "entry_head":
            space()
            row(b[1], b[2], "bold")
        elif kind == "entry_sub":
            space()
            row(b[1], b[2], "regular", flatten="italic")
        elif kind == "entry_end":
            pending = max(pending, 6 * gap)
        elif kind == "para":
            space()
            for text in b[1]:
                put(wrap(runs(text), fs, CONTENT_W), fs, fs * lh)
            pending = 3 * gap
        elif kind == "list_start":
            list_skills = b[1]
            space(2 * gap)
        elif kind == "item":
            space()
            indent = 0 if list_skills else 14
            first = put(wrap(runs(b[1]), fs, CONTENT_W - indent), fs, fs * lh, x0=indent)
            if first and not list_skills:  # the bullet sits on the item's first line
                first[0].append(("text", 4, first[1], b"\x95", "regular", fs, INK, 0, None))
            pending = (1 if list_skills else 1.5) * gap
        elif kind == "list_end":
            pass
        elif kind == "hr":
            space(6)
            pages[-1].append(("rule", y + 0.4, 0.75, RULE))
            y += 0.75
            pending = 6
    return pages, y


def fit(blocks, size="auto"):
    """The dial setting to use: the roomiest that fits on one page, as Auto-fit does on the page."""
    if size in SIZES:
        return SIZES[size], 0.0
    one_page = lambda t, g=0.0: (lambda p: len(p[0]) == 1 and p[1] <= LIMIT)(layout(blocks, t, g))

    def largest(hi, ok):
        if ok(hi):
            return hi
        lo = 0.0
        for _ in range(14):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if ok(mid) else (lo, mid)
        return lo

    if not one_page(0):
        return 0.0, 0.0
    t = largest(1.0, one_page)
    extra = largest(max(0.0, 1.9 - fit_style(t)["gap"]), lambda g: one_page(t, g))
    return t, extra


# ---------------------------------------------------------------- the PDF file

def _string(data):
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _info_string(text):
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">"  # Unicode, for titles with any letters


def render(markdown, size="auto", title="Resume", author=""):
    """The resume as PDF bytes, plus {"pages", "font_size"}."""
    blocks = parse(markdown)
    t, extra = fit(blocks, size)
    pages, _ = layout(blocks, t, extra)
    return _write(pages, title, author), {"pages": len(pages), "font_size": round(fit_style(t)["fs"], 1)}


def _write(pages, title, author):
    """Draw ops (in points from the top-left of the margins) as PDF file bytes."""
    objects = []  # bodies; object n is objects[n - 1]

    def add(body):
        objects.append(body)
        return len(objects)

    font_refs = {style: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode())
                 for style, name in FONTS.items()}
    pages_id = add(None)  # filled in once the page ids are known
    page_ids = []
    for ops in pages:
        stream, annots = [], []
        for op in ops:
            if op[0] == "rule":
                _, y, thick, color, *span = op
                x0, w = span or (0, CONTENT_W)
                py = PAGE_H - MARGIN_Y - y
                stream.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {thick:.2f} w "
                              f"{MARGIN_X + x0:.2f} {py:.2f} m {MARGIN_X + x0 + w:.2f} {py:.2f} l S".encode())
                continue
            _, x, base, data, style, fsize, color, spacing, link = op
            px, py = MARGIN_X + x, PAGE_H - MARGIN_Y - base
            stream.append(b"BT " + f"/{FONT_IDS[style]} {fsize:.2f} Tf {spacing:.2f} Tc "
                          f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg 1 0 0 1 {px:.2f} {py:.2f} Tm ".encode()
                          + _string(data) + b" Tj ET")
            if link and data.strip():
                w = width(data, style, fsize, spacing)
                annots.append(add(f"<< /Type /Annot /Subtype /Link /Border [0 0 0] /Rect [{px:.2f} {py - 0.25 * fsize:.2f} "
                                  f"{px + w:.2f} {py + 0.85 * fsize:.2f}] /A << /S /URI /URI ".encode()
                                  + _string(link.encode("ascii", "ignore")) + b" >> >>"))
        content = zlib.compress(b"\n".join(stream))
        content_id = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content) + content + b"\nendstream")
        fonts = " ".join(f"/{FONT_IDS[s]} {font_refs[s]} 0 R" for s in FONTS)
        page_ids.append(add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                            f"/Resources << /Font << {fonts} >> >> /Contents {content_id} 0 R"
                            f"{' /Annots [' + ' '.join(f'{a} 0 R' for a in annots) + ']' if annots else ''} >>".encode()))
    objects[pages_id - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] "
                             f"/Count {len(page_ids)} >>").encode()
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    info = add(b"<< /Title " + _info_string(title) + b" /Author " + _info_string(author) +
               b" /Creator (ApplyStack) /Producer (ApplyStack) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, catalog, info, xref)
    return bytes(out)


# ---------------------------------------------------------------- cover letters

LETTER_INSET_X, LETTER_INSET_Y = 36, 25.2  # on top of the resume margins: 1in sides, 0.75in top and bottom
LETTER_W, LETTER_LIMIT = CONTENT_W - 2 * LETTER_INSET_X, LIMIT - 2 * LETTER_INSET_Y


def parse_letter(src):
    """(name, contact items, paragraphs) from ai.COVER_FORMAT: '# Name', a contact line, then the letter.
    Each paragraph is a list of lines: short lines (the greeting, the sign-off) keep their breaks."""
    lines = str(src or "").replace("\r\n", "\n").strip().split("\n")
    name, contact = "", []
    if lines and (m := re.match(r"#\s+(.*)", lines[0].strip())):
        name, lines = m.group(1).strip(), lines[1:]
        while lines and lines[0].strip():
            contact += [c.strip() for c in re.split(r"\s+[|·•]\s+", lines.pop(0)) if c.strip()]
    paras, para = [], []
    for line in lines + [""]:
        line = re.sub(r"^#+\s*", "", line.strip())
        if line:
            para.append(line)
        elif para:
            paras.append(para if all(len(l) < 60 for l in para) else [" ".join(para)])
            para = []
    return name, contact, paras


def render_letter(markdown, date="", title="Cover Letter", author=""):
    """A cover letter as one-page PDF bytes: the candidate's name and contact line (styled like their resume),
    the date, then the letter. The font shrinks a little when a long letter wouldn't fit one page."""
    name, contact, paras = parse_letter(markdown)
    for fs in (11, 10.5, 10, 9.5, 9):
        pages, y = [[]], 0.0
        lh = fs * 1.42

        def put(lines, size, line_h, align="left"):
            nonlocal y
            for w, segs in lines:
                if y + line_h > LETTER_LIMIT and y > 0:
                    pages.append([])
                    y = 0.0
                base = LETTER_INSET_Y + y + (line_h - CONTENT * size) / 2 + ASCENT * size
                dx = LETTER_INSET_X + ((LETTER_W - w) / 2 if align == "center" else 0)
                for x, data, style, color, link in segs:
                    pages[-1].append(("text", dx + x, base, data, style, size, color, 0, link))
                y += line_h

        if name:
            size = fs * 1.9
            put(wrap(runs(name, "bold"), size, LETTER_W), size, size * 1.1, align="center")
            y += 3
        if contact:
            csize = fs * 0.9
            parts = []
            for k, item in enumerate(contact):
                if k:
                    parts.append((" | ", "regular", RULE, None))
                parts += [(txt, st, MUTED, link) for txt, st, _, link in runs(item, "regular", MUTED)]
            put(wrap(parts, csize, LETTER_W), csize, csize * 1.35, align="center")
        if name or contact:
            y += 6
            pages[-1].append(("rule", LETTER_INSET_Y + y, 0.75, RULE, LETTER_INSET_X, LETTER_W))
            y += 18
        if date:
            put(wrap(runs(date), fs, LETTER_W), fs, lh)
            y += fs * 0.9
        for para in paras:
            for line in para:
                put(wrap(runs(line), fs, LETTER_W), fs, lh)
            y += fs * 0.9
        if len(pages) == 1:
            break
    return _write(pages, title, author), {"pages": len(pages), "font_size": fs}


def file_name(markdown, company="", kind="Resume"):
    """'Full Name Resume - Company.pdf': clean for a recruiter, and different per company so downloads don't clash."""
    name = re.search(r"^#\s+(.+)$", str(markdown or ""), re.M)
    name = re.sub(r"[*_`\[\]]", "", name.group(1)).strip() if name else ""
    base = f"{name} {kind}" if name else kind
    if company.strip():
        base += f" - {company.strip()}"
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", base)).strip()[:120] + ".pdf"


# Character widths (per 1000 units of font size) for WinAnsi codes 32–255, from the Helvetica
# metrics (via the metric-compatible URW Nimbus Sans).
WIDTHS = {
    "regular": [
        278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722,
        278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278,
        469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333,
        500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 0, 556, 0, 222, 556, 333, 1000, 556, 556,
        333, 1000, 667, 333, 1000, 0, 611, 0, 0, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944,
        0, 500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584,
        333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667,
        1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722,
        722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278,
        278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500],
    "bold": [
        278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278,
        556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584,
        556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556,
        333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 0, 556, 0, 278, 556, 500, 1000, 556, 556, 333,
        1000, 667, 333, 1000, 0, 611, 0, 0, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 0,
        500, 667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333,
        333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722, 722, 722, 722, 1000,
        722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722,
        722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278, 611,
        611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556],
    "italic": [
        278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722,
        278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278,
        469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333,
        500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 0, 556, 0, 222, 556, 333, 1000, 556, 556,
        333, 1000, 667, 333, 1000, 0, 611, 0, 0, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944,
        0, 500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584,
        333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667,
        1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722,
        722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278,
        278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500],
    "bolditalic": [
        278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278,
        556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584,
        556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556,
        333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 0, 556, 0, 278, 556, 500, 1000, 556, 556, 333,
        1000, 667, 333, 1000, 0, 611, 0, 0, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 0,
        500, 667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333,
        333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722, 722, 722, 722, 1000,
        722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722,
        722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278, 611,
        611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556],
}
