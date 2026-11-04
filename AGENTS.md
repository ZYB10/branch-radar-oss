# Branch Radar agent guide

This repository contains a local-first monitoring CLI. Preserve these boundaries:

- Never persist Git credentials, Lark tokens, Codex auth files, or unredacted credential-bearing remote URLs.
- Run external programs with argv arrays and `shell=False`.
- Git facts and SQLite state are authoritative; AI output is advisory text only.
- Never credit Claude, Anthropic, Codex, ChatGPT, an AI assistant, or any AI model as a project author, contributor, or co-author. Do not add AI identities to manifest author fields, README, LICENSE, or CONTRIBUTORS files; do not add `Co-authored-by` trailers; and do not claim AI authorship in code comments. Authorship of all code and documentation belongs exclusively to the user. Immediately remove any violation.
- Do not advance a branch cursor unless the corresponding event write succeeds in the same transaction.
- Keep `BranchMonitor` free of Git specifics. Observation and change classification belong in a `FactSource`; cursors, transactions and model fallback belong in the monitor. A `FactSource` must never write to the database or advance a cursor.
- A source that cannot reconstruct evidence must set `analysis_complete=False` rather than infer it.
- Generated service units must pin the installing interpreter, `PATH` and `BRANCH_RADAR_HOME`, and must stay user-level (macOS LaunchAgent, systemd user unit) so `lark-cli` keeps Keychain access.
- Do not mark a delivery successful until marker reconciliation proves it. Sequencing, leases and the
  success/unknown decision belong to `Publisher`; a `PublicationTarget` owns only `contains` and `append`,
  must never write to the database or set a delivery state, and must raise rather than report a
  destination it could not read as empty.
- Keep the core usable without Codex CLI, lark-cli, network access, or third-party Python packages.
- Keep the Web server loopback-only. Preserve strict Host, same-origin, session-token and JSON checks for every state-changing endpoint.
- Render repository and commit data as plain text. Do not expose raw diffs, author emails, credential-bearing remotes, state-directory paths, or tracebacks through Web DTOs.
- Web scans, checks and external writes must remain bounded background jobs coordinated with the shared `operation.lock`.

Safe read-only commands for automation:

```bash
branch-radar doctor --json
branch-radar status --json
branch-radar events --json
branch-radar discover --json
```

`check` may fetch remote refs. `lark create-document`, `lark publish`, and `check --publish` perform external writes and require explicit user intent.
