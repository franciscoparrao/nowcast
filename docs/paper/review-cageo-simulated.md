# Simulated peer review — Computers & Geosciences

**Manuscript:** *nowcast: a forcing-agnostic Rust engine for dynamic geohazard nowcasting, and why forcing resolution — not the model — sets the skill ceiling*
**Calibration:** C&G (Elsevier), default reviewer mode Reject/Major Revision; acceptance ~20–25%. Peers sampled from `recent.bib`: FourCastLSTM (precipitation nowcasting), PIKANs (physics-informed landslide network), LADI (landslide displacement spatio-temporal interpolation).
*Simulated review for the author's internal use — not an editorial decision.*

---

## Phase 0.7 — Adversarial warmup (5 reasons this could be rejected, written before reading kindly)

1. The headline result is a **null** (AUC ≈ 0.48). The paper reframes "our distributed backtest failed" as "we diagnosed a resolution ceiling." A skeptical reviewer reads spin.
2. **No code is available at review** ("a Zenodo DOI will be deposited at submission"). For a *software* paper in C&G this is close to fatal — reproducibility is the journal's core demand and cannot be deferred.
3. **The computational contribution may be plumbing, not novelty.** The engine wraps six sibling models; the in-house algorithms (I–D rolling window, logistic, noisy-OR) are textbook. C&G failure-mode F2 (re-implementation/integration without methodological advance).
4. **Zero figures.** A 20-page geocomputation paper with only tables is a presentation red flag and will read as unfinished.
5. **Single-author, self-coupled ecosystem of unpublished engines.** The validation leans on sibling models (rainflow, snowmelt, hydroflux, the debris-flow ABM) that are not peer-reviewed; one of them was, to this reviewer's knowledge, recently rejected at this very journal.

---

## Phase 1 — Triage

Scope fit: **good.** Geospatial software + algorithm + a geoscience application is squarely C&G. The writing is clean and the honesty is unusual (explicit limitations section, a null reported as a null). The manuscript is complete in narrative but incomplete in the two things C&G weighs most: available code and computational evaluation. Not a desk reject on scope; serious substance gaps.

## Phase 2 — Multi-persona

### Persona A — Algorithm / software

The architectural idea — a single `Forcing` trait + composable `Trigger` so the hazard logic is decoupled from data source and trigger family — is clean and genuinely reusable, and the closed-form exact attribution is a nice touch (and a fair critique of post-hoc SHAP on this class of model). But I struggle to find the **computational novelty C&G requires**. The hazard model is `susceptibility × logistic(I–D exceedance)`; the I–D rolling-window with prefix sums is standard; noisy-OR is standard; the physical couplings are *delegated* to other engines. The paper's real contribution is software *architecture* and an *empirical diagnosis*, not a new algorithm. That can be publishable in C&G as a software article, but only if (i) the code is available and (ii) the engineering contribution is substantiated with what C&G expects from software papers: **performance** (timing, memory, scalability vs grid size and number of steps — the paper claims O(cells·steps·window) but reports no benchmarks), a real API/usage description, test coverage evidence, and a comparison to existing tools (e.g. is there an R/Python landslide-EWS toolchain this competes with? LandslideTools, the CTRL-T / I–D threshold calculators, glofas-style frameworks?). None of that is present. **Code availability deferred to "at submission" is not acceptable** — I cannot review reproducibility of a reproducibility-claiming paper.

Requests: release the repository (anonymised) now; add a performance/scalability section with wall-clock and memory vs problem size; position against existing EWS software, not only against the Caine threshold.

### Persona B — Data / statistics

The verification methodology is thoughtful (event-centred monthly matching; ROC-AUC and POD-at-area instead of CSI for a sparse, incomplete inventory — correct call, and well justified). But the validation is **thin and under-powered** for the claims:

- The distributed-discrimination conclusion rests on **one basin, one product, AUC ≈ 0.48** with no confidence interval, no bootstrap, no significance statement. "Near random" needs an uncertainty band; 0.48 vs 0.50 over 884 positives could be reported with a CI.
- There is **no baseline comparison**. The whole argument ("the model isn't the bottleneck") would be far stronger against an alternative model (e.g. an ML susceptibility-×-rainfall classifier, or a published regional I–D threshold) on the same data. As written, "distributing doesn't help" is shown only for *this* engine.
- The **lead-time result is anecdotal**: three events, no event onset *hour*, so "hours ahead" is not a measured lead time. The authors concede this, but it is then over-weighted in the abstract and conclusions ("pins the threshold crossing hours ahead of the documented flows").
- **a\*** is calibrated by a 1-D CSI sweep; no uncertainty on the threshold, no cross-basin transfer test (only odd/even years in one basin). Spatial autocorrelation of the daily field across the 270 cells is not accounted for in the AUC's effective sample size.
- The IMERG vs CR2MET head-to-head is on **one storm-core cell**; IMERG is known to overestimate in arid convective regimes (the 108 mm vs CR2MET 30 mm discrepancy is itself a result the paper does not interrogate — which product is right?).

Requests: add CIs/bootstrap to AUC and POD; add at least one model baseline; either down-scope the lead-time claim or validate it against day/hour-resolution events; address IMERG bias.

### Persona C — Applied / domain

The framing — susceptibility is static, the gap is dynamic triggering — is correct and well-cited (Bogaard & Greco, Segoni, Reichenbach). The honesty about the inventory ceiling is refreshing and correct. Two domain concerns. First, the **antecedent-moisture** mechanism, central to rainfall-triggered landslides, is only implicit in long windows; in a Mediterranean-to-arid gradient this matters and the high structural FAR partly reflects its absence — worth more than one sentence. Second, the paper's contribution to *operational* practice is asserted but not demonstrated: there is no real-time ingestion, no comparison with what SERNAGEOMIN/DGA actually use, and the "nowcast" label is, by the authors' own admission, hindcast. The domain reader is left convinced of a *diagnosis* (resolution matters) but not of a *tool* that advances Chilean EWS today.

## Phase 2.5 — Failure-mode checks

- **F1 (AI-generated content):** references verified and resolvable (good — this journal's reviewers actively hunt hallucinated DOIs). No figures, so no AI-figure artifacts. Prose is competent. **Pass**, but note: a single-author "engine of engines" written fluently will still draw AI scrutiny; the available code is the best defense.
- **Data availability (honest check):** **FAIL at review time.** Code "at submission", derived data "regenerable from scripts" not provided, input data third-party. This is the single most likely reject lever at C&G specifically.
- **Figure audit:** **FAIL.** Zero figures. At minimum: (Fig 1) architecture/dataflow; (Fig 2) the I–D calibration sweep / ROC curves; (Fig 3) the sub-daily lead-time timeline (a hyetograph with the crossing marked); (Fig 4) an example hazard map. Tables alone cannot carry a geocomputation paper.
- **Reference audit:** 12 references, all real and with DOIs. Adequate but **thin** for C&G — missing the EWS-software and operational-threshold literature (CTRL-T, regional Chilean I–D thresholds, IMERG-for-landslides validation studies, the SERNAGEOMIN inventory's own documentation). The IMERG and CR2MET citations should be the canonical dataset references.
- **Structure check:** sound. Abstract/intro/engine/data/methods/results/discussion/limitations/conclusions. Limitations section is unusually candid (credit).

## Phase 3 — Comparison with C&G peers

- **vs FourCastLSTM (precipitation nowcasting):** that peer delivers a *new model* with quantified skill against baselines and ablations. This manuscript delivers an *architecture + diagnosis*; against the journal's typical nowcasting paper it is light on quantitative model advance and baselines.
- **vs PIKANs (physics-informed landslide network):** PIKANs offers a methodological novelty (KAN architecture) with benchmarks. This manuscript's novelty is architectural/empirical; it must lean harder on the software contribution — which then *requires* the code and performance evidence that are missing.
- **vs LADI (landslide displacement spatio-temporal):** LADI is a focused algorithm with validation. This manuscript is broader but shallower per claim. C&G tends to reward depth over breadth; the "eight crates, six providers" breadth may read as unfocused rather than impressive.

The honest read: against recent C&G landslide/nowcasting papers, this one is **more honest and better-architected but weaker on quantitative novelty, benchmarking, and reproducibility-at-review**.

## Phase 4 — Pre-commitment (issues fixed before any praise)

**Reject-level (must resolve):**
1. Code not available at review — release the (anonymised) repository.
2. No figures — add at least four.
3. Computational contribution insufficiently substantiated for a software paper — add performance/scalability and position vs existing EWS software.

**Major:**
4. No baseline model; "model isn't the bottleneck" shown only for this engine.
5. Statistical rigor: no CIs on AUC/POD; spatial autocorrelation ignored in effective N.
6. Lead-time claim anecdotal (3 events, no onset hour) yet prominent in abstract/conclusions.
7. IMERG vs CR2MET magnitude discrepancy (108 vs 30 mm) not interrogated — which is correct?
8. Sibling engines unpublished; validation depends on them.

**Minor:**
9. Antecedent moisture under-treated.
10. Reference list thin on EWS-software / Chilean-threshold / IMERG-landslide literature.
11. "nowcast" vs hindcast tension should be flagged earlier than the limitations.

## Phase 5 — Synthesis and verdict

This is a clearly written, intellectually honest paper with a genuinely reusable software idea (the forcing-agnostic trigger) and a legitimately interesting negative result (forcing resolution as the binding constraint). Those are real and uncommon virtues. But measured against Computers & Geosciences' bar, three things block it now: **the code is not available**, **there are no figures**, and **the computational/quantitative contribution is under-substantiated** (no performance study, no baseline, no uncertainty). The validation is thoughtful but thin, and the lead-time claim outruns its evidence.

**Recommendation: Major Revision** — with the explicit caveat that a second reviewer applying C&G's default could reasonably **Reject**, primarily on (1) code-at-review and (3) thin novelty/benchmarking. The path to acceptance is concrete and achievable:
- Release the repository (now), with tests and a Zenodo DOI.
- Add the four figures.
- Add a performance/scalability section and position against existing EWS tooling.
- Add a model baseline and uncertainty bands; down-scope or properly validate the lead-time claim.
- Either lead with the *software* contribution (and make it bulletproof) or lead with the *resolution diagnosis* (and make the statistics bulletproof) — the paper currently does both at half strength.

## Phase 6 — Calibration self-check

Verdict (Major Revision, reject-risk) is consistent with C&G's observed default and with the two analysed rejects (both leaned on reproducibility + contribution-substantiation). I have not flipped a hidden weakness into praise: the architecture is praised only after the reproducibility and novelty gaps are stated. The single largest, most actionable lever is **releasing the code with figures and a performance study** — without it, the honest expectation at this journal is Reject.
