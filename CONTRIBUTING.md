# Contributing

Thanks for helping. AgentTape is a single-maintainer project; these notes describe how
changes are made and checked.

## Before you start

- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the boundaries and
  [docs/EXTENDING.md](docs/EXTENDING.md) for where a change belongs. Most features are a
  new file in a registry (router, CLI command, adapter, evaluator, executor, background
  service, seed, UI feature, run-detail tab), not an edit to a central list.
- Set up with `make setup`; see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Making a change

1. Work on a branch. Keep commits atomic: one logical change, imperative subject, a short
   body explaining why.
2. Write tests with the code: unit tests for pure logic, integration tests for services,
   the worker, and the API, Vitest for UI components, Playwright for user journeys.
3. Keep the one-engine rule: anything that starts agent work goes through
   `RunService.create_run`; status changes go through the transition helpers; events go
   through the event recorder.
4. Never fake data. Unknown usage, cost, or results are `null` and shown as unavailable;
   demo usage stays labelled simulated.
5. Never store secret values, never pass command text through a shell, never add
   automatic push, merge, or destructive Git operations.
6. A schema change is a new Alembic revision (`agenttape migrate` applies it); downgrade
   must work.
7. A change to the interface leaves the images in `docs/images/` alone; the maintainer
   updates them.
8. Update the docs page that owns the behaviour you changed, and `CHANGELOG.md`.
9. Never commit `.env` files, runtime databases, logs, build output, or captured
   repository contents.
10. Never capture or display hidden model reasoning; record only adapter-provided
    observable events and explicit summaries.
11. No em dash (U+2014) anywhere in the repository: reword with a colon, semicolon,
    comma, parentheses or a new sentence. `make check` fails if a tracked file has one.

## Before you hand it back

```bash
make check
```

It runs every gate (format, lint, types, tests, migration smoke test, frontend lint,
types, unit tests and build, secret scan, container config checks, Docker build when
Docker is installed, and Playwright including accessibility checks) and must pass.
`SKIPPED-EXTERNAL` gates are listed with the reason and never count as passing.

## Security issues

Do not file them publicly. See [docs/SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability).

## License

AgentTape is released under the [MIT License](LICENSE). By contributing, you agree that
your contributions are licensed under it too.
