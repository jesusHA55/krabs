//! Krabs native hot path.
//!
//! Everything here is a pure function over `f64` slices: no state, no I/O, no
//! knowledge of strategies or brokers. That keeps the FFI surface small and
//! makes the functions trivially testable with `cargo test --no-default-features`.
//!
//! Numerical contracts match `src/krabs/indicators/__init__.py` exactly
//! (including NaN warm-up), so swapping backends cannot change a backtest.

#[cfg(feature = "python")]
use pyo3::exceptions::PyValueError;
#[cfg(feature = "python")]
use pyo3::prelude::*;

/// Simple moving average. NaN until `period` values are available.
pub fn sma_impl(values: &[f64], period: usize) -> Vec<f64> {
    let mut out = vec![f64::NAN; values.len()];
    if period == 0 || values.len() < period {
        return out;
    }
    let mut sum: f64 = values[..period].iter().sum();
    out[period - 1] = sum / period as f64;
    for i in period..values.len() {
        sum += values[i] - values[i - period];
        out[i] = sum / period as f64;
    }
    out
}

/// Exponential moving average seeded with the SMA of the first `period` points.
pub fn ema_impl(values: &[f64], period: usize) -> Vec<f64> {
    let mut out = vec![f64::NAN; values.len()];
    if period == 0 || values.len() < period {
        return out;
    }
    let alpha = 2.0 / (period as f64 + 1.0);
    let seed = values[..period].iter().sum::<f64>() / period as f64;
    out[period - 1] = seed;
    let mut prev = seed;
    for i in period..values.len() {
        prev = values[i] * alpha + prev * (1.0 - alpha);
        out[i] = prev;
    }
    out
}

fn rsi_value(avg_gain: f64, avg_loss: f64) -> f64 {
    if avg_loss == 0.0 {
        return if avg_gain > 0.0 { 100.0 } else { 50.0 };
    }
    100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
}

/// Wilder RSI. NaN until index `period`.
pub fn rsi_impl(values: &[f64], period: usize) -> Vec<f64> {
    let mut out = vec![f64::NAN; values.len()];
    if period == 0 || values.len() <= period {
        return out;
    }
    let mut gains = 0.0;
    let mut losses = 0.0;
    for i in 1..=period {
        let delta = values[i] - values[i - 1];
        if delta >= 0.0 {
            gains += delta;
        } else {
            losses -= delta;
        }
    }
    let mut avg_gain = gains / period as f64;
    let mut avg_loss = losses / period as f64;
    out[period] = rsi_value(avg_gain, avg_loss);
    for i in (period + 1)..values.len() {
        let delta = values[i] - values[i - 1];
        let gain = delta.max(0.0);
        let loss = (-delta).max(0.0);
        avg_gain = (avg_gain * (period as f64 - 1.0) + gain) / period as f64;
        avg_loss = (avg_loss * (period as f64 - 1.0) + loss) / period as f64;
        out[i] = rsi_value(avg_gain, avg_loss);
    }
    out
}

/// Wilder ATR. Highs/lows/closes must have identical length.
pub fn atr_impl(highs: &[f64], lows: &[f64], closes: &[f64], period: usize) -> Vec<f64> {
    let n = closes.len();
    let mut out = vec![f64::NAN; n];
    if period == 0 || n <= period || highs.len() != n || lows.len() != n {
        return out;
    }
    let mut trs = Vec::with_capacity(n);
    trs.push(highs[0] - lows[0]);
    for i in 1..n {
        let tr = (highs[i] - lows[i])
            .max((highs[i] - closes[i - 1]).abs())
            .max((lows[i] - closes[i - 1]).abs());
        trs.push(tr);
    }
    let mut prev: f64 = trs[1..=period].iter().sum::<f64>() / period as f64;
    out[period] = prev;
    for i in (period + 1)..n {
        prev = (prev * (period as f64 - 1.0) + trs[i]) / period as f64;
        out[i] = prev;
    }
    out
}

/// Rolling population standard deviation (matches the Python fallback).
pub fn rolling_std_impl(values: &[f64], period: usize) -> Vec<f64> {
    let mut out = vec![f64::NAN; values.len()];
    if period == 0 || values.len() < period {
        return out;
    }
    for i in (period - 1)..values.len() {
        let window = &values[i + 1 - period..=i];
        let mean = window.iter().sum::<f64>() / period as f64;
        let var = window.iter().map(|x| (x - mean) * (x - mean)).sum::<f64>() / period as f64;
        out[i] = var.sqrt();
    }
    out
}

/// Worst peak-to-trough drawdown as a negative fraction (0.0 if never underwater).
pub fn max_drawdown_impl(equity: &[f64]) -> f64 {
    let mut peak = f64::NEG_INFINITY;
    let mut worst = 0.0_f64;
    for &value in equity {
        if value > peak {
            peak = value;
        }
        if peak > 0.0 {
            let dd = value / peak - 1.0;
            if dd < worst {
                worst = dd;
            }
        }
    }
    worst
}

/// Sample standard deviation of returns, annualised.
pub fn realized_volatility_impl(returns: &[f64], periods_per_year: usize) -> f64 {
    let n = returns.len();
    if n < 2 {
        return 0.0;
    }
    let mean = returns.iter().sum::<f64>() / n as f64;
    let var = returns.iter().map(|r| (r - mean) * (r - mean)).sum::<f64>() / (n - 1) as f64;
    var.sqrt() * (periods_per_year as f64).sqrt()
}

// ---------------------------------------------------------------------------
// Python bindings
// ---------------------------------------------------------------------------

#[cfg(feature = "python")]
fn check_period(period: usize) -> PyResult<()> {
    if period == 0 {
        return Err(PyValueError::new_err("period must be greater than zero"));
    }
    Ok(())
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (values, period))]
pub fn sma(values: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    check_period(period)?;
    Ok(sma_impl(&values, period))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (values, period))]
pub fn ema(values: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    check_period(period)?;
    Ok(ema_impl(&values, period))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (values, period))]
pub fn rsi(values: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    check_period(period)?;
    Ok(rsi_impl(&values, period))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (highs, lows, closes, period))]
pub fn atr(
    highs: Vec<f64>,
    lows: Vec<f64>,
    closes: Vec<f64>,
    period: usize,
) -> PyResult<Vec<f64>> {
    check_period(period)?;
    if highs.len() != closes.len() || lows.len() != closes.len() {
        return Err(PyValueError::new_err(
            "highs, lows and closes must have the same length",
        ));
    }
    Ok(atr_impl(&highs, &lows, &closes, period))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (values, period))]
pub fn rolling_std(values: Vec<f64>, period: usize) -> PyResult<Vec<f64>> {
    check_period(period)?;
    Ok(rolling_std_impl(&values, period))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (equity))]
pub fn max_drawdown(equity: Vec<f64>) -> f64 {
    max_drawdown_impl(&equity)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(signature = (returns, periods_per_year = 252))]
pub fn realized_volatility(returns: Vec<f64>, periods_per_year: usize) -> f64 {
    realized_volatility_impl(&returns, periods_per_year)
}

#[cfg(feature = "python")]
#[pymodule]
fn krabs_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(sma, m)?)?;
    m.add_function(wrap_pyfunction!(ema, m)?)?;
    m.add_function(wrap_pyfunction!(rsi, m)?)?;
    m.add_function(wrap_pyfunction!(atr, m)?)?;
    m.add_function(wrap_pyfunction!(rolling_std, m)?)?;
    m.add_function(wrap_pyfunction!(max_drawdown, m)?)?;
    m.add_function(wrap_pyfunction!(realized_volatility, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn approx(a: &[f64], b: &[f64]) {
        assert_eq!(a.len(), b.len());
        for (x, y) in a.iter().zip(b) {
            if y.is_nan() {
                assert!(x.is_nan(), "expected NaN, got {x}");
            } else {
                assert!((x - y).abs() < 1e-9, "expected {y}, got {x}");
            }
        }
    }

    #[test]
    fn sma_matches_reference() {
        approx(&sma_impl(&[1.0, 2.0, 3.0, 4.0, 5.0], 3), &[f64::NAN, f64::NAN, 2.0, 3.0, 4.0]);
    }

    #[test]
    fn ema_seeds_with_sma() {
        approx(&ema_impl(&[1.0, 2.0, 3.0, 4.0, 5.0], 3), &[f64::NAN, f64::NAN, 2.0, 3.0, 4.0]);
    }

    #[test]
    fn rsi_extremes() {
        let up: Vec<f64> = (1..30).map(|x| x as f64).collect();
        assert!((rsi_impl(&up, 14).last().unwrap() - 100.0).abs() < 1e-9);
        let down: Vec<f64> = (1..30).rev().map(|x| x as f64).collect();
        assert!((rsi_impl(&down, 14).last().unwrap() - 0.0).abs() < 1e-9);
    }

    #[test]
    fn atr_constant_range() {
        let highs = vec![11.0; 20];
        let lows = vec![9.0; 20];
        let closes = vec![10.0; 20];
        assert!((atr_impl(&highs, &lows, &closes, 14).last().unwrap() - 2.0).abs() < 1e-9);
    }

    #[test]
    fn drawdown_reference() {
        assert!((max_drawdown_impl(&[100.0, 120.0, 90.0, 110.0]) + 0.25).abs() < 1e-9);
    }

    #[test]
    fn handles_short_series() {
        assert!(sma_impl(&[1.0], 5)[0].is_nan());
        assert_eq!(max_drawdown_impl(&[]), 0.0);
        assert_eq!(realized_volatility_impl(&[0.1], 252), 0.0);
    }
}
