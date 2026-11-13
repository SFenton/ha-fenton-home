# S+H Home Integration: app contract (v2; Live Activities v2, sections 6 and 7)

How the S+H iOS app (bundle `com.sfenton.home`) and this integration talk. Home Assistant's own
`mobile_app` integration sits in the middle and is used unchanged.

**v2 (integration 1.0.0)** renames the integration from Fenton Home Push (`fenton_push`) to the S+H Home Integration
(`fenton_home`). Nothing else changes on the wire. The paths are now `/api/fenton_home/config` and
`/api/fenton_home/apns`, and the service is `fenton_home.problem`. **v1 paths stay valid** (`/api/fenton_push/config`,
`/api/fenton_push/apns`, `fenton_push.problem`) while the integration keeps its aliases: they answer on the same
handlers, `push_url` in the config answer is always the new path, and a Repairs issue tells the owner when every
registration has moved (and when something still calls `fenton_push.problem`). An app that gets a 404 from the new
config path should ask the old one. The notify action `notify.fenton_<person>` and the `fenton_live_activity_*`
events are unchanged.

**v2.1 (integration 1.1.0)** adds `POST /api/fenton_home/devices/unregister` (section 2a). Nothing existing changes.

## 1. Ask the integration where to point pushes

`GET /api/fenton_home/config` (Home Assistant access token) →

```json
{"available": true, "push_url": "http://192.168.1.20:8123/api/fenton_home/apns", "bundle_id": "com.sfenton.home", "critical_alerts": false}
```

A 404 means the integration isn't installed: the app still registers, without `push_url`/`push_token` (websocket
push only), because the same registration carries location and device sensors. It asks again on the next launch and
adds push details on a later `update_registration` once the integration answers. `available` is false (and `push_url` null) when it is installed
but not set up, or when `bundle_id` isn't the app's: the app then registers without `push_url`/`push_token` (websocket
push only) and adds them on a later `update_registration`. Until the app connects, a notification to such a device is logged and skipped; the person's other devices still get it.

## 2. Register as a mobile device

`POST /api/mobile_app/registrations` with the signed-in user's access token, as Home Assistant documents:

```json
{
  "app_id": "com.sfenton.home",
  "app_name": "Fenton Home",
  "app_version": "0.1.0 (1)",
  "device_name": "Stephen's iPhone · Fenton",
  "manufacturer": "Apple",
  "model": "iPhone19,2",
  "os_name": "iOS",
  "os_version": "27.0",
  "device_id": "<identifierForVendor>",
  "supports_encryption": false,
  "app_data": {
    "push_url": "<from step 1>",
    "push_token": "<APNs device token, lowercase hex>",
    "push_websocket_channel": true,
    "apns_environment": "sandbox"
  }
}
```

- `device_name` must end in ` · Fenton`. Home Assistant names notify services after the device name, so the
  suffix keeps ours (`notify.mobile_app_stephen_s_iphone_fenton`) from colliding with the official app's.
- `apns_environment` is `sandbox` for Xcode-installed builds (aps-environment `development`) and
  `production` for TestFlight/App Store builds.
- Keep the returned `webhook_id` in the Keychain, keyed by Home Assistant user id. Reuse it on later launches
  for the same user; register again only for a new user or after the webhook answers 410.
- On every launch and whenever APNs hands out a new token, send the `update_registration` webhook with the
  same `app_data` (fresh token) and `app_version`.
- **Sign-out:** before revoking the refresh token, send `update_registration` with
  `"app_data": {}`. That drops push for this device, so the person's targets stop reaching it at once.
  Signing back in as the same user restores `app_data`.

### 2a. Unregister (sign-out, and its retry)

`POST /api/fenton_home/devices/unregister`, JSON body `{"webhook_id": "<id>"}`. The Home Assistant login is optional:
send the bearer token when you still have it, and omit it (or send a dead one) on a deferred retry after sign-out.

- The webhook id is the proof, with the same trust as the `mobile_app` webhooks. It removes only a `mobile_app` entry
  whose `webhook_id` matches **and** whose `app_id` is this integration's bundle id; other apps' registrations
  are never touched.
- When the request carries a valid login, that user must own the registration (the entry's `user_id`), else 403.
- Answers:

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"removed": true}` | The registration was removed (entry, entities and notify target go; so does this integration's state for it). |
| 410 | `{"error": "unknown_webhook"}` | No such S+H registration (already removed, or never ours). Treat as success: forget the stored webhook. |
| 400 | `{"error": "bad_request"}` | Body isn't a JSON object with a non-empty string `webhook_id`. |
| 403 | `{"error": "forbidden"}` | Valid login, but another user's registration. |
| 429 | `{"error": "rate_limited"}`, `Retry-After: 60` | More than 10 calls a minute from one address without a login. Retry later. |
| 503 | `{"error": "unavailable"}` | The integration isn't loaded. Retry later. |

- Not retrying a 200 or 410 is correct. Retry 429, 503 and network errors.
- **A removed registration can't tell the app.** Home Assistant answers a post to a webhook it removed in this run with
  200 and an empty body (the 410 for removed webhooks only exists after the next Home Assistant restart), so an app
  whose registration an admin deleted may keep posting with no error until then. After this call the app should forget
  the webhook either way.

## 3. What arrives (APNs `userInfo`)

Alert pushes:

```json
{
  "aps": {
    "alert": {"title": "Front Door · Unlocked While Away", "subtitle": "…", "body": "The front door is unlocked…"},
    "sound": "default",
    "interruption-level": "time-sensitive",
    "thread-id": "front-door-unsecured",
    "category": "fenton.dynamic",
    "mutable-content": 1
  },
  "fenton": {
    "v": 1,
    "tag": "sfenton-front-door-unsecured-42",
    "group": "front-door-unsecured",
    "url": "/sfenton-react-dash/home?path=security",
    "actions": [
      {"action": "FRONT_DOOR_LOCK_42", "title": "Confirm: Lock Front Door", "authenticationRequired": true,
       "activationMode": "background", "icon": "sfsymbols:lock"},
      {"action": "URI", "title": "Open Security", "uri": "/sfenton-react-dash/home?path=security", "icon": "sfsymbols:shield"}
    ],
    "entity_id": "camera.front_door_camera",
    "ext": {"category": "security", "screen": "security/front-door"}
  }
}
```

- `aps.category` is `fenton.dynamic` whenever `fenton.actions` is non-empty. The notification service
  extension builds a category from the action list (id derived from the action ids), registers it with
  `setNotificationCategories` (keeping the existing ones) and sets `content.categoryIdentifier` to it.
  Action options: `authenticationRequired` → `.authenticationRequired`, `destructive` → `.destructive`,
  `activationMode: foreground` → `.foreground`, `behavior: textInput` → `UNTextInputNotificationAction`
  (`textInputButtonTitle`, `textInputPlaceholder`). `icon: sfsymbols:<name>` → `UNNotificationActionIcon(systemImageName:)`.
- `mutable-content` is 1 when there are actions or media. Media keys, all optional: `entity_id` (a camera:
  fetch `/api/camera_proxy/<entity_id>` with the app's token), `image`, `video`, `attachment.url` (paths are
  relative to the Home Assistant URL unless absolute).
- `url` (or `clickAction`) is where a tap on the body goes. It may be a dashboard path; the app maps it to a
  native screen and ignores what it can't map.
- `ext` is the automation's `data.fenton` block, passed through untouched (category, screen, layout …).
- `presentation_options` and anything else unknown in `data` lands under `fenton.extra`.
- `interruption-level` `critical` (with `sound: {"critical": 1, "name": …, "volume": …}`) only arrives when
  the integration's critical-alerts option is on; otherwise the integration downgrades it to
  `time-sensitive` with the default sound.
- `aps.sound` is omitted for silent notifications (`push.sound: none`).

Background commands (`apns-push-type: background`, `aps.content-available: 1`, no alert):

```json
{"aps": {"content-available": 1}, "fenton": {"v": 1, "command": "clear_notification", "tag": "sfenton-front-door-unsecured-42"}}
```

Commands: `clear_notification` (remove delivered notifications whose `fenton.tag` matches), and
`request_location_update`, `update_widgets`, `clear_badge`, passed through for the app to honour or ignore.

## 4. Button presses back to Home Assistant

For every action except `URI`, send the registration's webhook:

```json
{"type": "fire_event", "data": {"event_type": "mobile_app_notification_action",
  "event_data": {"action": "FRONT_DOOR_LOCK_42", "tag": "sfenton-front-door-unsecured-42", "reply_text": "…"}}}
```

`reply_text` only for text-input actions. Home Assistant stamps the event with the registration's user, which
is what your automations' identity checks compare against. `URI` actions open `uri` in the app instead.

## 5. Testing without Apple

Every payload above can be delivered to a simulator with
`xcrun simctl push <udid> com.sfenton.home payload.apns` (top-level `"Simulator Target Bundle"` key or the
bundle argument). `tests/fixtures/*.apns` holds the integration's real output for the front-door, doorbell,
Donetick and clear cases.

## 6. Live Activities (contract v2)

One aggregate **Home** activity lists the household jobs that are running (dishwasher, fan timers, laundry…). Home
Assistant's `mobile_app` already stores the phone's push-to-start token (`app_data.start_live_activity_token`) and each
activity's update token (webhooks `live_activity_token` and `live_activity_dismissed`), and chooses the event when a
notification carries `data.live_update: true` and a `tag`: a **start** with the push-to-start token, an **update** with
the activity's token, an **end** on `clear_notification`. It posts the result here with `live_activity_token` set and
`data.event`. Everything below is what this integration does with that post.

Only a post with `live_activity_token` becomes a `liveactivity` push (`apns-push-type: liveactivity`, topic
`com.sfenton.home.push-type.liveactivity`). The token key is the APNs device token of the activity, not `push_token`.

**A `live_update` post without `live_activity_token` is dropped.** `mobile_app` found no activity and no
push-to-start token, so the device cannot show one (the person turned Live Activities off, the app is older, or it is
the iPad before iPadOS 17). The answer is still `201` (nothing is retried, nothing is logged as an error), no push is
sent and the daily count does not move. Without this the post would arrive as a banner.

### Content

`data.fenton_activity` (the automation's block, or what the `fenton_home` job engine will build) becomes the activity's
`content-state`, and is validated against the app's `HomeActivityState`:

```json
{"v": 1, "focus": "dishwasher", "url": "/sfenton-react-dash/home?path=kitchen",
 "jobs": [
  {"id": "dishwasher", "kind": "dishwasher", "title": "Dishwasher", "status": "Eco 50° · Drying", "phase": "running",
   "start_ts": 1792241200, "end_ts": 1792251720, "progress": 0.62, "symbol": "dishwasher", "tint": "cleaning",
   "controls": [{"id": "stop", "title": "Stop", "role": "destructive"}]}]}
```

- `start_ts` and `end_ts` are **Unix seconds as numbers**. ActivityKit decodes pushes with a default `JSONDecoder`;
  a date string, or a `Date`, would be read from 2001. Milliseconds are recognised and converted.
- `phase` is `running`, `paused`, `waiting`, `attention`, `stopping` or `done` (anything else reads `running`).
- `estimated: true` shows "About 3:40 PM" and never a seconds countdown. `progress` is 0 to 1. `tint` is a colour
  family (`cleaning`, `air`, `water`…); `symbol` an SF Symbol name.
- **Budget**: 6 jobs, 40 characters of status, 2 controls per job, a 200 character `url` that carries no token. The
  focused job is kept when jobs are cut. A control keeps only `id`, `title` and `role`: the service it maps to
  (`action`, `target`) stays in Home Assistant and never reaches the phone. The whole push stays under Apple's 4 KB
  (`payload_too_large` otherwise).
- `stale_ts` in the block (Unix seconds) becomes `aps.stale-date`; without it the stale date is the earliest running
  `end_ts` plus 10 minutes, or 30 minutes from now when no job has an end. The views read "Updating…" past it.

Without a block, the companion app's keys map to **one `generic` job** so existing automations show something
sensible: `message` is the status, `title` the job title, `progress`/`progress_max` the bar, `chronometer` with `when`
(a Unix time, or seconds from now with `when_relative`) a countdown (a time already past is not drawn),
`notification_icon` (a few `mdi:` names) the symbol, `url` the tap target.

### Events

| `data.event` | `aps` |
|---|---|
| `start` | `attributes-type: HomeActivityAttributes`, `attributes: {tag, title, kind}`, a quiet `alert` (Apple requires one; no sound), `input-push-token: 1` (iOS 18 returns the new activity's token), `stale-date` |
| `update` | `content-state`, `stale-date`. An `alert` and sound only when `data.live_alert` is set |
| `end` | `content-state` (the final one, or an empty list on `clear_notification`), `dismissal-date` = now + `data.dismiss_after` seconds (default 0, at most 4 hours) |

`attributes.kind` is `home` for a `fenton_activity` block, `generic` otherwise. `relevance_score` passes through as
`relevance-score`. `fenton: {"v": 2, "tag": …}` rides along. The Swift type name `HomeActivityAttributes` is the
`attributes-type`: a push-to-start token is per type, so it never changes.

**Priority**: 10 for a start, an end, anything that alerts and anything with `data.urgent`; 5 for the many updates
between, which Apple does not count against its budget but may delay.

### Testing without Apple

`tests/fixtures/live-start.apns`, `live-update.apns`, `live-end.apns` and `live-generic.apns` are this integration's
real output at a fixed time. `xcrun simctl push` cannot deliver them: it parses the file as a remote notification
and refuses one with no alert (`UNErrorDomain 1401, Notification has no user visible content`, Xcode 27.1, iOS 27.0
simulator), so nothing reaches ActivityKit. The app decodes them in its unit tests instead
(`tools/push_fixtures/live-*.apns` in the app repository). Apple's Push Notifications Console and a real device
are the only end-to-end check.

## 7. One Live Activity per job (content state v2)

Specified by `Fenton-HASS-Native/.agents/platforms/apple/live-activities-per-job.md` (sections 3, 5, 7 and 9), which
supersedes the aggregate Home activity of section 6. The transport in section 6 is unchanged (`mobile_app` posts with
`live_activity_token` and `data.event`); what changes is the content, the attributes and the stamping. The Home
activity's content state (v1) is still accepted for one release.

The `fenton_home` job engine (`live_jobs.py`) sends one activity per job through core's
`notify.mobile_app_<device>` with `data: {tag: "job.<id>", live_update: true, fenton_activity: {...}, fenton_attributes: {...},
relevance_score, live_alert?, urgent?}`. Core owns the tokens and chooses start, update or end; this integration maps the
result as follows.

### Tags

`job.<id>`: `job.fan.guest`, `job.fan.master`, `job.dishwasher`, `job.dryer`, `job.washer`, `job.vacuum.main_floor`,
`job.vacuum.music_room`, `job.vacuum.theater_room`, `job.hot_flash`. A device's problem rides on its own tag.

### Which registrations get cards

Updates and ends go to every `mobile_app` registration of this app that can push (cloud push credentials or the
websocket channel), whether or not it currently holds a push-to-start token. Only **starting** a card needs the token.
The app replaces `app_data` on every `update_registration`, so for a short time after a launch it can lack
`start_live_activity_token`; a registration in that state keeps updating and ending the cards the engine already shows
on it, and starts no new one until the token is back. A card ends as `not eligible` only when its registration is gone,
can no longer push (signed out: `app_data` cleared), belongs to another app id, or is left out by the "Devices that get
cards" option.

At the relay, an update for such a registration whose activity token core has not yet stored carries no
`live_activity_token`: it is dropped with `201` (section 6), so nothing fails and the engine still counts the card as
shown. A `410` for an update with no start token to restart from is reported as before; the stored token is removed.

### Orphaned activities

When a phone reports a `job.*` activity's update token (the app's `live_activity_token` webhook) for a registration of
this app on which the engine shows no card with that tag, the engine ends that activity at once (`clear_notification`;
the end dismisses immediately, section 7 "Content state"). This repairs an end that went out before the app had reported
the token, which could not reach the phone, and any other activity the engine lost track of.

Core fires no event for the report, so the integration wraps the function core's webhook handler stores it with
(`mobile_app.live_activity.webhook.store_live_activity_token`; guarded the same way as the dead-token store above) and
also looks at core's token store on every engine cycle. It never ends: a card the engine shows or has just started
(`engine.cards` already holds it), an activity whose end the same cycle sends, anything while Home Assistant is starting
(`hold_until`, which also covers cards recovered after a restart), a tag that is not `job.*` (the aggregate Home
activity), or a token that has expired. A card the person swiped away is not in `engine.cards` either, so a late token
report for it ends it again, which is harmless. A token that stays after an end (the end failed) is ended again after
ten minutes, or at once when the phone reports a new token.

### Vacuum rooms on a robot with no coordinator

A robot with a coordinator reports its rooms exactly: a new room ends the card and starts another at once. A robot with
none (the music-room robot) has a manual run whose card is keyed by its status flag and its *estimated segment*, a
position estimate that flips while the robot crosses a doorway. A change of that key now ends and restarts the card
only after the new key has been read unchanged for **60 seconds** (`SEGMENT_SETTLE_SECONDS`); until then the card keeps
its room and updates in place (pause, resume), and a flip back, or on to a third room, starts the wait again. Within the
first 60 seconds of a run a change of key is the sensors catching up (the status flag is set a few milliseconds after
the vacuum starts), so it updates the card in place and starts nothing new.

### Attributes (sent on a start)

`{tag, title, kind, symbol, tint}`. `title` is the notification title (the device's name, capped at 32 characters);
`kind`, `symbol` and `tint` come from `data.fenton_attributes` (each at most 40 characters; anything else in that
object is ignored), and default to `kind: "generic"` with the app's own defaults when absent. The attributes type is
still `HomeActivityAttributes`.

### Content state

A `data.fenton_activity` block with `"v": 2` becomes the `content-state` after validation. Keys the relay understands
(anything else is dropped with a note in the log; `pending` is the app's alone and is never forwarded):

| Key | Rule |
|---|---|
| `v` | `2` |
| `phase` | `running`, `paused`, `waiting`, `problem` or `done` (anything else is refused) |
| `headline` | optional, 32 characters |
| `detail` | optional, 32 characters: a short line under the title (hot flash: "Your side", "Stephen's side", "Whole bed", plus " · by Steph" or " · by Home" when it fits) |
| `short` | optional, 10 characters: a word for the compact island when the card has no time (vacuums: the room or segment, "Area", "Spot", "Cleaning" or "Paused") |
| `value` | optional (a vacuum card has none, and no `progress`). `{kind: countdown, end_ts, start_ts?}`, `{kind: countup, start_ts, paused_elapsed_s?}`, `{kind: percent}` (needs `progress`) or `{kind: text, text}` (16 characters); `estimated: true` optional |
| `progress` | optional, 0 to 1 |
| `controls` | at most 2 `{id, title, role?}`; at most 3 when `phase` is `problem`. Titles 20 characters. The first control is primary; `role: "secondary"` is kept on the others |
| `problem` | present exactly when `phase` is `problem`: `{severity: critical\|warning, code, icon, problem (40), message (90), chips (at most 1, 20 characters), task_id?}`. An unknown `icon` reads `question` |
| `url` | 200 characters, no token |
| `notice` | optional, 60 characters: shown in place of the value for that one update ("Couldn't stop. Try again.") |
| `stale_ts` | Unix seconds; becomes `aps.stale-date`, not part of the state |

Times are Unix seconds as numbers. A structurally wrong block is refused with `invalid_activity`; the whole push stays
under 4 KB (`payload_too_large` otherwise). Without `stale_ts` the stale date is ten minutes after a countdown ends, else
half an hour from now. A `clear_notification` end of a per-job activity carries the stub
`{"v": 2, "phase": "done", "value": {"kind": "text", "text": "Ended"}, "controls": []}` and `dismissal-date` = now.

### Events

| Event | Difference from section 6 |
|---|---|
| `start` | `attributes` as above. `data.live_alert` adds `sound: "default"` to the start (a critical problem that begins the card); without it the start is silent |
| `update` | unchanged: `live_alert` adds an alert and the default sound |
| `end` | unchanged |

`relevance_score` is passed through as `relevance-score` unchanged (the engine sends 100, 90, 80 to 50, 70, 55 and 10).

### Timestamps

`aps.timestamp` is `max(ceil(now), last + 1)` per device and tag, so a push is always newer than the app's optimistic
write (`floor(now)`) and two pushes for one activity never tie. The sequence is held in memory; a restart resets it to
the clock.

### Dead tokens

Apple answers 410 to a push for an activity that has ended. Core keeps the stored token and turns every later start for
that tag into an update, so the tag could not start again until the token expired (8 hours). On a 410 for a Live Activity
push the relay:

1. removes the tag's token through core's `mobile_app.live_activity.store.remove_live_activity_token` (guarded: the
   Home Assistant version must be 2026.7 or later and the function must still take `(hass, webhook_id, tag)`; otherwise
   it logs once and carries on);
2. for an `end`, answers 201 (the activity is already gone);
3. for an `update`, retries once as a start with the registration's `start_live_activity_token` and answers 201 when
   that succeeds; otherwise it answers with the 410 as before.

### Fixtures

`tests/fixtures/live2-*.apns` are the engine's real output, run through the relay at a fixed time. Regenerate with
`python tests/fixtures/build.py`. The app's repository mirrors them in `tools/push_fixtures/`.

### Events from the phone and services

| Name | Direction | Data |
|---|---|---|
| `fenton_live_activity_action` | phone to engine (registration webhook `fire_event`) | `{tag, job, control}`; the event's user comes from the registration |
| `fenton_live_activity_dismissed` | phone to engine | `{tag}`; a webhook id is a credential and events are recorded, so none is sent. With several devices showing the tag the engine narrows to the one whose token core just dropped |
| `fenton_home.problem` | automation to engine | `{device, code?, raw?, title?, message?, task_title?, url?, tag?, data?}` |

## House configuration

One document, `/config/fenton_home/house.yaml` (or `house.json`; YAML wins), served over the websocket to any signed-in user. The integration checks only its shape; the app validates the fields.

| Name | Direction | Data |
|---|---|---|
| `fenton_home/house_config` | app to server (websocket command) | result: `{schema, hash, config}` (`hash` is the sha256 of the schema number and the canonical JSON of `config`), or `{configured: false}` |
| `fenton_home/house_config/subscribe` | app to server | result `null`, then an event with the same payload now and after each successful reload (and `{configured: false}` if the integration is unloaded). Unsubscribe with `unsubscribe_events` |
| `fenton_home.reload_house_config` | administrator to server (service) | none; reads the file again and notifies subscribers |

A file that fails the checks (not a mapping, no integer `schema` of 1 or more, a section that is neither mapping nor list, NaN or Infinity, nested too deeply, unreadable, over 256 KB) raises the `house_config_invalid` Repair and the last good document stays in service.
