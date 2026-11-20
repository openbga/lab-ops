---
date: 2026-11-20
type: fixed
impact: low
title: Ticket pages agree with the replies they show
---
A ticket opened while someone replies shows that reply only with forms that accept your answer to it, on the desk and in the portal.

## Technical notes

The scheduled jobs' account, when a deployment's first job run creates it, is now named `scheduled-jobs-` followed by eight random characters, with the matching address in the reserved `lab-ops.invalid` domain, so a person's account being created at that moment never delays it or ends up differing from it only in letter case. An account created earlier keeps its name.
