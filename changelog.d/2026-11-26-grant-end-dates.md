---
date: 2026-11-26
type: added
impact: high
title: Give a capability or portal access until a last day
area: Client organizations
link: clients:client-list
---
Set a last day on a capability given to an account directly, or on a portal account's access; after it, the grant gives nothing.

## Technical notes

Migration `accounts.0011_grant_ends` adds the table `accounts_capabilityend` (one end for each account and capability given directly). Migration `clients.0008_grant_ends` adds the nullable column `ends_at` to `clients_portalaccess` and replaces the move trigger function of `clients.0007` so that moving a portal account to another organization also empties its end. Neither changes any data: existing grants have no end and keep holding until someone sets one. Ends are compared with the database's clock (`transaction_timestamp()`) at every check. Setting or moving a capability's end is recorded in the account's `account.updated` event (key `capability_ends`), and a portal access's end in `portal_access.set` and `portal_access.reset` (key `ends_at`, present in every new event of either). At its next reset the demonstration seed gives `demo_supervisor`'s patient-information capability and `demo_client_water`'s site access a last day 90 days later, recorded for the former as `account.capability_end_set`. No new setting or environment variable.
