# Releasing to PyPI

GlyphAudit publishes via [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/) — the GitHub Actions workflow at [`.github/workflows/publish.yml`](../.github/workflows/publish.yml) authenticates with PyPI using OIDC, so there's no long-lived API token sitting in repo secrets.

## One-time PyPI + repo setup

1. **PyPI side** — at <https://pypi.org/manage/account/publishing/> click *Add a new publisher* and fill in:
   - PyPI project name: `docrepair-tools`
   - Owner: `agyeiagyeiagyei`
   - Repository: `docrepairtools`
   - Workflow filename: `publish.yml`
   - Environment name: `pypi`
2. **GitHub side** — in this repo's Settings → Environments, create one named `pypi`. Add yourself as a required reviewer if you want an approval gate before every upload.

(The first publish on PyPI needs a manual `twine upload dist/*` since trusted publishers can only be attached to an existing project. See *Bootstrap* below.)

## Cutting a release

1. Bump the version in [`pyproject.toml`](../pyproject.toml):

   ```toml
   [project]
   version = "0.2.0"
   ```

2. Commit, push, then tag:

   ```bash
   git commit -am "Release v0.2.0"
   git tag v0.2.0
   git push && git push --tags
   ```

3. The `Publish to PyPI` workflow runs. The build job validates that the tag matches `pyproject.toml`'s version, builds the sdist + wheel, and runs `twine check --strict`. If that passes, the publish job uploads via OIDC.

4. Verify on PyPI: <https://pypi.org/p/docrepair-tools>.

## Dry-run / release candidates

Tags with a dash suffix (e.g. `v0.2.0-rc1`, `v0.2.0-beta.2`) build and validate but **do not upload** — the publish job's `if: ${{ !contains(github.ref, '-') }}` guard skips them. Useful for testing the workflow without polluting the PyPI history.

## Bootstrap (first ever release)

Trusted publishers can only be configured against an existing PyPI project, so the very first upload has to use an API token:

```bash
python -m pip install --upgrade build twine
python -m build
twine check --strict dist/*
twine upload dist/*   # asks for username "__token__" and the PyPI token
```

After that first upload, the project exists on PyPI; configure the trusted publisher per *One-time PyPI + repo setup* and every subsequent release goes through Actions.

## Local sanity check

To exercise the build + check pipeline without pushing a tag:

```bash
python -m pip install --upgrade build twine
python -m build
twine check --strict dist/*
# Install the wheel into a clean venv and confirm the CLI works:
python -m venv /tmp/glyphaudit-smoke && \
  /tmp/glyphaudit-smoke/bin/pip install dist/docrepair_tools-*.whl && \
  /tmp/glyphaudit-smoke/bin/glyph-audit --help
# (console script stays `glyph-audit`; only the distribution name is namespaced)
```
