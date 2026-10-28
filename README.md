# Draft persistence API

Milestone 3 service: Python standard library HTTP adapter, independent layout validation, and transactional SQLite. This uses the design's optional stack choice to avoid third-party deployment dependencies. Run one service process behind your existing HTTPS reverse proxy. Domain and deployment remain owner-configured; nothing has been published or connected to a live account.

## Configure and run

Use Python 3.10 or newer. Create a private data directory owned by the service account. Configure these environment variables outside source control:

| Variable | Value |
| --- | --- |
| `OBBY_BOOTSTRAP_SECRET` | A cryptographically random credential of at least 32 characters |
| `OBBY_GAME_ID` | The numeric `world.WorldID` this API accepts; `0` only for a deliberate local test |
| `OBBY_DATABASE` | Absolute path to the SQLite file in the private data directory |
| `OBBY_BOOTSTRAP_SECRET_PREVIOUS` | Optional old credential for a brief rotation overlap |

Generate a credential on the deployment host with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. Do not paste actual credentials into committed game scripts, shared modules, URLs, or issue/log output. `site-api/.env.example` contains names only; the service reads environment variables and does not automatically load `.env` files.

Start the service with your environment already set:

```sh
python3 site-api/app.py --host 127.0.0.1 --port 8127
```

The first start applies migration 1 transactionally. The service fails startup if required settings are missing. Route your chosen HTTPS API host to `127.0.0.1:8080`. Keep the database and backups outside the web document root. Configure proxy request limits at 150,000 bytes and a response timeout that accommodates short SQLite contention. Only `/health` is anonymous. All game API calls use `Authorization: Bearer ...`, including GETs. The adapter suppresses request/body/header logging and returns bounded display-safe errors.

The service limits requests to 120/minute per authorization/IP budget and registration to 30/minute. SQLite foreign keys and a five-second busy timeout are enabled. WAL is deliberately not enabled before operational testing. No transaction waits on an external network service. Do not run multiple service processes until write contention and migrations have been evaluated.

## Connect the game

1. Open **`game/persistence-preview.ptproj`** (or `persistence-preview.poly`) in Creator. It includes the new server-only modules and worker. Existing runner/editor preview worlds remain available.
2. Leave `PersistenceConfig.enabled=false` while verifying the HTTPS JSON/header round trip with the existing HTTP probe. The game continues to show **Session-only**.
3. Verify in the actual target client that `ServerHidden` contents and linked server-only configuration are unavailable to clients. The supplied API documents that ServerHidden is not replicated, but distribution/source access must also be checked before provisioning a real credential. The project does not invent an environment-variable or secret-manager API for Polytoria.
4. Provision a server-only **StringValue** named **ApiBootstrapSecret** under **ServerHidden** using your supported private deployment mechanism. Its Value must match `OBBY_BOOTSTRAP_SECRET`. No real value is included in the generated world. Do not commit a world file containing this value.
5. In `game/scripts/server/PersistenceConfig.luau`, set `baseURL` to the HTTPS API base, without `/v1`, and set `enabled=true`. The bootstrap game ID must match `world.WorldID`. For an explicit local-only experiment, `allowLocalHTTP=true` permits `http://127.0.0.1:PORT` only when `world.IsLocalTest` is true.
6. Restart Play. Click **My Courses / Build**. The server registers, opens trusted player presence, loads account quantities/five slots, acquires a lease, and fetches the selected draft. Save and Reload buttons appear once persistence is active. Initial connection can take several seconds because calls are globally serialized and spaced by 1.5 seconds.

This is ready for integration once the URL, transport, and credential-isolation gates are satisfied. Until then, durable saving remains disabled rather than claiming edits are stored.

## Save behavior

The game holds local editor revisions separately from API draft revisions. Autosave runs ten seconds after the latest edit, with at most one write in flight per slot. Manual Save uses the same queue. A save captures one immutable packet and request ID; timeout retries resend that exact packet. Edits made during upload remain **Unsaved** after the older acknowledgement and are coalesced into a later snapshot. Background acknowledgements update the status without cancelling a drag or typing.

Slot changes and editor exit flush the newest edits first. A failed save stops the transition and retains the in-memory layout. Revision conflicts stop blind retries. Reload requires a second press within six seconds before discarding local unsaved edits and loading the API draft. Disconnect saving is best-effort; only an API-acknowledged revision is durable after a game-server crash.

Heartbeats renew a 180-second server lease approximately every 60 seconds with jitter; edit leases renew independently. Sessions have a 12-hour absolute lifetime. A clean session handover closes the old server session, registers again, reopens presences/leases, and compares revisions before resuming writes. An expired connection with dirty or ambiguous writes preserves local data and requires explicit reload/conflict resolution. Tokens are stored only as SHA-256 digests by the API and are never sent to game clients.

## Implemented routes

| Method | Route |
| --- | --- |
| GET | `/health` |
| POST | `/v1/server-sessions` |
| POST | `/v1/server-sessions/{id}/heartbeat` |
| POST | `/v1/server-sessions/{id}/close` |
| POST / DELETE | `/v1/player-sessions` / `/v1/player-sessions/{id}` |
| GET | `/v1/players/{id}/bootstrap?presence_id=...` |
| POST / DELETE | `/v1/slots/{id}/edit-lease` |
| GET | `/v1/slots/{id}/draft?presence_id=...` |
| PUT | `/v1/slots/{id}/draft` |

Registration uses the bootstrap credential in its bearer header and `{game_id, protocol_version:1, display_name}`. Other routes use the issued per-server token. Lease operations carry `presence_id`; DELETE also carries `lease_id`. Draft PUT carries `{presence_id, lease_id, expected_revision, request_id, layout}`. The layout has schema/rules version 1, fixed start, parts, and checkpoint order. Responses use numeric UTC Unix timestamps. Error envelopes are `{code,message}`.

The API independently validates stock per slot, object count, field/type allowlists, finite transforms/colours, rotated bounds, entrance exclusion, and checkpoint ordering. It normalizes Luau's empty-table ambiguity only for empty part/order arrays and empty property objects; nonempty wrong container types are rejected. Canonical layout hashing sorts stable part IDs/keys and normalizes integral floats. Courses and public IDs are stable, but publication, discovery, moderation, catalogue purchases, verified entitlements, and currency rewards are not implemented in this milestone.

## Backup, retention, and restore

Use the online SQLite backup API instead of copying a possibly active database file:

```sh
python3 site-api/manage.py backup /srv/obby-api/data/obby.sqlite3 /private/backups/obby-backup.sqlite3
python3 site-api/manage.py maintain /srv/obby-api/data/obby.sqlite3
```

Backup requires a new destination and checks its integrity. Schedule backups and maintenance through your existing host tooling. Maintenance removes expired leases and authentication/idempotency history older than seven days; durable drafts and player inventory remain. Protect backup permissions like the original database. To restore, stop the service, retain the current database for recovery, restore a verified backup to the configured path, then restart. The automated test confirms an acknowledged draft can be read from a restored backup. Perform the same drill on the deployment host before launch.

## Verification

Run `python3 -m unittest discover -s tests -p 'test_*.py' -v` and the Deno Lua harness from the repository root. Coverage includes actual local HTTP routing, database reopen/backup, wrong owners/presences/tokens, competing leases, concurrent revision writes, idempotent retries and key mismatch, inventory reuse, invalid geometry, edit-during-save acknowledgements, delayed slot switching, outage feedback, explicit reload, and session renewal. Live HTTPS/Creator integration is still pending.
