# Security Policy

**Experimental software. Do not use for unattended mission-critical work.**

## Reporting a vulnerability
Please use GitHub's private vulnerability reporting where available. Never include account credentials, tokens, real Codex conversation contents, machine names, local file paths, or authentication databases in a public issue.

## Boundaries
- Never terminate unrelated Codex processes.
- No Accessibility/keyboard automation, approval bypass, or blind input injection.
- Only exact-thread writes via an existing verified desktop control channel.
- After uncertain request delivery, stop instead of retrying automatically.
- Protect local SQLite and logs; no telemetry or credential collection by design.

## Known limitations
Cross-platform live reset/recovery behavior and writer ownership are not fully validated. Windows may lack the required desktop socket. Automatic rearming after another quota interruption and task-completion detection are incomplete. Treat all auto-resume features as experimental until real desktop E2E tests pass.
