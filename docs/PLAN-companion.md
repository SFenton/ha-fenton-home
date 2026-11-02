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
2. Restart, then Settings → Add integration → S+H Home Integration (or add `fenton_home:` to `configuration.yaml`
   before the restart): HA loads a domain only with an entry or a YAML key. The import copies the old entry, removes it,
   and the integration serves the new and alias paths.
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
- Check first: deleted registration `device_tracker.stephen_s_iphone_fenton_2` (webhook 4D77…, removed 2026-11-02) kept
  sending fixes until the 16:28Z restart, although `devicectl` showed no `com.sfenton.home.uikit` app on the Duo.
  Find which install held it before trusting "a deleted registration stays deleted".
- Later, behind a dry-run option: sweep abandoned S+H registrations (no push details and 14 days without an update, or
  superseded by the same user's newer registration of the same model and 7 days without an update). Never touches
  other apps' registrations; every removal is logged.

## 1.2 (with or right after 1.1): presence auto-enrolment

Owner request, 2026-11-02. Full brief: Fenton-HASS-Native `build/handoff/presence-fenton-home-brief.md`; background
`build/handoff/presence-composite.md`.

- Why: Stephen carries a Duo or an 18 Pro. The person followed whichever tracker reported last, so the phone left at
  home flipped the house Away → Home about 30 times a day. Live fix today: `ha-composite-tracker` 4.3.0, package
  `/config/packages/presence_composite.yaml` (`require_movement: true`), and each person tracks only its composite.
- Gap: mobile_app adds a new registration's tracker straight to its user's person, which bypasses the composite.
- `presence/` part, its own on/off option:
  1. On startup and on any mobile_app tracker create or remove, map registration → `entry.data["user_id"]` → person
     (the ownership lookup 1.1 adds).
  2. Keep that person's composite inputs equal to all the user's mobile_app trackers (Companion ones included) minus an
     exclude list, seeded with `device_tracker.iphone_18_pro` and `device_tracker.ipad`.
  3. Keep the person bound only to its composite; undo mobile_app's auto-add.
  4. A registration removed by 1.1's sign-out drops out of the composite through the same path.
  5. `composite` in `after_dependencies`; missing → presence off with a Repairs issue.
  6. No writes when nothing differs; each change logged at info.
  7. One source of truth: move both composites from YAML to Composite config entries and own their options in code
     (read `composite/config_flow.py` and `__init__.py` for how a YAML import overwrites options); retire the package.
- Done: a dry run shows no change today; a test adds a tracker to a person and sees it moved into the composite;
  memory `presence-composite-trackers` and the package header updated or the file removed; ask before any HA restart.
