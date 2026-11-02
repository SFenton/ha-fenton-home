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
bathroom fans, the dishwasher, the washer and dryer, the vacuums (one card per room, or per manual run) and a hot flash.
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
button, and the notification is cleared when it ends. A problem with no task ends when the condition clears.

## Options

- **Send critical alerts.** Leave off until Apple grants the app the Critical Alerts entitlement. While it is off, `critical` notifications arrive as time-sensitive with the default sound.
- **Push address base URL.** Where Home Assistant can reach itself (for example `http://127.0.0.1:8123`). Empty uses the internal URL.
- **Show job cards**, **Jobs that get a card**, **Devices that get cards.** The master switch for Live Activity cards, which jobs run, and which devices (empty: every device with Live Activities allowed) get them. With the switch off, problems go to everyone as ordinary notifications.
- **Notify action for people without the app.** The action that reaches a person's whole phone, with `*` for their first name; the default `*_s_phone` is `notify.stephen_s_phone`.

## How it works

`docs/CONTRACT.md` is the wire contract with the app. In short:

- `GET /api/fenton_home/config` (access token) tells the app which `push_url` to register.
- `POST /api/fenton_home/apns` is that `push_url`. Home Assistant's `mobile_app` posts each notification there without credentials, so a request is honoured only when its webhook id and push token match a real Fenton Home registration. It maps the notification to an APNs payload (`payload.py`) and sends it over HTTP/2 with a token signed by your key (`apns.py`).
- A device gets at most 1,000 pushes per UTC day, as a guard against runaway automations.
- Apple tokens from Xcode builds use the sandbox service and TestFlight builds use production; the app says which, and a token from the other environment is retried there once.

## Development

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/unit` (no Home Assistant needed; the job engine, adapters, problems and relay are all unit-tested on recorded state snapshots in `tests/scenarios.py`); CI also runs `tests/ha` with `pytest-homeassistant-custom-component`, plus HACS and hassfest validation. `tests/fixtures/*.apns` are real payloads for `xcrun simctl push`; regenerate them with `python tests/fixtures/build.py`.
