#the Streamlit demo, driven by Streamlit's own test runner with the pipeline faked out
import os
import unittest
from unittest import mock

from pipeline import GateStopped
from tests.test_stages import full_report

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    AppTest = None

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def fake_pipeline(result):
    class FakePipeline:
        def __init__(self, name, log = print):
            log("1. Validity gate")

        def run(self):
            if isinstance(result, Exception):
                raise result
            return result
    return FakePipeline


@unittest.skipIf(AppTest is None, "streamlit is not installed")
class AppTestCase(unittest.TestCase):
    def submit(self, result):
        with mock.patch("pipeline.Pipeline", fake_pipeline(result)):
            app = AppTest.from_file(APP, default_timeout = 30)
            app.run()
            app.text_input[0].input("Immanuel Kant")
            app.button[0].click()
            app.run()
        self.assertFalse(app.exception)
        return app

    def test_shows_the_report(self):
        report = full_report()
        report["usage"] = {"seconds": 12, "cost_usd": 0.31, "cap_usd": 5.0, "run_id": "test"}
        report["evaluation"] = {"passed": True, "failed": [], "rounds": 0, "stop_rule": "all criteria pass"}
        app = self.submit(report)
        self.assertIn("# Immanuel Kant\n", " ".join(block.value for block in app.markdown))
        self.assertEqual([button.label for button in app.button], ["Get the verdict", "Save as a Google Doc"])
        self.assertEqual(app.metric[0].value, "8.0 / 10")

    def test_ambiguous_name_is_a_warning(self):
        app = self.submit(GateStopped("ambiguous", "Two philosophers share the name.", ["Francis Bacon", "Roger Bacon"]))
        self.assertEqual(app.warning[0].value, "Two philosophers share the name.")
        self.assertEqual([button.label for button in app.button[1:]], ["Francis Bacon", "Roger Bacon"])


if __name__ == "__main__":
    unittest.main()
