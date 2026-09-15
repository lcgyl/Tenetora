# Risk Checklist

## Secrets

Flag without printing values:

- GitHub tokens: `glpat-...`
- GitHub tokens: `ghp_...`, `github_pat_...`
- Private key blocks
- `TOKEN=...`, `PASSWORD=...`, `SECRET=...`
- JWT-looking values
- Cookies and session IDs
- MCP service tokens

## Local-only paths

Flag paths like:

- `/Users/<name>/...`
- `/home/<name>/...`
- `C:\Users\<name>\...`

Do not automatically remove them from unrelated files. Record and ask before changing.

## Tool config risks

Flag:

- tracked `.mcp.json` with token values
- tracked local settings files
- broad shell permissions such as unrestricted `git`, `rm -rf`, `git reset`, `git checkout`
- tool-specific rules that duplicate project rules

## Historical leaks

If a tracked file currently has no secret but `HEAD:<file>` still contains a secret pattern:

1. Tell the user to rotate the credential.
2. Avoid printing the secret.
3. Do not rewrite history unless explicitly asked.
