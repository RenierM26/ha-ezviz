# Security policy

## Reporting a vulnerability

Please [report vulnerabilities privately through GitHub](https://github.com/RenierM26/ha-ezviz/security/advisories/new).
Do not publish exploit details or credentials in issues, PRs or logs. Include
an affected tag/commit, impact and minimal synthetic reproduction. Never send
real passwords, tokens, verification/encryption keys, account responses or
signed media/RTSP URLs. If private reporting is temporarily unavailable, do not
post the vulnerability publicly; wait for that channel to be restored.

## Scope and updates

Reports about this custom integration are welcome. No fixed response SLA or
maintenance/backport guarantee for older tags is offered. Fixes normally target
`main` and a subsequent reviewed release; unreleased main is not automatically
a published HACS update. Report upstream SDK issues privately to its maintainer
where appropriate, noting the consumer integration and affected SDK version.

## Development protections

Use synthetic offline fixtures. GitHub secret scanning/push protection and
security-update PRs supplement review; they are not proof a log or diagnostic
is safe to share. Never auto-merge compatibility changes or publish from a PR
workflow. See [support guidance](SUPPORT.md) and [release policy](docs/releasing.md).
