# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A Claude Code **plugin** (`paper-search`) wrapping the OpenAlex `/works` API — both **verification** (detecting hallucinated / fabricated citations) and **discovery** (keyword / author search to find papers).

## Layout

```
.claude-plugin/plugin.json     # plugin manifest (name: paper-search)
skills/paper-search/
  SKILL.md                     # skill instructions + frontmatter
  paper_search.py              # the working core (stdlib-only)
```

The skill is model-invoked: when the user wants to find or verify a paper, Claude reads `SKILL.md` and runs `paper_search.py` via Bash. `SKILL.md` references the script as `${CLAUDE_SKILL_DIR}/paper_search.py` so it resolves at any install scope.

## Running directly

```bash
python3 skills/paper-search/paper_search.py --title "Hierarchical memorization in LLMs"
python3 skills/paper-search/paper_search.py --doi 10.48550/arXiv.2511.08877 --abstract
python3 skills/paper-search/paper_search.py --keyword "llm memorization" --year 2023 --n 10
python3 skills/paper-search/paper_search.py --authors "Nicholas Carlini" --sort date --n 10
```

Standard library only — no `uv`, no `pip install`. One of `--doi`, `--title`, `--keyword`, or `--authors` is required. No `marketplace.json` yet — test locally with `claude --plugin-dir .` (path to the repo root).

## Design constraints (decided deliberately — keep them)

- **Zero required dependencies.** Uses `urllib`, not `httpx`/`requests`. The `.env` loader is hand-rolled to avoid `python-dotenv`. `certifi` is used *only if importable* (`ssl_context()`) — never required — to work around python.org macOS builds whose CA store isn't wired up. Don't add a hard dependency without good reason.
- **The script does not judge.** It fetches raw OpenAlex records and emits JSON; the existence/match decision is left to Claude. No similarity scoring, no verdict field.
- **`OPENALEX_API_KEY` is optional.** Read from env or a `.env` in the **current working directory** (not next to the script — as a bundled plugin the script lives in a cache dir); absent → request goes through the common pool with a one-line stderr note.

## Query logic

- `--doi` takes priority → direct `GET /works/doi:{doi}` lookup (`strategy: "doi_exact"`). A clean 404 yields empty `candidates` — the intended "does not exist" signal.
- Otherwise → `GET /works` search (`strategy: "search"`). Key distinction:
  - `--title` → `title.search` **filter** (title-only; for verifying a known paper).
  - `--keyword` → `search` **query param** (full text: title + abstract + fulltext; for discovery).
  - `--authors` / `--venue` / `--year` are filters that combine with either — and `--authors` alone is also a valid query (e.g. "recent papers by X").
  - `--sort` (`date` / `citations` / `relevance`) sets result order; `relevance` requires `--keyword` (guarded in `main()`). When omitted, OpenAlex's own order is used. Note `--sort` *replaces* relevance ranking, so `--keyword ... --sort citations` returns the most-cited loose matches, not the most relevant.
- Output JSON: `query`, `strategy`, `n_requested`, `n_returned`, `candidates[]`, `errors[]`. `--abstract` adds the reconstructed abstract per candidate (rebuilt from OpenAlex's `abstract_inverted_index`).

Note: OpenAlex `publication_year` can disagree with a paper's commonly-cited year (re-indexed preprints), so `--year` being an exact-match filter can drop valid hits.
