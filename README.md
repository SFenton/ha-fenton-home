# S+H Home Integration

A Home Assistant integration that delivers notifications to the **Fenton Home** iOS app through Apple Push, with targets that follow the person who is signed in.

The app registers as an ordinary Home Assistant mobile device. Its push address is this integration, which signs pushes with your own APNs key and sends them straight to Apple. Nothing runs outside Home Assistant, and existing automations keep their notification data unchanged: tags, groups, buttons, interruption levels, camera snapshots and `clear_notification`.

## Install

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/SFenton/ha-fenton-home` (category Integration), then download **S+H Home Integration** and restart Home Assistant.
2. In your Apple Developer account, open Certificates, Identifiers & Profiles → Keys, create a key with **Apple Push Notifications service** enabled and download the `.p8` file (Apple only lets you download it once). Note its Key ID and your Team ID.
3. Settings → Devices & services → Add integration → **S+H Home Integration**. Paste the whole `.p8` file, the Key ID and the Team ID. Apple checks the key before it is saved.
4. Open Fenton Home and allow notifications. The app registers itself; a new device appears under Mobile App named `<device> · Fenton`.

## Upgrading from Fenton Home Push (`fenton_push`)

The integration was renamed in 1.0.0; its domain is now `fenton_home`. Install it from HACS, remove Fenton Home Push,
restart, then add **S+H Home Integration** under Devices & services: it takes the old entry's APNs key and options
without asking for them again, moves the live-job store, and removes the old entry. (A `fenton_home:` line in
`configuration.yaml` makes it do the same at startup.) The old `/api/fenton_push/...` paths and the `fenton_push.problem`
service keep working for now; Repairs tells you when to switch an automation and when the old paths are unused.

Don't open S+H on a phone between the restart and Add integration: until the entry is set up the config endpoint answers 404, and the app then clears its push details. If you did, open it again afterwards. If Fenton Home Push is still installed and running, the import refuses and Repairs asks you to remove it in HACS and restart.

## Sending

Every Fenton Home device gets the usual `notify.mobile_app_<device>_fenton` service. On top of that:

| Service | Reaches |
|---|---|
| `notify.fenton_<first name>` (for example `notify.fenton_stephen`) | Every device where that person is signed in to Fenton Home |
| `notify.fenton` / `notify.fenton_everyone` | Everyone signed in to Fenton Home |

People are the `person` entities linked to a Home Assistant user. When two people share a first name, their target uses the person's entity id instead. A device stops receiving the moment its user signs out of the app.

`data` is the same as for the official app. Add an optional `fenton` block for app-only behaviour; the official app ignores it:

```yaml
action: notify.fenton_stephen
data:
  title: Front Door · Unlocked While Away
  message: The front door is unlocked and nobody is home.
  data:
    tag: front-door-unsecured-42
    push:
      interruption-level: time-sensitive
    actions:
      - action: FRONT_DOOR_LOCK_42
        title: "Confirm: Lock Front Door"
        authenticationRequired: true
    fenton:
      devices: [phone]        # phone, tablet, watch
      category: security
```

### Moving existing automations

Automations that already send through a notify group need no edits: add the Fenton targets to the group.

```yaml
notify:
  - platform: group
    name: stephen_s_phone
    services:
      - service: mobile_app_iphone_18_pro   # official app, while you still use it
      - service: fenton_stephen
```

Button handlers keep working: the app fires `mobile_app_notification_action` through its own registration, so the event carries the signed-in user just like the official app's.

## Live Activities: one card per job

With Live Activities allowed, the app hands Home Assistant a push-to-start token and this integration's job engine
(`live_jobs.py`) shows each running household job as its own Live Activity, kept current while the app is closed: the
bathroom fans, the dishwasher, the washer and dryer, the vacuums (one card per room, or per manual run; a robot with no coordinator changes room only once the new room has held for a minute) and a hot flash.
Cards go to every person with the app, ordered by relevance (the Dynamic Island shows the most relevant), with up to
four jobs and one problem on a device at a time. A card's Stop, Pause, Resume and Dock buttons run the matching Home
Assistant action; the card ends once the device confirms. A job that finishes by itself shows **Complete** for five
minutes. The design and every rule is in `Fenton-HASS-Native/.agents/platforms/apple/live-activities-per-job.md`;
`docs/CONTRACT.md` section 7 is the wire contract.

`sensor.fenton_live_jobs` lists what each device is showing, for debugging. The job entities and service mappings the
engine reads are in `custom_components/fenton_home/entities.py` and `controls.py`.

### Problem cards: `fenton_home.problem`

A device problem (a robot that is stuck, a dishwasher that needs attention) is a card too, and replaces the ordinary
notification for people who have the app. Call the service where the notification used to be sent:

```yaml
action: fenton_home.problem
data:
  device: main_floor                      # main_floor, music_room, theater_room or dishwasher
  code: navigation.stuck                  # or raw: "Unknown error 12" to map the robot's own text
  title: "Main Floor Vacuum · Robot Stuck"      # the ordinary notification, for people without the app
  message: "The vacuum is stuck in the Kitchen."
  task_title: "Main Floor Vacuum · Robot Stuck"  # the Donetick task the card follows
  url: /sfenton-react-dash/home?path=vacuums
  tag: vacuum-kitchen-error
```

People with the app see a card (red with a sound when the robot stopped, amber and silent when it carries on). Everyone
else gets the ordinary notification through their own notify action (`notify.stephen_s_phone`; the pattern is an
option). The card stays until the Donetick task is completed, by Home Assistant, a person or the card's own **Done**
button, or earlier in one case: the sensors reported the problem, the device was then seen not running, and now its issue
is clear and it has been running its job for half a minute. The card then shows the job and ends when the job does, and
the task stays open. A device that is paused or waiting does not count as running. A problem the sensors never reported
(such as a rinse aid refill raised during a run), or one whose history is unknown after an upgrade, waits for its task.
The history is stored with the problem, so it survives a restart and a reload; the half minute starts again after
either. The notification is cleared when the problem ends. A problem with no task ends when the condition clears.

Every button press and every card start, update and end writes one info line to the log (`fenton_home.press tag=… control=…
user=… outcome=confirmed|timed out|refused|service error`, `fenton_home.card start|update|end tag=…`). These new lines carry
no payload body and no webhook id; older warnings elsewhere may still name a device.

## Options

- **Send critical alerts.** Leave off until Apple grants the app the Critical Alerts entitlement. While it is off, `critical` notifications arrive as time-sensitive with the default sound.
- **Push address base URL.** Where Home Assistant can reach itself (for example `http://127.0.0.1:8123`). Empty uses the internal URL.
- **Show job cards**, **Jobs that get a card**, **Devices that get cards.** The master switch for Live Activity cards, which jobs run, and which devices (empty: every device that can receive pushes) get them. A device that has lost its push-to-start token for a moment keeps updating and ending the cards it shows; it just starts no new one. With the switch off, problems go to everyone as ordinary notifications.
- **Sweep abandoned registrations.** `off`, `dry_run` (default: list candidates, remove nothing) or `on`; see Device lifecycle.
- **Presence auto-enrolment**, **Presence dry run**, **Trackers kept out of presence.** See Presence.
- **Notify action for people without the app.** The action that reaches a person's whole phone, with `*` for their first name; the default `*_s_phone` is `notify.stephen_s_phone`.

## Device lifecycle (1.1)

Each phone, iPad or watch signed in to the app is a `mobile_app` registration. The app removes its own when the person
signs out (`POST /api/fenton_home/devices/unregister`, contract section 2a), and a failed call is retried later with the
registration's webhook id alone. A removal by the app, by an admin in Settings, or by the sweep below also drops what
this integration kept for that registration: job cards and their timers, the daily push counter and timestamp counters.
Home Assistant itself removes the entities, the notify target and its Live Activity tokens.

### Sweep of abandoned registrations

A reinstall or a new login that registers afresh leaves the old registration behind. The **Sweep abandoned
registrations** option (`off`, `dry_run`, `on`; default `dry_run`) finds those, at startup and then daily. A
registration of this app is a candidate when it has stopped updating (newest state among its entities, else the
entry's last change) and either:

- it has no push details (the app signed out) and 14 days have passed, or
- the same person has a newer S+H registration of the same model and 7 days have passed.

Other apps' registrations (the official Companion app included) are never touched. In `dry_run` each candidate is
logged at info and listed in a Repairs issue, and nothing is removed; in `on` each is removed and logged. Read the
dry-run list for a while before turning it on.

Known limit: Home Assistant restores entity states at startup with the restart time as their last update, and no
other reliable per-registration activity signal exists, so right after a restart a registration looks freshly
updated and the sweep finds little until that many days pass without a restart. The sweep errs towards keeping.

## Presence (1.2)

A person that tracks every phone follows whichever reported last, so a phone left at home flips the house between Away
and Home. The fix is one [Composite](https://github.com/pnbruckner/ha-composite-tracker) tracker per person (with
`require_movement`) that the person tracks alone. But Home Assistant's `mobile_app` adds each new registration's tracker
straight to its person, which bypasses the composite. **Presence** keeps the two in step:

1. A registration (`entry.data["user_id"]`) belongs to the person with that `user_id`, Companion registrations included.
2. That person's composite inputs become all of that user's `mobile_app` trackers minus the exclude list. Inputs keep
   their place and their own settings; inputs from other integrations (a car) stay; inputs of someone else's device,
   a disabled tracker or one that no longer exists are removed. The last input is never removed.
3. The person's `device_trackers` lose every `mobile_app` tracker (the composite stays), once the composite covers it.
4. Registrations removed by the app's sign-out, the sweep or an admin drop out the same way: removing a registration
   removes its entities, which triggers a pass.

A pass runs at startup, two seconds after any `device_tracker` entity is created, removed or disabled, and when a
person changes (that is how the auto-add is noticed). It writes nothing when nothing differs and logs each change at
info (`Presence changed ...`).

Options (the integration's Configure dialog):

- **Presence auto-enrolment** (`presence_enabled`, default **off**).
- **Presence dry run** (`presence_dry_run`, default **on**): log `Presence (dry run) would change ...` and change nothing.
- **Trackers kept out of presence** (`presence_exclude`), seeded with `device_tracker.iphone_18_pro` (Companion, stale
  since Nov 1), `device_tracker.iphone_17_pro` (Companion "iPhone 13 Pro (6)", iPhone18,1) and `device_tracker.ipad`;
  all three are Stephen's (checked against their registrations' `user_id`). An excluded tracker is also taken off the person.

If the Composite integration isn't installed, presence does nothing and raises a Repairs issue. A person with none of
their trackers being a Composite tracker is left exactly as is, with a Repairs issue per person.

**Composite must be a UI entry.** Presence writes a Composite config entry's options. An entry created from YAML
(`source: import`) has its options replaced by the YAML on every `composite.reload` and restart, so presence never
writes one: it logs what it would change and raises the Repairs issue "Move the Composite trackers out of YAML". Until
the move below, presence is a report only.

### Moving the composites from YAML to the UI (done 2026-11-02)

On the owner's server the composites are now Composite UI entries `device_tracker.stephen_presence` (Duo
`device_tracker.stephen_s_iphone_fenton` + 18 Pro `device_tracker.iphone_fenton`, both S+H) and
`device_tracker.stephanie_presence` (`device_tracker.stephanies_iphone`), `require_movement` on. They replaced the YAML
trackers `device_tracker.stephen_location` / `device_tracker.stephanie_location`; `/config/packages/presence_composite.yaml`
is a comment only (backup `presence_composite.yaml.yaml-to-ui-20261102.bak`, never restore it next to the UI entries).
Presence is on with dry run off.

How it was done, without a gap. A person only follows a tracker's removal, not a rename, so reusing the old entity ids
would leave each person `unknown` for a while, and a `home` → `unknown` → `home` blip fires "left/arrived" automations.
New names avoid that:

1. Back up the package file and `.storage/person`.
2. Settings → Devices & services → Composite → Add entry, once per person, with new names (`Stephen Presence`,
   `Stephanie Presence`): the same inputs, Require movement on, the other options off, entity picture "keep current".
   Check the new trackers show the same state as the old ones.
3. Settings → People: give each person both trackers, old and new.
4. Remove the `composite:` block from the package file, `ha core check`, call `composite.reload`. The old entries and
   their entities go, and each person keeps only the new tracker without changing state.
5. Turn presence on with dry run, confirm it reports nothing, then turn dry run off.
6. Rollback: restore the `.bak`, `composite.reload`, re-bind the persons to the old trackers, delete the UI entries.

Side effect: the composites' battery and speed sensors are new entities too (`sensor.stephen_presence_battery_level`
and so on); anything that recorded the old `sensor.*_location_*` ids has to be pointed at the new ones.

## House configuration (1.3)

The integration can serve one configuration document to the S+H app, so house-specific data (rooms, helper roles and the like) lives on your server and not in the app.

- **File:** `/config/fenton_home/house.yaml` or `/config/fenton_home/house.json`. YAML wins when both exist; with neither, the integration answers "not configured".
- **Checks (shallow on purpose):** the file is a mapping with a whole-number `schema` of 1 or more, every other top-level value is a mapping or a list, and the file is under 256 KB. The integration knows no section names; the app validates the fields against its own versioned schema.
- **Bad file:** a Repairs issue ("The house configuration file was rejected") names the reason, and the last good document stays in service. Fixing the file and reloading clears it.
- **Websocket commands** (any signed-in user, not admin-only):
  - `fenton_home/house_config` returns `{"schema": n, "hash": "<sha256>", "config": {...}}`, or `{"configured": false}`.
  - `fenton_home/house_config/subscribe` sends the current document, then a new message after each successful reload. Unsubscribe with `unsubscribe_events`.
- **Service:** `fenton_home.reload_house_config` (administrators) reads the file again and notifies subscribers. The file is also read when the integration starts.
- YAML anchors and aliases are not limited (only an administrator can write the file); a file that expands to something huge is the author's doing.
- Keep the file out of this repository: it describes your own house.

## How it works

`docs/CONTRACT.md` is the wire contract with the app. In short:

- `GET /api/fenton_home/config` (access token) tells the app which `push_url` to register.
- `POST /api/fenton_home/apns` is that `push_url`. Home Assistant's `mobile_app` posts each notification there without credentials, so a request is honoured only when its webhook id and push token match a real Fenton Home registration. It maps the notification to an APNs payload (`payload.py`) and sends it over HTTP/2 with a token signed by your key (`apns.py`).
- `POST /api/fenton_home/devices/unregister` removes the caller's own S+H registration; the webhook id is the proof, and a calls-per-minute cap guards the no-login form.
- A device gets at most 1,000 pushes per UTC day, as a guard against runaway automations.
- Apple tokens from Xcode builds use the sandbox service and TestFlight builds use production; the app says which, and a token from the other environment is retried there once.

## Development

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/unit` (no Home Assistant needed; the job engine, adapters, problems and relay are all unit-tested on recorded state snapshots in `tests/scenarios.py`); CI also runs `tests/ha` with `pytest-homeassistant-custom-component`, plus HACS and hassfest validation. `tests/fixtures/*.apns` are real payloads for `xcrun simctl push`; regenerate them with `python tests/fixtures/build.py`.
