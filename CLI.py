import os
import sys
import json
import argparse
import contextlib

#read .env before the other modules read their settings from the environment
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from pipeline import Pipeline, GateStopped
from state import RunStopped
from report import render_markdown, render_run_notes, compare
from google_doc import create_google_doc, GoogleDocsNotSetUp


#usage: python CLI.py "Immanuel Kant" [--no-doc] [--json] [--trace] [--versus "David Hume"]
#The essay and its sources go to a Google Doc; the run notes stay in the terminal.
def main():
    parser = argparse.ArgumentParser(description = "Philosopher Verdict: a sourced report and verdict on one philosopher.")
    parser.add_argument("name", nargs = "+", help = "the philosopher's name")
    parser.add_argument("--no-doc", action = "store_true", help = "print the essay in the terminal instead of making a Google Doc")
    parser.add_argument("--json", action = "store_true", help = "print the report as JSON")
    parser.add_argument("--trace", action = "store_true", help = "also save every call's inputs and outputs to runs/<run_id>/trace.jsonl")
    parser.add_argument("--versus", metavar = "NAME", help = "head-to-head: also report on NAME and compare the two")
    args = parser.parse_args()

    names = [" ".join(args.name)] + ([args.versus] if args.versus else [])
    pipelines, reports = [], []
    #with --json, progress goes to stderr so stdout holds only the JSON
    with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
        for name in names:
            try:
                pipelines.append(Pipeline(name, trace = args.trace))
                reports.append(pipelines[-1].run())
            except GateStopped as e:
                print(f"Stopped at the validity gate ({e.outcome}): {e.message}", file = sys.stderr)
                sys.exit(1)
            except RunStopped as e:  #the spend cap or deadline hit before research finished, so there is no report
                print(f"Stopped: {e}", file = sys.stderr)
                sys.exit(1)

    if args.json:
        print(json.dumps(reports[0] if len(reports) == 1 else reports, indent = 2, ensure_ascii = False))
        return
    for pipeline, report in zip(pipelines, reports):
        print("\n" + render_run_notes(report))
        if args.no_doc:
            print("\n" + render_markdown(report))
            continue
        try:
            url = create_google_doc(report)
        except GoogleDocsNotSetUp as e:
            print(f"\n{e}\nHere is the essay instead:\n\n" + render_markdown(report))
            continue
        except Exception as e:  #the report is finished and saved, so a failed upload shouldn't lose the essay
            print(f"\nCouldn't create the Google Doc ({type(e).__name__}: {e}). Here is the essay instead:\n\n" + render_markdown(report))
            continue
        print(f"  Google Doc: {url}")
        pipeline.state.decide("output", google_doc = url)
        pipeline.state.save()
    if len(reports) == 2:
        print("\n" + compare(*reports))


if __name__ == "__main__":
    main()
