from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import MinMaxScaler


def make_supervised_1d(values: np.ndarray, window: int = 10) -> tuple[np.ndarray, np.ndarray]:
    arr = np.asarray(values, dtype=float)
    X, y = [], []
    for i in range(window, len(arr)):
        X.append(arr[i - window : i])
        y.append(arr[i])
    return np.array(X), np.array(y)


def fit_linear_autoreg(series: pd.Series, window: int = 15) -> tuple[LinearRegression, MinMaxScaler, np.ndarray]:
    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(series.values.reshape(-1, 1)).flatten()
    X, y = make_supervised_1d(scaled, window=window)
    if len(X) < 20:
        raise RuntimeError("Not enough samples to train autoregressive model.")
    model = LinearRegression()
    model.fit(X, y)
    return model, scaler, scaled


def iterative_forecast_1d(
    model,
    scaler: MinMaxScaler,
    scaled_series: np.ndarray,
    *,
    steps: int = 3,
    window: int = 15,
) -> np.ndarray:
    w = np.asarray(scaled_series[-window:], dtype=float).copy()
    preds_scaled = []
    for _ in range(steps):
        nxt = float(model.predict(w.reshape(1, -1))[0])
        preds_scaled.append(nxt)
        w = np.r_[w[1:], nxt]
    return scaler.inverse_transform(np.array(preds_scaled).reshape(-1, 1)).flatten()


def build_sequence_xy(data_2d: np.ndarray, sequence_length: int, forecast_step: int = 1) -> tuple[np.ndarray, np.ndarray]:
    arr = np.asarray(data_2d, dtype=float)
    X, y = [], []
    max_i = len(arr) - sequence_length - forecast_step + 1
    for i in range(max_i):
        X.append(arr[i : i + sequence_length])
        y.append(arr[i + sequence_length + forecast_step - 1])
    return np.array(X), np.array(y)


def time_split(X: np.ndarray, y: np.ndarray, train_ratio: float = 0.8) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    split = int(len(X) * train_ratio)
    return X[:split], X[split:], y[:split], y[split:]


def flatten_sequence_X(X: np.ndarray) -> np.ndarray:
    return X.reshape(X.shape[0], -1)


def nearest_before_index(index: pd.Index, ts: pd.Timestamp) -> pd.Timestamp | None:
    c = index[index <= ts]
    return c.max() if len(c) else None


def select_quarter_snapshots(df: pd.DataFrame, n_quarters: int = 5) -> pd.DataFrame:
    end_ts = df.index[-1]
    qe = end_ts + pd.offsets.QuarterEnd(-1)
    target_dates = [(qe - pd.offsets.QuarterEnd(i)) for i in range(n_quarters - 1, -1, -1)]
    available = pd.Index(df.index)
    picked = [nearest_before_index(available, d) for d in target_dates]
    picked = [d for d in picked if d is not None]
    return df.loc[picked + [end_ts]].drop_duplicates()


def nelson_siegel_curve(t: np.ndarray, beta0: float, beta1: float, beta2: float, tau: float) -> np.ndarray:
    tau = max(float(tau), 1e-6)
    t = np.asarray(t, dtype=float)
    x = t / tau
    load1 = (1 - np.exp(-x)) / x
    load2 = load1 - np.exp(-x)
    return beta0 + beta1 * load1 + beta2 * load2


def svensson_curve(
    t: np.ndarray,
    beta0: float,
    beta1: float,
    beta2: float,
    beta3: float,
    tau1: float,
    tau2: float,
) -> np.ndarray:
    tau1 = max(float(tau1), 1e-6)
    tau2 = max(float(tau2), 1e-6)
    t = np.asarray(t, dtype=float)
    x1 = t / tau1
    x2 = t / tau2
    load1 = (1 - np.exp(-x1)) / x1
    load2 = load1 - np.exp(-x1)
    load3 = ((1 - np.exp(-x2)) / x2) - np.exp(-x2)
    return beta0 + beta1 * load1 + beta2 * load2 + beta3 * load3


def ns_curve(tau: np.ndarray, beta0: float, beta1: float, beta2: float, lamb: float) -> np.ndarray:
    x = np.asarray(tau, dtype=float) * max(float(lamb), 1e-6)
    with np.errstate(divide="ignore", invalid="ignore"):
        f1 = (1 - np.exp(-x)) / np.where(x == 0, 1e-6, x)
        f2 = f1 - np.exp(-x)
    return beta0 + beta1 * f1 + beta2 * f2


def build_sector_symbol_map(components: pd.DataFrame, monthly_returns_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    available_symbols = set(monthly_returns_df.columns)
    sector_data = components[components["Symbol"].isin(available_symbols)][["Symbol", "Sector"]].copy()
    if sector_data.empty:
        raise RuntimeError("No overlap between component list and return series columns.")
    sector_to_symbols = sector_data.groupby("Sector")["Symbol"].apply(list)
    return sector_data, sector_to_symbols


def equal_weight_sector_returns(monthly_returns_df: pd.DataFrame, sector_to_symbols: pd.Series) -> pd.DataFrame:
    sector_monthly_avg = {}
    for sector, symbols_in_sector in sector_to_symbols.items():
        valid = [s for s in symbols_in_sector if s in monthly_returns_df.columns]
        if not valid:
            continue
        sector_monthly_avg[sector] = monthly_returns_df[valid].mean(axis=1)
    out = pd.DataFrame(sector_monthly_avg).sort_index()
    out.index.name = "Date"
    return out


__all__ = [
    "make_supervised_1d",
    "fit_linear_autoreg",
    "iterative_forecast_1d",
    "build_sequence_xy",
    "time_split",
    "flatten_sequence_X",
    "nearest_before_index",
    "select_quarter_snapshots",
    "nelson_siegel_curve",
    "svensson_curve",
    "ns_curve",
    "build_sector_symbol_map",
    "equal_weight_sector_returns",
]
