//! Backtesting against a dated event inventory.
//!
//! Validation of a nowcast is a binary forecast-verification problem: on each
//! time unit the model either raises an alert or not, and an event either
//! occurred or not. That gives a 2×2 [`Contingency`] table and the standard
//! categorical skill scores (POD, FAR, CSI, frequency bias).
//!
//! The matching unit here is the **calendar month**, because dated landslide
//! inventories (SERNAGEOMIN) are reliable to the year and only approximately to
//! the month — the id-encoded month can be off by weeks (e.g. the May 1993
//! Quebrada de Macul debris flow is filed under March in the inventory). Daily
//! lead-time scoring needs day-resolution events and is left to future work.

use std::collections::BTreeSet;

use crate::error::{Error, Result};
use crate::grid::GridDims;

/// A 2×2 contingency table for binary event forecasting.
///
/// ```text
///                     event observed
///                      yes        no
///   alert  yes   |   hits   | false_alarms |
///          no    |  misses  | corr_neg     |
/// ```
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
pub struct Contingency {
    pub hits: u64,
    pub misses: u64,
    pub false_alarms: u64,
    pub correct_negatives: u64,
}

impl Contingency {
    /// Total number of units (months) scored.
    ///
    /// Counters are `u64` because a catalog-scale spatial verification (e.g.
    /// a continental grid × a decade of days) overflows `u32` on the
    /// correct-negative count alone.
    pub fn n(&self) -> u64 {
        self.hits + self.misses + self.false_alarms + self.correct_negatives
    }

    /// Probability of detection (hit rate): `hits / (hits + misses)`.
    /// Fraction of observed events that were alerted. `None` if no events.
    pub fn pod(&self) -> Option<f64> {
        let denom = self.hits + self.misses;
        (denom > 0).then(|| self.hits as f64 / denom as f64)
    }

    /// False alarm ratio: `false_alarms / (hits + false_alarms)`.
    /// Fraction of alerts that were wrong. `None` if no alerts.
    pub fn far(&self) -> Option<f64> {
        let denom = self.hits + self.false_alarms;
        (denom > 0).then(|| self.false_alarms as f64 / denom as f64)
    }

    /// Critical success index (threat score):
    /// `hits / (hits + misses + false_alarms)`. `None` if all three are zero.
    pub fn csi(&self) -> Option<f64> {
        let denom = self.hits + self.misses + self.false_alarms;
        (denom > 0).then(|| self.hits as f64 / denom as f64)
    }

    /// Frequency bias: `(hits + false_alarms) / (hits + misses)`. `>1` over-
    /// forecasts, `<1` under-forecasts. `None` if no events.
    pub fn frequency_bias(&self) -> Option<f64> {
        let denom = self.hits + self.misses;
        (denom > 0).then(|| (self.hits + self.false_alarms) as f64 / denom as f64)
    }
}

/// A `(year, month)` key, 1-based month.
pub type MonthKey = (i32, u32);

/// `(year, month)` keys parsed from a leading `YYYY-MM-...` date column
/// (column 0), one per line whose date parses to a valid month (a header row
/// is skipped naturally). The shared dated-gauge parser behind the CLI
/// backtest; align it 1:1 with the depths from
/// [`csv_column`](crate::csv_column) on the same text.
pub fn csv_month_keys(text: &str) -> Vec<MonthKey> {
    text.lines()
        .filter_map(|line| {
            let date = line.split(',').next()?;
            let mut d = date.trim().split('-');
            let y: i32 = d.next()?.trim().parse().ok()?;
            let m: u32 = d.next()?.trim().parse().ok()?;
            (1..=12).contains(&m).then_some((y, m))
        })
        .collect()
}

/// Event inventory `(year, month)` keys from a CSV with columns
/// `id, year, month` (header row skipped, rows that fail to parse ignored) —
/// the SERNAGEOMIN-style inventory layout used by the backtests.
pub fn csv_events(text: &str) -> Vec<MonthKey> {
    text.lines()
        .skip(1)
        .filter_map(|line| {
            let mut f = line.split(',');
            let _id = f.next()?;
            let y: i32 = f.next()?.trim().parse().ok()?;
            let m: u32 = f.next()?.trim().parse().ok()?;
            (1..=12).contains(&m).then_some((y, m))
        })
        .collect()
}

/// Shift a month key by `delta` months (handles year rollover).
fn shift_month((y, m): MonthKey, delta: i32) -> MonthKey {
    let zero = y * 12 + (m as i32 - 1) + delta;
    (zero.div_euclid(12), (zero.rem_euclid(12) + 1) as u32)
}

/// Build a contingency table by **event-centric** matching with a ±`tol_months`
/// window — the standard verification unit for early-warning systems, which
/// avoids inflating misses when one event spans a multi-month tolerance window.
///
/// - `day_month` is the `(year, month)` key for each day, aligned 1:1 with
///   `alert_days` (the per-day alert flags from the nowcast).
/// - `event_months` are the months in which an event was observed (duplicates,
///   i.e. several events in one month, collapse to that month).
/// - `tol_months` is the matching half-window, absorbing the inventory's
///   month-level date uncertainty.
///
/// Counting (note the units differ by category, as is conventional for EWS
/// verification):
/// - **hit**  — an observed event-month with ≥1 alert month within ±tol.
/// - **miss** — an observed event-month with no alert within ±tol.
/// - **false_alarm** — an alert month with no event within ±tol.
/// - **correct_negative** — a present, non-alerted month with no event within
///   ±tol (a quiet, correctly-silent month).
pub fn monthly_contingency(
    day_month: &[MonthKey],
    alert_days: &[bool],
    event_months: &[MonthKey],
    tol_months: u32,
) -> Result<Contingency> {
    if day_month.len() != alert_days.len() {
        return Err(Error::InvalidParameter {
            name: "alert_days",
            reason: format!(
                "{} day_month entries but {} alert_days",
                day_month.len(),
                alert_days.len()
            ),
        });
    }
    let tol = tol_months as i32;

    // Months with ≥1 alert, and the full set of months present in the record.
    let mut alerted: BTreeSet<MonthKey> = BTreeSet::new();
    let mut present: BTreeSet<MonthKey> = BTreeSet::new();
    for (&mk, &a) in day_month.iter().zip(alert_days) {
        present.insert(mk);
        if a {
            alerted.insert(mk);
        }
    }

    // Distinct observed event-months that actually fall inside the record.
    let observed: BTreeSet<MonthKey> = event_months
        .iter()
        .copied()
        .filter(|mk| present.contains(mk))
        .collect();

    let alert_near = |mk: MonthKey| (-tol..=tol).any(|d| alerted.contains(&shift_month(mk, d)));
    let event_near = |mk: MonthKey| (-tol..=tol).any(|d| observed.contains(&shift_month(mk, d)));

    let mut c = Contingency::default();
    // Events: hit if any alert within ±tol, else miss.
    for &ev in &observed {
        if alert_near(ev) {
            c.hits += 1;
        } else {
            c.misses += 1;
        }
    }
    // Months: false alarms (alert, no nearby event) and correct negatives
    // (quiet month, no nearby event). Months near an event are already scored
    // through the event loop above.
    for &mk in &present {
        if event_near(mk) {
            continue;
        }
        if alerted.contains(&mk) {
            c.false_alarms += 1;
        } else {
            c.correct_negatives += 1;
        }
    }
    Ok(c)
}

/// Row-major cells within Chebyshev `radius` of `center`, clipped to the grid.
fn chebyshev_window(dims: GridDims, center: usize, radius: usize) -> Vec<usize> {
    let (er, ec) = ((center / dims.ncols) as i64, (center % dims.ncols) as i64);
    let r = radius as i64;
    let mut cells = Vec::with_capacity((2 * radius + 1).pow(2));
    for dr in -r..=r {
        for dc in -r..=r {
            let (nr, nc) = (er + dr, ec + dc);
            if nr < 0 || nc < 0 || nr >= dims.nrows as i64 || nc >= dims.ncols as i64 {
                continue;
            }
            cells.push(nr as usize * dims.ncols + nc as usize);
        }
    }
    cells
}

/// **Spatial** event-centric contingency over a grid of per-cell monthly alerts.
///
/// Generalises [`monthly_contingency`] to space: an event is matched only if a
/// nearby cell alerted, so a wet month that triggers far from the slide no
/// longer counts as a hit — the core of attacking structural false alarms.
///
/// - `dims` — the grid the cells index into (row-major).
/// - `months` — the distinct calendar months in the analysed period.
/// - `alerted` — the `(cell, month)` pairs that raised an alert.
/// - `events` — observed `(cell, month)` events (deduplicated internally).
/// - `cell_radius` — neighbourhood half-width (Chebyshev) for a spatial match.
/// - `tol_months` — month-matching half-window (inventory date slack).
///
/// Counting mirrors [`monthly_contingency`]: hit = event with an alert within
/// the space–time window; miss = event without one; false_alarm = alerted
/// `(cell, month)` outside every event's window; correct_negative = a
/// non-alerted `(cell, month)` outside every event's window.
///
/// Complexity is catalog-friendly: hits probe each event's own space–time
/// window (O(events · radius² · tol) membership tests, never a scan of the
/// whole alert set), false alarms are one pass over the alerts, and correct
/// negatives come from set arithmetic instead of a months × cells sweep.
pub fn spatial_monthly_contingency(
    dims: GridDims,
    months: &[MonthKey],
    alerted: &BTreeSet<(usize, MonthKey)>,
    events: &[(usize, MonthKey)],
    cell_radius: usize,
    tol_months: u32,
) -> Contingency {
    let month_set: BTreeSet<MonthKey> = months.iter().copied().collect();
    let tol = tol_months as i32;
    let n_cells = dims.len();

    // Deduplicated events that fall inside the analysed period.
    let observed: BTreeSet<(usize, MonthKey)> = events
        .iter()
        .copied()
        .filter(|(c, m)| *c < n_cells && month_set.contains(m))
        .collect();

    // Space–time footprint of all events (cells within radius × months ±tol).
    let mut footprint: BTreeSet<(usize, MonthKey)> = BTreeSet::new();
    for &(ec, em) in &observed {
        for cell in chebyshev_window(dims, ec, cell_radius) {
            for d in -tol..=tol {
                footprint.insert((cell, shift_month(em, d)));
            }
        }
    }

    let mut c = Contingency::default();
    // Hits: probe the event's own window against the alert set.
    for &(ec, em) in &observed {
        let hit = chebyshev_window(dims, ec, cell_radius)
            .iter()
            .any(|&cell| (-tol..=tol).any(|d| alerted.contains(&(cell, shift_month(em, d)))));
        if hit {
            c.hits += 1;
        } else {
            c.misses += 1;
        }
    }

    // False alarms in one pass; count in-grid alerts outside every footprint on
    // the side for the correct-negative arithmetic below.
    let mut alerted_in_grid_not_fp: u64 = 0;
    for &(ac, am) in alerted {
        if month_set.contains(&am) && !footprint.contains(&(ac, am)) {
            c.false_alarms += 1;
            if ac < n_cells {
                alerted_in_grid_not_fp += 1;
            }
        }
    }

    // Correct negatives by set arithmetic: every in-period unit that is neither
    // alerted nor inside a footprint. (Footprint cells are in-grid by
    // construction; only its months can leave the period.)
    let fp_in_period = footprint.iter().filter(|(_, m)| month_set.contains(m)).count() as u64;
    let total_units = month_set.len() as u64 * n_cells as u64;
    c.correct_negatives = total_units - fp_in_period - alerted_in_grid_not_fp;
    c
}

/// A monotone integer time index — a day number, or a sub-daily step index.
///
/// Unlike [`MonthKey`], matching is **linear**: there is no calendar rollover,
/// so the tolerance window is simply `key ± tol`. Day numbers (e.g. days since a
/// fixed epoch) are the intended use for a day-resolution inventory such as the
/// NASA Global Landslide Catalog / COOLR; feeding step indices instead scores at
/// the forcing's native sub-daily resolution, no other change required.
pub type DayKey = i64;

/// **Day-resolution** spatial, event-centric contingency.
///
/// The day-resolution analogue of [`spatial_monthly_contingency`], for
/// inventories dated to the day (or finer) rather than the month. The space–time
/// matching rule is identical — an event is a hit iff some cell within
/// `cell_radius` (Chebyshev) alerted within `tol_days` of it — but the temporal
/// key is a linear integer index ([`DayKey`]), so the tolerance window is
/// `day ± tol_days` with no calendar arithmetic. This is the matcher a
/// day-dated inventory needs to turn the month-resolution backtest into a genuine
/// lead-time verification (the step left to future work in the month version).
///
/// Counting mirrors [`spatial_monthly_contingency`] exactly: hit = event with an
/// alert inside its space–time window; miss = event without one; false_alarm =
/// alerted `(cell, day)` outside every event's window; correct_negative = a
/// non-alerted `(cell, day)` outside every event's window.
///
/// Complexity is catalog-friendly — this is the matcher a COOLR × IMERG-scale
/// run needs: hits probe each event's own space–time window (O(events ·
/// radius² · tol) membership tests, never a scan of the whole alert set),
/// false alarms are one pass over the alerts, and correct negatives come from
/// set arithmetic instead of a days × cells sweep.
pub fn spatial_daily_contingency(
    dims: GridDims,
    days: &[DayKey],
    alerted: &BTreeSet<(usize, DayKey)>,
    events: &[(usize, DayKey)],
    cell_radius: usize,
    tol_days: u32,
) -> Contingency {
    let day_set: BTreeSet<DayKey> = days.iter().copied().collect();
    let tol = tol_days as i64;
    let n_cells = dims.len();

    // Deduplicated events that fall inside the analysed period.
    let observed: BTreeSet<(usize, DayKey)> = events
        .iter()
        .copied()
        .filter(|(c, d)| *c < n_cells && day_set.contains(d))
        .collect();

    // Space–time footprint of all events (cells within radius × days ±tol).
    let mut footprint: BTreeSet<(usize, DayKey)> = BTreeSet::new();
    for &(ec, ed) in &observed {
        for cell in chebyshev_window(dims, ec, cell_radius) {
            for d in -tol..=tol {
                footprint.insert((cell, ed + d));
            }
        }
    }

    let mut c = Contingency::default();
    // Hits: probe the event's own window against the alert set.
    for &(ec, ed) in &observed {
        let hit = chebyshev_window(dims, ec, cell_radius)
            .iter()
            .any(|&cell| (-tol..=tol).any(|d| alerted.contains(&(cell, ed + d))));
        if hit {
            c.hits += 1;
        } else {
            c.misses += 1;
        }
    }

    // False alarms in one pass; count in-grid alerts outside every footprint on
    // the side for the correct-negative arithmetic below.
    let mut alerted_in_grid_not_fp: u64 = 0;
    for &(ac, ad) in alerted {
        if day_set.contains(&ad) && !footprint.contains(&(ac, ad)) {
            c.false_alarms += 1;
            if ac < n_cells {
                alerted_in_grid_not_fp += 1;
            }
        }
    }

    // Correct negatives by set arithmetic: every in-period unit that is neither
    // alerted nor inside a footprint. (Footprint cells are in-grid by
    // construction; only its days can leave the period.)
    let fp_in_period = footprint.iter().filter(|(_, d)| day_set.contains(d)).count() as u64;
    let total_units = day_set.len() as u64 * n_cells as u64;
    c.correct_negatives = total_units - fp_in_period - alerted_in_grid_not_fp;
    c
}

/// Area under the ROC curve of a ranked hazard score against binary labels.
///
/// Computed from the Mann–Whitney U statistic (rank-sum), which equals the AUC
/// exactly and handles ties by average ranks. `scores` and `labels` are aligned
/// 1:1, one entry per scored unit (e.g. a `(cell, day)` hazard value and whether
/// an event occurred there). Returns `None` if either class is empty, since AUC
/// is undefined without both positives and negatives.
///
/// AUC is the discrimination metric of choice when the inventory is spatially
/// sparse and incomplete, which makes threshold-dependent scores (FAR, CSI)
/// uninformative but leaves ranking skill measurable.
pub fn roc_auc(scores: &[f64], labels: &[bool]) -> Result<Option<f64>> {
    if scores.len() != labels.len() {
        return Err(Error::InvalidParameter {
            name: "labels",
            reason: format!("{} scores but {} labels", scores.len(), labels.len()),
        });
    }
    let n_pos = labels.iter().filter(|&&l| l).count();
    let n_neg = labels.len() - n_pos;
    if n_pos == 0 || n_neg == 0 {
        return Ok(None);
    }

    // Average (1-based) ranks over scores sorted ascending, ties shared.
    let mut idx: Vec<usize> = (0..scores.len()).collect();
    idx.sort_by(|&a, &b| {
        scores[a]
            .partial_cmp(&scores[b])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut ranks = vec![0.0f64; scores.len()];
    let mut i = 0;
    while i < idx.len() {
        let mut j = i + 1;
        while j < idx.len()
            && scores[idx[j]].partial_cmp(&scores[idx[i]]) == Some(std::cmp::Ordering::Equal)
        {
            j += 1;
        }
        // Sorted positions i..j (0-based) hold 1-based ranks (i+1)..=j.
        let avg_rank = ((i + 1 + j) as f64) / 2.0;
        for &k in &idx[i..j] {
            ranks[k] = avg_rank;
        }
        i = j;
    }

    let sum_pos: f64 = labels
        .iter()
        .zip(&ranks)
        .filter_map(|(&l, &r)| l.then_some(r))
        .sum();
    let u = sum_pos - (n_pos as f64) * (n_pos as f64 + 1.0) / 2.0;
    Ok(Some(u / (n_pos as f64 * n_neg as f64)))
}

/// Area under the precision–recall curve (average precision) of a ranked
/// hazard score against binary labels.
///
/// The discrimination metric of choice when events are **rare**: unlike
/// [`roc_auc`], it is not inflated by the overwhelming true-negative mass (the
/// Maipo backtest sits at a ~4 % event base rate; a catalog-scale cell-day
/// panel is far rarer still, where a ROC-AUC of 0.9 can coexist with useless
/// precision). Baseline for a skill-less score is the base rate, not 0.5.
///
/// Computed as `AP = Σ (R_i − R_{i−1}) · P_i` over the descending-ranked
/// scores, processing tied scores as one block (precision evaluated at the
/// block's end, so ties cannot fake resolution). Returns `None` if there are
/// no positive labels.
pub fn pr_auc(scores: &[f64], labels: &[bool]) -> Result<Option<f64>> {
    if scores.len() != labels.len() {
        return Err(Error::InvalidParameter {
            name: "labels",
            reason: format!("{} scores but {} labels", scores.len(), labels.len()),
        });
    }
    let n_pos = labels.iter().filter(|&&l| l).count();
    if n_pos == 0 {
        return Ok(None);
    }
    let mut idx: Vec<usize> = (0..scores.len()).collect();
    idx.sort_by(|&a, &b| {
        scores[b]
            .partial_cmp(&scores[a])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let (mut tp, mut fp) = (0usize, 0usize);
    let mut ap = 0.0;
    let mut i = 0;
    while i < idx.len() {
        // Tie block [i, j): scored as one thresholding decision.
        let mut j = i + 1;
        while j < idx.len()
            && scores[idx[j]].partial_cmp(&scores[idx[i]]) == Some(std::cmp::Ordering::Equal)
        {
            j += 1;
        }
        let tp_before = tp;
        for &k in &idx[i..j] {
            if labels[k] {
                tp += 1;
            } else {
                fp += 1;
            }
        }
        if tp > tp_before {
            let precision = tp as f64 / (tp + fp) as f64;
            let d_recall = (tp - tp_before) as f64 / n_pos as f64;
            ap += precision * d_recall;
        }
        i = j;
    }
    Ok(Some(ap))
}

/// Best warning lead time per event, in days (or steps — whatever [`DayKey`]
/// indexes).
///
/// For each deduplicated in-period event, scans its space–time window (cells
/// within `cell_radius` Chebyshev, days within ±`tol_days`) for alerts and
/// reports `event_day − earliest_alert_day`: positive = warned in advance,
/// `0` = alerted the same day, negative = only a late alert inside the
/// tolerance. `None` = no alert in the window (a miss, in the exact sense of
/// [`spatial_daily_contingency`]). Returned in ascending `(cell, day)` order,
/// one entry per deduplicated event, so hit/miss counts here agree with the
/// contingency table.
pub fn lead_times(
    dims: GridDims,
    days: &[DayKey],
    alerted: &BTreeSet<(usize, DayKey)>,
    events: &[(usize, DayKey)],
    cell_radius: usize,
    tol_days: u32,
) -> Vec<((usize, DayKey), Option<i64>)> {
    let day_set: BTreeSet<DayKey> = days.iter().copied().collect();
    let tol = tol_days as i64;
    let n_cells = dims.len();
    let observed: BTreeSet<(usize, DayKey)> = events
        .iter()
        .copied()
        .filter(|(c, d)| *c < n_cells && day_set.contains(d))
        .collect();

    observed
        .iter()
        .map(|&(ec, ed)| {
            let earliest = chebyshev_window(dims, ec, cell_radius)
                .iter()
                .flat_map(|&cell| {
                    (-tol..=tol)
                        .map(move |d| (cell, ed + d))
                        .filter(|k| alerted.contains(k))
                        .map(|(_, ad)| ad)
                })
                .min();
            ((ec, ed), earliest.map(|ad| ed - ad))
        })
        .collect()
}

/// Probability of detection when only the top `area_fraction` of the ranked
/// hazard field can be warned: "if I can alert this share of the area, what
/// fraction of events do I catch?"
///
/// Ranks all units by `scores` descending, treats the top `area_fraction` as
/// alerted, and returns the fraction of positive-labelled units inside that set.
/// This is the operationally honest counterpart to [`roc_auc`] for a sparse
/// inventory, fixing the warned area instead of a hazard threshold. Returns
/// `None` if there are no positives or `area_fraction` is not in `(0, 1]`.
pub fn pod_at_area(scores: &[f64], labels: &[bool], area_fraction: f64) -> Result<Option<f64>> {
    if scores.len() != labels.len() {
        return Err(Error::InvalidParameter {
            name: "labels",
            reason: format!("{} scores but {} labels", scores.len(), labels.len()),
        });
    }
    if !(area_fraction > 0.0 && area_fraction <= 1.0) {
        return Ok(None);
    }
    let n_pos = labels.iter().filter(|&&l| l).count();
    if n_pos == 0 || scores.is_empty() {
        return Ok(None);
    }
    let k = (((scores.len() as f64) * area_fraction).ceil() as usize).clamp(1, scores.len());
    let mut idx: Vec<usize> = (0..scores.len()).collect();
    idx.sort_by(|&a, &b| {
        scores[b]
            .partial_cmp(&scores[a])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let caught = idx[..k].iter().filter(|&&i| labels[i]).count();
    Ok(Some(caught as f64 / n_pos as f64))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn skill_scores() {
        let c = Contingency {
            hits: 8,
            misses: 2,
            false_alarms: 4,
            correct_negatives: 86,
        };
        assert_eq!(c.n(), 100);
        assert!((c.pod().unwrap() - 0.8).abs() < 1e-9);
        assert!((c.far().unwrap() - 4.0 / 12.0).abs() < 1e-9);
        assert!((c.csi().unwrap() - 8.0 / 14.0).abs() < 1e-9);
        assert!((c.frequency_bias().unwrap() - 12.0 / 10.0).abs() < 1e-9);
    }

    #[test]
    fn empty_categories_are_none() {
        let c = Contingency::default();
        assert!(c.pod().is_none());
        assert!(c.far().is_none());
        assert!(c.csi().is_none());
    }

    #[test]
    fn csv_parsers_share_the_row_semantics() {
        let text = "date,mm\n2000-01-05,1.5\n2000-02-07,NaN\n2000-03-09,2.5\n";
        // The depth column drops the NaN row...
        assert_eq!(crate::csv_column(text, 1), vec![1.5, 2.5]);
        // ...but the date column still sees it: the caller must check alignment
        // (the CLI backtest does), which is exactly why both parsers live here.
        assert_eq!(
            csv_month_keys(text),
            vec![(2000, 1), (2000, 2), (2000, 3)]
        );
        // A malformed month is rejected, not wrapped.
        assert!(csv_month_keys("2000-13-01,1.0\n").is_empty());
    }

    #[test]
    fn csv_events_parses_the_inventory_layout() {
        let text = "id,year,month\n42,1993,5\nbad,row\n43,2015,3\n44,2015,0\n";
        assert_eq!(csv_events(text), vec![(1993, 5), (2015, 3)]);
    }

    #[test]
    fn month_shift_rolls_over() {
        assert_eq!(shift_month((2000, 1), -1), (1999, 12));
        assert_eq!(shift_month((2000, 12), 1), (2001, 1));
        assert_eq!(shift_month((2000, 6), 0), (2000, 6));
        assert_eq!(shift_month((2000, 1), -13), (1998, 12));
    }

    #[test]
    fn perfect_forecast() {
        // Three months; an alert exactly on the one event month.
        let days = vec![(2000, 1), (2000, 1), (2000, 2), (2000, 3)];
        let alerts = vec![false, false, true, false];
        let events = vec![(2000, 2)];
        let c = monthly_contingency(&days, &alerts, &events, 0).unwrap();
        assert_eq!(
            c,
            Contingency {
                hits: 1,
                misses: 0,
                false_alarms: 0,
                correct_negatives: 2
            }
        );
        assert_eq!(c.pod(), Some(1.0));
        assert_eq!(c.far(), Some(0.0));
    }

    #[test]
    fn spatial_match_needs_a_nearby_alert() {
        // 5x5 grid. Event at center cell 12 (row 2, col 2) in month (2000,2).
        let dims = GridDims::new(5, 5);
        let months = vec![(2000, 1), (2000, 2), (2000, 3)];
        let events = vec![(12usize, (2000, 2))];

        // Alert in an adjacent cell 11 (row 2, col 1), same month → hit (r=1).
        let near: BTreeSet<(usize, MonthKey)> = [(11usize, (2000, 2))].into_iter().collect();
        let c = spatial_monthly_contingency(dims, &months, &near, &events, 1, 0);
        assert_eq!(c.hits, 1);
        assert_eq!(c.misses, 0);
        assert_eq!(c.false_alarms, 0);

        // Alert only in the far corner cell 0 (Chebyshev distance 2 > 1) → miss,
        // and that alert is a false alarm (outside the event footprint).
        let far: BTreeSet<(usize, MonthKey)> = [(0usize, (2000, 2))].into_iter().collect();
        let c = spatial_monthly_contingency(dims, &months, &far, &events, 1, 0);
        assert_eq!(c.hits, 0);
        assert_eq!(c.misses, 1);
        assert_eq!(c.false_alarms, 1);
    }

    #[test]
    fn tolerance_absorbs_a_one_month_offset() {
        // Event filed in Feb, alert actually fired in Mar (inventory off by a
        // month). tol=0 → miss + false alarm; tol=1 → hit.
        let days = vec![(2000, 1), (2000, 2), (2000, 3), (2000, 4)];
        let alerts = vec![false, false, true, false];
        let events = vec![(2000, 2)];

        let strict = monthly_contingency(&days, &alerts, &events, 0).unwrap();
        assert_eq!(strict.hits, 0);
        assert_eq!(strict.misses, 1);
        assert_eq!(strict.false_alarms, 1);

        let tol = monthly_contingency(&days, &alerts, &events, 1).unwrap();
        assert_eq!(tol.hits, 1);
        assert_eq!(tol.misses, 0);
        assert_eq!(tol.false_alarms, 0);
    }

    #[test]
    fn daily_spatial_match_needs_a_nearby_alert() {
        // 5x5 grid. Event at centre cell 12 (row 2, col 2) on day 1.
        let dims = GridDims::new(5, 5);
        let days = vec![0i64, 1, 2];
        let events = vec![(12usize, 1i64)];

        // Adjacent cell 11 alerts the same day → hit (r=1).
        let near: BTreeSet<(usize, DayKey)> = [(11usize, 1i64)].into_iter().collect();
        let c = spatial_daily_contingency(dims, &days, &near, &events, 1, 0);
        assert_eq!(c.hits, 1);
        assert_eq!(c.misses, 0);
        assert_eq!(c.false_alarms, 0);

        // Far corner cell 0 (Chebyshev 2 > 1) → miss + a false alarm.
        let far: BTreeSet<(usize, DayKey)> = [(0usize, 1i64)].into_iter().collect();
        let c = spatial_daily_contingency(dims, &days, &far, &events, 1, 0);
        assert_eq!(c.hits, 0);
        assert_eq!(c.misses, 1);
        assert_eq!(c.false_alarms, 1);
    }

    #[test]
    fn daily_tolerance_absorbs_a_one_day_offset() {
        // Event on day 1; alert same cell on day 2 (off by one).
        let dims = GridDims::new(3, 3);
        let days = vec![0i64, 1, 2, 3];
        let events = vec![(4usize, 1i64)];
        let alerted: BTreeSet<(usize, DayKey)> = [(4usize, 2i64)].into_iter().collect();

        let strict = spatial_daily_contingency(dims, &days, &alerted, &events, 0, 0);
        assert_eq!(strict.hits, 0);
        assert_eq!(strict.misses, 1);
        assert_eq!(strict.false_alarms, 1);

        let tol = spatial_daily_contingency(dims, &days, &alerted, &events, 0, 1);
        assert_eq!(tol.hits, 1);
        assert_eq!(tol.misses, 0);
        assert_eq!(tol.false_alarms, 0);
    }

    /// Chebyshev distance between two row-major cells (reference-only).
    fn chebyshev(dims: GridDims, a: usize, b: usize) -> usize {
        let (ra, ca) = (a / dims.ncols, a % dims.ncols);
        let (rb, cb) = (b / dims.ncols, b % dims.ncols);
        ra.abs_diff(rb).max(ca.abs_diff(cb))
    }

    /// The original O(events·alerted) + O(days·cells) daily counting, kept as
    /// the semantic reference the optimised implementation must reproduce.
    fn naive_daily(
        dims: GridDims,
        days: &[DayKey],
        alerted: &BTreeSet<(usize, DayKey)>,
        events: &[(usize, DayKey)],
        cell_radius: usize,
        tol_days: u32,
    ) -> Contingency {
        let day_set: BTreeSet<DayKey> = days.iter().copied().collect();
        let tol = tol_days as i64;
        let n_cells = dims.len();
        let observed: BTreeSet<(usize, DayKey)> = events
            .iter()
            .copied()
            .filter(|(c, d)| *c < n_cells && day_set.contains(d))
            .collect();
        let mut footprint: BTreeSet<(usize, DayKey)> = BTreeSet::new();
        for &(ec, ed) in &observed {
            for cell in 0..n_cells {
                if chebyshev(dims, ec, cell) <= cell_radius {
                    for d in -tol..=tol {
                        footprint.insert((cell, ed + d));
                    }
                }
            }
        }
        let mut c = Contingency::default();
        for &(ec, ed) in &observed {
            let hit = alerted
                .iter()
                .any(|&(ac, ad)| chebyshev(dims, ec, ac) <= cell_radius && (ad - ed).abs() <= tol);
            if hit { c.hits += 1 } else { c.misses += 1 }
        }
        for &(ac, ad) in alerted {
            if day_set.contains(&ad) && !footprint.contains(&(ac, ad)) {
                c.false_alarms += 1;
            }
        }
        for &d in &day_set {
            for cell in 0..n_cells {
                if !alerted.contains(&(cell, d)) && !footprint.contains(&(cell, d)) {
                    c.correct_negatives += 1;
                }
            }
        }
        c
    }

    /// The optimised spatial matchers must reproduce the naive reference
    /// counting exactly, across radii and tolerances, on random data.
    #[test]
    fn daily_contingency_matches_naive_reference_on_random_data() {
        let dims = GridDims::new(12, 9);
        let n_cells = dims.len();
        let days: Vec<DayKey> = (0..60).collect();
        let mut x = 3u64;
        let mut next = |m: u64| {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1);
            ((x >> 33) % m) as usize
        };
        // ~300 random alerts (some duplicated → set) and 15 random events,
        // a few deliberately outside the grid/period to exercise the filters.
        let mut alerted: BTreeSet<(usize, DayKey)> = BTreeSet::new();
        for _ in 0..300 {
            alerted.insert((next(n_cells as u64), next(60) as DayKey));
        }
        alerted.insert((n_cells + 5, 10)); // out-of-grid alert
        let mut events: Vec<(usize, DayKey)> = (0..15)
            .map(|_| (next(n_cells as u64), next(60) as DayKey))
            .collect();
        events.push((3, 999)); // out-of-period event
        events.push((n_cells + 1, 5)); // out-of-grid event

        for (radius, tol) in [(0usize, 0u32), (1, 1), (2, 3), (4, 0)] {
            let fast = spatial_daily_contingency(dims, &days, &alerted, &events, radius, tol);
            let slow = naive_daily(dims, &days, &alerted, &events, radius, tol);
            assert_eq!(fast, slow, "diverged at radius {radius}, tol {tol}");
        }
    }

    #[test]
    fn monthly_contingency_matches_daily_shape_on_shared_data() {
        // The monthly matcher shares the counting scheme; check it against the
        // daily one on data where months behave like a linear axis (one year,
        // no rollover), so both must agree unit-for-unit.
        let dims = GridDims::new(6, 6);
        let months: Vec<MonthKey> = (1..=12).map(|m| (2000, m)).collect();
        let days: Vec<DayKey> = (1..=12).collect();
        let mut x = 11u64;
        let mut next = |m: u64| {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1);
            ((x >> 33) % m) as usize
        };
        let mut am: BTreeSet<(usize, MonthKey)> = BTreeSet::new();
        let mut ad: BTreeSet<(usize, DayKey)> = BTreeSet::new();
        for _ in 0..80 {
            let (c, m) = (next(36), next(12) as u32 + 1);
            am.insert((c, (2000, m)));
            ad.insert((c, m as DayKey));
        }
        let mut em: Vec<(usize, MonthKey)> = Vec::new();
        let mut ed: Vec<(usize, DayKey)> = Vec::new();
        for _ in 0..8 {
            let (c, m) = (next(36), next(12) as u32 + 1);
            em.push((c, (2000, m)));
            ed.push((c, m as DayKey));
        }
        for (radius, tol) in [(0usize, 0u32), (1, 1), (2, 2)] {
            let monthly = spatial_monthly_contingency(dims, &months, &am, &em, radius, tol);
            let daily = spatial_daily_contingency(dims, &days, &ad, &ed, radius, tol);
            // Not strictly identical near the axis edges: the month axis
            // rolls over into 1999-12 / 2001-01 (still out-of-period, thus
            // equivalent), so the full tables must match.
            assert_eq!(monthly, daily, "diverged at radius {radius}, tol {tol}");
        }
    }

    #[test]
    fn roc_auc_ranks_positives_above_negatives() {
        let scores = vec![0.1, 0.2, 0.8, 0.9];
        // Perfect separation → 1.0.
        let labels = vec![false, false, true, true];
        assert_eq!(roc_auc(&scores, &labels).unwrap(), Some(1.0));
        // Reversed labels → 0.0.
        let labels_rev = vec![true, true, false, false];
        assert_eq!(roc_auc(&scores, &labels_rev).unwrap(), Some(0.0));
        // A single class → undefined.
        assert!(roc_auc(&scores, &[true, true, true, true]).unwrap().is_none());
        // All scores tied → no discrimination → 0.5 via average ranks.
        let flat = vec![0.5, 0.5, 0.5, 0.5];
        assert_eq!(roc_auc(&flat, &labels).unwrap(), Some(0.5));
        // Length mismatch is an error, not a panic.
        assert!(roc_auc(&scores, &[true, false]).is_err());
    }

    #[test]
    fn pr_auc_behaves_at_the_extremes() {
        let scores = vec![0.1, 0.2, 0.8, 0.9];
        // Perfect separation → 1.0.
        assert_eq!(pr_auc(&scores, &[false, false, true, true]).unwrap(), Some(1.0));
        // No positives → undefined.
        assert!(pr_auc(&scores, &[false; 4]).unwrap().is_none());
        // All scores tied → precision equals the base rate everywhere.
        let flat = vec![0.5; 4];
        let ap = pr_auc(&flat, &[true, false, false, false]).unwrap().unwrap();
        assert!((ap - 0.25).abs() < 1e-12, "tied scores → base rate, got {ap}");
        // Reversed ranking is far below the base rate 0.5 baseline of perfect.
        let ap_rev = pr_auc(&scores, &[true, true, false, false]).unwrap().unwrap();
        assert!(ap_rev < 0.6, "reversed ranking should score poorly, got {ap_rev}");
        // Unlike ROC-AUC, PR-AUC penalises rarity: same ranking quality, rarer
        // positives → lower AP (this is the property that matters at 4% base rate).
        let many_neg: Vec<f64> = (0..100).map(|i| i as f64).collect();
        let mut labels = vec![false; 100];
        labels[98] = true; // second-best score is the only positive
        let ap_rare = pr_auc(&many_neg, &labels).unwrap().unwrap();
        assert!((ap_rare - 0.5).abs() < 1e-12);
        assert!(roc_auc(&many_neg, &labels).unwrap().unwrap() > 0.98, "ROC barely notices");
        // Length mismatch is an error, not a panic.
        assert!(pr_auc(&scores, &[true, false]).is_err());
    }

    #[test]
    fn lead_times_report_the_earliest_alert_in_window() {
        let dims = GridDims::new(5, 5);
        let days: Vec<DayKey> = (0..10).collect();
        // Event at cell 12, day 6.
        let events = vec![(12usize, 6i64)];
        // Alerts: same cell day 4 (lead 2) and adjacent cell 11 day 3 (lead 3).
        let alerted: BTreeSet<(usize, DayKey)> =
            [(12usize, 4i64), (11usize, 3i64)].into_iter().collect();

        // Radius 1 sees both alerts → best (earliest) gives lead 3.
        let lt = lead_times(dims, &days, &alerted, &events, 1, 3);
        assert_eq!(lt, vec![((12, 6), Some(3))]);
        // Radius 0 sees only the same-cell alert → lead 2.
        let lt = lead_times(dims, &days, &alerted, &events, 0, 3);
        assert_eq!(lt, vec![((12, 6), Some(2))]);
        // Tolerance too small → miss.
        let lt = lead_times(dims, &days, &alerted, &events, 0, 1);
        assert_eq!(lt, vec![((12, 6), None)]);
        // A late-only alert reports a negative lead (alerted, but after the fact).
        let late: BTreeSet<(usize, DayKey)> = [(12usize, 7i64)].into_iter().collect();
        let lt = lead_times(dims, &days, &late, &events, 0, 2);
        assert_eq!(lt, vec![((12, 6), Some(-1))]);
    }

    #[test]
    fn pod_at_area_catches_top_ranked_events() {
        let scores = vec![0.9, 0.1, 0.2, 0.8, 0.3, 0.4, 0.5, 0.6, 0.7, 0.05];
        let mut labels = vec![false; 10];
        labels[0] = true; // score 0.9, rank 1
        labels[3] = true; // score 0.8, rank 2
        // Top 20 % = top 2 units → both events caught.
        assert_eq!(pod_at_area(&scores, &labels, 0.2).unwrap(), Some(1.0));
        // Top 10 % = top 1 unit → only the 0.9 event caught.
        assert_eq!(pod_at_area(&scores, &labels, 0.1).unwrap(), Some(0.5));
        // Degenerate fractions → None.
        assert!(pod_at_area(&scores, &labels, 0.0).unwrap().is_none());
        assert!(pod_at_area(&scores, &[false; 10], 0.5).unwrap().is_none());
        // Length mismatch is an error, not a panic.
        assert!(pod_at_area(&scores, &[true, false], 0.5).is_err());
    }
}
