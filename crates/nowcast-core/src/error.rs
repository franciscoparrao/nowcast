use thiserror::Error;

/// Errors produced while building or running a nowcast.
#[derive(Debug, Error)]
pub enum Error {
    /// A model parameter is outside its admissible range.
    #[error("invalid parameter `{name}`: {reason}")]
    InvalidParameter {
        name: &'static str,
        reason: String,
    },

    /// A grid was constructed with the wrong number of values.
    #[error("grid size mismatch: expected {expected} values for {ncols}x{nrows} grid, got {got}")]
    GridSizeMismatch {
        expected: usize,
        got: usize,
        ncols: usize,
        nrows: usize,
    },

    /// A susceptibility value fell outside the unit interval [0, 1].
    #[error("susceptibility value at cell {cell} is {value}, expected within [0, 1]")]
    SusceptibilityOutOfRange { cell: usize, value: f64 },

    /// The forcing grid does not match the susceptibility grid.
    #[error(
        "grid mismatch: susceptibility is {susc_cols}x{susc_rows} but forcing is {forc_cols}x{forc_rows}"
    )]
    GridMismatch {
        susc_cols: usize,
        susc_rows: usize,
        forc_cols: usize,
        forc_rows: usize,
    },

    /// A cell or step index beyond the grid or series bounds.
    #[error("{name} index {index} is out of range ({len} {name}s available)")]
    OutOfRange {
        name: &'static str,
        index: usize,
        len: usize,
    },

    /// An external engine wrapped by an adapter crate failed (the simulation
    /// itself, not a nowcast parameter) — e.g. firespread, hydroflux.
    #[error("{engine} engine error: {reason}")]
    Engine {
        engine: &'static str,
        reason: String,
    },

    /// Failed to parse an observed forcing series from text/CSV.
    #[error("failed to parse forcing series: {0}")]
    Parse(String),
}

/// Convenience alias used across the crate.
pub type Result<T> = std::result::Result<T, Error>;
