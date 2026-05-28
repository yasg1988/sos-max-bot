# Organization Bot Stack

This file preserves the organization/staff feature scope that was removed from
the active Оралтыш family bot UI. Use it as the starting checklist for a
separate MAX bot.

## Scope

- Organization registry:
  - admin creates an organization by type, name, and INN;
  - supported types: school, medical;
  - organization status controls availability.
- Staff registration:
  - user chooses school or medical registration;
  - user enters organization INN;
  - bot asks for full name, phone, and position;
  - request is sent to the organization's approval chat.
- Approval flow:
  - approval chat receives request with buttons `Подтвердить` and `Отказать`;
  - approved staff gets access to organization alarm buttons;
  - rejected staff gets a notification.
- Alarm chats:
  - admin assigns one approval chat per organization;
  - admin assigns one or more alarm chats per organization;
  - alarm chat ids are stored as signed MAX chat ids.
- Staff alarm:
  - approved staff chooses organization;
  - real alarm has a cooldown;
  - test alarm is available without cooldown;
  - alarm is sent to all active alarm chats;
  - responder can press `Принял`, and staff receives confirmation.
- Staff disabling:
  - admin can find organization by INN;
  - admin can disable an approved staff member;
  - disabled staff is set to `blocked` and cannot re-register automatically.

## Existing Database Schemas

- `sos_org.organizations`
- `sos_org.staff`
- `sos_org.staff_requests`
- `sos_org.alerts`
- `sos_org.alert_chats`

## Existing Code Areas In `app.py`

The family bot no longer exposes these flows in menus or callbacks, but the old
implementation remains in `app.py` and can be copied into the future
organization bot:

- `staff_menu`
- `start_staff_registration`
- `continue_staff_registration`
- `create_staff_request`
- `notify_staff_request`
- `active_staff`
- `staff_alert_menu`
- `create_org_alert`
- `admin_menu`
- `list_orgs`
- `admin_staff_list`
- `bind_chat`
- callback payload prefixes:
  - `staff:*`
  - `staffalert:*`
  - `staffreq:*`
  - `orgalert:*`
  - `admin:*`
  - `adminstaff:*`

## Current Family Bot Behavior

The active bot now exposes only:

- parent/child registration and linking;
- child help alarms and location sharing;
- parent location request;
- family safety recommendations for children and parents.

