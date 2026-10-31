import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin, quote

import requests


#guardrails (see the README's Guardrails table)
TOOL_TIMEOUT_S = float(os.getenv("TOOL_TIMEOUT_S", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
MAX_PAGE_CHARS = 15000       #longest page text handed to a model (about 4,000 tokens)
MAX_REDIRECTS = 5
PASSAGE_CHARS = 1500         #text kept either side of each match when fetch_page is given a phrase to find
RESULTS_PER_SITE = 3
HEADERS = {"User-Agent": "Philosopher-Verdict/0.1 (contact: you@example.com)"}


#a search or fetch the tools refused or couldn't finish; the message is shown to the model
class ToolError(Exception):
    pass


#send a request, retrying rate limits, server errors and dropped connections with backoff
def request_with_retries(method, url, **kwargs):
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    for attempt in range(MAX_RETRIES + 1):
        last_try = attempt == MAX_RETRIES
        try:
            response = requests.request(method, url, headers = headers, timeout = TOOL_TIMEOUT_S, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if last_try:
                raise
        else:
            if last_try or (response.status_code != 429 and response.status_code < 500):
                return response
        time.sleep(2 ** attempt)  #wait 1s, then 2s


#read a response as text; pages that don't name a charset are decoded as UTF-8, not requests' Latin-1 default
def decoded_text(response):
    if "charset=" not in response.headers.get("Content-Type", "").lower():
        response.encoding = "utf-8"
    return response.text


#check a url is http(s) on one of the allowed domains (exact host match, no custom ports)
def is_allowed(url, allowlist):
    try:
        parts = urlparse(url)
        port = parts.port
    except ValueError:
        return False
    allowed = {domain.lower() for domain in allowlist}
    return parts.scheme in ("http", "https") and port is None and (parts.hostname or "") in allowed


#remove HTML tags and entities from a short string such as a title or snippet
def strip_tags(text):
    return " ".join(unescape(re.sub(r"<[^>]*>", "", text or "")).split())


#turns a page's HTML into plain text, dropping scripts, styles, menus and comments
class PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "header", "nav", "footer", "form", "iframe"}
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs = True)
        self.parts = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)

    #handle_comment isn't overridden, so HTML comments are dropped

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def page_text(html):
    parser = PageText()
    parser.feed(html)
    parser.close()
    return parser.text()


#the passages of a page around each mention of a phrase, up to MAX_PAGE_CHARS in total
def matching_passages(text, find):
    windows = []
    for match in re.finditer(re.escape(find), text, re.IGNORECASE):
        start, end = max(0, match.start() - PASSAGE_CHARS), min(len(text), match.end() + PASSAGE_CHARS)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = end  #overlaps the previous passage, so join them
        else:
            windows.append([start, end])
    if not windows:
        return f'["{find}" does not appear on this page.]'

    passages, used = [], 0
    for start, end in windows:
        end = min(end, start + MAX_PAGE_CHARS - used)
        if end <= start:
            break
        passages.append(text[start:end])
        used += end - start
    return "\n...\n".join(passages) + f'\n[Showing {len(passages)} of {len(windows)} passages that mention "{find}".]'


#fetch a page, following redirects by hand so every hop is checked against the allowlist.
#Returns the final url and the response.
def get_page(url, allowlist):
    for hop in range(MAX_REDIRECTS + 1):
        if not is_allowed(url, allowlist):
            raise ToolError(f"Refused: {url} is not on this route's allowlist ({', '.join(allowlist)}).")
        #follow redirects by hand so every hop is checked against the allowlist
        response = request_with_retries("GET", url, allow_redirects = False)
        if not response.is_redirect:
            break
        try:
            url = urljoin(url, response.headers["Location"])
        except ValueError:
            raise ToolError(f"{url} redirected to a malformed address.")
    else:
        raise ToolError(f"Gave up after {MAX_REDIRECTS} redirects.")

    if response.status_code != 200:
        raise ToolError(f"{url} returned HTTP {response.status_code}.")
    return url, response


#read one whole page as plain text; refuses any url, including redirects, outside the allowlist.
#Returns the final url (after any redirects) and the page's text.
def read_page(url, allowlist):
    url, response = get_page(url, allowlist)
    content_type = response.headers.get("Content-Type", "").lower()
    if "html" in content_type:
        text = page_text(decoded_text(response))
    elif "text/plain" in content_type:
        text = decoded_text(response)
    else:
        raise ToolError(f"{url} is not a web page ({content_type or 'unknown type'}).")
    return url, text


#a page's <title>, or None if the page can't be read or has none
def page_title(url, allowlist):
    try:
        url, response = get_page(url, allowlist)
    except (ToolError, requests.RequestException):
        return None
    if "html" not in response.headers.get("Content-Type", "").lower():
        return None
    title = re.search(r"<title[^>]*>(.*?)</title>", decoded_text(response), re.S | re.I)
    if not title:
        return None
    return strip_tags(title.group(1)) or None


#stop fetched text from faking the start or end of the tags it is wrapped in
def neutralise(text):
    return re.sub(r"<(\s*/?\s*)(retrieved_document|search_results)", r"&lt;\1\2", text, flags = re.IGNORECASE)


#wrap a page's text in <retrieved_document> tags, so prompts can say it is material, not instructions
def wrap_document(url, text):
    return f'<retrieved_document url="{escape(url)}">\n{neutralise(text)}\n</retrieved_document>'


#read one page, or with find, the passages that mention a phrase
def fetch_page(url, allowlist, find = ""):
    url, text = read_page(url, allowlist)
    if find.strip():
        text = matching_passages(text, find.strip())
    elif len(text) > MAX_PAGE_CHARS:
        text = text[:MAX_PAGE_CHARS] + (
            f"\n[Page cut off after {MAX_PAGE_CHARS} of {len(text)} characters. "
            "To read a later part, fetch it again with find set to a name or phrase from that part.]"
        )
    return {
        "url": url,  #the final url, after any redirects
        "text": wrap_document(url, text),
    }


#search the Stanford Encyclopedia of Philosophy's own search page
def search_sep(query):
    response = request_with_retries("GET", "https://plato.stanford.edu/search/searcher.py", params = {"query": query})
    response.raise_for_status()
    results = []
    for listing in re.findall(r'<div class="result_listing">(.*?)<!-- end result_listing -->', decoded_text(response), re.S):
        entry = re.search(r"entry=(/entries/[^&\"]+)", listing)
        title = re.search(r'<div class="result_title">\s*<a[^>]*>(.*?)</a>', listing, re.S)
        snippet = re.search(r'<div class="result_snippet">(.*?)</div>', listing, re.S)
        if entry and title:
            results.append({
                "title": title.group(1),
                "url": "https://plato.stanford.edu" + entry.group(1),
                "snippet": snippet.group(1) if snippet else "",
            })
    return results[:RESULTS_PER_SITE]


#search the Internet Encyclopedia of Philosophy (a WordPress site with a JSON search API)
def search_iep(query):
    response = request_with_retries("GET", "https://iep.utm.edu/wp-json/wp/v2/search", params = {
        "search": query,
        "per_page": RESULTS_PER_SITE,
    })
    response.raise_for_status()
    return [{"title": hit["title"], "url": hit["url"], "snippet": ""} for hit in response.json()]


#search a MediaWiki site such as English Wikipedia or Wikisource
def search_mediawiki(host, query):
    response = request_with_retries("GET", f"https://{host}/w/api.php", params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": RESULTS_PER_SITE,
        "format": "json",
    })
    response.raise_for_status()
    return [
        {
            "title": hit["title"],
            "url": f"https://{host}/wiki/" + quote(hit["title"].replace(" ", "_")),
            "snippet": hit.get("snippet", ""),
        }
        for hit in response.json()["query"]["search"]
    ]


def search_wikipedia(query):
    return search_mediawiki("en.wikipedia.org", query)


#primary texts in translation
def search_wikisource(query):
    return search_mediawiki("en.wikisource.org", query)


#free search: ask each allowed site that has a search we can use, and skip the rest
FREE_SEARCHES = {
    "plato.stanford.edu": search_sep,
    "iep.utm.edu": search_iep,
    "en.wikipedia.org": search_wikipedia,
    "en.wikisource.org": search_wikisource,
}


#one site's results, or the reason it failed
def search_site(domain, query):
    try:
        return FREE_SEARCHES[domain](query), None
    except (requests.RequestException, KeyError, ValueError) as e:
        return [], f"{domain}: {e}"


#the sites are asked at the same time; results keep the order of FREE_SEARCHES
def search_free(query, allowlist):
    domains = [domain for domain in FREE_SEARCHES if domain in allowlist]
    with ThreadPoolExecutor(max_workers = max(1, len(domains))) as pool:
        answers = list(pool.map(lambda domain: search_site(domain, query), domains))
    results = [hit for hits, failure in answers for hit in hits]
    failures = [failure for hits, failure in answers if failure]
    if failures and not results:
        raise ToolError("Search failed on every site: " + "; ".join(failures))
    return results


def search_tavily(query, allowlist, api_key):
    response = request_with_retries("POST", "https://api.tavily.com/search",
        headers = {"Authorization": f"Bearer {api_key}"},
        json = {"query": query, "max_results": 8, "include_domains": list(allowlist)},
    )
    response.raise_for_status()
    return [
        {"title": hit.get("title", ""), "url": hit["url"], "snippet": hit.get("content", "")}
        for hit in response.json().get("results", [])
    ]


def search_brave(query, allowlist, api_key):
    sites = " OR ".join(f"site:{domain}" for domain in allowlist)
    response = request_with_retries("GET", "https://api.search.brave.com/res/v1/web/search",
        headers = {"X-Subscription-Token": api_key, "Accept": "application/json"},
        params = {"q": f"{query} ({sites})", "count": 8},
    )
    response.raise_for_status()
    return [
        {"title": hit.get("title", ""), "url": hit["url"], "snippet": hit.get("description", "")}
        for hit in response.json().get("web", {}).get("results", [])
    ]


#search the route's allowed sites; SEARCH_PROVIDER picks tavily, brave, or free (the default, no key needed)
def search(query, allowlist):
    query = query.strip()
    if not query or len(query) > 200:
        raise ToolError("The query must be between 1 and 200 characters.")

    provider = os.getenv("SEARCH_PROVIDER", "free").strip().lower()
    api_key = os.getenv("SEARCH_API_KEY", "")
    if provider in ("tavily", "brave") and not api_key:
        raise ValueError(f"SEARCH_PROVIDER is {provider} but SEARCH_API_KEY is not set.")

    try:
        if provider == "tavily":
            results = search_tavily(query, allowlist, api_key)
        elif provider == "brave":
            results = search_brave(query, allowlist, api_key)
        elif provider == "free":
            results = search_free(query, allowlist)
        else:
            raise ValueError(f"Unknown SEARCH_PROVIDER '{provider}'. Use tavily, brave or free.")
    except (requests.RequestException, KeyError) as e:
        raise ToolError(f"Search failed: {e}")

    #only return links fetch_page will open, with any HTML stripped out of the text
    return [
        {"title": strip_tags(hit["title"]), "url": hit["url"], "snippet": strip_tags(hit["snippet"])[:300]}
        for hit in results
        if is_allowed(hit["url"], allowlist)
    ]
