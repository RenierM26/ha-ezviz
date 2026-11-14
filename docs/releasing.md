# Maintainer release checklist

A merge is not a release or deployment. This guide defines reviewable evidence;
it does not authorize pushing a tag, publishing a GitHub release or changing HA.
The F06 PR deliberately leaves the integration manifest at `0.2.0.25`.

## 1. Prepare a versioned candidate

- Choose the version/prerelease policy explicitly. Existing tags use **bare
  versions**, e.g. `0.2.0.25`, exactly equal to `manifest.version`; do not add a
  `v` prefix or move an existing tag. A new source revision needs a new version.
- Update `custom_components/ezviz_cloud/manifest.json` in a reviewed version PR.
  Convert the relevant [Unreleased change notes](../CHANGELOG.md) to the chosen
  version and update README/setup/upgrade release-status wording for that tag.
  Do not describe untagged main as an already published HACS update.
- Review breaking changes, migration/rollback limits and HA/SDK minima. If the HA
  baseline changes, update `hacs.json`, native/acceptance/dev profiles, CI and docs
  together, checking the matching HA plugin in a separate environment.
- Adopt SDK changes only after the needed SDK version is published on PyPI.
  Keep `requirements/sdk.txt`'s exact qualification pin, manifest minimum and
  installed test version aligned. Qualify the published wheel, not an editable
  SDK checkout. An SDK release does not publish this integration.

Current main cannot be released again as `0.2.0.25`: the existing tag names older
source. The new preflight rejects that condition instead of silently reusing it.

## 2. Qualify the exact commit

Use a clean candidate checkout and the [shared profiles](development.md):

```sh
.venv-tools/bin/python tools/check.py lint
.venv-tools/bin/python tools/check.py standalone
.venv/bin/python tools/check.py native
```

Also run native tests on both supported HA versions, the separate matching-plugin
acceptance profile, and actual Core namespace lint/types/translations/hassfest plus
runtime tests. Use the CI jobs for isolated hosted environments; do not mix layers
or install the HA plugin into the native environment.

Store the candidate's full SHA, workflow URLs, job conclusions and actual
Python/HA/SDK/tool versions. The moving-Core run reports its full Core commit;
record that as well. To repeat that policy baseline, manually dispatch
`core-policy.yaml` on the candidate branch with `core_ref` set to the recorded
**full Core SHA**, then verify the run's `headSha` is the candidate SHA. A branch
name or a newer successful main run is not evidence for another commit.

CI/HACS/hassfest now validate **all pushed tags**, including the repository's bare
version convention. Moving-Core remains its separate PR/manual/weekly policy gate,
not an automatic tag-publish job. Validation-action/image pins are documented in
[validation-pins.md](validation-pins.md). Record the existing custom-only SDK
conflict exception; never present it as built-in/custom coexistence approval.

## 3. Generate the source/metadata report

Fetch remote tags without forcing/replacing local ones, then qualify the clean
checked-out candidate:

```sh
git fetch origin --tags
release_commit=$(git rev-parse HEAD)
release_version=$(.venv-tools/bin/python -c 'import json; print(json.load(open("custom_components/ezviz_cloud/manifest.json"))["version"])')
.venv-tools/bin/python tools/check_release.py \
  --commit "$release_commit" --tag "$release_version" \
  --output /tmp/ha-ezviz-release-source.json
```

The command never creates/moves tags, edits versions, fetches credentials, performs
network requests, runs tests or publishes. It requires full commit identity, that
HEAD, a clean worktree, tag/manifest equality, matching qualified SDK minimum, and
regular tracked component files. It rejects a local existing tag at other source.
It reports the component Git tree and each committed file's SHA-256/size/mode.
A repeatable report does not imply CI or physical tests passed.

**Local-tag boundary:** fetch first and independently inspect remote tags and
GitHub release records before publication. An absent local tag does not prove
remote availability; the tool neither queries GitHub nor claims a version is
available on PyPI/GitHub. A matching existing tag is accepted for read-only
verification, not permission to republish it. Keep reports outside the worktree
or in an ignored evidence directory.

For a review artifact, use `git archive` of that **same SHA**, not a ZIP of your
working folder, and compare its component paths/contents with the report. HACS
currently uses the repository archive (`zip_release` is not enabled); do not add
or change a custom release-asset layout without qualifying that install route.
Git/archive tooling can affect container bytes; the committed tree/file hashes
are the source identity. These profiles are not a full OS/transitive-wheel lock.

## 4. Approval and publication are separate

Prepare the exact commit/tag, reviewed change notes, preflight report, hosted
results and any live-test limits before requesting publication approval. Notes
must distinguish source installed, SDK accepted, HA reported and physically
observed results. Offline CI is not live cloud/device/browser qualification.

Only after explicit authorization: create the chosen immutable tag and release
at the qualified commit, using correct stable/prerelease designation and reviewed
notes. Do not move a tag to fix a mistake; prepare a new version. Verify the
resulting tag resolves to the intended SHA and its tag-triggered validation runs
pass. Check release contents and selected-version HACS/manual installation in a
separate test environment before making any deployment claim.

Do not deploy/restart a household HA instance as part of publishing. A release,
HACS download, source installation, entry migration, runtime reload and physical
camera check are distinct outcomes with separate authorization/evidence.
