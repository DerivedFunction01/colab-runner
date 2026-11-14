# Study-round protocol

The canonical form of an iterative choice study: per-item
panels, verdict rounds, and a freeze. `docs/method.md`
(Study pattern) says when a study runs and that the picked
study is the spec for the build; this document is the full
protocol the study itself follows. Mock content inside a
study obeys `docs/mockup-approval.md` like any mock.

## The form

- A study is a set of **items**. Each item is a panel drawn
  from real built source — the surface as it actually
  stands — carrying one or more independent **axes**; the
  owner composes the item's form mix-and-match across its
  axes.
- Each axis offers a small set of options — two or three.
  An option has a slug id, a label, and the composed-form
  fragment it contributes. Option identity is the slug,
  never a letter: a verdict payload must read as a record
  without the study open beside it.
- Every item renders a **composed line**: its current form
  stated as one sentence assembled from the picked options'
  fragments. A summary view lists every item's picks over
  the same state the panels hold — two views, one state,
  so they cannot drift.
- Defaults are staged honestly: a default is the author's
  staging, marked as the current state, never dressed as
  a decision.

## Live binding

- The composed preview **re-renders from the current
  selections on every change**: the composed line and the
  after-frames are rebuilt from the picks as they stand,
  and the frames say so ("live from your picks"). A static
  preview composed from defaults is a defect — the owner
  approves what he sees, and what he sees must be the
  composition he has actually picked.
- Before-frames never re-render. Where a pick is
  pattern-level — an axis of a shared component — the
  re-render propagates to every item that component
  appears in.

## No "full" on any axis

- No option on any axis is a "full" variant — the complete,
  exploded, or everything form of its axis. It is never
  picked, and its presence makes the actual picks
  untrackable. Option sets shrink toward the decision,
  never sprawl.
- If a round drops an option a recorded pick names, the
  pick settles to the first remaining option on that axis
  — settled by the drop, not by a pick — and the account
  records the supersession as such.

## The verdict payload

- One round produces exactly **one verdict payload**: a
  single Copy-JSON action on the study, no per-item or
  per-part variants. The payload is the record of the
  round. Its shape:

  - the study id and the round number;
  - per item: the pick per axis (a slug, or a slug list
    for a multi axis), the composed line, and the item's
    status (live / frozen, with the round it froze at);
  - the items the owner did not touch this round, listed
    as unanswered — a displayed default is not a pick;
  - the closed-by-prior-picks list (below);
  - every prior round's payload, carried verbatim;
  - the feedback entries (below).

- The paste is the transport: the owner copies the payload
  back, and the receiving agent applies it. A payload with
  no picks — feedback only — is a legitimate verdict.

## Feedback

- Feedback is given in **feedback mode**: while it is on,
  the study's controls are inert, and tapping any part of
  a frame targets that exact part. An entry names its
  item, the part, the targeted content, and the comment,
  verbatim.
- Feedback rides the verdict payload, and it drives the
  next round: the next round's account quotes each entry
  verbatim and names the application it produced. Feedback
  that produces no application is recorded as a follow-up,
  not silently dropped.

## Rounds

- A round is an application pass: the agent rebuilds the
  study from the submitted payload and feedback. There is
  no in-page round machinery; the round boundary is the
  payload.
- The submitted picks become the study's defaults: the
  next round **opens on the picks**.
- Items without feedback are **locked**: no new variants,
  no re-presentation; their frames stand exactly as the
  previous round left them. New axes appear only where
  feedback points, and an axis added by a round is titled
  with its round.
- Option sets only shrink across rounds. A decided axis
  is closed (below) and is not re-offered; a reopened item
  returns with new axes, its decided axes surviving only
  in the record.

## Freeze

- Freeze is **per item**. A frozen item opens on its
  picks with its controls locked: real disabling, the
  picked option keeping the accent, the unpicked muted
  with a dashed outline but readable. The item is headed
  as a frozen verdict of record, and the freeze appends
  an entry to the study's **verdict account**.
- Freeze triggers on the owner's word, or on a submitted
  verdict payload covering the item. A study is fully
  frozen when every item is frozen; a fully frozen study
  carries the freeze as a banner over the whole study.
- The freeze locks picks, not the record: feedback mode
  and the payload remain readable on a frozen study.
- The **verdict account** lives in the study: a
  round-by-round record of applications, supersessions
  (the superseded form kept visible, marked superseded),
  freeze and unfreeze closings, and follow-ups not
  applied.

## Partial unfreeze

- On the owner's word, a **single item** unfreezes while
  every other item stays frozen: its controls go live
  under an unfrozen heading that names the exception, the
  ruled change is applied as the item's new default, and
  its other picks stand as frozen. The superseded option
  stays visible, labelled superseded.
- The item re-freezes on a submitted verdict payload
  covering it, or on the owner's word. Both acts append
  their closing entries to the verdict account.

## Closed by prior picks

- Decided axes are a closed set, held in the project's
  **standing-picks record**. Before composing any study,
  diff every axis about to be offered against that record.
  An axis a prior pick already decides is **closed**:
  compose it in the decided form, do not offer it, and do
  not re-ask it in any wording. Re-offering a decided form
  manufactures a duplicate pick the owner must spot and
  correct — the failure this rule exists to prevent.
- The study carries its closed list — item, closed form,
  and the pick it follows — in the study itself and in
  the payload. An item whose every axis is closed renders
  with no choices at all: its composed line is the settled
  form, not a selection.
- A pattern-level pick enters the standing-picks record
  when it freezes, so later studies inherit it as closed.

## The durable record

- The payload is the round's record and the verdict
  account is the study's, but neither is the project's
  durable record: study artifacts are scaffolding, and a
  registry's data is prunable by design. The owning thread
  transcribes the picks into the project's durable
  records — the answer record where a harness carries the
  study, the standing-picks record for pattern-level
  forms — and only then is the verdict durable. Artifact
  deletion at implementation follows `docs/method.md`
  (Artifact lifecycle).

## Open rulings

Two points this protocol deliberately does not decide;
each is stated with both forms and awaits the owner's
ruling, after which this section is struck and the ruled
form lands in the body above.

- **Build vehicle.** Form A: a study is stamped from the
  shared study kit and never hand-rolled — uniformity and
  the registration path are guaranteed, at the price that
  every protocol feature (live binding, freeze, account)
  must exist in the kit before a study may use it.
  Form B: a study may be hand-built to this protocol —
  studies ship at per-study build cost, with drift between
  builds as the standing risk the form exists to prevent.
- **Unanswered semantics.** Form A: a submitted payload
  asserts only the items it answers; an unanswered item
  keeps its prior state untouched. Form B: a submitted
  payload asserts the study's whole visible state,
  displayed defaults included. Form A protects a staged
  default from silent adoption; Form B makes one payload
  a complete snapshot of the study.
