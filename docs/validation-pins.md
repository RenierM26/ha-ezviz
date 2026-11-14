# Validation dependency audit

Audited 2026-11-14 for F06. These are validation-only dependencies; no component,
HA baseline or SDK requirement is changed. Exact hashes are in workflow files.

| Dependency | Audited selector / immutable execution reference |
| --- | --- |
| `actions/checkout` | `v7` → `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/setup-python` | `v7` → `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` |
| `actions/cache` | `v6` → `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` |
| HACS executable image | `ghcr.io/hacs/action:main@sha256:187499a09c5d41f9f0d42ff80d203ed87b974c028290140913b61e005fb0aab2` |
| hassfest executable image | `ghcr.io/home-assistant/hassfest:latest@sha256:da6522993280166fe072704989a2b4c33e639e8ceb69c8ab6ad7ec5045c1a1e3` |

Action refs were resolved through their official GitHub repositories, dereferencing
annotated tags where necessary. Container digests were obtained from the GHCR
OCI manifest responses. Hosted execution must qualify these pins; a successful
metadata lookup alone is not a validator pass or an independent security audit.

## Why HACS has a local wrapper

Pinning only `hacs/action`'s outer Git commit would not freeze its executable:
its [action metadata at `1ebf01c`](https://github.com/hacs/action/blob/1ebf01c408f29afcb6406bd431bc98fd8cbb15aa/action.yml)
selects floating `docker://ghcr.io/hacs/action:main`. The small
[local action wrapper](../.github/actions/hacs/action.yml) preserves that input
contract/defaults and builds a Dockerfile whose **FROM image is digest-pinned**,
retaining the upstream image entrypoint. No HACS implementation is copied.
Category/ignore behavior remains `integration` / the pre-existing `brands` skip.
This is not permission to broaden ignored checks or the hassfest SDK exception.

## Moving references that remain intentional

- Core policy defaults to `dev` so PR/weekly runs detect current policy changes.
  Record the resolved Core commit; `workflow_dispatch.core_ref` accepts an exact
  Core SHA for reproduction. A default moving-dev result is not a fixed baseline.
- `ubuntu-latest`, Python `3.14`, HA-owned/transitive requirements and PyPI wheels
  still evolve. Record actual versions/job logs and reproduce in the stated
  profiles; do not claim a complete transitive/OS lock or bit-for-bit environment.
- Image selectors include readable `main`/`latest` labels **with digests**. Docker
  executes the fixed digest, not whatever the label points to on a later date.

## Refresh procedure

1. Review the official action/tag/image source and change notes. Resolve the new
   commit/digest; do not copy an unverified hash from an issue or silently unpin.
2. Update pins in a reviewable PR and update this audit record. Dependabot monitors
   GitHub Actions and the HACS Dockerfile; hassfest's inline digest needs deliberate
   refresh. Review validator pins at each release and at least monthly.
3. Run shared lint/standalone, actual HACS wrapper, pinned hassfest and the native/
   acceptance/Core policy matrix on that exact head. Preserve existing narrowly
   justified exceptions; a newly failing validator is a finding, not grounds to
   widen an ignore list.
4. Record refs, digests, platform, actual tool/Core versions and hosted result URLs.
   Reject a proposed action downgrade merely because an older version tag exists;
   HACS's old release tags do not represent its current main image contract.

The repository does not publish/deploy from these validation jobs. Tag validation
now includes bare versions as well as `v`-prefixed tags; the release preflight and
maintainer convention still require the chosen tag to equal the manifest version.

## Repository automation additions (2026-11-14)

- `actions/labeler` `v7.0.0` → `bf12e9b00b37c5c0ca2b87b79b2daf7891dbda13`.
- `actions/upload-artifact` `v7.0.2` → `cf430e030ddbb5b0abf93d22962f4752f3646cd9`.
- Workflow expression/schema validation uses Go module
  `github.com/rhysd/actionlint/cmd/actionlint@v1.7.12`.
- Repository forms/YAML validation uses `requirements/repository.txt` (PyYAML).

Action refs were resolved through official repository tag refs. These actions
remain SHA-pinned and covered by weekly Dependabot Actions PRs. Python tooling
updates and the HACS Docker wrapper are also proposed weekly; review Core/HA/SDK
profiles separately. The release-preparation workflow creates drafts only.
