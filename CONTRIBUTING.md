# Contributing to sponsorscan

Thanks for helping out. Bug reports, new company boards, and fixes are all
welcome.

## Quick ways to help

- **Add a company.** Open an issue with the "Add a company" template, or send a
  PR that edits `companies.yaml`. Verify the slug first using the URL patterns
  at the top of that file.
- **Report a bug.** Use the bug report template and include the output of
  `python sponsorscan.py doctor`.
- **Suggest a feature.** Open a feature request describing the problem before
  writing a large change, so we can agree on the approach.

## Development setup

```bash
git clone https://github.com/pruhnav/sponsorscan.git
cd sponsorscan
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

Run the tests before opening a PR:

```bash
python -m pytest
python selftest.py
```

## Pull requests

- Keep each PR focused on one change. Two fixes are two PRs.
- Title it `type(scope): summary`, in the imperative. The type is one of
  `fix`, `feat`, `docs`, `test`, `refactor`, `ci` or `chore`; the scope is the
  area it touches, such as `report`, `fetch`, `load-lca`, `setup` or `boards`.
  For example `fix(fetch): handle empty Lever boards` or
  `feat(fetch): add Workable support`. Keep `feat` for something a user notices.
- Fill in every section of the PR template, including how you tested it.
- Add or update tests in `tests/` for behavior changes. Tests must not touch the
  network.
- Update the README or `docs/` when you change commands, flags, or setup steps.

PRs are squash-merged, so the PR title becomes the one commit on `main` and the
PR body becomes its message. Commits inside the branch can be as small and
messy as you like. The Tests workflow runs both suites on Linux, macOS and
Windows for every PR and must pass before merging.

Labels are applied for you, so there is no need to set them by hand:

- **One type label from the title:** `fix` gives `bug`, `feat` gives
  `enhancement`, `docs` gives `documentation`, and `ci`, `chore`, `test` or
  `refactor` give `maintenance`. Editing the title updates it.
- **Area labels from the files changed:** `area: report`, `area: scanner`,
  `area: boards`, `area: setup`, `area: alerts`, `area: docs` and `area: ci`.
  `.github/labeler.yml` maps files to areas.

## Never commit personal data

sponsorscan works with personal details such as your profile, visa status, and
API credentials. `.gitignore` already excludes profiles, state files, `.env`,
and credential files. Double check `git status` before committing, and only
commit the `*.example.json` profiles.

## Code of Conduct

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
