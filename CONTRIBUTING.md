# Contributing

Thanks for your interest in Qingyu!

1. Look for issues labelled `good first issue` or `help wanted`. Open an issue to discuss larger changes first. The Gateway import pipeline and parser contract are specified in [docs/IMPORTER_SPEC.md](docs/IMPORTER_SPEC.md). That page describes the code that exists. A new source such as a Claude export stays in its own issue.
2. Fork, create a branch, and keep changes focused.
3. Run `ruff check --select E9,F63,F7,F82 .` and the tests for the component you changed before opening a PR (see "Running tests" in the README). These are the same checks as [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
4. Do not include credentials, personal data or real chat exports in commits or fixtures.

By contributing you agree that your contributions are licensed under Apache-2.0.
