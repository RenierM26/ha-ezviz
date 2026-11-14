# Repository maintenance

## Settings versus versioned files

GitHub's description/topics/homepage, label definitions, security features,
merged-branch deletion and branch protection are live repository settings, not
activated by merging YAML. `.github/labels.json` records the additional labels;
GitHub defaults plus `dependencies` and `github_actions` are retained. Change the
live definitions when editing that file. No automatic issue closure, dependency
auto-merge or release publication is configured.

Issue forms label reports by type. The path labeler adds area/documentation/
maintenance labels to PRs, without removing manual labels. `needs-info`,
`needs-device-testing`, `breaking-change` and `fix` are maintainer decisions;
the bug-form's area dropdown does not automatically add an area label.
The labeler uses `pull_request_target` solely for labeling: it never checks out
or executes PR code, and reads its configuration from the base branch.

## Merge gate

`CI` runs on every PR, main push, tag and manual invocation. Its scope job compares
all changed paths against the PR merge base, with rename detection disabled to
include both old and new paths. Exclusively Markdown changes skip the runtime
matrix; every other path, including unknown/configuration paths, runs it fully.
Failures to determine scope fail closed. Repository YAML/forms/label validation
and pinned actionlint run in either case.

The **Merge gate** job uses `always()` and requires scope/configuration success.
Full changes require lint/standalone, acceptance, both native HA versions,
moving-Core policy, HACS and hassfest success; docs-only changes require those
jobs to be explicitly skipped. Failed/cancelled/missing jobs cannot pass. CI also
runs for draft PRs; making a PR ready reruns it. GitHub branch protection requires
this single gate from the GitHub Actions app, with the branch up to date. Existing
review/admin policies are retained; this is not a promise administrators cannot
bypass protection. Do not add individual path-filtered required checks.

CI calls the existing validators as reusable workflows, avoiding duplicate PR
runs; their standalone manual/scheduled paths remain. Their logs still record
actual HA/SDK/Core/tool versions. Release qualification calls the same CI against
an explicit candidate SHA and full Core SHA, not a newer main revision.

Local repository checks (separate from HA environments):

```sh
python -m pip install -r requirements/repository.txt
python tools/check_repository.py
go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12
"$(go env GOPATH)/bin/actionlint" -shellcheck= -pyflakes=
```

The Go module checksum database verifies the pinned actionlint module; Go may
fetch a compatible toolchain. Shellcheck/Pyflakes integration is disabled so the
workflow schema/expression check does not depend on runner-installed extras.
See [development](development.md) for the existing Python lint/test profiles.

## Dependencies and security

Dependabot opens weekly grouped Actions updates and Python development-tooling
updates, plus HACS Docker updates. HA, its pytest plugin and `pyezvizapi` are
excluded from the tooling group, not excluded from update proposals. Review their
individual PRs together with compatibility profiles/minima. Dependabot cannot
update the manifest's JSON SDK minimum or inline hassfest image automatically;
maintainers must align those in the PR. See [validation pins](validation-pins.md).

Secret scanning, push protection, private vulnerability reporting and Dependabot
security updates should remain enabled where GitHub supports them. Neither a
scan nor passing CI proves fixtures/logs are safe to publish. See [security](../SECURITY.md).
