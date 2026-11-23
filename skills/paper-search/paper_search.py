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
    python3 paper_search.py --authors "Nicholas Carlini" --type article,preprint,book-chapter --sort date

One of --doi, --title, --keyword or --authors is required. Depends only on
the Python
standard
library — no install step, no uv. OPENALEX_API_KEY and OPENALEX_API_KEY<N>
(N = 1, 2, …) are read from the environment (or a .env file) if present and
tried in that order, moving to the next key on 401/403/429; they are
optional — without any, the request goes through OpenAlex's common pool.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://api.openalex.org"
TIMEOUT = 60.0

# OPENALEX_API_KEY, OPENALEX_API_KEY1, OPENALEX_API_KEY2, ...
KEY_ENV_RE = re.compile(r"OPENALEX_API_KEY(\d*)")
# Statuses that depend on the key (invalid / not activated / quota or rate
# limit hit) — worth retrying with the next key. Anything else (400, 5xx, …)
# would fail the same way with every key, so we stop there.
ROTATE_ON = {401, 403, 429}


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


def get_api_keys() -> list[tuple[str, str]]:
    """Collect ``(env var name, key)`` pairs to try, in order.

    Picks up ``OPENALEX_API_KEY`` first, then ``OPENALEX_API_KEY<N>`` in
    ascending N, from the environment or a ``.env`` file. Empty values are
    skipped and duplicate keys are tried only once.
    """
    load_dotenv_if_present()
    found: list[tuple[int, str, str]] = []
    for name, value in os.environ.items():
        m = KEY_ENV_RE.fullmatch(name)
        if m and value.strip():
            order = int(m.group(1)) if m.group(1) else -1
            found.append((order, name, value.strip()))
    keys: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, name, value in sorted(found):
        if value not in seen:
            seen.add(value)
            keys.append((name, value))
    if not keys:
        print(
            "Note: no OPENALEX_API_KEY* found — querying OpenAlex via the "
            "common pool (no key required, but rate limits are stricter).",
            file=sys.stderr,
        )
    return keys


def http_get_json(path: str, params: dict[str, str | int],
                  api_key: str | None = None) -> dict:
    """GET ``BASE_URL + path`` with query params and return parsed JSON.

    The API key is sent as an ``Authorization: Bearer`` header, never as an
    ``api_key`` query parameter, so it cannot leak through a URL (e.g. the
    ``url`` attribute of an HTTPError).

    Raises urllib.error.HTTPError for non-2xx responses (callers handle 404).
    """
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    )
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    headers = {"User-Agent": "paper_search.py"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
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
    try:
        work = http_get_json(f"/works/doi:{doi}", {}, api_key)
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
    types: list[str],
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
    if types:
        # OpenAlex ORs values within one filter via `|`.
        filters.append("type:" + "|".join(types))
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
    data = http_get_json("/works", params, api_key)
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
    p.add_argument("--type", dest="types", action="append", default=[],
                   metavar="TYPE",
                   help="OpenAlex work type filter (repeatable, or "
                        "comma-separated), e.g. article, preprint, "
                        "book-chapter, dataset, software. Multiple values "
                        "are OR-ed")
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

    # Allow either repeated flags or a single comma-separated value.
    authors: list[str] = []
    for chunk in args.authors:
        authors.extend(a.strip() for a in chunk.split(",") if a.strip())
    types: list[str] = []
    for chunk in args.types:
        types.extend(t.strip() for t in chunk.split(",") if t.strip())

    def fetch(api_key: str | None) -> list[dict]:
        if args.doi:
            return fetch_by_doi(args.doi, api_key)
        return fetch_by_search(
            title=args.title,
            keyword=args.keyword,
            authors=authors,
            venue=args.venue,
            year=args.year,
            types=types,
            sort=args.sort,
            n=args.n,
            api_key=api_key,
        )

    # Try each registered key in turn, then the keyless common pool as a
    # last resort. Only key-dependent failures (ROTATE_ON) move on to the
    # next attempt. Only env var *names* are ever reported, never values.
    attempts: list[tuple[str | None, str | None]] = [
        *get_api_keys(), (None, None)
    ]
    strategy = "doi_exact" if args.doi else "search"
    works: list[dict] = []
    errors: list[str] = []
    api_key_used: str | None = None
    for i, (name, key) in enumerate(attempts):
        label = name or "no key"
        is_last = i == len(attempts) - 1
        try:
            works = fetch(key)
            api_key_used = name
            errors = []
            break
        # Error messages deliberately omit the request URL. A non-empty
        # `errors` means the lookup did not complete — empty `candidates`
        # then says nothing about whether the paper exists.
        except urllib.error.HTTPError as e:
            errors.append(
                f"OpenAlex request failed ({label}): HTTP {e.code} {e.reason}"
            )
            if e.code in ROTATE_ON and not is_last:
                print(f"Note: HTTP {e.code} with {label} — trying "
                      f"{attempts[i + 1][0] or 'without a key'}.",
                      file=sys.stderr)
                continue
            break
        except urllib.error.URLError as e:
            errors.append(f"OpenAlex request error ({label}): {e.reason}")
            break
        except (TimeoutError, OSError, ValueError) as e:
            errors.append(
                f"OpenAlex request error ({label}): {type(e).__name__}"
            )
            break

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
            "types": types,
            "sort": args.sort,
        },
        "strategy": strategy,
        "n_requested": args.n,
        "n_returned": len(candidates),
        "lookup_ok": not errors,
        "api_key_used": api_key_used,
        "candidates": candidates,
        "errors": errors,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
