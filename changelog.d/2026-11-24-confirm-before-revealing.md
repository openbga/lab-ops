---
date: 2026-11-24
type: added
impact: high
title: Confirm it is you before revealing patient information
area: Security settings
link: security-settings
---
Administrators can ask staff to confirm it is them before their first reveal of patient information after signing in: their password, and an authenticator or recovery code where they have an app.

## Technical notes

Migration `core` 0008 adds the `RevealConfirmationRule` table. Its one row is written only when an administrator first saves the setting on Security settings, so an upgraded or new database asks nothing until then. When it is on, the first Reveal of a patient-information value or Reveal key of a person-level link in a browser session goes to the new Confirm it is you page (`/account/confirm-reveal/`); nothing is read, revealed or recorded until the person confirms, and they then choose Reveal again. The confirmation is kept in the session under `_reveal_confirmed`, naming the account and the sign-in's session row (`_account_session`), so it lasts for that sign-in only: a sign-out clears it, and a new sign-in, even of the same account in the same browser, asks again. A confirmation given in that sign-in within `ACCOUNT_REAUTHENTICATION_TIMEOUT` (5 minutes) for another change, or on allauth's own re-authentication pages with every factor the account has, counts and is remembered for the sign-in, and opening the confirmation page then returns straight to the page without asking; the sign-in itself does not count. Failed confirmations are written to the sign-in history and count toward the failed sign-in limit. Staff without the patient-information permission who post to a reveal address are asked first, then refused as before (404). Not asked before a person-level key search or for what client portal accounts see. Changes to the setting need a reason and the person's confirmation, and are recorded as `lab.reveal_confirmation_changed` in the Security settings history.
