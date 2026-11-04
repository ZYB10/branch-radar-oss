# Releasing Branch Radar

Branch Radar is packaged as a zero-runtime-dependency Python application. A
published release can be started without a checkout:

```bash
uvx branch-radar web
```

Publishing is intentionally limited to a GitHub Release and uses PyPI Trusted
Publishing. The repository does not need, and must not store, a PyPI API token.

## One-time PyPI setup

1. Confirm that the `branch-radar` project name is available on PyPI.
2. In PyPI, create a pending Trusted Publisher for:
   - Owner: `ZYB10`
   - Repository: `branch-radar`
   - Workflow: `publish.yml`
   - Environment: `pypi`
3. Create a protected GitHub environment named `pypi`. Requiring an approver is
   recommended.

The pending publisher creates the project on the first successful release. For
later releases, configure the same publisher on the existing PyPI project.

## Release checklist

1. Update the version in `pyproject.toml`, `src/branch_radar/__init__.py`, and the
   web changelog together.
2. Run the same checks as CI:

   ```bash
   python3 -m unittest discover -s tests -p 'test_*.py'
   ruff check .
   mypy --platform=linux
   mypy --platform=darwin
   node --check src/branch_radar/resources/web/theme-bootstrap.js
   node --check src/branch_radar/resources/web/app.js
   node --check src/branch_radar/resources/web/ui-model.js
   node --test tests/web_ui_model.test.js
   python3 -m build
   ```

3. Merge the release commit to `main` and wait for CI to pass.
4. Create and publish a GitHub Release whose tag is exactly `v<version>`, for
   example `v0.2.0`.
5. Approve the protected `pypi` environment deployment if required.
6. Verify the published artifact in a clean environment:

   ```bash
   uvx --from branch-radar branch-radar --version
   ```

The publish workflow rejects a release tag that does not match the package
version, reruns tests and static checks, builds both wheel and source
distribution, and exchanges GitHub's short-lived OIDC identity directly with
PyPI. It is not callable from ordinary pushes or pull requests.
