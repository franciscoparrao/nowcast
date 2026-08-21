//! Published intensity--duration power laws, shared by the examples that need
//! them (`published_thresholds`, `intercept_sensitivity`).
//!
//! Kept in ONE place on purpose: two examples quoting the same literature table
//! from two private copies is exactly the sibling-drift pattern that
//! `docs/checklist-hermanas.md` documents — a correction applied to the named
//! copy and missed on the adjacent one.
//!
//! This directory has no `main.rs`, so Cargo does not build it as an example;
//! consumers pull it in with `#[path = "shared/curves.rs"] mod curves;`.

// Not every consumer needs both constants (most want only REGIONAL).
#![allow(dead_code)]

/// Published I–D power laws `I = a·D^-b` (a in mm/h, b dimensionless), verbatim
/// from Guzzetti, Peruccacci, Rossi & Stark (2007), "Rainfall thresholds for the
/// initiation of landslides in central and southern Europe", Meteorology and
/// Atmospheric Physics 98, 239–267, Table 2 (the 35 pure power laws; the c≠0
/// asymptotic and normalized forms are excluded). Entry 0 is Caine (1980).
pub const PUBLISHED: &[(f64, f64)] = &[
    (14.82, 0.39), // Caine (1980), global
    (41.66, 0.77),
    (44.668, 0.78),
    (66.18, 0.52),
    (41.83, 0.58),
    (39.71, 0.62),
    (35.23, 0.54),
    (26.51, 0.19),
    (30.53, 0.57),
    (176.40, 0.90),
    (27.3, 0.38),
    (20.1, 0.55),
    (91.46, 0.82),
    (9.23, 0.37),
    (10.0, 0.77),
    (5.94, 1.50),
    (32.0, 0.70),
    (47.742, 0.507),
    (9.521, 0.4955),
    (11.698, 0.4783),
    (11.00, 0.4459),
    (10.67, 0.5043),
    (12.649, 0.5324),
    (18.675, 0.565),
    (28.10, 0.74),
    (9.9, 0.52),
    (116.48, 0.63),
    (15.0, 0.70),
    (11.5, 0.26),
    (4.0, 0.45),
    (19.0, 0.50),
    (18.83, 0.59),
    (82.73, 1.13),
    (7.00, 0.60),
    (115.47, 0.80),
];

/// Self-calibrated regional intercept from the `backtest` example (Section 5.1
/// of the paper): the only I--D curve in this work fitted to data.
pub const REGIONAL: (f64, f64) = (5.5, 0.39);
