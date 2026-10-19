"""Resume PDFs (pdf.py): a valid file, one page when it fits, readable text, sensible names."""
import re
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf  # noqa: E402


def resume(bullets):
    out = ["# Alex Example", "City, ST | alex@example.com | [LinkedIn](https://linkedin.com/in/alex)", "",
           "## Experience", "### Acme | Remote", "**Engineer** | Jan 2020 – Present"]
    out += [f"- Built service {i} that handles millions of requests a day with Python and PostgreSQL" for i in range(bullets)]
    out += ["", "## Skills", "- Languages: Python, Go, SQL"]
    return "\n".join(out)


def check_structure(data):
    """Every xref entry points at its object, and startxref points at the table."""
    start = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[start:start + 4] == b"xref", "startxref is off"
    count = int(re.search(rb"xref\n0 (\d+)", data).group(1))
    offsets = [int(m) for m in re.findall(rb"(\d{10}) 00000 n ", data)]
    assert len(offsets) == count - 1
    for n, off in enumerate(offsets, 1):
        assert data[off:].startswith(b"%d 0 obj" % n), f"object {n} isn't where the xref says"


def page_text(data):
    streams = re.findall(rb"stream\n(.*?)\nendstream", data, re.S)
    return b"".join(zlib.decompress(s) for s in streams)


class PdfTest(unittest.TestCase):
    def test_short_resume_is_one_valid_page_at_full_size(self):
        data, info = pdf.render(resume(6), title="Alex Example Resume - Acme", author="Alex Example")
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        check_structure(data)
        self.assertEqual(info, {"pages": 1, "font_size": 11.0})
        text = page_text(data)
        for words in (b"(Alex Example)", b"(EXPERIENCE)", b"(Jan 2020 \x96 Present)", b"(Languages:)"):
            self.assertIn(words, text)
        self.assertIn(b"/URI (https://linkedin.com/in/alex)", data)  # links stay clickable
        self.assertIn(b"/Title <FEFF", data)

    def test_longer_resumes_shrink_to_fit_then_spill(self):
        sizes = []
        for n in (10, 25, 40):
            data, info = pdf.render(resume(n))
            check_structure(data)
            self.assertEqual(info["pages"], 1, n)
            sizes.append(info["font_size"])
        self.assertEqual(sizes, sorted(sizes, reverse=True))  # more content, smaller text
        data, info = pdf.render(resume(90))
        check_structure(data)
        self.assertEqual((info["pages"], info["font_size"]), (2, 9.0))  # never smaller than 9 pt

    def test_a_chosen_size_is_used_as_is(self):
        self.assertEqual(pdf.render(resume(6), size="tight")[1]["font_size"], 9.4)

    def test_characters_outside_the_standard_fonts(self):
        self.assertEqual(pdf.win("Café – “quoted” • Zoë"), "Café – “quoted” • Zoë".encode("cp1252"))
        self.assertEqual(pdf.win("\u0106uk \u2192 \u0141\u00f3d\u017a \u2713 \u4f60"), b"Cuk -> L\xf3dz  ?")  # never a crash

    def test_file_names(self):
        self.assertEqual(pdf.file_name(resume(1), "Acme"), "Alex Example Resume - Acme.pdf")
        self.assertEqual(pdf.file_name(resume(1)), "Alex Example Resume.pdf")
        self.assertEqual(pdf.file_name("no heading", 'A/B: "C"'), "Resume - A B C.pdf")


if __name__ == "__main__":
    unittest.main()
