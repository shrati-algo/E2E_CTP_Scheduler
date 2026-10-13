# CTP Scheduler — Open Issues (handover)

**The scheduler is built and runs. It stops at phase 2 on a data gap, not a code bug.**
Everything below is a **master-data** problem. None of it needs a code change.

Fill the data → `python run_all.py` → phases 2–6 run straight through.

The machine-readable version of this list, with empty `PLEASE_FILL_*` columns, is
**`CTP_SCHEDULER_REPORT.xlsx`** (regenerate any time with
`python make_data_issues_report.py`).

---

## 🛑 1. BLOCKING — 3 incomplete routing records

**Nothing runs until these are filled.** In `ctp_inputfiles/jkt_routing_pcr 14.xlsx`:

| routed_product | ItemType | department | NET demand | missing |
|---|---|---|---|---|
| `GT 2568 HT2` | Green Tyres | BUILDING | **19,036** | `proc_time`, `proc_time_UOM`, `machines` |
| `GT 1482 UHL` | Green Tyres | BUILDING | **14,670** | `proc_time`, `proc_time_UOM`, `machines` |
| `TCA313` | FINAL COMPOUND | FINAL MIXING | 239 | `proc_time`, `proc_time_UOM`, `machines`, `batch_size` |

These are not "a missing number" — the whole record is empty, and each has only **one**
operation, so there is nothing to fall back on. For reference, comparable operations run at:

- `GT BUILDING` — 187 items, median **65 SEC**, range 39.5–82
- `FINAL MIXING` — 3,654 items, median **130.26 SEC/BATCH**, range 90–155

**Those medians are shown to help you judge. The scheduler does not use them. No value has been
invented.**

> ### ⚠️ Do not "fix" this by defaulting the blank to zero.
> `phase4_cpm.py:368` does `pd.to_numeric(..., errors="coerce").fillna(0)`. A blank that
> survives phase 2 becomes a **zero-hour lot** — the plan would build **33,706 green tyres in no
> time at all** and report success.
>
> Deleting the routing rows is no better: `phase1b:225` (`if ch not in routed: continue`) would
> then drop those items from demand entirely, trading a loud crash for the **silent omission of
> two green tyres from the curing feed**.
>
> **The crash is the only thing currently preventing a silently-wrong schedule. Leave it until
> the data is real.**

*(`TBA827` and `MTCM313` are also blank, but they are MASTER COMPOUND, which the scheduler skips
outright — harmless.)*

---

## 🔴 2. Ply-cutter capacity is understated by half

The plant machine list (`ctp_inputfiles/pcr machines.png`) shows **two** ply cutters:

- `1103` — Textile Ply Cutter PCR
- `1104` — **Fischer Ply Cutter PCR**

**The routing only ever assigns ply cutting to `1103`.** `1104` appears nowhere.

So the scheduler believes ply-cutting has **half** the capacity the plant actually has, and will
build a queue on a bottleneck that does not exist in reality.

**→ Should `1104` be added to the routing's `machines` for the ply-cutting operations?**

Other machines on the plant list that the routing never uses (confirm whether this is correct):
`2001` Bias cutter · `1102` Edge gum calender · `2501/2502` Chafer doubling.
The plant list says **86 curing presses**; the routing contains **92**.

---

## ⚠️ 3. The compound lot-size floor silently disappears

`ctp_inputfiles/MOQ (3).xlsx` gives `Minimum Order Qty` as the **text `"3 Batches "`** — with
`UOM = KG`.

The scheduler does `float("3 Batches ")` → fails → **drops the MPQ floor entirely**, with no
warning. So **FINAL COMPOUND (43 demand items)** currently runs with **no minimum lot size at
all** — risking undersized mixer lots and a changeover explosion on one of the two
highest-volume item types.

**→ We need kg-per-batch, so the floor becomes `3 × batch_kg`.**
(A newer file, `MPQ_CTP_PCR_clean.xlsx`, exists in the source folder — if that is the correct
one, say so and it will be wired in.)

---

## ⚠️ 4. Latent: 25 routed products will silently inflate duration ~20×

25 routed products have `proc_time_UOM = SEC` / `SEC/BATCH` but **no `batch_size`**.

The scheduler's check is `if u == "SEC" and bs > 0`. A blank `bs` is `NaN`, and `NaN > 0` is
**False** — so it skips the per-**batch** branch and falls through to the per-**unit** branch.
Duration is then inflated by exactly `batch_size`× (measured: **20×** on one item). **Silently —
no crash, no warning.**

**None of them carry demand today.** It fires the moment one does. **This is the highest-risk
latent item in the set.**

---

## ⚠️ 5. One SKU is missing its second steel belt

`1325221715100SBRU0` carries **only `B1-PBLT008`** — no belt-2 row.

**227 of the 228 SKUs carry both belt plies** (`B1` + `B2`, 1 NOS each). A PCR radial belt
package is by definition ≥2 crossed steel plies; a single-belt PCR radial is not a real
construction. **This is almost certainly a missing BOM row**, not a real single-belt tyre.

**→ Please confirm and add the belt-2 row.**

---

## ⚠️ 6. Smaller data gaps

- **4 demand items have no row in the itemtype master** (`CAP 66 - CAPSTRIP`,
  `CAP 66-MOTHERROLL`, `MT1511`, `MT1512`) → no MPQ floor and no aging rule fires for them.
- **`child_quantity = 0` rows** → those children raise no demand and never get a producer.

> **CORRECTION.** An earlier version of this document claimed *"duplicate BOM lines →
> demand double-counted"*. **That was wrong, and it has been removed.** Those rows are
> legitimate: they differ by `grand_parent` (which component consumes the material), and the
> BOM's own arithmetic proves they must be **summed**, not deduplicated. See the next
> section — this was a bug in the scheduler, not in your BOM.
- **789 aging-master rows have an ItemCode and no aging values** (238 Carcass, 64 Bead Bundle,
  449 SMALL CHEMICAL, …). These are currently **dropped**, so the scheduler's own default for an
  unlisted item — **min 0 h, max 72 h** — applies. Nothing was invented, but if the real aging
  differs, populate the master.

---

## ℹ️ Three things already fixed — recorded so they are not re-introduced

### 🔴 The big one: phase 1b was discarding half the belt/ply compound

v6 keyed BOM edges on `(Parent, child)` and, where several rows differed only by
`grand_parent`, **kept the first row and threw the rest away.**

But `grand_parent` says **which component consumes this slice of a shared material**:

```
grand_parent   Parent       child   child_quantity
B1-PBLT071     CWBHTCP100   B186    0.5356 KG      <- belt ply 1
B2-PBLT071     CWBHTCP100   B186    0.4947 KG      <- belt ply 2
```

**Both belt plies are in every tyre** (227 of 228 SKUs list `B1` and `B2` as separate children,
1 NOS each). v6 provisioned **one belt's rubber for a two-belt tyre** — while the same
explosion correctly demanded 2.000 steel belts per tyre. It was internally contradicting itself.

**The BOM proves the correct rule itself.** It carries this identity:

> `Parent_qty(X)` == **SUM** of `child_quantity` over **every** row where `child == X`

| rule | nodes matching | violations |
|---|---|---|
| v6's first-row rule | 9,463 | **1,292** ❌ |
| **summing every row** | **10,755** | **0** ✅ |

Chemistry agrees independently: master-batch ÷ final-compound = **0.925** when summed (correct —
curatives are only 3–8% of a compound) versus **0.473** first-row-only (**chemically
impossible** — you cannot cure rubber with more sulphur than rubber).

**Effect of the fix:** FINAL COMPOUND demand **+9.1%** (7.29 → 7.95 kg/tyre); MASTER COMPOUND
+12.2%. **Every physical item is unchanged** — Green Tyre still exactly 1.000/tyre, Steel Belt
2.000, Bead 2.000, Bead Apex 2.000, Tread/Ply/SideWall/Inner Liner all identical.

**Before the fix, the final mixers were being told to mix ~9% less compound than the plant
actually needs — permanently and invisibly.** It would have surfaced on the floor as phantom
"yield loss", never as a planning error.

**A permanent gate now enforces the BOM identity on every run** (`10,755/10,755, 0 violations`).
If anyone reintroduces a dedup, or a future BOM breaks the identity, phase 1b **stops**.

⚠️ **This is a bug in v6 itself, not in CTP.** v6's own BOM has **7,782** such groups — **BTP is
under-counting its compound too.** Worth raising with the BTP team.

---

### Two more, fixed earlier

**`db_loader.py` was missing, and WIP netting was silently doing nothing.**
Every phase imports it inside a `try/except` with a fallback, so **nothing crashed** — phase 1c
printed one line and carried on:

```
[WARN] Inventory load failed (No module named 'db_loader'); proceeding with no WIP
```

The entire 39,510-row opening WIP was ignored, and the plan **over-produced by 12%**
(402,367 → 272,018 demand rows once fixed). **A phase that warns and continues is more dangerous
than one that crashes.**

**Opening-WIP `unit` was blank on 19,784 rows.** For length items the scheduler defaults a blank
to MM, but the real unit is M — a **1000×** under-credit. Now backfilled by **exact** itemcode
match from the BOM / routing (no fuzzy matching). Residual risk on demanded items: **zero**.

---

## The three intentional differences from BTP v6

Everything else in `phases/` is **byte-identical** to `v6_wave_p2` — verify with `diff`.

| # | what | why |
|---|---|---|
| 1 | **Efficiency from the routing** | v6 hardcodes `eff = 1.0`; CTP's routing says **0.96**. It divides, so every operation takes **+4.17%** longer. |
| 2 | **Transfer time from the routing** | v6 never reads it. Applied as a lag added to `min_aging` — green tyres get a **90-min** building→curing floor. |
| 3 | **The plant changeover matrix** | v6 has none; its rule is machine-**name**-based and matches **0 of CTP's 123 machine IDs**, so everything would be a flat 15 min. |

> **On (3):** the matrix is keyed on a machine **name** (`"4 RC Calendar"`), the routing on a
> machine **ID** (`"901"`) — **zero overlap**. The bridge is `machine_to_changeover_line` in
> `config.yaml`, built from the plant's own machine list. **Please review that table.**
> Curing, building and the mixers have **no line in the matrix**, so **building still uses a flat
> 15-min changeover.** If real building changeover data exists, that is the next thing to ask for.

Full reasoning for every decision is in **`MEMORY.md`**.
