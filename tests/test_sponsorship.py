"""Visa sponsorship detection on wording taken from real job postings."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sponsorship import check  # noqa: E402

NO = [
    "We are unable to sponsor or take over sponsorship of an employment visa at this time.",
    "This position is not eligible for visa sponsorship.",
    "Applicants must be authorized to work in the U.S. without the need for employer sponsorship now or in the future.",
    "No visa sponsorship is available for this role.",
    "No H-1B sponsorship.",
    "We will not provide immigration sponsorship for this position.",
    "Sponsorship is not available.",
    "The company does not sponsor work visas.",
    "Candidates requiring sponsorship will not be considered.",
    "Must be a U.S. citizen.",
    "US citizenship is required due to government contract requirements.",
    "Active Secret security clearance required.",
    "This role requires an active TS/SCI clearance.",
    "Visa sponsorship: No",
    "We cannot support OPT or CPT candidates for this role.",
    "Open to U.S. citizens and green card holders only.",
    "We don’t sponsor visas.",
]
YES = [
    "Visa sponsorship is available for the right candidate.",
    "We will sponsor H-1B visas for qualified candidates.",
    "We offer visa sponsorship and relocation support.",
    "H-1B transfers welcome.",
    "OPT and STEM OPT candidates are welcome to apply.",
    "Sponsorship: Yes",
    "The company is open to sponsoring the right candidate.",
    "Immigration support is available.",
]
SILENT = [
    "You'll partner with executive sponsors across the business.",
    "Applicants must be authorized to work in the United States.",
    "We are an equal opportunity employer and do not discriminate on the basis of citizenship status.",
    "Build REST APIs in Python. Not a remote role.",
    "Manage event sponsorships and partner relationships.",
    "We work closely with sponsors and stakeholders across the company.",
    "You will report to the project sponsor.",
    "",
]


class SponsorshipTest(unittest.TestCase):
    def test_says_no(self):
        for text in NO:
            with self.subTest(text=text):
                self.assertEqual(check(text)["status"], "no")

    def test_says_yes(self):
        for text in YES:
            with self.subTest(text=text):
                self.assertEqual(check(text)["status"], "yes")

    def test_says_nothing(self):
        for text in SILENT:
            with self.subTest(text=text):
                self.assertEqual(check(text)["status"], "")

    def test_mixed_and_quote(self):
        result = check("About us\nWe build payments.\n\nWe can transfer existing H-1B visas. "
                       "We are unable to sponsor new visas at this time.")
        self.assertEqual(result["status"], "mixed")
        self.assertIn("unable to sponsor", result["quote"])

    def test_quote_is_the_sentence(self):
        desc = "Great team, great pay.\n\nRequirements:\n• 5+ years of Go\n• We do not offer visa sponsorship\n"
        self.assertEqual(check(desc)["quote"], "We do not offer visa sponsorship")


if __name__ == "__main__":
    unittest.main()
