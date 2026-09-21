# New-app API validation

Validated on 21 September 2026 against a real account before and after migration.
The API is unofficial; the implementation retains native response fields rather
than assuming the new service is a drop-in replacement for the PHP API.

## Evidence

- [Integration issue #178](https://github.com/ThomasHFWright/kippy-homeassistant/issues/178)
  identifies Cognito/AppSync as the new account backend.
- [Issue #179](https://github.com/ThomasHFWright/kippy-homeassistant/issues/179)
  shows legacy login result 108. That can mean a wrong password or credentials
  which only exist in the new app; it is not a version indicator on its own.
- [Issue #180](https://github.com/ThomasHFWright/kippy-homeassistant/issues/180)
  reports a missing second tracker. Our multi-pet account worked on both APIs;
  this does not establish the cause of that reporter's problem.
- The [Lola integration at 048ca22](https://github.com/IamDiesel/kippy-homeassistant-lola/tree/048ca22a3693276ca96d93310fdca15346ba3c78)
  supplied the protocol reference. Its MIT attribution is retained. Live schema
  introspection confirmed the operation names, inputs and response structures.
- Cognito password authentication follows [AWS InitiateAuth](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_InitiateAuth.html).
  The client uses aiohttp, normal certificate verification and bounded requests;
  no AWS SDK or additional runtime dependency is required.

## Account detection

Before migration, legacy login succeeded and Cognito returned UserNotFoundException.
After migration, both logins succeeded. GraphQL getUser reported migrated=true.
All four pets existed in both services, but pet IDs differed. All four tracker
serial numbers matched; three trackers had active subscriptions and cached GPS data.

`async_connect(..., backend="auto")` tries GraphQL first. Only Cognito
UserNotFoundException or NotAuthorizedException triggers a legacy login attempt.
Network failures, throttling, MFA, unconfirmed accounts, required password resets
and empty pet lists do not trigger a switch. Backend selection is retained by the
returned client; subsequent requests and token refreshes never re-detect it.
Explicit `legacy` and `graphql` selection remain available.

This detects which account can authenticate and prefers the modern service. It
cannot establish that an independently created new account contains all devices
from a legacy account. Do not select by firmware, pet type, empty responses, or a
successful legacy login alone. The migrated flag is useful confirmation once
GraphQL authentication succeeds, not an unauthenticated discovery endpoint.

## Methods and live results

| Library method | GraphQL operation | Validation |
| --- | --- | --- |
| `get_user` | `getUser` | Account ID and migrated flag returned |
| `get_pets` | `getPets` | Four pets; IDs remain strings |
| `get_products` | `getProducts` | All four trackers and subscription states returned |
| `get_petlink_gps` | `getPetlinkGps` | Cached position, status and settings for all three active trackers |
| `get_activity_report` | `getActivities` | General report returned for all four pets |
| `get_cat_activity_report` | `getActivitiesCat` | Schema valid; application result 404 after migration, surfaced as an error |
| `get_activities_by_hour` | `getActivitiesByHour` | Successful empty lists after migration |
| `get_positions_history` | `getPositionsHistory` | Successful empty lists for the requested recent day |
| `send_command` | `sendCommand` | Schema and synthetic tests; no live device command sent |
| `app_keep_alive` | `appKeepAlive` | Schema and synthetic tests; not sent live |
| `send_setting` | `sendSetting` | Schema and synthetic tests; no live settings changed |

Read responses use application code "200" inside HTTP 200. Missing or different
codes raise KippyResponseError with return_code metadata, including "404".
GraphQL errors and partial responses also raise. Never manufacture zero activity
when a report is missing. An empty history is evidence of an accepted query, not
proof that populated history or historical migration has been validated.

Mutations require explicit native inputs and return the server acknowledgement.
A successful acknowledgement does not prove that a tracker executed a command.
Tokens renew before Cognito's ExpiresIn deadline, with a per-client login lock.
Only HTTP 401 permits one retry after login; transport failures and GraphQL errors
are not replayed. A malformed token returned HTTP 500/AuthorizerFailureException in live validation;
that remains a service error, not a reason to replay a request. No request or
response payload, token, email, location or backend error message is logged.

## Differences from the reference implementation

- Cached GPS reads never issue WAKEUP, LIVE_TRACKING or appKeepAlive mutations.
- Activity calls use the supplied dates and timezone. Cat reports require one ISO
  week; no weekly total is mislabeled as today's measurement.
- Native activity names are retained: onTheMove is not renamed to rest, and
  highMovement is not duplicated into run, play and climb.
- Settings are explicit. Changing GPS behavior does not silently overwrite update
  frequency with 60, or vice versa.
- All products remain visible; unknown entity types are not silently filtered out.
- Caller-owned HTTP sessions remain open; TLS compatibility settings remain scoped
  to the existing legacy client, not AWS.

## Home Assistant follow-up

KippyApi remains the legacy interface. KippyGraphQLApi is an additive native client,
not a replacement with fabricated legacy payloads. The current integration pin
continues to use the legacy client until a registry-safe migration is implemented.

For an existing config entry, enumerate both APIs while available, match trackers
by unique serial number, and persist new pet/product IDs alongside existing entity
identifiers. Match only unambiguous serials; never guess by pet name or coerce UUIDs
to integers. Preserve the existing registry IDs and user customization. If legacy
access is unavailable or matching is ambiguous, request explicit resolution.
After migration, persist the chosen backend and do not switch during polling.
New config entries can use automatic authentication selection directly.

The HA adapter also needs native activity semantics and capability-aware controls,
with migration/reload tests. Do not close the three issues merely because API
packaging or this native client has landed: their original reports need validation.

## Verification of this change

The standalone suite passes 142 tests on Python 3.13 and 3.14 (99.6% coverage),
including backend selection, error/partial-response handling, date boundaries,
explicit mutations, expiry scheduling and concurrent refresh. Ruff, mypy,
wheel/sdist builds and distribution metadata checks pass.
The installed 1.1.0 wheel was also exercised without Home Assistant against the
migrated account, including automatic GraphQL selection, all read methods listed
above and a forced token-renewal deadline. No live device command was sent.
