# Rule Capture Rules

Source: Tenetora default rules.

- When the user states a durable rule, constraint, preference, workflow, or approval boundary, check existing `.tenetora/rules/` first.
- If the rule is already covered, follow the existing rule and avoid duplicating it.
- If the rule is new or materially sharper than existing rules, ask the user before recording it.
- Record accepted rules in the narrowest durable file under `.tenetora/rules/`; use `.tenetora/wiki/` for facts and `.tenetora/workflows/` for procedures.
- Include provenance when useful: source message, date, affected scope, and known exceptions.
- Do not record secrets, local credentials, private tokens, or machine-specific paths as shared rules.
