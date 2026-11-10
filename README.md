# 🛒 ICA Shopping – Home Assistant Integration

Keeps one ICA shopping list and one Home Assistant to-do list in sync, in both directions.
Link the to-do list to Apple Reminders (Companion app → Reminders Sync) or to voice
assistants and the same items reach your ICA list.

## How syncing works

The integration remembers which items were present on both sides after the previous run.
That memory is what separates "new on this side" from "removed on the other side":

| situation | result |
|---|---|
| item is new in Home Assistant | added to ICA |
| item is new on ICA | added to the to-do list |
| item is removed or completed in Home Assistant | removed from ICA |
| item is ticked off or removed on ICA | removed from the to-do list |
| item exists on only one side and was never synced | never deleted; it is copied across |

Items match on their text, ignoring case and extra spaces. `Milk` and `milk ` are one item;
if the to-do list holds both, the first one is kept and the other is removed.

With **Remove checked and completed items** on (default), ticked ICA rows and completed
to-do items are cleaned up after they have been synced.

**When it runs:** every 10 minutes (ICA does not push changes), 5 seconds after the to-do
list changes, and whenever you call the `ica_shopping.refresh` service.

**First run:** the two lists are merged, so everything on either side ends up on both.
Tidy the ICA list first if it holds old items you do not want in your to-do list.

**Safety:**
- If ICA or the to-do entity cannot be read, nothing is changed.
- If the configured ICA list no longer exists, syncing pauses and a repair is shown.
- A single run never removes more than 15 items. If it would, nothing is changed and a repair is
  shown; check both lists, then call `ica_shopping.refresh` with `force: true`.
- Changing the ICA list or the to-do entity in the options starts from a clean slate instead of
  treating the old list's items as removed.

Item text is sent to ICA exactly as written. ICA assigns a store group to free text, so
English words generally land in the same group as the Swedish ones.

### Log

Each run that changes anything logs one line, for example
`ICA sync: to ICA +1 -0, to HA +2 -1, purged 1 completed, 0 failed (before: HA 12 items, ICA 11 rows)`.
Runs that change nothing log the same line at debug level. Home Assistant shows only warnings
by default, so to see these lines add:

```yaml
logger:
  logs:
    custom_components.ica_shopping: info
```

## Installation via HACS

Add Custom Repository:

Open HACS in Home Assistant.
Click on the ... in the top right corner and select Custom repositories.
Add the URL https://github.com/mellamomax/ica_shopping/
Set the category to Integration and click Add.
Download the Integration and restart Home Assistant.

Configure Integration:

Go to Settings -> Devices & Services.
Click Add Integration.
Search for and select Ica Shopping.
5. Enter your `session_id` and `ica_list_id` which you want to add.
6. Choose the `todo` entity to sync with.
Submit

## How to Get Your `session_id`

1. Open [ica.se](https://www.ica.se) in Chrome.
2. Log in.
3. Open Developer Tools → Application → Cookies.
4. Find the cookie called `thSessionId`.
5. Copy its value and paste into the integration config.

The cookie is valid for about 3 months. ICA does not report when it ends, so the integration counts from the day it first saw the cookie. After 75 days it raises a repair ("ICA session expires soon") and fires the event `ica_shopping_session_expiring` once. Click Fix to paste a new value; the count then starts again. If the cookie expires anyway, the existing "Session ID is invalid" repair appears and syncing pauses without changing any list.

To get a push notification on your phone, use an automation:

```yaml
alias: ICA session expiring
triggers:
  - trigger: event
    event_type: ica_shopping_session_expiring
actions:
  - action: notify.mobile_app_YOUR_PHONE
    data:
      title: ICA Shopping
      message: Renew the ICA cookie (thSessionId) in Settings → Repairs.
```


## How to Get Your `ica_list_id`

1. Open [ica.se](https://www.ica.se) in Chrome
2. Log in
3. Go to one of your shopping list or create a new
4. Open Developer Tools → Network.
5. Make a change in your list
6. Under Network it will now show the request made for your list with your list ID
7. Copy its value and paste into the integration config.

The url looks like this:
https://apim-pub.gw.ica.se/sverige/digx/shopping-list/v1/api/row/ab95586e-ffd3-4927-bfc7-85d1c5193dbb
with 'ab95586e-ffd3-4927-bfc7-85d1c5193dbb' being your list_id


## Example Automation (ICA Refresh)

Since ICA does not push updates, you can refresh the list every X minutes:

```yaml
automation:
  - alias: "Sync ICA List Every 10 Minutes"
    trigger:
      - platform: time_pattern
        minutes: "/10"
    action:
      - service: ica_shopping.refresh
```

You can also create an automation that refreshes the list when you enter or leave a store.
This only works for ICA stores you've added as zones in Home Assistant.
Purchases made in other locations won't trigger any update.

```yaml
alias: Ica shopping update
description: ""
triggers:
  - trigger: zone
    entity_id: device_tracker.max
    zone: zone.ica
    event: leave
  - trigger: zone
    entity_id: device_tracker.max
    zone: zone.ica
    event: enter
conditions: []
actions:
  - action: ica_shopping.refresh
    data: {}
mode: single

```