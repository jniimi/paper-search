---
name: paper-search
description: Search the OpenAlex API for academic papers — verify whether a cited paper actually exists (hallucination / fabricated-citation checks), look up a paper by DOI or exact title, run keyword / full-text discovery, or list papers by a given author. Use whenever the user wants to find, look up, or confirm the existence of a scholarly paper, citation, or DOI.
allowed-tools: Bash(python3 *)
---

# Paper search (OpenAlex)

`paper_search.py` queries the OpenAlex `/works` API and prints candidate
records as JSON on stdout. It only fetches raw records — **you** decide
whether a paper exists or matches what the user described.

## Running it

```bash
python3 "${CLAUDE_SKILL_DIR}/paper_search.py" [args]
```

Standard-library only — no install step. Exactly one of `--doi`, `--title`,
`--keyword`, or `--authors` must be given (filters may be added on top).

| Argument | Purpose |
|----------|---------|
| `--doi` | Direct lookup by DOI (any form: bare, `doi:`, or full URL). Highest priority. |
| `--title` | Title-only filter match — for verifying one known paper. |
| `--keyword` | Full-text query (title + abstract + fulltext) — for discovery. |
| `--authors` | Author name; repeatable or comma-separated. Valid on its own. |
| `--venue` | Journal / conference name filter. |
| `--year` | Publication year (exact match). |
| `--type` | OpenAlex work type filter, repeatable or comma-separated (e.g. `article,preprint`). Multiple values are OR-ed. |
| `--sort` | `date` (newest first), `citations` (most cited), or `relevance` (requires `--keyword`). |
| `--n` | Number of candidates (default 5). |
| `--abstract` | Include the reconstructed abstract in each candidate. |

## Choosing the mode

- **Verify a citation exists** — prefer `--doi` if a DOI is given (a clean
  miss returns empty `candidates` = strong "does not exist" signal). Else
  use `--title` plus `--authors` / `--year` to pin it down. **Do not** add
  `--type` when verifying — the real record may be a book chapter,
  dataset, etc., and restricting type would cause a false "does not exist".
- **Discover papers on a topic** — use `--keyword`.
- **Find an author's papers** — use `--authors` alone, usually with
  `--sort date` for recent work.

For **discovery** searches (`--keyword` / `--authors`), default to
`--type article,preprint,book-chapter,review` so software, datasets,
editorials, peer reviews, etc. don't crowd the results — unless the user
explicitly wants those other types. `book-chapter` is kept because OpenAlex
classes Springer LNCS conference proceedings (MICCAI, ECCV, …) as
`book-chapter`; `review` is kept because review articles are often the
most useful discovery hits.

## Reading the output

JSON with `query`, `strategy` (`doi_exact` or `search`), `n_returned`,
`candidates[]`, and `errors[]`. Each candidate has `openalex_id`, `doi`,
`title`, `authors`, `publication_year`, `venue`, `type`, `cited_by_count`,
`is_retracted`, and `relevance_score` (search only).

When verifying, compare the user's claimed title/authors/year against the
candidates yourself and report whether it is a confident match, a near
match, or absent. Flag `is_retracted: true`.

## Caveats

- `--year` is an exact-match filter and OpenAlex's `publication_year` can
  differ from a paper's commonly-cited year (re-indexed preprints). If a
  verification turns up nothing, retry without `--year`.
- `--sort` *replaces* relevance ranking. `--keyword ... --sort citations`
  returns the most-cited loose matches, not the most relevant — leave
  `--sort` off for precise keyword search.
- `OPENALEX_API_KEY` is optional; without it requests use the common pool
  (a note is printed to stderr — not an error).
