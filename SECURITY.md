# Security Policy

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/ZYB10/branch-radar-oss/security/advisories/new).
Please do not open a public issue for an unfixed vulnerability.

Include what you did, what happened, and the version from `branch-radar --version`.
Expect an acknowledgement within a week.

## Supported versions

Fixes land on `main` and ship in the next release. Older releases are not
patched separately.

## What Branch Radar is, in security terms

It runs on one person's machine, reads Git repositories that person already
has, and writes to destinations that person already signed into. It is not a
server, and it is not multi-user.

**It never stores credentials.** Git access reuses the machine's existing
credential helper or SSH agent, Codex uses the existing `codex` session, and
Lark uses `lark-cli` and its keychain entry. Branch Radar reads none of them,
and interactive credential prompts are disabled for the Git commands it runs.

**Credential-bearing text is redacted before it is stored or sent anywhere.**
That covers URLs with userinfo, bearer tokens, JWTs and common provider token
formats, on the way into SQLite and on the way to any model or destination.

**It does not modify the repositories it watches.** Checks update remote refs
and read history; working trees are never touched.

### The Web console

The console binds to loopback only and mints a fresh session token per start,
delivered in the URL fragment so it never reaches the server log. Every
state-changing request is checked for the `Host`, the `Origin`, that token and
a JSON content type; the token comparison is constant-time. Responses carry a
strict `Content-Security-Policy` with no inline script, and the page builds its
DOM without `innerHTML`.

Repository data is rendered as plain text. Raw diffs, author email addresses,
credential-bearing remotes, state-directory paths and tracebacks are excluded
from the console's own responses by construction.

### Where a real problem would live

Reports are most valuable where those properties could be broken: a credential
reaching disk, a request accepted without one of the four checks, data from a
repository escaping as markup rather than text, a path from console input to
shell or filesystem access outside a configured scan root, or a delivery marked
successful without proof.

### Out of scope

Anything that assumes an attacker already runs code as the same user, or that
requires reaching a port on `127.0.0.1` from another user account on a shared
machine. Both are outside the model this tool defends.
