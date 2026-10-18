"""Matching application-form questions to profile facts and saved answers (autofill.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import autofill  # noqa: E402

PROFILE = """# Alex Example
Miami, FL | (305) 555-0142 | alex@example.com | https://www.linkedin.com/in/alex-example/ | GitHub - https://github.com/alexex | alex.dev

## Experience
### Engineer | Acme | 2020 – Present
- Built things at https://acme.com
"""


def field(label, kind="text", options=(), value="", fid=None):
    return {"id": fid or label[:20], "label": label, "kind": kind, "options": list(options), "value": value}


class ProfileFactsTest(unittest.TestCase):
    def test_contact_details_from_the_top_of_the_profile(self):
        facts = autofill.profile_facts(PROFILE)
        self.assertEqual(facts["linkedin"], "https://www.linkedin.com/in/alex-example")
        self.assertEqual(facts["github"], "https://github.com/alexex")
        self.assertEqual(facts["website"], "https://alex.dev")  # not the email's domain, not a link further down
        self.assertEqual(facts["email"], "alex@example.com")
        self.assertEqual(facts["phone"], "(305) 555-0142")
        self.assertEqual((facts["first_name"], facts["last_name"]), ("Alex", "Example"))

    def test_account_fills_the_gaps(self):
        facts = autofill.profile_facts("Backend engineer.\nlinkedin.com/in/abc", email="me@x.com", name="Ann Lee")
        self.assertEqual(facts["linkedin"], "https://linkedin.com/in/abc")
        self.assertEqual((facts["email"], facts["full_name"]), ("me@x.com", "Ann Lee"))
        self.assertNotIn("phone", facts)


class MatchTest(unittest.TestCase):
    facts = autofill.profile_facts(PROFILE)

    def run_match(self, fields, saved=()):
        answers, rest = autofill.match(autofill.clean_fields(fields), list(saved), self.facts)
        return {a["id"]: (a["value"], a["source"]) for a in answers}, [f["id"] for f in rest]

    def test_profile_facts_fill_short_text_fields_only(self):
        answers, rest = self.run_match([
            field("LinkedIn Profile*", fid="li"),
            field("Website", fid="web"),
            field("Do you have a GitHub with distributed systems projects?", "radio", ["Yes", "No"], fid="gh"),
        ])
        self.assertEqual(answers["li"], ("https://www.linkedin.com/in/alex-example", "profile"))
        self.assertEqual(answers["web"], ("https://alex.dev", "profile"))
        self.assertIn("gh", rest)  # a yes/no question that mentions GitHub isn't the GitHub URL field

    def test_saved_answers_match_the_same_question_and_the_same_kind_of_question(self):
        saved = [{"question": "Do you require sponsorship to work in the US?", "answer": "No"},
                 {"question": "Desired salary", "answer": "$180,000"},
                 {"question": "How many years of experience do you have with Python?", "answer": "6 years"}]
        answers, rest = self.run_match([
            field("Will you now or in the future require visa sponsorship?", "select", ["Yes", "No"], fid="visa"),
            field("What is your desired salary for this role? *", fid="pay"),
            field("How many years of experience do you have with Python?", "number", fid="py"),
            field("How many years of experience do you have with Go?", "number", fid="go"),
        ], saved)
        self.assertEqual(answers["visa"], ("No", "saved"))
        self.assertEqual(answers["pay"], ("$180,000", "saved"))
        self.assertEqual(answers["py"], ("6", "saved"))  # a number field gets just the number
        self.assertEqual(rest, ["go"])  # a different skill is a different question

    def test_choice_fields_only_get_one_of_their_options(self):
        self.assertEqual(autofill.pick_option("Yes", ["Yes, I am authorized", "No"]), "Yes, I am authorized")
        self.assertEqual(autofill.pick_option("no, I do not", ["Yes", "No", "Prefer not to say"]), "No")
        self.assertIsNone(autofill.pick_option("Maybe", ["Yes", "No"]))
        answers, rest = self.run_match([field("Gender", "select", ["Male", "Female", "Decline"], fid="g")],
                                       [{"question": "Gender", "answer": "Non-binary"}])
        self.assertEqual((answers, rest), ({}, ["g"]))

    def test_personal_questions_are_never_for_the_ai(self):
        for label in ("Will you require visa sponsorship?", "What is your desired salary?", "Gender",
                      "Are you a protected veteran?", "How did you hear about us?", "When can you start?",
                      "I agree to the privacy policy"):
            self.assertTrue(autofill.is_personal(label), label)
        for label in ("Do you have experience with Kubernetes?", "What excites you about working at Acme?",
                      "How many years of experience do you have with Python?"):
            self.assertFalse(autofill.is_personal(label), label)


class WorthSavingTest(unittest.TestCase):
    def test_new_and_changed_answers_but_not_company_specific_or_profile_facts(self):
        saved = [{"question": "Desired salary", "answer": "$170,000"},
                 {"question": "Are you open to relocation?", "answer": "Yes"}]
        fields = autofill.clean_fields([
            field("Desired salary", value="$180,000"),
            field("Are you open to relocation?", "radio", ["Yes", "No"], value="Yes"),
            field("Will you require sponsorship?", "radio", ["Yes", "No"], value="No"),
            field("Why do you want to work at Acme?", "textarea", value="Because I love it"),
            field("Tell us about a project you're proud of", "textarea", value="At Acme I built the thing"),
            field("LinkedIn Profile", value="https://www.linkedin.com/in/alex-example"),
            field("Website", value=""),
        ])
        out = autofill.worth_saving(fields, saved, autofill.profile_facts(PROFILE), "Acme")
        self.assertEqual([(c["question"], c["status"]) for c in out],
                         [("Desired salary", "changed"), ("Will you require sponsorship?", "new")])


if __name__ == "__main__":
    unittest.main()
