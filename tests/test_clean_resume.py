"""AI-written resumes must hold only the resume: no notes about what was changed or emphasized."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import clean_resume  # noqa: E402

RESUME = """# Jane Doe
Austin, TX | 512-555-0100 | jane@example.com

## Summary
Backend engineer with 6 years building distributed systems in Python.

## Experience
### Globex | Austin, TX
**Senior Software Engineer** | Jan 2021 – Present
- Built microservices in Python handling 2M requests/day
- Led the migration to Kubernetes

## Projects
### Tracker | Python, React
- Job tracker used by 40 friends"""

COMMENTARY_AFTER = [
    "The resume emphasizes React, Node.js, TypeScript, and AI tooling experience while preserving all "
    "original dates, titles, technologies, and achievements.",
    "This version highlights your backend work for the role.",
    "I've reordered the skills to put the job's keywords first.",
    "---\n\nI kept all dates and titles from your profile and emphasized the TypeScript work.",
    "Changes made:\n- Moved React to the top of Skills\n- Reworded the Globex bullets",
    "## Notes\n- I emphasized the Kubernetes migration\n- Removed the 2016 internship",
    "```",
    "_(The answer was cut off — ask it to continue.)_",
    "Let me know if you'd like a version focused on frontend.",
]
COMMENTARY_BEFORE = [
    "Here's your tailored resume:",
    "Here is the resume tailored to the Stripe Backend Engineer role:\n\n",
    "```resume",
]


class CleanResumeTest(unittest.TestCase):
    def test_clean_resume_is_unchanged(self):
        self.assertEqual(clean_resume(RESUME), RESUME)

    def test_drops_commentary_after_the_resume(self):
        for note in COMMENTARY_AFTER:
            with self.subTest(note=note[:40]):
                self.assertEqual(clean_resume(RESUME + "\n\n" + note), RESUME)

    def test_drops_commentary_before_the_resume(self):
        for note in COMMENTARY_BEFORE:
            with self.subTest(note=note[:40]):
                self.assertEqual(clean_resume(note + "\n" + RESUME), RESUME)

    def test_drops_note_lines_inside(self):
        noisy = RESUME.replace("## Projects", "> Note: I left out the 2016 internship.\n\n**Note:** dates kept.\n\n## Projects")
        self.assertEqual(clean_resume(noisy), RESUME)

    def test_keeps_real_content_that_looks_a_bit_like_commentary(self):
        real = [
            RESUME + "\n\n## Interests\nHiking, chess and photography.",
            RESUME + "\n\n## Certifications\nAWS Solutions Architect – Associate (2023)",
            RESUME.replace("Backend engineer with", "Engineer emphasizing reliability, with"),
            RESUME + "\n- Tailored onboarding flows for 3 enterprise clients",  # a bullet is never removed
        ]
        for text in real:
            with self.subTest(text=text[-50:]):
                self.assertEqual(clean_resume(text), text)


if __name__ == "__main__":
    unittest.main()
