# Contributing

Keep changes small and focused. Describe public behavior, not internal service
topology or development history.

Pull requests are squash-merged, so the title and description become the
commit on `main`. Write them as the final commit message.

## Commit messages

Use a short, imperative subject:

```text
<type>(<scope>): <outcome>
```

Common types are `feat`, `fix`, `refactor`, `test`, `docs`, `ci`, `chore`, and
`release`. Common scopes are `cli`, `python`, `typescript`, `go`, `sdk`,
`contract`, and `examples`.

- Keep the subject at or below 72 characters.
- Put one observable change in each commit.
- Add a body only when the reason, compatibility effect, or invariant is not
  clear from the diff.
- Describe current behavior. Do not include tickets, private endpoints,
  incident notes, squash logs, or validation transcripts.
- Use `BREAKING CHANGE:` only for a public compatibility break.

Examples:

```text
fix(cli): reject extra positional arguments
feat(go): add identity lookup
docs(examples): simplify the pool quickstart
```

## Generated API files

`openapi.json` is synced from upstream, which regenerates the language
bindings:

```bash
./scripts/sync-openapi --source-file <path-to-upstream>/openapi.json
```

Fix description wording upstream, or the next sync reverts it. Do not edit
`openapi.json` or the generated models directly.

## Validation

Run `scripts/check-local` before opening a pull request. JavaScript workspaces
use the root `bun.lock`; do not maintain a separate npm lockfile. TypeScript
builds and tests generate the shared guest helpers from their Python sources.
Run `bun run generate:validators` in `typescript/` before invoking individual
TypeScript test files from a fresh checkout.
