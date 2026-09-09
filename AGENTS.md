# Repository agent entry point

Read [CLAUDE.md](CLAUDE.md) for the maintained architecture invariants and commands.
For feature changes and reliability fixes, use
[the tenantchat-change skill](skills/tenantchat-change/SKILL.md).

Keep specifications, implementation, and verification evidence in the same change.
Do not regenerate the API snapshot merely to silence a failed contract gate:
explain the intended contract change first. Never describe generated tests or
agent reviews as human approval. Record actual commands and unverified limits.
