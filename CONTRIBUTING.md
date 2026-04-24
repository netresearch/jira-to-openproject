# Contributing

Thanks for taking the time to contribute.

## Workflow

1. **Fork & branch.** Work on a feature branch named `feat/<scope>`, `fix/<scope>`, or similar. Never commit directly to `main`.
2. **Install dependencies.** `uv sync --frozen --extra dev --extra test`
3. **Make your change.** Keep PRs focused and under ~300 net lines of code.
4. **Lint and test locally.**
   ```bash
   make lint
   make dev-test
   ```
   For changes that touch container behaviour: `make container-test`.
5. **Commit with [Conventional Commits](https://www.conventionalcommits.org/).** Examples:
   `feat(migrations): add time log preview mode`, `fix(clients): retry on transient 502`.
6. **Open a pull request.** Link any related issue. Include a brief _Test plan_.

## Code standards

- **Python 3.14**, `uv` for dependency management.
- **Ruff** (`make format`, `make lint`) — most style rules are enforced automatically. See `pyproject.toml` for the active rule set and intentional exceptions.
- **Mypy** — the project is type-checked; new code should not introduce new errors.
- **Pytest** — every non-trivial change ships with tests. See `tests/AGENTS.md` and the Golden samples in [`AGENTS.md`](AGENTS.md).
- **Conventional Commits** — commit messages scope changes (`feat`, `fix`, `refactor`, `docs`, `test`, `build`, `chore`).

## Reporting issues

- Reproducible steps, expected vs. actual behaviour, and environment (`python --version`, `uv --version`, OS) speed up triage.
- Security vulnerabilities: follow [`SECURITY.md`](SECURITY.md); do not open a public issue.

## Licensing

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE) of this project.
