# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release (`0.2.x`) | Supported |
| Older releases | Not supported |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities — especially anything involving browser cookie extraction, KWallet decryption, OAuth tokens, credential scrubbing, or file permissions.

Report vulnerabilities privately through GitHub Security Advisories:

https://github.com/DLANSAMA/TallyBarPlasmoid/security/advisories/new

To help triage the report, include:
- The affected provider or component
- Steps to reproduce
- Your KDE Plasma and Python versions

Before attaching any logs, snapshots, or captures, double-check that all tokens, cookies, and personal identifiers have been scrubbed.

## Security Model

TallyBar's security boundaries are documented in the [Privacy and security](README.md#privacy-and-security) section of `README.md`: no telemetry or third-party reporting, transient browser cookie copies in `$XDG_RUNTIME_DIR` tmpfs opened with `O_NOFOLLOW`, `0600` atomic file writes (`fsync` + `os.replace`) under `0700` directories, credential scrubbing across logs and UI diagnostics, and redirect refusal on bearer-authenticated requests.
