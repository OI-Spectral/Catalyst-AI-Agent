#the Google Doc: its layout, its index arithmetic and the calls it makes, against a fake Docs service (no network)
import io
import os
import tempfile
import unittest
import contextlib
from datetime import date
from unittest import mock

import google_doc
from google_doc import build_document, create_google_doc, title_and_publication, units
from tests.test_stages import full_report

KANT = "https://plato.stanford.edu/entries/kant/"
TITLES = {KANT: "Immanuel Kant (Stanford Encyclopedia of Philosophy)"}


def finished_report():
    report = full_report()
    report["warnings"] = ["Dropped F3: never fetched."]
    report["evaluation"] = {"passed": True, "failed": [], "criteria": {}, "spot_checks": [], "rounds": 1, "stop_rule": "all criteria pass"}
    report["usage"] = {"cost_usd": 0.42, "cap_usd": 5.0, "seconds": 120, "run_id": "20261030-kant"}
    return report


#the text between two Docs indices, counted in UTF-16 units from index 1
def between(text, start, end):
    return text.encode("utf-16-le")[(start - 1) * 2:(end - 1) * 2].decode("utf-16-le")


class LayoutTest(unittest.TestCase):
    def test_essay_and_sources_only(self):
        doc = build_document(finished_report(), TITLES, date(2026, 10, 30))
        text = doc.text()
        for expected in ("Immanuel Kant\nPhilosopher Verdict · First published 30 October 2026", "Entry Contents",
                         "1. Life", "2. Key Theory: CI", "3. The Case For", "5. Panel Scores", "6. Verdict",
                         "Weighted score 8.0 / 10", "1. “Immanuel Kant.” Stanford Encyclopedia of Philosophy. plato.stanford.edu/entries/kant"):
            self.assertIn(expected, text)
        for run_note in ("Dropped F3", "early_modern", "0.42", "Run notes", "all criteria pass", "20261030-kant"):
            self.assertNotIn(run_note, text)

    def test_every_range_fits_and_lands_on_its_text(self):
        report = finished_report()
        report["life"]["text"] = "Born in Königsberg \U0001d50e (two UTF-16 units) in 1724."
        doc = build_document(report, TITLES)
        text, requests = doc.text(), doc.requests()
        end = 1 + units(text)
        for request in requests:
            for body in request.values():
                if "range" in body:
                    self.assertTrue(1 <= body["range"]["startIndex"] < body["range"]["endIndex"] <= end, request)
        self.assertEqual(between(text, *doc.runs[0][:2]), "Immanuel Kant")
        cites = [(between(text, start, stop), style) for start, stop, style in doc.runs if style.get("baselineOffset") == "SUPERSCRIPT"]
        self.assertTrue(cites)
        for label, style in cites:  #every superscript is a source number linked to its page, or the comma between two
            self.assertTrue(label == "," or (label.isdigit() and style["link"]["url"] == KANT), label)
        self.assertEqual(between(text, *doc.runs[-1][:2]), "“con quote”")  #after the astral character, still exact
        self.assertEqual(requests[1]["insertText"]["text"], text)
        self.assertFalse(text.endswith("\n"))  #the new document's own final newline ends the last paragraph

    def test_premises_become_a_numbered_list(self):
        doc = build_document(finished_report(), TITLES)
        lists = [r["createParagraphBullets"] for r in doc.requests() if "createParagraphBullets" in r]
        self.assertEqual(len(lists), 2)
        self.assertEqual(between(doc.text(), lists[0]["range"]["startIndex"], lists[0]["range"]["endIndex"]), "p1\np2")

    def test_a_report_that_stopped_early(self):
        report = finished_report()
        del report["panel"], report["verdict"]
        report["case_against"].update(claim = "", premises = [], conclusion = "", proponents = [], sources = [])
        text = build_document(report, TITLES).text()
        self.assertIn("4. The Case Against\nNot written.", text)
        self.assertIn("5. Panel Scores\nNot scored.", text)


class SourceTitleTest(unittest.TestCase):
    def test_titles_and_publications(self):
        for url, raw, expected in (
            (KANT, "Immanuel Kant (Stanford Encyclopedia of Philosophy)", ("Immanuel Kant", "Stanford Encyclopedia of Philosophy")),
            ("https://iep.utm.edu/wiredu/", "Wiredu, Kwasi | Internet Encyclopedia of Philosophy", ("Wiredu, Kwasi", "Internet Encyclopedia of Philosophy")),
            ("https://en.wikipedia.org/wiki/Zera_Yacob_%28philosopher%29", "Zera Yacob (philosopher) - Wikipedia", ("Zera Yacob (philosopher)", "Wikipedia")),
            ("https://en.wikisource.org/wiki/Analects", "Analects - Wikisource, the free online library", ("Analects", "Wikisource")),
            ("https://en.wikipedia.org/wiki/Zera_Yacob_%28philosopher%29", None, ("Zera yacob (philosopher)", "Wikipedia")),
            ("https://iep.utm.edu/x/", "Ends with a question?", ("Ends with a question?", "Internet Encyclopedia of Philosophy")),
        ):
            self.assertEqual(title_and_publication(url, raw), expected)

    def test_only_allowed_pages_are_fetched_for_titles(self):
        report = finished_report()
        report["sources"][0]["url"] = "https://evil.com/x"
        asked = []
        with mock.patch("google_doc.page_title", lambda url, allowed: asked.append(url) or "T"):
            titles = google_doc.source_titles(report)
        self.assertNotIn("https://evil.com/x", asked)
        self.assertIsNone(titles["https://evil.com/x"])


class Executes:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeDocs:
    def __init__(self, heading_ids):
        self.calls = []
        self.content = [{"paragraph": {"paragraphStyle": {"namedStyleType": "TITLE", "headingId": "h.title"}}}] + [
            {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_1", "headingId": h}}} for h in heading_ids]

    def documents(self):
        return self

    def create(self, body):
        self.calls.append(("create", body))
        return Executes({"documentId": "doc-1"})

    def batchUpdate(self, documentId, body):
        self.calls.append(("batchUpdate", body))
        return Executes({})

    def get(self, documentId):
        return Executes({"body": {"content": self.content}})


class CreateTest(unittest.TestCase):
    def test_creates_styles_and_links_the_contents(self):
        fake = FakeDocs([f"h.{n}" for n in range(1, 8)])
        with mock.patch("google_doc.page_title", lambda url, allowed: TITLES.get(url)):
            url = create_google_doc(finished_report(), fake)
        self.assertEqual(url, "https://docs.google.com/document/d/doc-1/edit")
        self.assertEqual(fake.calls[0], ("create", {"title": "Immanuel Kant · Philosopher Verdict"}))
        links = [r["updateTextStyle"]["textStyle"]["link"]["headingId"] for r in fake.calls[2][1]["requests"]]
        self.assertEqual(links, [f"h.{n}" for n in range(1, 8)])  #the title's heading id is not one of them

    def test_skips_the_contents_links_if_the_headings_dont_match(self):
        fake = FakeDocs(["h.1"])
        with mock.patch("google_doc.page_title", lambda url, allowed: None):
            create_google_doc(finished_report(), fake)
        self.assertEqual([call[0] for call in fake.calls], ["create", "batchUpdate"])

    def test_needs_credentials(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("google_doc.CREDENTIALS_FILE", os.path.join(folder, "missing.json")), \
                mock.patch("google_doc.TOKEN_FILE", os.path.join(folder, "token.json")):
            with self.assertRaises(google_doc.GoogleDocsNotSetUp) as caught:
                google_doc.docs_service()
        self.assertIn("Google Docs output", str(caught.exception))


class CliTest(unittest.TestCase):
    def run_cli(self, argv, make_doc):
        import CLI
        report = finished_report()

        class FakePipeline:
            def __init__(self, name, trace = False):
                self.state = mock.Mock()

            def run(self):
                return report

        out = io.StringIO()
        with mock.patch("CLI.Pipeline", FakePipeline), mock.patch("CLI.create_google_doc", make_doc), \
                mock.patch("sys.argv", ["CLI.py"] + argv), contextlib.redirect_stdout(out):
            CLI.main()
        return out.getvalue()

    def test_the_essay_goes_to_the_doc_and_the_notes_to_the_terminal(self):
        out = self.run_cli(["Kant"], lambda report: "https://docs.google.com/document/d/doc-1/edit")
        self.assertIn("Run notes", out)
        self.assertIn("Dropped F3", out)
        self.assertIn("Google Doc: https://docs.google.com/document/d/doc-1/edit", out)
        self.assertNotIn("## 1. Life", out)

    def test_without_google_the_essay_prints_instead(self):
        def not_set_up(report):
            raise google_doc.GoogleDocsNotSetUp("Google Docs isn't set up.")
        out = self.run_cli(["Kant"], not_set_up)
        self.assertIn("Google Docs isn't set up.", out)
        self.assertIn("## 1. Life", out)
        self.assertIn("## 1. Life", self.run_cli(["Kant", "--no-doc"], None))


if __name__ == "__main__":
    unittest.main()
