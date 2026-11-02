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

- `POST /api/fenton_home/devices/unregister` with `{"webhook_id"}` (shipped 1.1.0, `docs/CONTRACT.md` 2a): one route, no
  login required (the webhook id is the proof, as for `mobile_app` webhooks), removes only an S+H `mobile_app` entry; a
  valid login, when sent, must be the registration's user. 410 for an unknown webhook, a per-address cap without login.
- On any S+H `mobile_app` entry removal (app, admin, sweep), drop this integration's state for that webhook: live
  jobs, start tokens, rate counters.
- App: sign-out unregisters before revoking the login and forgets the Keychain registration. A failed call stores
  `{server, webhook}` and retries the deferred route on next launch or network change. Re-login registers fresh
  (a 410 on a stale webhook already re-registers).
- Check first (done 2026-11-02, read-only on the server). Home Assistant 2026.10.4 core logs run back to Oct 30. They
  hold **no mention of `4D77`**, of the removed entry id, or of any post to a removed webhook in the 08:28 to 10:28
  window (times are server-local, UTC-8, so the restarts are 16:28Z, 18:28Z and 19:52Z). The only "Received message for
  unregistered webhook" lines (45, Oct 30 to Nov 2 10:28:49, from the home address) are for one other webhook
  `dcb1a2…`, which began the minute the first S+H registration was created, so it looks like a stale webhook of an
  earlier install. `4D77…` is not 64 lowercase hex like a webhook id; it looks like the Duo's device id, which the
  logs and registries I may read don't show. What the core source does show: `webhook` answers a post to an
  unregistered id with 200 and an empty body (logged at info), so the app is never told it was removed; `mobile_app`
  only registers the 410 "Deleted Webhook" handler for removed ids at *setup* (`async_setup`, from its stored
  `deleted_ids`), so the 410 appears only after the next restart. That explains "kept sending until the restart"
  (the app sees success, then 410 and re-registers) but not stored fixes: a post to a removed webhook is dropped and
  can't move a tracker. Conclusion: "a deleted registration stays deleted" **holds on the server** (entry, entities and
  webhook handler are gone and posts are discarded), and I found no evidence of fixes being accepted after deletion.
  What doesn't hold is the app finding out. Unverified: which install on the Duo held `4D77…`, and what moved
  `device_tracker.stephen_s_iphone_fenton_2` (the recorder database and `.storage` are off limits); a composite
  tracker or the other registration may have kept the person's location current. Hence 1.1: the app unregisters on
  sign-out and treats an unknown webhook as gone, and removal purges our state.
- Shipped in 1.1.0 (`sweep_mode`, default `dry_run`): sweep abandoned S+H registrations (no push details and 14 days without an update, or
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

### Shipped in 1.2.0 (`presence.py`; `presence_enabled` off, `presence_dry_run` on, `presence_exclude` seeded)

- Pass at startup, 2 s after any `device_tracker` registry create, remove or disable, and on any person change.
  Registration -> `entry.data["user_id"]` -> person with that `user_id`; composite = the person's tracker whose registry
  platform is `composite`. Inputs keep order and settings, other integrations' inputs stay, the last input is never
  removed; `mobile_app` trackers come off the person once the composite covers them (or they are excluded).
- Read-only facts from the live server (2026-11-02, HA 2026.10.4, websocket `config/entity_registry/list`, `person/list`,
  `get_states`; fixture `tests/fixtures/presence-real-2026-11-02.json`): both persons track only their composite; both
  composites are `source: import`; their inputs are as the brief says. Composite 4.3.0 `async_step_import` replaces an
  import entry's options on every `composite.reload` and restart, and import entries have no options flow, so presence
  never writes one: it reports (log + Repairs "Move the Composite trackers out of YAML"). UI entries get their options
  from the code through `async_update_entry`; Composite's own update listener applies them without a reload.
- Found, and the reason the migration order matters: when an entity leaves the registry, `person` removes it from every
  person's trackers, so deleting the YAML entries un-binds both persons until step 5.
- Unverified: whose registration `device_tracker.iphone_17_pro` ("Stephen's iPhone", model "iPhone 13 Pro (6)", Companion,
  entry `01K7MRNWZ39QSNV4M86SJZ9JAP`) is. No read-only API exposes a `mobile_app` entry's `user_id`. If it is Stephen's,
  presence would add it to his composite (once the composite is a UI entry); add it to the exclude list first if that is
  not wanted. The same holds for who owns `device_tracker.ipad`, which is excluded anyway.
- The YAML to UI move is **not done**; the owner approves it first.

#### Moving the composites from YAML to the UI (to be approved by the owner; nothing here has been done)

Today `/config/packages/presence_composite.yaml` defines `device_tracker.stephen_location` (Duo
`device_tracker.stephen_s_iphone_fenton` + 18 Pro `device_tracker.iphone_fenton`, both S+H) and
`device_tracker.stephanie_location` (`device_tracker.stephanies_iphone`) with `require_movement: true`. Nothing else in
`/config` YAML mentions them (checked 2026-11-02; UI-built dashboards and automations in `.storage` were not).

Two facts decide the order. A UI entry's entity id comes from its name, and the YAML entries own the ids until they are
removed. And when an entity leaves the registry, Home Assistant's `person` removes it from every person's trackers.
So the persons are un-bound for a few minutes in the middle; do it when both are at home or both away, and expect
`person.*` to show `unknown` meanwhile.

1. Install 1.2.0 and restart. Leave presence off, or on with dry run, and read the log: with YAML composites a dry run
   reports "managed by YAML" if anything differs and otherwise says nothing. Turn dry run off only after step 5.
2. Back up: `cp /config/packages/presence_composite.yaml /config/packages/presence_composite.yaml.yaml-to-ui-<date>.bak`.
3. Remove the `composite:` block from the package file and leave only a comment saying the composites are UI entries
   owned by S+H Home Integration (or delete the file). `ha core check`, then call `composite.reload` (no restart): the two
   "from configuration" entries, their entities and the battery and speed sensors are removed, and both persons lose
   their composite tracker.
4. Settings, Devices & services, Composite, Add entry, twice. Name exactly `Stephen Location` and `Stephanie Location`
   (the removed entries' names are free again). Inputs: Stephen
   `device_tracker.stephen_s_iphone_fenton` and `device_tracker.iphone_fenton`; Stephanie `device_tracker.stephanies_iphone`.
   Require movement on, the other options off, no picture. Check the new entities are `device_tracker.stephen_location`
   and `device_tracker.stephanie_location`; if Home Assistant added a `_2`, rename in the entity settings. The battery
   and speed sensors are named from the entry title and should land on `sensor.stephen_location_battery_level` and
   `..._speed`; check them the same way.
5. Settings, People: set each person's tracker to its composite alone (Stephen: `device_tracker.stephen_location`; Stephanie:
   `device_tracker.stephanie_location`). Check `person.stephen_fenton` and `person.stephanie_hobart` have a state again.
6. In the integration's options turn **Presence auto-enrolment** on (dry run still on), read the log for "would change"
   lines (the expected result is none), then turn dry run off.
7. Rollback: delete the two UI entries and restore the `.bak` package file, call `composite.reload`, re-bind the persons.
   Presence is a report only while a composite is YAML, so it never fights the file.

No restart is needed for 2 to 6; the restart in step 1 only installs 1.2.0. Ask before any restart.
