"""
Fetches and caches pharma company pipeline pages, extracts drug/programme names,
and cross-references them against normalized trial records.

Adding a new company: add its name (lowercase) and pipeline URL to PIPELINE_URLS.
The scraper works on raw HTML (not JS-rendered) so drug names embedded in the
page source are found even on React/AEM sites.
"""

import re
import requests

import db

TIMEOUT = 15

PIPELINE_URLS: dict[str, str] = {
    "novo nordisk": "https://www.novonordisk.com/science-and-technology/r-d-pipeline.html",
    "eli lilly": "https://www.lilly.com/pipeline",
    "astrazeneca": "https://www.astrazeneca.com/our-therapy-areas/pipeline.html",
    "pfizer": "https://www.pfizer.com/science/drug-product-pipeline",
    "roche": "https://www.roche.com/innovation/pipeline/",
    "merck": "https://www.merck.com/research/pipeline/",
    "sanofi": "https://www.sanofi.com/en/innovation-and-science/pipeline",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Require ≥4 chars before the INN suffix so generic words ("kinase", "peptide") don't match
_DRUG_SUFFIX = re.compile(
    r"\b\w{4,}(?:mab|nib|tinib|zumab|glutide|gliptin|flozin|parin|mycin|cillin"
    r"|ikimab|ibart|ekimab|gamtide|setron|lukast|sartan|dipine|vastatin|oxetine)\b",
    re.I,
)
_DRUG_CODE = re.compile(r"\b[A-Z]{2,5}[-_]?\d{3,6}\b")

_BLOCKLIST = {"inhibitor", "receptor", "peptide", "protein", "antibody",
              "oligonucleotide", "analogue", "agonist", "antagonist"}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _extract_terms(raw_html: str) -> list[str]:
    """
    Extract drug/programme names directly from raw HTML via regex.
    Avoids HTML parsing entirely — drug names embedded in the source
    (AEM, React SSR, etc.) are captured without needing a JS runtime.
    """
    candidates: set[str] = set()

    # Match words with INN suffixes
    for m in _DRUG_SUFFIX.finditer(raw_html):
        word = m.group(0).strip("():/\\<>&;\"'")
        if 3 <= len(word) <= 50 and word.replace("-", "").isalnum():
            norm = _normalise(word)
            if norm not in _BLOCKLIST:
                candidates.add(norm)

    # Match drug codes like NN-1234 or ABC123
    for m in _DRUG_CODE.finditer(raw_html):
        word = m.group(0)
        if 4 <= len(word) <= 20:
            candidates.add(_normalise(word))

    return sorted(candidates)


def fetch_pipeline(company: str) -> list[str]:
    """
    Return list of drug/programme name strings from the company's pipeline page.
    Cached for 24 h in SQLite.  Returns [] (no raise) on any failure.
    """
    key = _normalise(company)
    cached = db.get_pipeline_cache(key)
    if cached is not None:
        return cached

    url = PIPELINE_URLS.get(key)
    if url is None:
        return []

    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        terms = _extract_terms(r.text)
    except Exception:
        terms = []

    if terms:
        db.set_pipeline_cache(key, terms)

    return terms


def cross_reference(trials: list[dict], company: str) -> list[dict]:
    """
    Annotate each trial dict with `pipeline_match`:
      None  — pipeline unavailable for this company
      []    — pipeline fetched, no intervention match found
      [str] — list of matching intervention names found on the pipeline page
    """
    pipeline = fetch_pipeline(company)

    for trial in trials:
        if not pipeline:
            trial["pipeline_match"] = None
            continue

        matches = []
        for iv in trial.get("interventions", []):
            iv_norm = _normalise(iv)
            if any(iv_norm in p or p in iv_norm for p in pipeline):
                matches.append(iv)
        trial["pipeline_match"] = matches

    return trials


if __name__ == "__main__":
    terms = fetch_pipeline("novo nordisk")
    print(f"Novo Nordisk pipeline terms ({len(terms)}):")
    for t in terms[:30]:
        print(" ", t)
