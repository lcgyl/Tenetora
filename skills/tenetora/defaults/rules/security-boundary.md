# Security Boundary Rules

Source: Tenetora default rules.

- Never copy secrets, tokens, private keys, password values, or credential-bearing local configs into shared `.tenetora` content.
- Treat tool-private local files as sensitive unless a registry or user confirmation says otherwise.
- When reporting a secret finding, name the file and key or pattern, but do not repeat the secret value.
- If a secret may have been committed, ask the user to rotate it; do not claim that deletion alone fixes exposure.
- Prefer environment variables, user-level secret stores, CI secret stores, or ignored local files for credentials.
- Do not weaken scans, ignore patterns, or guardrails just to make an audit pass.
- A gate failure never authorizes destroying local configuration; keep `.env*` and other local files intact and remove them from the index with `git restore --staged <path>`.
