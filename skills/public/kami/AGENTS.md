# Kami Agent Guide

Kami generates editorial HTML templates and PDF/PPTX/PNG documents. Full detail (repository map, pitfalls, fonts, release flow) is in `CLAUDE.md`; design specs in `references/`.

## Commands

```bash
python3 scripts/build.py --check   # CSS lint + token sync
python3 scripts/build.py --verify  # full verification
python3 scripts/tests/test_build.py
bash scripts/ensure-fonts.sh       # missing CJK fonts
bash scripts/package-skill.sh      # refresh dist/kami.zip
```

## Rules

- Lock the output contract before editing: language, template, format, page/length target, verification command.
- Prefer the nearest template and the deterministic verifier; do not add templates, shared CSS, dependencies, or flags unless the request requires them.
- Style changes update `references/design.md` and template tokens together.
- Templates stay self-contained HTML with inline CSS; fix CSS drift across affected templates.
- `HTML_TEMPLATES` lives in `scripts/shared.py`; change it there only.
- No graphic emoji in docs, template comments, or script output; use `OK:` / `ERROR:`.
- After build or packaging changes, refresh and inspect `dist/kami.zip`; new files must be Git-tracked to be packaged.
- Do not commit one-off review reports; put stable rules in this file, `CLAUDE.md`, `SKILL.md`, or `references/`.

See `CLAUDE.md` for high-risk pitfalls (rgba tags, `break-inside` in flex, `100vh` under `@page`, resume overflow) and expected page counts.
