# How to test and release `i-dot-ai-utilities`

This guide covers local verification, pre-release publishing, and stable releases for `i-dot-ai-utilities`. Merging to `main` never publishes a package. Every release is a manual action triggered through `.github/workflows/deploy.yaml` (**Publish python package**).

## Conventions and prerequisites

Confirm these rules before you tag a commit.

- **Branch naming.** Any branch works for a pre-release because `.github/workflows/deploy.yaml` checks out the git tag (`ref: ${{ steps.resolve.outputs.tag }}`), not a branch. Stable releases must target `main`.
- **Commit style.** CI does not enforce Conventional Commits, and versions do not come from commit messages. Recent history uses Conventional Commits by convention, so follow that format for readable release notes.
- **Package version in git.** Leave `version = "0.0.1"` untouched in `pyproject.toml`. The deploy workflow stamps the release version at build time with `uv version`.
- **Tag format.** Use Semantic Versioning without a leading `v` (for example `0.8.0` or `0.8.0rc1`), which `matt-usurp/validate-semver` validates in CI. Pre-release tags must match `^[0-9]+\.[0-9]+\.[0-9]+(rc|a|b|\.dev)` with no hyphen before `rc` (write `0.8.0rc1`, not `0.8.0-rc1`).
- **GitHub environment.** The `deploy` job runs in the `release` GitHub environment and publishes via PyPI Trusted Publishing (OIDC). Ensure that you have permission to trigger workflows, push tags, and approve deployments if the environment requires approval.

## Verify changes locally

Run the same checks that `.github/workflows/checks.yaml` runs in CI before you tag a commit.

1. Sync the virtual environment with all extras and development tools.

```bash
uv sync --all-extras --dev
```

2. Run the formatters, linter, type checker, and security scanner.

```bash
make lint
```

This target runs `ruff format`, `ruff check --fix`, `mypy`, and `bandit`.

3. Run the full test suite. Docker must be running because `make test` starts backing services with Docker Compose and enforces a 75% coverage floor across `logging`, `otel`, `metrics`, `file_store`, `litellm`, and `auth`.

```bash
make test
```

If you changed only `logging` or `otel` and do not have Docker available, run the isolated targets instead.

```bash
make test-logging
make test-otel
```

4. Optionally run the repository pre-commit hooks to catch secrets, IP addresses, or AWS account numbers.

```bash
uv run pre-commit run --all-files
```

## Choose a pre-release channel

Two pre-release channels exist.

| Channel | Trigger | Version stamped | Target index |
| --- | --- | --- | --- |
| Pre-release to production PyPI | Manual workflow dispatch (`pypi=production`) | Tag verbatim (for example `0.8.0rc1`) | Production PyPI |
| Pre-release to Test PyPI | GitHub pre-release, or manual workflow dispatch (`pypi=test`) | `<tag>.<YYYYMMDDHHMM>` (for example `0.8.0rc1.202611260930`) | Test PyPI |

Prefer **pre-release to production PyPI** when testing inside real applications. `pip`, `uv`, and `poetry` ignore PEP 440 pre-releases unless a consumer opts in or pins the exact version, so normal installs remain on the latest stable release. Use **Test PyPI** only when you want to test the packaging and upload pipeline without touching production PyPI.

## Publish a pre-release to production PyPI

The tag must exist on the remote repository before you dispatch the workflow.

1. Ensure that your branch commits are pushed and that the **Check code** workflow passes on Python 3.10, 3.11, and 3.12.
2. Create and push a PEP 440 pre-release tag on the commit you want to test.

```bash
git tag 0.8.0rc1
git push origin 0.8.0rc1
```

3. Trigger **Publish python package** with `pypi=production` and your tag.

From the GitHub CLI:

```bash
gh workflow run deploy.yaml -f pypi=production -f version=0.8.0rc1
```

From the GitHub UI, open **Actions**, select **Publish python package**, click **Run workflow**, set `pypi` to `production`, enter `0.8.0rc1` in `version`, and click **Run workflow**.

The workflow rejects manual `production` runs if the tag does not match the PEP 440 pre-release pattern.

4. Wait for the workflow to finish and confirm the green Slack notification.
5. Test the published pre-release in a consuming project by opting in to pre-releases or pinning the exact version.

```bash
# Pin the exact pre-release version
pip install "i-dot-ai-utilities[otel_native_fastapi]==0.8.0rc1"

# Or resolve the newest pre-release
pip install --pre "i-dot-ai-utilities[otel_native_fastapi]"
uv pip install --prerelease=allow "i-dot-ai-utilities[otel_native_fastapi]"
poetry add --allow-prereleases "i-dot-ai-utilities[otel_native_fastapi]"
```

If you need another iteration before merging, push new commits, increment the suffix (`0.8.0rc2`), push the new tag, and run the workflow again.

## Publish a pre-release to Test PyPI

Use either a GitHub pre-release or a manual workflow dispatch.

1. Push your branch commits and confirm **Check code** passes.
2. Create a GitHub pre-release targeting your branch with a PEP 440 pre-release tag.

From the GitHub CLI:

```bash
gh release create 0.8.0rc1 --target <your-branch> --prerelease --generate-notes
```

From the GitHub UI, open **Releases**, click **Draft a new release**, choose your branch as the target, enter `0.8.0rc1` as the tag, tick **Set as a pre-release**, and click **Publish release**.

Alternatively, push the tag manually and run `gh workflow run deploy.yaml -f pypi=test -f version=0.8.0rc1`.

3. Open the workflow run logs and note the timestamped version printed by the **Resolve release target** step (for example `0.8.0rc1.202611260930`). The workflow adds the UTC timestamp so repeated uploads to Test PyPI do not fail with filename-reuse errors.
4. Install the exact timestamped version in a consuming project. Point the installer at Test PyPI and keep production PyPI as a fallback so transitive dependencies resolve.

```bash
uv pip install \
  --index https://test.pypi.org/simple/ \
  --index-strategy unsafe-best-match \
  "i-dot-ai-utilities[otel_native_fastapi]==0.8.0rc1.202611260930"
```

Or with `pip`:

```bash
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  "i-dot-ai-utilities[otel_native_fastapi]==0.8.0rc1.202611260930"
```

## Complete a full stable release

Cut stable releases only from `main` and only through a GitHub Release. Manual workflow dispatch does not allow stable versions on production PyPI.

1. Open a pull request from your branch to `main`.
2. Confirm that **Check code** passes on Python 3.10, 3.11, and 3.12, obtain review approval, and merge the pull request.
3. Pull the latest `main` locally if you want to inspect the commit history before choosing the version number.

```bash
git checkout main
git pull origin main
git tag --sort=-creatordate | head -n 10
```

4. Create a stable GitHub Release targeting `main` with a bare SemVer tag (no leading `v`, no pre-release suffix).

From the GitHub CLI:

```bash
gh release create 0.8.0 --target main --generate-notes
```

From the GitHub UI, open **Releases**, click **Draft a new release**, set the target to `main`, enter the new tag (for example `0.8.0`), click **Generate release notes**, leave **Set as a pre-release** unchecked, and click **Publish release**.

5. Monitor **Actions** for the **Publish python package** run triggered by the release event. When it finishes, verify the Slack notification and confirm that the new version is live on [PyPI](https://pypi.org/project/i-dot-ai-utilities/).
6. Verify installation in a clean environment without `--pre`.

```bash
uv pip install "i-dot-ai-utilities==0.8.0"
```
