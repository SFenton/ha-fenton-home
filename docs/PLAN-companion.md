# Plan: Fenton Home Push becomes the S+H Home Integration

Owner decisions (2026-11-02):
- The integration is the S+H app's server half, not only a push relay: push, Live Activities, and now device lifecycle.
- Display name **S+H Home Integration**. Domain `fenton_home`. GitHub repo `SFenton/ha-fenton-home` (GitHub redirects the old URL).
- No separate API key. Requests are authorized by Home Assistant's own login (short-lived access token plus refresh token,
  revocable from the HA profile). The one exception is a deferred unregister, authorized by the registration's webhook id.
- Rename first (1.0, no behavior change), then device lifecycle (1.1), so no new endpoint is born under the old path.

## What the old name touches (inventory, 2026-11-02)

| Where | What | Plan |
|---|---|---|
| HA config entry | domain `fenton_push`; holds the APNs key | copied in-process by `fenton_home` on first start, then removed |
| `.storage/fenton_push.live_jobs` | live job store | loaded and saved as `fenton_home.live_jobs`, old store removed |
| `/api/fenton_push/config`, `/api/fenton_push/apns` | the app reads config; S+H registrations' `push_url` is `/apns` | new `/api/fenton_home/...`; old paths kept as aliases |
| `automations.yaml:5339` | `fenton_push.problem` | edited at cutover; alias service kept one release with a Repairs issue |
| manifest, `hacs.json`, `strings.json`, README | "Fenton Home Push" | "S+H Home Integration" |
| app (`HomeAssistantPushClient.configPath`, 5 source comments, ~26 docs/tests/tools) | `fenton_push` | new path, fallback to old on 404 |

Unchanged: `notify.fenton_<person>` (named by `NOTIFY_SERVICE_NAME = "fenton"`), the `notify.stephen_s_phone` group,
the `fenton_live_activity_*` events. The integration owns no entities.

## 1.0: rename, no behavior change

1. Move `custom_components/fenton_push` to `custom_components/fenton_home`; split into `push/`, `live/`, `devices/`.
2. Import on first start: when a `fenton_push` config entry exists, start an import flow that copies its data and
   options (never logged), remove the old entry, migrate the live-job store. No `.storage` hand edits (HA AGENTS.md).
3. Aliases: register `/api/fenton_push/config` and `/api/fenton_push/apns` on the new handlers, and a `fenton_push.problem`
   service alias that raises a Repairs issue when called. When every S+H registration's `push_url` points at
   `/api/fenton_home/apns`, raise a Repairs issue saying the aliases can go; a later release drops them.
4. Contract: `docs/CONTRACT.md` v2 names the new paths; v1 paths stay valid while the aliases exist.
5. App (Fenton-HASS-Native, `claude/uikit-port`): read `/api/fenton_home/config`, fall back to `/api/fenton_push/config`
   on 404. The app already re-sends `push_url` on every launch, so registrations move on the next open.
6. Tests: import flow (data and options copied, old entry removed, store moved), aliases, the Repairs issues.

### Cutover on the owner's server (one restart, minutes without push)
1. Back up `/config/automations.yaml`. HACS: install S+H Home Integration, remove Fenton Home Push.
2. Restart. `fenton_home` imports the old entry and serves the new and alias paths.
3. Edit `automations.yaml:5339` to `fenton_home.problem`; `ha core check`; reload automations.
4. Send a test `notify.fenton_stephen`; open S+H on each phone; confirm `push_url` moved.

## 1.1: device lifecycle

- `POST /api/fenton_home/devices/unregister`, two routes:
  - authenticated (HA access token): the entry's `user_id` must be the caller's; removes the `mobile_app` entry.
  - deferred (no login): body carries the webhook id; removes only that registration. Same trust as `mobile_app` webhooks.
- On any S+H `mobile_app` entry removal (app, admin, sweep), drop this integration's state for that webhook: live
  jobs, start tokens, rate counters.
- App: sign-out unregisters before revoking the login and forgets the Keychain registration. A failed call stores
  `{server, webhook}` and retries the deferred route on next launch or network change. Re-login registers fresh
  (a 410 on a stale webhook already re-registers).
- Later, behind a dry-run option: sweep abandoned S+H registrations (no push details and 14 days without an update, or
  superseded by the same user's newer registration of the same model and 7 days without an update). Never touches
  other apps' registrations; every removal is logged.
