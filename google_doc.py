import os
from datetime import date
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor

from report import numbered_sources, TITLES
from tools import page_title, is_allowed


#Google sign-in: see "Google Docs output" in the README. Both files are gitignored.
ROOT = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", os.path.join(ROOT, "google_credentials.json"))
TOKEN_FILE = os.path.join(ROOT, "google_token.json")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]  #only the documents this app creates, nothing else in your Drive

#where each allowed site's pages come from, for the Sources list
PUBLICATIONS = {
    "plato.stanford.edu": "Stanford Encyclopedia of Philosophy",
    "iep.utm.edu": "Internet Encyclopedia of Philosophy",
    "en.wikipedia.org": "Wikipedia",
    "en.wikisource.org": "Wikisource",
    "www.perseus.tufts.edu": "Perseus Digital Library",
    "www.earlymoderntexts.com": "Early Modern Texts",
    "philpapers.org": "PhilPapers",
}

#the design: one classical serif throughout, near-black text, and the Stanford cardinal as the only accent
SERIF = "EB Garamond"
INK = (0.12, 0.12, 0.12)
MUTED = (0.42, 0.40, 0.38)
CARDINAL = (0.55, 0.08, 0.08)


class GoogleDocsNotSetUp(Exception):
    pass


def pt(size):
    return {"magnitude": size, "unit": "PT"}


def colour(rgb):
    return {"color": {"rgbColor": {"red": rgb[0], "green": rgb[1], "blue": rgb[2]}}}


def font(size, weight = 400, rgb = INK, **more):
    return {"weightedFontFamily": {"fontFamily": SERIF, "weight": weight}, "fontSize": pt(size), "foregroundColor": colour(rgb), **more}


def rule(rgb, width):
    return {"color": colour(rgb), "width": pt(width), "padding": pt(6), "dashStyle": "SOLID"}


#text styles
BODY = font(12.5)
TITLE = font(30, 500)
BYLINE = font(10.5, rgb = MUTED, smallCaps = True)
DEK = font(13, rgb = MUTED, italic = True)
LABEL = font(10.5, 600, MUTED, smallCaps = True)
CONTENTS = font(11.5)
NUMBER = font(17, 600, CARDINAL)
HEADING = font(17, 600)
STATEMENT = font(13.5, italic = True)
LEAD = font(13.5)
CITE = font(9, 600, CARDINAL, baselineOffset = "SUPERSCRIPT")
SCORE = font(12.5, 700, CARDINAL)
BIG_SCORE = font(24, 600, CARDINAL)
SMALL = font(10.5, rgb = MUTED)
EXCERPT = font(10.5, rgb = MUTED, italic = True)

#paragraph styles
P_BODY = {"lineSpacing": 132, "spaceBelow": pt(9)}
P_TITLE = {"namedStyleType": "TITLE", "spaceBelow": pt(2)}
P_BYLINE = {"spaceBelow": pt(14), "borderBottom": rule(CARDINAL, 1)}
P_DEK = {"lineSpacing": 120, "spaceBelow": pt(18)}
P_LABEL = {"spaceAbove": pt(4), "spaceBelow": pt(4), "keepWithNext": True}
P_CONTENTS = {"lineSpacing": 115, "spaceBelow": pt(1), "indentStart": pt(12), "indentFirstLine": pt(12)}
P_CONTENTS_LAST = {**P_CONTENTS, "spaceBelow": pt(18), "borderBottom": rule(MUTED, 0.5)}
P_HEADING = {"namedStyleType": "HEADING_1", "spaceAbove": pt(22), "spaceBelow": pt(8), "keepWithNext": True}
P_STATEMENT = {"lineSpacing": 128, "spaceAbove": pt(2), "spaceBelow": pt(12), "indentStart": pt(26), "indentFirstLine": pt(26),
               "indentEnd": pt(18), "borderLeft": rule(CARDINAL, 2)}
P_PREMISE = {"lineSpacing": 128, "spaceBelow": pt(4)}
P_SCORE = {"spaceAbove": pt(6), "spaceBelow": pt(2), "keepWithNext": True}
P_JUSTIFICATION = {"lineSpacing": 128, "spaceBelow": pt(8), "indentStart": pt(14), "indentFirstLine": pt(14)}
P_WEIGHTED = {"spaceAbove": pt(8), "spaceBelow": pt(4), "borderTop": rule(MUTED, 0.5)}
P_SOURCE = {"lineSpacing": 120, "spaceAbove": pt(6), "spaceBelow": pt(2), "indentStart": pt(22), "indentFirstLine": pt(0)}
P_EXCERPT = {"lineSpacing": 118, "spaceBelow": pt(2), "indentStart": pt(22), "indentFirstLine": pt(22)}


#Google Docs counts positions in UTF-16 code units, so a character outside the Basic Multilingual Plane counts twice
def units(text):
    return len(text.encode("utf-16-le")) // 2


#builds a document as one block of text plus the styles to apply to its ranges
class DocWriter:
    def __init__(self):
        self.pieces = []
        self.at = 1          #the body's text starts at index 1
        self.paragraphs = []
        self.runs = []
        self.lists = []
        self.links = []      #(start, end, heading number): contents entries, linked to headings once they exist

    #one paragraph made of runs; a run is (text, text style) or (text, text style, link url).
    #Returns the paragraph's range, without its newline.
    def paragraph(self, runs, style):
        start = self.at
        for run in runs:
            text, run_style = run[0], dict(run[1])
            if len(run) > 2 and run[2]:
                run_style["link"] = {"url": run[2]}
            if text:
                self.runs.append((self.at, self.at + units(text), run_style))
                self.pieces.append(text)
                self.at += units(text)
        if self.at == start:
            raise ValueError("A paragraph needs some text.")  #an empty range can't be styled
        self.paragraphs.append((start, self.at, style))
        self.pieces.append("\n")
        self.at += 1
        return start, self.at - 1

    def text(self):
        return "".join(self.pieces)[:-1]  #a new document already ends with a newline

    def requests(self):
        text = self.text()
        requests = [
            {"updateDocumentStyle": {"documentStyle": {"marginTop": pt(72), "marginBottom": pt(72), "marginLeft": pt(84), "marginRight": pt(84)},
                                     "fields": "marginTop,marginBottom,marginLeft,marginRight"}},
            {"insertText": {"location": {"index": 1}, "text": text}},
        ]
        #paragraph styles (including heading types) first, then text styles, so the heading types can't override the fonts
        for start, end, style in self.paragraphs:
            requests.append({"updateParagraphStyle": {"range": {"startIndex": start, "endIndex": end}, "paragraphStyle": style,
                                                      "fields": ",".join(style)}})
        requests.append({"updateTextStyle": {"range": {"startIndex": 1, "endIndex": 1 + units(text)}, "textStyle": BODY,
                                             "fields": ",".join(BODY)}})
        for start, end, style in self.runs:
            if style != BODY:
                requests.append({"updateTextStyle": {"range": {"startIndex": start, "endIndex": end}, "textStyle": style,
                                                     "fields": ",".join(style)}})
        for start, end in self.lists:
            requests.append({"createParagraphBullets": {"range": {"startIndex": start, "endIndex": end},
                                                        "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN"}})
        return requests


#a source page's title and where it was published, from its <title> ("Wiredu, Kwasi | Internet Encyclopedia of Philosophy")
def title_and_publication(url, raw_title):
    host = (urlparse(url).hostname or "").lower()
    publication = PUBLICATIONS.get(host, host)
    title = raw_title or unquote(urlparse(url).path.rstrip("/").split("/")[-1]).replace("_", " ").replace("-", " ").capitalize()
    title = title.replace(f" ({publication})", "")
    for separator in (" | ", " - ", " \u2014 ", " \u2013 "):
        if separator in title:
            head, tail = title.rsplit(separator, 1)
            if publication.split()[0].lower() in tail.lower():
                title = head
    return title.strip() or url, publication


def short_url(url):
    return url.split("://", 1)[-1].rstrip("/")


def capitalised(text):
    return text[:1].upper() + text[1:]


#the essay and its sources, laid out like a Stanford Encyclopedia entry; titles maps each page url to its <title>
def build_document(report, titles, published = None):
    pages, numbers = numbered_sources(report)
    page_by_number = {page["number"]: page for page in pages}
    theory, panel = report["key_theory"], report.get("panel") or {"scores": []}
    published = published or date.today()
    doc = DocWriter()

    #superscript source numbers, each linked to its page
    def cites(ids):
        found = sorted({numbers[i] for i in ids if i in numbers})
        runs = []
        for k, number in enumerate(found):
            runs += ([(",", CITE)] if k else []) + [(str(number), CITE, page_by_number[number]["url"])]
        return runs

    #a numbered section heading; the number is in the accent colour
    def heading(number, title):
        doc.paragraph([(f"{number}.\u2002" if number else "", NUMBER), (title, HEADING)], P_HEADING)

    theory_title = f"Key Theory: {capitalised(theory['name'])}"
    sections = ["Life", theory_title, TITLES["case_for"].title(), TITLES["case_against"].title(), "Panel Scores", "Verdict"]

    doc.paragraph([(report["philosopher"], TITLE)], P_TITLE)
    doc.paragraph([(f"Philosopher Verdict \u00b7 First published {published.day} {published:%B %Y}", BYLINE)], P_BYLINE)
    if panel["scores"]:
        doc.paragraph([(f"Key theory: {capitalised(theory['name'])}\u2003\u00b7\u2003Panel score {panel['weighted_score']} / 10", DEK)], P_DEK)

    doc.paragraph([("Entry Contents", LABEL)], P_LABEL)
    for number, title in enumerate(sections + ["Sources"], 1):
        label = f"{number}. {title}" if number <= len(sections) else title
        start, end = doc.paragraph([(label, CONTENTS)], P_CONTENTS_LAST if title == "Sources" else P_CONTENTS)
        doc.links.append((start, end, number))

    heading(1, "Life")
    doc.paragraph([(report["life"]["text"], BODY)] + cites(report["life"]["sources"]), P_BODY)

    heading(2, theory_title)
    doc.paragraph([(theory["statement"], STATEMENT)] + cites(theory["sources"]), P_STATEMENT)
    doc.paragraph([("Why this theory.\u2002", LABEL), (theory["why_chosen"], BODY)], P_BODY)

    for number, section in ((3, "case_for"), (4, "case_against")):
        argument = report[section]
        heading(number, sections[number - 1])
        if not argument["claim"]:
            doc.paragraph([("Not written.", DEK)], P_BODY)
            continue
        doc.paragraph([(argument["claim"], LEAD)], P_BODY)
        premises = [doc.paragraph([(premise, BODY)], P_PREMISE) for premise in argument["premises"] if premise.strip()]
        if premises:
            doc.lists.append((premises[0][0], premises[-1][1]))
        doc.paragraph([("Therefore,\u2002", LABEL), (argument["conclusion"], BODY)] + cites(argument["sources"]), {**P_BODY, "spaceAbove": pt(6)})
        if argument["proponents"]:
            names = argument["proponents"]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            doc.paragraph([("Defended by\u2002", LABEL), (listed, font(12.5, italic = True))], P_BODY)

    heading(5, "Panel Scores")
    if not panel["scores"]:
        doc.paragraph([("Not scored.", DEK)], P_BODY)
    else:
        doc.paragraph([("Five judges each scored one criterion from 1 to 10. The weighted score is computed from their scores.", DEK)], P_BODY)
        for score in panel["scores"]:
            doc.paragraph([(score["criterion"].title() + "\u2003", LABEL), (str(score["score"]), SCORE),
                           (f" / 10\u2003weight {score['weight']:.0%}", SMALL)], P_SCORE)
            doc.paragraph([(score["justification"], BODY)] + cites(score["sources"]), P_JUSTIFICATION)
        doc.paragraph([("Weighted score\u2003", LABEL), (str(panel["weighted_score"]), BIG_SCORE), (" / 10", font(14, rgb = MUTED))], P_WEIGHTED)

    heading(6, "Verdict")
    verdict = (report.get("verdict") or {}).get("text") or "Not written."
    for part in [part.strip() for part in verdict.split("\n") if part.strip()]:
        doc.paragraph([(part, BODY)], P_BODY)

    heading(None, "Sources")
    for page in pages:
        title, publication = title_and_publication(page["url"], titles.get(page["url"]))
        stop = "" if title[-1:] in ".?!" else "."
        doc.paragraph([(f"{page['number']}.\u2002", font(12, 600, CARDINAL)), (f"\u201c{title}{stop}\u201d ", font(12)),
                       (publication + ". ", font(12, italic = True)), (short_url(page["url"]), SMALL, page["url"])], P_SOURCE)
        for excerpt in page["excerpts"]:
            doc.paragraph([(f"\u201c{excerpt}\u201d", EXCERPT)], P_EXCERPT)
    return doc


#fetch every cited page's <title> at once; the urls passed the allowlist when they were cited
def source_titles(report):
    pages, _ = numbered_sources(report)
    urls = [page["url"] for page in pages]
    allowed = sorted({(urlparse(url).hostname or "").lower() for url in urls if (urlparse(url).hostname or "").lower() in PUBLICATIONS})
    with ThreadPoolExecutor(max_workers = max(1, len(urls))) as pool:
        found = list(pool.map(lambda url: page_title(url, allowed) if is_allowed(url, allowed) else None, urls))
    return dict(zip(urls, found))


#a Docs API client, signed in as you; the first time, a browser opens to ask for permission
def docs_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise GoogleDocsNotSetUp("The Google client libraries aren't installed: run pip install -r requirements.txt")

    credentials = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES) if os.path.exists(TOKEN_FILE) else None
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        elif not os.path.exists(CREDENTIALS_FILE):
            raise GoogleDocsNotSetUp(f"Google Docs isn't set up: {CREDENTIALS_FILE} is missing. See \"Google Docs output\" in the README.")
        else:
            credentials = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES).run_local_server(port = 0)
        with open(TOKEN_FILE, "w") as f:
            f.write(credentials.to_json())
        os.chmod(TOKEN_FILE, 0o600)  #the token lets this app create documents as you, so keep it private
    return build("docs", "v1", credentials = credentials, cache_discovery = False)


#create the Google Doc for a report and return its link
def create_google_doc(report, service = None):
    service = service or docs_service()
    doc = build_document(report, source_titles(report))
    document_id = service.documents().create(body = {"title": f"{report['philosopher']} \u00b7 Philosopher Verdict"}).execute()["documentId"]
    service.documents().batchUpdate(documentId = document_id, body = {"requests": doc.requests()}).execute()

    #link the contents entries to their headings, now that the headings have ids; the document is complete without this
    from googleapiclient.errors import HttpError
    try:
        body = service.documents().get(documentId = document_id).execute()["body"]["content"]
        styles = [element["paragraph"]["paragraphStyle"] for element in body if "paragraph" in element]
        heading_ids = [style["headingId"] for style in styles if style.get("namedStyleType") == "HEADING_1" and style.get("headingId")]
        if len(heading_ids) == len(doc.links):
            requests = [{"updateTextStyle": {"range": {"startIndex": start, "endIndex": end},
                                             "textStyle": {"link": {"headingId": heading_ids[number - 1]}}, "fields": "link"}}
                        for start, end, number in doc.links]
            service.documents().batchUpdate(documentId = document_id, body = {"requests": requests}).execute()
    except (HttpError, KeyError):
        pass
    return f"https://docs.google.com/document/d/{document_id}/edit"
