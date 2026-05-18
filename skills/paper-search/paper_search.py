"""Search OpenAlex /works and verify a paper's existence.

Given bibliographic fields (DOI, title, keyword, authors, venue, year), query
OpenAlex and emit candidate records as JSON on stdout. Two use cases:

  * verification — detecting hallucinated / fabricated citations;
  * discovery   — keyword search to find papers.

This script only fetches raw records — the existence/match judgement is
left to Claude.

Usage:
    python3 paper_search.py --title "Hierarchical memorization in LLMs"
    python3 paper_search.py --doi 10.48550/arXiv.2511.08877 --abstract
    python3 paper_search.py --keyword "llm memorization" --year 2023 --n 10
    python3 paper_search.py --authors "Nicholas Carlini" --sort date --n 10

One of --doi, --title, --keyword or --authors is required. Depends only on
the Python
standard
library — no install step, no uv. OPENALEX_API_KEY is read from the
environment (or a .env file) if present; it is optional — without it the
request goes through OpenAlex's common pool.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://api.openalex.org"
TIMEOUT = 60.0


def ssl_context() -> ssl.SSLContext:
    """Build a TLS context.

    Some Python builds (notably the python.org macOS framework build) ship
    without a wired-up CA store, so the stdlib default context fails to
    verify api.openalex.org. If the `certifi` package happens to be
    installed we use its bundle; otherwise we fall back to the default. We
    never *require* certifi — keeping this script dependency-free.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def load_dotenv_if_present() -> None:
    """Minimal .env loader (KEY=VALUE lines), read from the current directory.

    Looks in the working directory rather than next to the script: as a
    bundled plugin the script lives in a cache dir, so the user's project
    root (cwd) is where a .env would sensibly live.
    """
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def get_api_key() -> str | None:
    load_dotenv_if_present()
    key = os.environ.get("OPENALEX_API_KEY")
    if not key:
        print(
            "Note: OPENALEX_API_KEY not found — querying OpenAlex via the "
            "common pool (no key required, but rate limits are stricter).",
            file=sys.stderr,
        )
    return key


def http_get_json(path: str, params: dict[str, str | int]) -> dict:
    """GET ``BASE_URL + path`` with query params and return parsed JSON.

    Raises urllib.error.HTTPError for non-2xx responses (callers handle 404).
    """
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    )
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "paper_search.py"})
    with urllib.request.urlopen(req, timeout=TIMEOUT,
                                context=ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def reconstruct_abstract(inv_index: dict | None) -> str | None:
    """Rebuild abstract text from OpenAlex `abstract_inverted_index`."""
    if not inv_index:
        return None
    positions: dict[int, str] = {}
    for word, pos_list in inv_index.items():
        for p in pos_list:
            positions[p] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))


def normalize_work(w: dict, *, include_abstract: bool) -> dict:
    primary = w.get("primary_location") or {}
    primary_src = primary.get("source") or {}
    authorships = w.get("authorships") or []
    author_names = [
        ((a.get("author") or {}).get("display_name") or "")
        for a in authorships
    ]
    record = {
        "openalex_id": w.get("id"),
        "doi": (w.get("ids") or {}).get("doi"),
        "title": w.get("title") or w.get("display_name"),
        "authors": author_names,
        "publication_year": w.get("publication_year"),
        "venue": primary_src.get("display_name"),
        "type": w.get("type"),
        "cited_by_count": w.get("cited_by_count"),
        "is_retracted": w.get("is_retracted"),
        "relevance_score": w.get("relevance_score"),
    }
    if include_abstract:
        record["abstract"] = reconstruct_abstract(
            w.get("abstract_inverted_index")
        )
    return record


def fetch_by_doi(doi: str, api_key: str | None) -> list[dict]:
    """Direct lookup by DOI. Returns [] on a clean 404."""
    doi = doi.strip()
    # Strip a URL/`doi:` prefix so the user can paste any common form.
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    params = {"api_key": api_key} if api_key else {}
    try:
        work = http_get_json(f"/works/doi:{doi}", params)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    return [work]


# Maps the user-facing --sort choice to an OpenAlex `sort` expression.
# `relevance` is only valid when a `search` query is present (OpenAlex
# rejects it otherwise), so main() guards that combination.
SORT_EXPR: dict[str, str] = {
    "date": "publication_date:desc",
    "citations": "cited_by_count:desc",
    "relevance": "relevance_score:desc",
}


def fetch_by_search(
    *,
    title: str | None,
    keyword: str | None,
    authors: list[str],
    venue: str | None,
    year: int | None,
    sort: str | None,
    n: int,
    api_key: str | None,
) -> list[dict]:
    filters: list[str] = []
    if title:
        filters.append(f"title.search:{title}")
    for name in authors:
        filters.append(f"raw_author_name.search:{name}")
    if venue:
        filters.append(f"primary_location.source.display_name.search:{venue}")
    if year is not None:
        filters.append(f"publication_year:{year}")
    params: dict[str, str | int] = {"per-page": n}
    if filters:
        params["filter"] = ",".join(filters)
    # `search` is OpenAlex's full-text query (title + abstract + fulltext),
    # distinct from the title-only `title.search` filter — used for keyword
    # discovery rather than verifying one known title.
    if keyword:
        params["search"] = keyword
    if sort:
        params["sort"] = SORT_EXPR[sort]
    if api_key:
        params["api_key"] = api_key
    data = http_get_json("/works", params)
    return (data.get("results") or [])[:n]


def main() -> None:
    p = argparse.ArgumentParser(
        description="Verify a paper's existence against OpenAlex."
    )
    p.add_argument("--doi", help="DOI; takes priority — direct lookup")
    p.add_argument("--title", help="paper title (title-only filter match)")
    p.add_argument("--keyword",
                   help="free-text query across title/abstract/fulltext "
                        "(for discovery / keyword search)")
    p.add_argument("--authors", action="append", default=[],
                   help="author name (repeatable, or comma-separated)")
    p.add_argument("--venue", help="journal / conference name")
    p.add_argument("--year", type=int, help="publication year")
    p.add_argument("--sort", choices=sorted(SORT_EXPR),
                   help="result order: date (newest first), citations (most "
                        "cited first), or relevance (needs --keyword). "
                        "Default: OpenAlex's own order")
    p.add_argument("--n", type=int, default=5,
                   help="number of candidates to return (default 5)")
    p.add_argument("--abstract", action="store_true",
                   help="include the reconstructed abstract in each candidate")
    args = p.parse_args()

    if not (args.doi or args.title or args.keyword or args.authors):
        p.error("at least one of --doi, --title, --keyword or --authors "
                "is required")
    if args.sort == "relevance" and not args.keyword:
        p.error("--sort relevance requires --keyword")

    # Allow either repeated --authors or a single comma-separated value.
    authors: list[str] = []
    for chunk in args.authors:
        authors.extend(a.strip() for a in chunk.split(",") if a.strip())

    api_key = get_api_key()
    strategy = "doi_exact" if args.doi else "search"
    works: list[dict] = []
    errors: list[str] = []
    try:
        if args.doi:
            works = fetch_by_doi(args.doi, api_key)
        else:
            works = fetch_by_search(
                title=args.title,
                keyword=args.keyword,
                authors=authors,
                venue=args.venue,
                year=args.year,
                sort=args.sort,
                n=args.n,
                api_key=api_key,
            )
    except urllib.error.HTTPError as e:
        errors = [f"OpenAlex request failed: {e.code} {e.url}"]
    except urllib.error.URLError as e:
        errors = [f"OpenAlex request error: {e.reason}"]

    candidates = [
        normalize_work(w, include_abstract=args.abstract) for w in works
    ]
    output = {
        "query": {
            "doi": args.doi,
            "title": args.title,
            "keyword": args.keyword,
            "authors": authors,
            "venue": args.venue,
            "year": args.year,
            "sort": args.sort,
        },
        "strategy": strategy,
        "n_requested": args.n,
        "n_returned": len(candidates),
        "candidates": candidates,
        "errors": errors,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
