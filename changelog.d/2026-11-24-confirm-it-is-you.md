---
date: 2026-11-24
type: added
impact: high
title: Confirm it is you before changing access or security settings
area: Security settings
link: security-settings
---
Saving a change to accounts, roles, session limits or the second-factor rule asks for your password again, and your authenticator code if you have one (an account without a password gives the code alone).

## Technical notes

No migration and no new setting. The 5 minutes follow `ACCOUNT_REAUTHENTICATION_TIMEOUT`, and a confirmation is recorded in the browser's session as allauth records a re-authentication, so allauth's own re-authentication pages count toward it and it counts for them; signing in does not count. A password given counts only while the account keeps the password that was checked (the session keeps a keyed digest of its stored hash, never the password), and a code only while the account keeps the authenticator it came from and when it was given after the newest authenticator app was added, so a new password or a new or replaced app asks again. It asks on the admin console's Add user, an account's page, its password page, Mark as compromised, removing an authenticator, Add group and a group's page, and on the staff and client portal session limits and the second-factor rule forms on Security settings. A password or code not accepted is written to the person's sign-in history as a failed re-authentication and counts toward the failed sign-in limit; nothing typed is stored, logged or written to an audit event or the admin console's history, and every request marks the two fields as sensitive, so a Django error report never shows them. A password change refused in the admin console (both buttons sent, as without script) no longer writes a password-changed audit event, and removing an authenticator keeps the admin console's popup. An account with neither a usable password nor an authenticator cannot make these changes. Ending an account's sessions does not ask. Security settings lists the rule under Sign-in as Changing access and security settings.
