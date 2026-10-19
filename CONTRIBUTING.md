# Contributing

Keep changes small and focused. Describe public behavior, not internal service
topology or development history.

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
- Describe current behavior. Do not include tickets, session URLs, assistant
  metadata, private endpoints, incident notes, squash logs, or validation
  transcripts.
- Use `BREAKING CHANGE:` only for a public compatibility break.

Examples:

```text
fix(cli): reject extra positional arguments
feat(go): add identity lookup
docs(examples): simplify the pool quickstart
```

## Generated API files

Edit `openapi.json`, then regenerate the language bindings:

```bash
python3 scripts/openapi_contract.py generate --contract openapi.json
```

Do not edit generated models directly.

## Validation

Run `scripts/check-local` before opening a pull request.
