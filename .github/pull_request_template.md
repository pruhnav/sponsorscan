## What does this PR do?

<!-- What changes for someone using SponsorScan, in 1-3 sentences. Then the why:
     what was wrong, or what was missing. -->

## Related issue

<!-- "Fixes #12" or "Closes #12". Open an issue first for new features and new
     commands. Bug fixes, new company boards and docs can come straight in. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Company boards (`companies.yaml`)
- [ ] Documentation
- [ ] Refactor (no behavior change)

## How it was tested

<!-- What you ran and what it showed. For a bug fix, say whether the new test
     failed before the fix. Live checks (a real fetch, a real report) go here too. -->

## Checklist

- [ ] The title follows `type(scope): summary`, for example `fix(report): key jobs by posting ID`
- [ ] `python -m pytest tests -q`, `python selftest.py` and `ruff check .` pass
- [ ] Tests added or updated for behavior changes, with no network access
- [ ] README or `docs/` updated if commands, flags or setup changed
- [ ] No personal data or credentials (profiles, state files, `.env`, keys)
