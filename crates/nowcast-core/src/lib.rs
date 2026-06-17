//! # nowcast-core
//!
//! Dynamic geohazard **nowcasting**: modulate a static susceptibility map with a
//! time-varying trigger (rainfall, snowmelt) to produce a hazard probability
//! that changes step by step — not a fixed susceptibility surface.
//!
//! ```text
//!     hazard(cell, t) = susceptibility(cell) × trigger_factor(I–D exceedance, t)
//! ```
//!
//! ## v0.1 — decoupled
//!
//! The core depends only on `std` + `thiserror`, so it builds and tests offline
//! with no upstream Rust engines. The dynamic forcing is abstracted behind the
//! [`Forcing`] trait; the shipped implementation, [`UniformRain`], replays an
//! observed rain-gauge series (CR2/DGA) over a susceptibility raster.
//!
//! ## v0.2 — native providers (planned)
//!
//! Separate adapter crates implement [`Forcing`] on top of the sibling engines:
//! `rainflow` (routed discharge → flood nowcasting) and `snowmelt-rs` (rain +
//! snowmelt runoff per cell → rain-on-snow landslide triggering).
//!
//! ## Pipeline
//!
//! [`SusceptibilityMap`] + a [`Forcing`] + an [`IdThreshold`] + a
//! [`TriggerModel`] are assembled into a [`Nowcast`], whose [`Nowcast::run`]
//! yields a [`HazardField`] per step and [`Nowcast::alerts`] flags steps that
//! cross an alert level.
//!
//! ```
//! use nowcast_core::{
//!     GridDims, SusceptibilityMap, UniformRain, IdThreshold, TriggerModel, Nowcast,
//! };
//!
//! let dims = GridDims::new(1, 1);
//! let forcing = UniformRain::new(dims, 1.0, vec![0.0, 40.0, 0.0]).unwrap();
//! let susceptibility = SusceptibilityMap::uniform(dims, 0.8).unwrap();
//! let nowcast = Nowcast::new(
//!     susceptibility,
//!     forcing,
//!     IdThreshold::caine(),
//!     TriggerModel::default(),
//!     24,
//! )
//! .unwrap();
//!
//! let alerts = nowcast.alerts(0.5);
//! assert_eq!(alerts[0].step, 1); // alerts begin at the 40 mm/h burst
//! ```

mod backtest;
mod error;
mod forcing;
mod grid;
mod nowcast;
mod threshold;
mod trigger;

pub use backtest::{Contingency, MonthKey, monthly_contingency, spatial_monthly_contingency};
pub use error::{Error, Result};
pub use forcing::{Forcing, GriddedRain, UniformRain};
pub use grid::{GridDims, SusceptibilityMap};
pub use nowcast::{Alert, HazardField, Nowcast};
pub use threshold::IdThreshold;
pub use trigger::TriggerModel;
