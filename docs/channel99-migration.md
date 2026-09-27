# Channel-99 migration (preview)

This branch requires pyEzvizApi PR #276. The manifest temporarily pins its current
commit as a source archive; replace this with the published package version before
releasing the integration. Tested runtime: Home Assistant 2026.9.2 / Python 3.14.

## Upgrade experience

Existing web-profile entries require a fresh EZVIZ sign-in using HA's
reauthentication flow, including MFA when EZVIZ requests it. Camera options and
entity identifiers are retained. Do not delete/re-add the account entry.

The complete initial Android-profile token is stored with the config entry; no
account password or MFA code is stored. Subsequent HTTPS and push credential
rotation is stored privately through HA's Store API. Storage writes are atomic,
use fsync, and must complete successfully before the SDK continues its handshake.
Storage errors stop push and create a repair issue; they never trigger silent
allocation of another push-device identity. Do not delete token storage to work
around a disk/permission problem. Setup verifies the private store with an
acknowledged write before clearing that repair, so merely reading a token after a
restart does not claim that disk or permission problems are fixed.

## Lifecycle

Entities and 30-second polling do not wait for push registration. The SDK handles
ordinary registration retries and reconnects; there is no second HA retry loop.
The integration checks fatal worker errors every 5 seconds;
authentication/recovery errors request user reauthentication, while storage errors
create a separate repair issue. Polling remains independent.

All blocking SDK operations and stop/join calls run in executors. Token callbacks
marshal storage writes to HA's event loop and wait in the worker, never in that
event loop. Token saves across reloads are serialized, and obsolete reauth workers
cannot overwrite current credentials.

Unload waits up to 5 seconds for cleanup. If a setup/network operation is still
pending, unload returns False and keeps the old runtime registered while cleanup
continues. Retry reload once cleanup completes. This avoids a second worker using
the same persisted push identity. HA shutdown itself is not held indefinitely.
At terminal shutdown the integration cancels its tracked cleanup retry after the
five-second deadline and marks it abandoned; late startup completion still runs
the SDK's synchronous stop guard, but cannot create another HA cleanup task.

Loaded resources live in typed `ConfigEntry.runtime_data`. Platforms, diagnostics,
options and media browsing read that same owner. Diagnostics are generated from
cached state only: they never make an EZVIZ request. They report polling health,
push lifecycle and retry state, event timing, credential-storage health, and safe
setup-failure categories. Camera serials, names, credentials, endpoints, and other
identifiers are redacted, including serials used as mapping keys.

The monitor is an entry-owned
background task; cleanup is a separate tracked task so monitor cancellation cannot
skip SDK stop. An awaited HA stop-event handler quiesces polling and waits for that same
cleanup task after background cancellation. Ordinary unload unregisters the
handler. Token storage continues to reject deferred shutdown writes. Failed setup closes the HTTP client and shuts down its
coordinator. If platform unload fails after push stops, a replacement handler is
started only after the old SDK worker has exited.

The five-second fatal-error check and diagnostic snapshot are nonblocking SDK state
reads, not executor jobs or reconnect loops.

## Validation and remaining release work

- Offline lifecycle and real HA-runtime tests cover setup/polling independence,
  save acknowledgement/failure, reauth races, initial login/MFA, reauth/MFA,
  degraded diagnostics, and recursive identifier redaction.
- This is not yet a deployment test in the user's running Home Assistant.
- Replace the temporary dependency archive pin with a released version.
- Confirm Android background notification coexistence and long-duration behavior.
- Binary mobile variants remain outside the decoded notification callback scope.
