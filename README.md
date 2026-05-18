# Claude Code `paper-search` Skill

You can search the published papers including preprints, and validate the existance of LLM-generated paper records easily.

This skill is based on the free API of [OpenAlex (http://openalex.org)](http://openalex.org). You can search the papers in details using API Key.

## Preparation
### Install the plugin
Load it locally (no marketplace yet):
```
$ claude --plugin-dir /path/to/paper-search
```
Ask Claude to look up or verify a paper and the `paper-search` skill is
invoked automatically. No `uv` / `pip install` — Python standard library only.

### API key (optional)
OpenAlex works without a key. To use the faster pool, set `OPENALEX_API_KEY`
as an environment variable or in a `.env` file in your working directory.

## Citations
```LaTeX
J. Niimi (2026) paper-search skill
```
