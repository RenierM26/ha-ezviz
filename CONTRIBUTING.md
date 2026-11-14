# Contributing

Start with [development and shared check commands](docs/development.md),
[test layer responsibilities](tests/README.md) and [architecture](docs/architecture.md).
Use Python 3.14.2+ and the separate pinned environments; do not mix the HA pytest
plugin into native runtime tests. No live credentials are needed for offline checks.

1. Search existing issues; use the bug, device-support or feature form.
2. Make a focused branch and PR against `main`; use the PR template.
3. Run the checks appropriate to the change and report exact versions/results.
   Behavioral changes need regression coverage at the layer that owns the contract.
4. Preserve public entity/device IDs, credential ownership and migration recovery.
   Explain intentional compatibility changes and update relevant docs/change notes.
5. Never commit real tokens, keys, account payloads, device identifiers or private
   media URLs. Use synthetic fixtures; see [security reporting](SECURITY.md).

The always-running **Merge gate** summarizes repository checks and the selected
runtime/validator suites. Docs-only PRs still get a gate result. Maintainers apply
release-note labels, including `breaking-change` manually where appropriate.
Device/app/browser outcomes must be identified as observed or unverified.

Dependency updates remain reviewable PRs. HA, its pytest plugin and SDK upgrades
must align profiles, manifest/HACS minima, compatibility tests and docs; no
SDK auto-merge is configured. See [repository maintenance](docs/repository-maintenance.md).
A merge does not publish a release; follow the [release checklist](docs/releasing.md).
