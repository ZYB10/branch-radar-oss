# Contributing

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Python 3.10 or newer. The runtime has no third-party dependencies and should
stay that way — see the invariants below.

## Before you open a pull request

Run what CI runs:

```bash
.venv/bin/ruff check . && .venv/bin/mypy --platform=linux && .venv/bin/mypy --platform=darwin && .venv/bin/python -m unittest discover -s tests -p "test_*.py" && node --test tests/web_ui_model.test.js
```

`mypy` runs for both platforms because the service manager and folder picker
differ between macOS and Linux, and a change can typecheck on one and not the
other.

## Invariants

These are properties the code holds on purpose. [AGENTS.md](AGENTS.md) has the
full list and [docs/architecture.md](docs/architecture.md) explains why. The
ones that most often catch people out:

- **No credentials are ever stored.** Not Git, not Codex, not Lark. Reuse what
  the machine already has.
- **Git facts are authoritative; model output is advisory.** A summary can be
  wrong or missing without changing what was recorded.
- **A branch cursor advances only when the matching event write commits in the
  same transaction.** Otherwise a change is silently lost.
- **A source that cannot reconstruct evidence sets `analysis_complete=False`.**
  It never infers.
- **A delivery is successful only once reconciliation proves it.** "The API
  returned 200" is not proof.
- **The core works without Codex, without `lark-cli`, and without a network.**
- **The Web server stays loopback-only**, and every state-changing request
  keeps its `Host`, `Origin`, session-token and JSON checks.
- **External programs run with argv arrays and `shell=False`.**

## Adding user-facing text

The console and the CLI are bilingual, and both are keyed by the Chinese
source string.

- A new error code needs an entry in `ERROR_COPY` in
  `src/branch_radar/resources/web/app.js`, and English for that copy in
  `i18n.js`. A test walks the Python sources and fails on a code without one.
- New terminal output needs English in
  `src/branch_radar/resources/cli.en.json`. Translate the format string, not
  the formatted result — `t("已关注 %d 个分支") % count`, never
  `t("已关注 %d 个分支" % count)`.
- `--json` output stays in the source language. It is a machine interface.

## Adding a destination

Implement `PublicationTarget` (`contains` and `append`) in
`src/branch_radar/publishers/`. `Publisher` already owns leases, verification
order and the success/error/unknown decision, so do not reimplement them, and
do not write to the database from a target. `MarkdownFileTarget` is the
smallest complete example.

## Commits

Conventional commits (`feat:`, `fix:`, `perf:`, `refactor:`, `docs:`,
`chore:`). Explain in the body why the change is needed, not what the diff
already shows.

## AI authorship

Per [AGENTS.md](AGENTS.md), no AI model or assistant may be credited as an
author, contributor or co-author — no `Co-authored-by` trailers, no AI
identities in manifest author fields, README, LICENSE or CONTRIBUTORS.
Authorship belongs to the person submitting the work.
