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

/// A 2×2 contingency table for binary event forecasting.
///
/// ```text
///                     event observed
///                      yes        no
///   alert  yes   |   hits   | false_alarms |
///          no    |  misses  | corr_neg     |
/// ```
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Contingency {
    pub hits: u32,
    pub misses: u32,
    pub false_alarms: u32,
    pub correct_negatives: u32,
}

impl Contingency {
    /// Total number of units (months) scored.
    pub fn n(&self) -> u32 {
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
) -> Contingency {
    assert_eq!(
        day_month.len(),
        alert_days.len(),
        "day_month and alert_days must be the same length"
    );
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
    c
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
        let c = monthly_contingency(&days, &alerts, &events, 0);
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
    fn tolerance_absorbs_a_one_month_offset() {
        // Event filed in Feb, alert actually fired in Mar (inventory off by a
        // month). tol=0 → miss + false alarm; tol=1 → hit.
        let days = vec![(2000, 1), (2000, 2), (2000, 3), (2000, 4)];
        let alerts = vec![false, false, true, false];
        let events = vec![(2000, 2)];

        let strict = monthly_contingency(&days, &alerts, &events, 0);
        assert_eq!(strict.hits, 0);
        assert_eq!(strict.misses, 1);
        assert_eq!(strict.false_alarms, 1);

        let tol = monthly_contingency(&days, &alerts, &events, 1);
        assert_eq!(tol.hits, 1);
        assert_eq!(tol.misses, 0);
        assert_eq!(tol.false_alarms, 0);
    }
}
