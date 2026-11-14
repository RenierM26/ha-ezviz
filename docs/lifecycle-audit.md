# Lifecycle and credential ownership audit (F02)

Baseline: merged foundations PR #239 (`7ef0caa`), 2026-11-14. Offline audit
using real HA entry setup/unload/removal, private Store files and executor work;
SDK/network and selected platform/view boundaries are synthetic. No live device
operation or production deployment is implied.

## Contract and evidence matrix

| Transition | Owner / invariant | Regression evidence |
| --- | --- | --- |
| Invalid saved token / client construction | Known SDK auth failures request reauth; no identity/token rewrite to bypass binding. SDK owns allocations before its constructor returns. | `test_token_identity.py` actual SDK constructor controls. |
| Initial login, MFA and abandoned flow | Pre-entry stable identity and staged token Store writes have critical-task owners; clients close in `credential_login` even on failure/cancellation. | `test_token_layout.py`, `test_hardening.py` flow-cancellation/HA-stop controls. |
| Setup after first poll | The retained runtime owns the client, coordinator and push wrapper through view registration, platform forwarding and push startup. Any failure/cancellation in this region stops that owner before setup is abandoned. | New `core_policy/test_ownership.py` view/push-start failure controls use HA's real entry setup and then a fresh successful retry. |
| Poll/command caller cancellation | Executor/credential operations remain tracked; caller cancellation cannot cause HTTP close before work/option commit completes. | `test_hardening.py`, `test_reliability.py`. |
| Successful unload / reload | Cleanup remains owned when foreground wait ends. A replacement client is not created until prior cleanup completes, including retry after cleanup-stage failure. | `test_reliability.py` retained-owner setup and cleanup-stage controls. |
| Platform refuses unload | HA 2026.10.4 marks `FAILED_UNLOAD` unrecoverable. Do not fabricate a framework retry or stop the still-active owner. | New ownership control runs real HA unload, confirms retained `runtime_data`, no HTTP close and still-usable command executor. |
| Reauth while old writes are pending | Private Store lock/revision orders replacement after old writes and rejects superseded callbacks. Public metadata does not duplicate the token. | `test_token_layout.py` in-flight old save, stale owner, cancelled reauth and restart controls. |
| Camera key operation during unload/removal | A device-successful key operation retains its local commit while the entry exists; queued operations reject shutdown; removed entries are not resurrected. | `test_reliability.py` rotation/cancellation/actual HA-stop controls. |
| Removed entry / transient private unlink failure | One critical removal task retains persistence ownership, retries deletion and cannot accept late token writes. Log failure class once, not private exception text. | New ownership controls inject two unlink failures at each real token/staging/identity file and verify complete removal; actual HA stop also joins a stalled retry. |
| Old entry removal overlaps replacement account | Account identity is shared across entry lifetimes. Old token/staging files may be deleted, but identity used by another current cloud entry must survive. | New ownership control stalls old file deletion, registers/selects the replacement identity, then releases old cleanup and checks the real identity file. |
| Entity registry rename | Entity lifetime does not equal account lifetime. Stream recreation and siren remaining expiry survive re-add; stale callbacks/results stay fenced. | `core_policy/test_lifecycle.py` actual registry/EntityPlatform and restoration controls. |

## Confirmed gaps closed

1. View registration and push startup were outside the post-poll setup cleanup
   boundary. Failure left the retained runtime incomplete with no cleanup task;
   later setup could refuse the overlapping client indefinitely. Both stages
   now share the existing cleanup boundary.
2. Background removal attempted private deletion only once. An unlink failure
   left private files behind and produced an unhandled task exception without
   another owner trying deletion. Removal now retries every 30 seconds, retaining
   the same persistence object and critical-task ownership through HA stop.
3. Delayed removal always deleted the shared account identity, even if another
   entry had taken ownership of that account. Identity deletion now checks other
   current cloud entries while holding the shared identity lock.

Eight new controls pass; seven fail against `7ef0caa`, while the failed-unload
control passes both versions. The new controls do not claim to reproduce live
vendor failures; these are concrete lifecycle and filesystem fault-injection cases.

## Preserved boundaries and limits

No changes to entity/action IDs, config-entry version/migrations, private file
formats, SDK requirements, callback token contents or normal authentication.
The retained `hass.data` anchors and their line-scoped lint exceptions remain
necessary. No new retained-data anchor or blanket lint waiver was introduced;
one explained, line-scoped BLE001 boundary retains failed removal for retry.

Deletion retry ownership is process-local, not a durable cross-restart deletion
queue. Persistent storage failures can keep cleanup pending and are reported;
forced process termination is outside this guarantee. A successor-entry check
protects current entries, not a durable reservation for an unfinished initial
flow. HA's Store private-write acknowledgement hook remains narrowly tested and
must be requalified as HA evolves. These limits must not be hidden by a structural
refactor or confused with physical cloud/device qualification.

Next: F03 may extract cohesive config/credential-flow helpers after this slice,
keeping the same ownership, form/MFA/storage and identity contracts.
