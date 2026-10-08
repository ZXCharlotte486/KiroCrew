---
title: Work-ledger person-wait hold -- a conductor patrol rests while every open item waits on a person, and a worker can say why it is blocked
status: in-progress
author: iamwhatever, with kirocrew-worker
created: 2026-10-08
last-audited: 2026-10-08
audited-at: 230b9141c8
doc-pr: null
implementation-prs: []
tracking-issues: [18054]
supersedes: []
superseded-by: []
---

# RFC: Work-ledger person-wait hold

- Status: in-progress. The decision was made by the operator who owns the conductor patrol (2026-10-08). The implementation PR follows this document and links back to it; its First Principles lane reads an RFC's status off the base branch, so this document lands on its own first, as GOVERNANCE.md asks of an RFC.
- Amends [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) in one place, named in section 3.3: a `work-ledger` watch whose open items all wait on a person is no longer extended.
- Related: [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the ledger and `work_report`), [rfc-crew-log-wake.md](rfc-crew-log-wake.md) (how a worker's report wakes the conductor).
- Measured at `230b9141c8`.

## 1. Problem

A conductor's `watch: "work-ledger"` loop is kept alive while any item is open. When the cycle cap or runtime budget runs out, `_extend_for_open_ledger` in `src/kiro_crew/autonudge_service/firing.py` raises it, up to the seven-day backstop. That is the right call while a worker is building or testing.

It is the wrong call when every open item is waiting on a person. A worker whose tool approval timed out, or whose question only the user can answer, will not move until that person acts. The loop still takes turns on its timer and on the quiet floor, and still extends itself, for up to seven days. Each turn reads a board that cannot change.

The ledger cannot tell these cases apart. `work_report` carries `status` (`blocked` or `question`) and free-text `summary`. A worker whose `git` call timed out on an approval reported `blocked` with a sentence; no code can read a sentence.

## 2. Goals and non-goals

Goals:

- A worker can say, as data, why it is blocked: on an approval, on a person, or on something external.
- A `work-ledger` watch rests while every open item waits on a person and nothing on the ledger has changed since the conductor last looked. It fires no turn, takes no floor turn, and is not extended.
- Any change to the ledger ends the rest with no re-arm by anyone.
- The rest is visible in `monitor_inspect`, in the goal popover, and as a WARNING in the gateway log.

Non-goals:

- Changing the quiet floor or the probe gate in `autonudge_service/gate.py`. The hold sits in front of them.
- A notification when the hold starts. The conductor's own last turn is the one that reports the wait to the user.
- Holding any loop other than a `work-ledger` watch.
- Inferring a reason from the summary text.

## 3. Design

### 3.1 The `reason` field

`work_report` takes an optional `reason`. Allowed values:

| `reason` | Means | Holds the patrol |
|---|---|---|
| `approval` | a tool approval timed out or is waiting | yes |
| `needs_human` | only a person can answer or act | yes |
| `external` | a build, service or other item the worker does not control | no |

- It is accepted only with `status` `blocked` or `question`. With `progress` or `done` the report is refused (`invalid_value`, field `reason`).
- It is a worker-owned field, like `summary`. Every report replaces it, so a report without `reason` clears it.
- It is stored on the item, carried in the item's `work/recorded` crew-log entry, and rebuilt by the `work` fold, so a rebuilt ledger keeps it.
- `work_ledger_read` shows it on every row, full and compact.

### 3.2 The hold

On each tick of a `work-ledger` watch, before the cycle cap is checked, the timer reads the ledger once (off the event loop) and asks two questions:

1. **Is everyone waiting on a person?** True when the ledger has at least one open item, every item file is readable, and every open item has `status` `blocked` or `question` with `reason` `approval` or `needs_human`.
2. **Has anything changed since the conductor last looked?** The loop records a fingerprint of the ledger (every item record and the header) when it delivers a turn. The tick compares the ledger's current fingerprint with that one.

When both are true, the loop **holds**:

- It fires no turn. The probe gate is not consulted, so no quiet-floor turn fires either.
- It is not extended and not stopped by a spent bound: the hold is checked before both.
- It re-arms at its own interval, so the next check costs one ledger read and no model call.
- It sets `waiting_on_person` on the loop record and logs one WARNING when the hold starts.

**Release.** Any change to the ledger changes its fingerprint: a worker's new report (even one that stays blocked), a conductor write (decide, close, verdict, accept, goal), a new item. The next tick finds the fingerprints different, clears `waiting_on_person`, logs a WARNING, and runs as a normal tick. A worker's report also pulls that tick forward through the existing crew-log wake, so release is immediate. No person or agent has to re-arm anything.

The turn that delivers after a release records the new fingerprint. If every open item still waits on a person, the tick after it holds again. So the conductor gets exactly one turn per ledger change while everyone waits.

**Doubt fires.** An unreadable ledger, a torn item file, a read that raises, or a loop with no recorded fingerprint (a fresh loop) does not hold. That keeps the shipped behaviour whenever the hold cannot prove itself.

**Restart.** The recorded fingerprint and `waiting_on_person` persist with the loop. The boot replay tick reads the ledger and holds again if nothing changed while the gateway was down.

### 3.3 Interaction with the bound extension

This amends [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) section 3.2. A held loop never reaches the bound checks, so it is not extended while held. On release it reaches them as before: a spent bound with an open item is extended if the loop is younger than the backstop, and stops the loop otherwise.

Held time is **not** credited back to the runtime clock, unlike the approval hold. The approval hold credits it because a stop at the budget would end a loop nobody chose to pause. Here the extension already covers a spent budget on release, and the seven-day backstop still bounds the loop's whole life, held or not.

### 3.4 Visible

- `monitor_inspect` reports `waiting_on_person`.
- The REST loop row and the `autonudge_state` frame carry it, so the goal popover shows the loop as **Waiting on you** with a line naming why: every open task needs a person.
- The hold's start and its release each log one WARNING naming the loop.

### 3.5 Guidance

- The worker prompt and the goal-conductor skill tell a worker: when a tool approval times out, report `blocked` with `reason=approval`; when only a person can unblock you, use `needs_human`; for a build or service you do not control, `external`.
- The goal-conductor skill tells the conductor that a ledger waiting only on people holds its patrol, and that its job on that turn is to put the ask in front of the user.

## 4. Risks

- **A wrong reason holds a patrol.** A worker that says `needs_human` while it could move on its own stops being patrolled until something on the ledger changes. Its own next report releases the hold, and a stalled worker shows in `work_ledger_read`.
- **A hold nobody ends.** If the person never acts, the loop stays held. It costs one file read per interval, no turns. It shows in the popover and in `monitor_inspect`.
- **Held time spends the backstop.** A loop held past seven days of age stops on its next spent bound after release. That is the same bound the extension already has.
- **One extra turn after a conductor write.** The conductor's own write during a turn changes the fingerprint, so one more turn fires before the hold resumes. That is the release rule working as written, not a leak.

## 5. Security

No new authority. `reason` is one more worker-owned field on the worker's own item, behind the same binding and validation as `status`. The hold only withholds fires; the release only lets an already-armed loop run. Nothing a worker writes can make a conductor's loop fire more than it does today.

## 6. Alternatives considered

- **Stop the loop instead of holding it.** A person would then have to re-arm the patrol after answering. Rejected: that is the failure the approval hold already removed.
- **Hold on the reason alone, with no fingerprint.** A worker's new report that stays blocked would not reach the conductor. Rejected: any report is news.
- **Release only on a worker report.** A conductor write (closing the last item that was progressing, say) would not wake its own patrol. Rejected.
- **Credit held time back to the runtime clock.** Not needed: the extension covers a spent budget on release, and crediting would let a loop live past the backstop.
- **Infer the reason from the summary.** Free text is not a contract. Rejected.
- **Count `external` as a hold.** An external dependency (a CI run, another item) moves without a person, and the conductor must see it move. Rejected.

## 7. Open questions

None open.

## 8. Rollout

One implementation PR, after this document lands. Exit criteria, each pinned by a test there:

- A ledger whose open items all wait on a person, unchanged since the last delivered turn, holds the loop: no fire, no extension, `waiting_on_person` set.
- A new worker report releases the hold, and the next tick fires.
- While held, a spent bound runs no extension and does not stop the loop.
- A mixed ledger (one item still progressing, or one blocked with `external`) does not hold.
- `reason` is refused with `progress` or `done`, stored, cleared by a later report, rebuilt by the fold, and shown by `work_ledger_read`.
