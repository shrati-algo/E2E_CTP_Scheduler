> **⚠ SUPERSEDED — written 2026-09-26, describes e2e_v1–v3.**
> The current run is **e2e_v14** and several things below have since
> changed (virtual WIP removed, two-database pipeline, bead gate, even
> curing lots, the v14 dispatch fix).
> **Start from [CLAUDE_CONTEXT.md](CLAUDE_CONTEXT.md)** — the single
> source of truth — and [HOW_TO_RUN.md](HOW_TO_RUN.md) for commands.
> Kept for background on the original port only.

---

# The BTP scheduler, explained simply

No jargon, one tyre followed all the way through. If you want the full detail
afterwards, it is in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## The idea in one sentence

> **Someone else decides when each tyre gets built. This engine works out
> everything that has to happen *before* that, and tells you honestly where it
> won't be ready in time.**

That's it. The engine never argues with the building plan. It just answers:
*can we actually feed it?*

---

## Why that's hard

A tyre isn't assembled from parts you buy. Almost everything is made in the
same factory, in a chain, and **most of it goes off.**

Here is one real chain from the plant — reading right to left is how it's made,
left to right is how it's consumed:

```
MB230  →  MB231  →  MB1232  →  B460  →  CPJ1218  →  cut belt  →  GREEN TYRE
(mix)     (mix)     (mix)      (mix)    (calender)  (cutter)     (building drum)
```

Six operations, six different machines, before one belt exists. And a real
green tyre needs about **sixty** components like this, each with its own chain.

Three things make it genuinely hard:

**1. Everything has a clock.** Rubber compound must rest before you can use it
(*minimum aging*), and it dies after a while (*shelf life*). Cap strip lives
24 hours. Some compound lives 15 days. So you can't just make everything early.

**2. Everything shares machines.** Six mixers make hundreds of different
compounds. One calender makes every belt roll. Machines have to be shared, and
changing what a machine is making costs time.

**3. A tyre needs *all* its parts.** Fifty-nine parts ready and one missing
means zero tyres. The last part to arrive decides everything.

---

## The shape of the answer

The engine does two passes, and this is the whole trick:

```
        BACKWARDS                              FORWARDS
   "what must exist,                     "what the machines
    and by when?"                          can actually do"
        │                                        │
        ▼                                        ▼
   deadlines                              real start times
        └──────────────► THE GAP ◄──────────────┘
                     = the honest answer
```

Work backwards from the frozen build to get **deadlines**. Then work forwards
through real machines to get **actual times**. Wherever the actual time is later
than the deadline, you have a problem — and the engine names it instead of
hiding it.

---

## The seven steps

Let's follow **one building row**: *"build 500 tyres of SKU X on drum 6003,
starting Tuesday 07:00."*

---

### Step 1 — What does this build need?

**Input:** the frozen plan, plus the recipe (BOM).

The engine looks up the tyre in the recipe book and walks down the tree,
collecting every component: tread, sidewall, inner liner, beads, belts, cap
strip, and all the compounds underneath them.

Two small but important things happen here:

**It splits the row into trolleys.** The plan says "500 tyres from 07:00 to
14:17", but the floor actually takes them off the drum onto trolleys of 16.
So it's really 32 trolleys, spread across those seven hours. That matters,
because a part feeding the *last* trolley is genuinely needed seven hours later
than one feeding the first.

**It sets a deadline for each part.** The rule is simple:

> **deadline = when the build starts − how long that part must rest**

If a compound has to rest 4 hours and the build starts Tuesday 07:00, that
compound has to be **finished by Tuesday 03:00**. Not started — *finished*.

**Output:** a long list — *this part, this much, needed by this time.*

---

### Step 2 — What do we already have?

**Input:** the stock snapshot (what's physically on the racks).

Before making anything, check the shelves. But stock isn't just a number — each
batch has a birthday, and two questions get asked about it:

- **Is it ready yet?** (has it rested long enough)
- **Is it still alive?** (has it gone off)

Oldest-usable stock gets used first, so short-dated material goes before it
rots.

One neat consequence: if you already have a finished belt, you no longer need
the compound that would have gone into it. That saving flows all the way down
the chain automatically.

**Output:** the same list, minus what stock already covers. *This is what we
actually have to make.*

---

### Step 3 — Group it into sensible batches

**Input:** the remaining demand.

You don't make 3 kg of compound because one trolley needs 3 kg. You make a
proper batch. But you also can't make a month's worth at once — it would go off.

So demand gets grouped, with two limits:

- **Don't group demand that's spread wider than the material survives.** If a
  part lives 24 hours, you can't make one batch to cover three days of need.
- **Stop when the floor stops asking.** If nothing needs this part for the next
  8 hours, that's the end of the run.

The mixer is a special case: it physically cannot make half a batch, so compound
always rounds up to whole batches.

**Output:** actual lots. *Make this much of this, in one go.*

---

### Step 4 — When does each lot have to be made?

**Input:** the lots, and the chain that links them.

Now walk backwards down the chain, charging the resting time at **every single
level** — not just the last one.

Using our chain, and reading right to left:

```
Green tyre builds           Tuesday 07:00
  belt must be cut by       Tuesday 07:00 − belt resting time − 10 min handling
  roll must be made by      (belt start) − roll resting time − 10 min
  B460 must be mixed by     (roll start) − B460 resting time − 10 min
  MB1232 must be mixed by   (B460 start) − MB1232 resting time − 10 min
  ...and so on to MB230
```

That 10 minutes is moving material between machines. Small, but it adds up over
six levels.

Each lot ends up with three times:

| | Meaning |
|---|---|
| **Latest finish** | any later and the next step can't start on time |
| **Earliest start** | any earlier and it goes off before it's used |
| **Slack** | the gap between them — how much room you have |

Zero slack means that lot is on the critical path. Negative means it's already
impossible.

> **A subtlety worth knowing:** how early you may make something depends on
> **how long it keeps**, not on how deep it sits in the recipe. A 15-day
> compound and a 24-hour cap strip sit at similar depths but deserve completely
> different treatment. Earlier versions of this engine got that wrong, and
> short-life parts kept expiring.

**Output:** every lot now has a window it must fit inside.

---

### Step 5 — Which machine?

**Input:** the lots, the machine list.

Most operations only have one machine that can do them, so there's nothing to
decide. Where there is a choice — mostly the mixers — the work gets spread so no
machine is overloaded **on any given day**.

Why per day and not per month? Because the plant is judged daily. A machine
that's 60% busy on average can still be hopelessly jammed on the Tuesday the
work is actually due.

One rule of thumb: **keep an item on one machine.** Every extra machine an item
visits costs another changeover. It only gets split when one machine genuinely
can't hold it.

**Output:** every lot knows its machine.

---

### Step 6 — When, exactly?

**Input:** everything above.

This is the real scheduling. It happens in two passes.

#### Pass 1 — forwards, to make it *possible*

Go through the chain in order. A lot can only start when **all four** of these
are true:

1. The factory is open
2. Everything it needs is finished **and has rested**
3. Its machine is free
4. It's allowed to start yet (not too early — it would go off)

Take the latest of those four, and that's the start time.

Because you only ever place a lot after everything feeding it, the schedule
can't contain an impossible sequence. It's correct *by construction*, not by
checking afterwards.

When choosing what to run next, the machine prefers **to keep making what it's
already set up for** — switching costs time.

#### Pass 2 — backwards, to keep it *fresh*

Pass 1 makes everything as early as possible. That's the worst thing for
freshness: parts sit around going off.

So the engine now slides each lot **as late as it legally can**, without:

- missing its deadline,
- overtaking whatever it feeds,
- or crashing into the next lot on its machine.

Nothing can move earlier. Nothing can become impossible. The waiting-around just
gets squeezed out.

**Output:** a real schedule — every lot, on a machine, at a time.

---

### Step 7 — So can the tyre actually be built?

**Input:** the schedule.

Now the moment of truth. For each trolley:

> **The kit is ready when the *last* component is finished and has rested.**

If that's before the planned build time — great, it builds on plan.

If it's after, the trolley moves, and the engine records **which component was
responsible**. That's the important bit. It's usually not the one you'd guess.

Then the same logic applies one level up: a press can only cure a tyre that's
actually been built. Green tyres are matched up in order — the 100th cured tyre
needs the 100th green tyre — and if a press lot has to wait, everything behind
it on that press waits too.

**Output:** three numbers, per day:

- how many green tyres could really be built
- how many carcasses
- how many tyres actually cured

---

## The whole thing on one page

```
  THE PLAN (frozen)  ──┐
                       │
  1. What does it need?          recipe × quantity, deadline per part
                       │
  2. What do we have?            check the racks, oldest first
                       │
  3. Batch it up                 big enough to be sensible,
                       │         small enough not to go off
  4. When must it be made?       walk backwards, charge resting
                       │         time at every level
  5. Which machine?              spread the load, per day
                       │
  6. When exactly?               forwards to make it possible,
                       │         backwards to keep it fresh
  7. Is the kit complete?  ──►   THE ANSWER
                                 + who's to blame if not
```

---

## The handful of rules that matter most

There are dozens. These five carry most of the weight.

**You cannot use expired material.** Absolute. And freshness is checked at the
moment the material is actually *picked up*, not when the tyre is finished — for
something deep in the chain those can be days apart.

**Resting time is charged at every level.** Skip it anywhere and the plan
quietly assumes material can be used the instant it's made.

**A machine does one thing at a time.** Obvious, but it's checked by re-reading
the finished schedule rather than trusting the code that wrote it.

**Changing what a machine makes costs time**, and how much depends on what
you're switching *from* and *to*. So the engine groups similar work together —
but not too much, which brings us to the most surprising lesson in this engine:

**Saving setup time is not the goal.** Once, a change cut extruder changeovers
by 464 hours — and output *collapsed* from 99.5% to 45.1%. The machine had
stopped alternating between tread and sidewall, and every tyre needs both. It
was beautifully efficient at making the wrong mix.

> That's the theme of the whole system: **a plan that looks tidy isn't the same
> as a plan that makes tyres.**

---

## What honest output looks like

The engine reports what it *couldn't* do as carefully as what it could:

- trolleys that had to move, and the exact component that blocked each one
- lots that can't be made in time no matter what
- material that would go off before it's used
- machines over capacity
- rules the engine enforces that the plant's own data doesn't actually support

That last one is unusual and worth calling out. The engine charges a 15-minute
setup for one changeover that, measured across 104 real instances in the plant's
own records, has a **median of zero**. It's written down, in the docs, as a
thing we do that the evidence doesn't back.

---

## Where it got to

On the September plan: **508,777 tyres of a 542,612 plan — 93.8%**, with the
first ten days at 96.6%, zero machine clashes, and zero short changeovers.

The gap isn't a mystery. It's mostly one thing: the calender is genuinely too
busy, and everything downstream waits for it.

---

## If you remember four things

1. **The building plan is fixed.** The engine feeds it; it never rewrites it.
2. **Backwards for deadlines, forwards for reality.** The gap between them is
   the answer.
3. **Everything has a clock** — a resting time and an expiry — and that's what
   makes it hard.
4. **A tyre waits for its worst part.** Naming that part is the single most
   useful thing the engine does.
