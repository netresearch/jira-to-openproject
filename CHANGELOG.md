# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `CHANGELOG.md` (this file) tracking release history.
- `CONTRIBUTING.md` with branching, testing, and PR guidelines.
- `.github/workflows/ci.yml` running `ruff`, `mypy`, `pytest`, and container tests on every push and pull request.
- Multi-stage Dockerfile with `HEALTHCHECK`, OCI labels, and a slimmer runtime image.
- Expanded `AGENTS.md` with Overview, Setup, Development, Architecture, Testing, and Critical-constraints sections.

### Changed
- `Dockerfile.test` now includes OCI labels and a `HEALTHCHECK`.
- All Python dependencies upgraded to their latest compatible releases (see commit history and `uv.lock`).

### Removed
- Redundant `.github/workflows/container-test.yml` (its job moved into the new `ci.yml`).

## [0.1.0] - 2025-04-15

Initial project import.

[Unreleased]: https://github.com/netresearch/jira-to-openproject/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/netresearch/jira-to-openproject/releases/tag/v0.1.0
