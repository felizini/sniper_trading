#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SNIPER PHOENIX v8.3.0 - HÍBRIDO CORRIGIDO (REVERSÃO + TREND FOLLOWING)
- ATR robusto com seeding inicial e smoothing Wilder
- Persistência completa do ATR (salvo/restaurado)
- Stop loss de tendência com fallback (2% do preço se ATR inválido)
- Pullback com confirmação de vela de alta
- Momentum com verificação adicional de ADX (força da tendência)
- Breakout com confirmação de volume
- Interface corrigida (não exibe "todas condições" quando não há condições)
"""

import ccxt
import pandas as pd
import numpy as np
import time
import configparser
import os
import sys
import threading
from datetime import datetime
from colorama import Fore, init
import signal
import asyncio
import websockets
import json
import logging
import logging.handlers
import csv
from collections import deque
from typing import Optional, Dict, Any, List, Tuple

# =============================================================================
# JSON ENCODER
# =============================================================================
class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, deque):
            return list(obj)
        return super().default(obj)

init(autoreset=True)
inicio_bot = datetime.now()

# --- ARQUIVOS ---
ARQUIVO_ESTADO      = "sniper_state.json"
ARQUIVO_LOG_SISTEMA = "sniper_system.log"
ARQUIVO_LOG_TRADES  = "sniper_trades.csv"

# --- LOCKS ---
state_lock    = threading.Lock()
config_lock   = threading.Lock()
engine_lock   = threading.RLock()
csv_lock      = threading.Lock()
exchange_lock = threading.Lock()

# =============================================================================
# INDICADORES (CORRIGIDOS)
# =============================================================================

def compute_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-period-1:])
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    return 100 - 100 / (1 + rs)

def compute_ema(closes: List[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    s = pd.Series(closes)
    return float(s.ewm(span=period, adjust=False).mean().iloc[-1])

def compute_adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Tuple[float, float, float]:
    n = len(closes)
    if n < period * 2:
        return 0.0, 0.0, 0.0

    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)

    for i in range(1, n):
        high_diff = highs[i] - highs[i-1]
        low_diff  = lows[i-1] - lows[i]
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1]))
        plus_dm[i] = high_diff if (high_diff > low_diff and high_diff > 0) else 0
        minus_dm[i] = low_diff if (low_diff > high_diff and low_diff > 0) else 0

    atr = np.zeros(n)
    pdi = np.zeros(n)
    ndi = np.zeros(n)

    # Wilder smoothing: inicializar com soma dos primeiros `period` valores
    atr[period] = tr[1:period+1].sum()
    pdi[period] = plus_dm[1:period+1].sum()
    ndi[period] = minus_dm[1:period+1].sum()

    for i in range(period+1, n):
        atr[i] = atr[i-1] - atr[i-1]/period + tr[i]
        pdi[i] = pdi[i-1] - pdi[i-1]/period + plus_dm[i]
        ndi[i] = ndi[i-1] - ndi[i-1]/period + minus_dm[i]

    with np.errstate(invalid='ignore', divide='ignore'):
        pdi_pct = np.where(atr > 0, 100 * pdi / atr, 0)
        ndi_pct = np.where(atr > 0, 100 * ndi / atr, 0)
        dx = np.where((pdi_pct + ndi_pct) > 0,
                      100 * np.abs(pdi_pct - ndi_pct) / (pdi_pct + ndi_pct), 0)

    if n >= period * 2:
        adx = np.mean(dx[-period:]) if len(dx) >= period else dx[-1]
    else:
        adx = dx[-1]
    return float(adx), float(pdi_pct[-1]), float(ndi_pct[-1])

def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Calcula ATR usando Wilder smoothing (mesma lógica do ADX)."""
    n = len(highs)
    if n < period:
        return 0.0
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].sum() / period   # média simples inicial
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    return atr[-1]

# =============================================================================
# CLASSES DE REGIME (sem alterações, já corretas)
# =============================================================================

REGIME_LATERAL = "LATERAL"
REGIME_BULLISH = "BULLISH"
REGIME_BEARISH = "BEARISH"
REGIME_CRASH   = "CRASH"

class RegimeDetector:
    def __init__(self, config: Dict):
        self.adx_period = config.get("regime_adx_period", 14)
        self.ema_period = config.get("regime_ema_period", 200)
        self.adx_thresh = config.get("regime_adx_thresh", 25.0)
        self.crash_pct = config.get("regime_crash_pct", 8.0)
        self.crash_lookback = config.get("regime_crash_lookback", 20)

        self.candles_15m = []
        self.candles_1h = []

    def carregar_candles_tf_superior(self, exchange, symbol, limite=250):
        try:
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=limite)
            self.candles_15m = []
            for k in ohlcv_15m:
                self.candles_15m.append({
                    'timestamp': k[0],
                    'open': k[1],
                    'high': k[2],
                    'low': k[3],
                    'close': k[4]
                })
            ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=limite)
            self.candles_1h = []
            for k in ohlcv_1h:
                self.candles_1h.append({
                    'timestamp': k[0],
                    'open': k[1],
                    'high': k[2],
                    'low': k[3],
                    'close': k[4]
                })
            return True
        except Exception as e:
            logging.error(f"Erro ao carregar candles superiores: {e}")
            return False

    def _add_candle(self, candles_list: List, ts: int, open_p: float, high: float, low: float, close: float):
        if candles_list and candles_list[-1]['timestamp'] == ts:
            candles_list[-1]['high'] = max(candles_list[-1]['high'], high)
            candles_list[-1]['low'] = min(candles_list[-1]['low'], low)
            candles_list[-1]['close'] = close
        else:
            candles_list.append({
                'timestamp': ts,
                'open': open_p,
                'high': high,
                'low': low,
                'close': close
            })

    def _compute_regime_for_tf(self, candles: List) -> Optional[str]:
        if len(candles) < max(self.ema_period, self.adx_period * 2):
            return None
        highs = [c['high'] for c in candles]
        lows  = [c['low'] for c in candles]
        closes = [c['close'] for c in candles]
        adx, _, _ = compute_adx(highs, lows, closes, self.adx_period)
        ema = compute_ema(closes, self.ema_period)
        last_close = closes[-1]

        if adx < self.adx_thresh:
            return REGIME_LATERAL
        elif last_close > ema:
            return REGIME_BULLISH
        else:
            start = max(0, len(candles) - self.crash_lookback)
            ref_max = max(highs[start:])
            drop = (ref_max - last_close) / ref_max * 100 if ref_max > 0 else 0
            if drop >= self.crash_pct:
                return REGIME_CRASH
            else:
                return REGIME_BEARISH

    def update(self, ts_1m: int, open_1m: float, high_1m: float, low_1m: float, close_1m: float):
        dt = datetime.fromtimestamp(ts_1m / 1000)
        minute_15 = (dt.minute // 15) * 15
        ts_15m = int(datetime(dt.year, dt.month, dt.day, dt.hour, minute_15, 0).timestamp() * 1000)
        self._add_candle(self.candles_15m, ts_15m, open_1m, high_1m, low_1m, close_1m)
        ts_1h = int(datetime(dt.year, dt.month, dt.day, dt.hour, 0, 0).timestamp() * 1000)
        self._add_candle(self.candles_1h, ts_1h, open_1m, high_1m, low_1m, close_1m)

    def get_regime(self) -> Optional[str]:
        regime_15m = self._compute_regime_for_tf(self.candles_15m)
        regime_1h  = self._compute_regime_for_tf(self.candles_1h)
        ordem = {REGIME_CRASH: 4, REGIME_BEARISH: 3, REGIME_LATERAL: 2, REGIME_BULLISH: 1, None: 0}
        if ordem.get(regime_1h, 0) >= ordem.get(regime_15m, 0):
            return regime_1h
        else:
            return regime_15m

    def to_dict(self) -> Dict:
        return {
            'candles_15m': self.candles_15m[-200:],
            'candles_1h': self.candles_1h[-200:],
        }

    def from_dict(self, data: Dict):
        self.candles_15m = data.get('candles_15m', [])
        self.candles_1h = data.get('candles_1h', [])

class DrawdownFilter:
    def __init__(self, lookback_days: int = 90, max_drawdown_pct: float = 40.0):
        self.lookback_days = lookback_days
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_highs = deque(maxlen=lookback_days * 2)
        self.daily_closes = deque(maxlen=lookback_days * 2)
        self.ath = 0.0
        self.last_day = None

    def update(self, ts: int, high: float, close: float):
        dt = datetime.fromtimestamp(ts / 1000).date()
        if self.last_day is None or dt != self.last_day:
            self.last_day = dt
            self.daily_highs.append(high)
            self.daily_closes.append(close)
            self.ath = max(self.daily_highs) if self.daily_highs else high
        else:
            if self.daily_highs:
                self.daily_highs[-1] = max(self.daily_highs[-1], high)
                self.daily_closes[-1] = close
                self.ath = max(self.daily_highs)

    def current_drawdown(self) -> float:
        if self.ath == 0:
            return 0.0
        last_close = self.daily_closes[-1] if self.daily_closes else 0
        return (self.ath - last_close) / self.ath * 100.0

    def allowed(self) -> bool:
        return self.current_drawdown() <= self.max_drawdown_pct

    def to_dict(self) -> Dict:
        return {
            'daily_highs': list(self.daily_highs),
            'daily_closes': list(self.daily_closes),
            'ath': self.ath,
            'last_day': self.last_day.isoformat() if self.last_day else None,
        }

    def from_dict(self, data: Dict):
        self.daily_highs = deque(data.get('daily_highs', []), maxlen=self.lookback_days*2)
        self.daily_closes = deque(data.get('daily_closes', []), maxlen=self.lookback_days*2)
        self.ath = data.get('ath', 0.0)
        last_day_str = data.get('last_day')
        self.last_day = datetime.fromisoformat(last_day_str).date() if last_day_str else None

# =============================================================================
# MOTOR DE NEGOCIAÇÃO (VERSÃO CORRIGIDA)
# =============================================================================
class TradingEngine:
    def __init__(self, config: Dict, exchange, state_lock, shared_state):
        self.config = config
        self.exchange = exchange
        self.state_lock = state_lock
        self.shared_state = shared_state

        # Capital
        self.cash = config["CAPITAL_TOTAL"]
        self.base_capital = config["CAPITAL_BASE"]
        self.fee = config["FEE_PCT"] / 100.0

        # Posição
        self.position = None
        self.cooldown = 0
        self.last_candle_time = 0

        # Histórico
        self.closes = deque(maxlen=500)
        self.highs = deque(maxlen=500)
        self.lows = deque(maxlen=500)
        self.volumes_usd = deque(maxlen=500)
        self.candles_raw = deque(maxlen=200)

        # Indicadores
        self.rsi_period = config.get("entry_rsi_period", 14)
        self.rsi_value = 50.0
        self.atr_period = config.get("atr_period", 14)
        self.atr_value = 0.0
        self.last_close_for_atr = None
        self.atr_initialized = False  # flag para saber se o ATR já tem seeding

        # Detectores
        self.regime_detector = RegimeDetector(config)
        self.drawdown_filter = DrawdownFilter(
            lookback_days=config.get("drawdown_lookback_days", 90),
            max_drawdown_pct=config.get("drawdown_max_pct", 40.0)
        )

        # Estratégia de reversão (original)
        self.reversal_enabled = config.get("REVERSAL_ENABLED", True)
        self.rsi_threshold = config.get("entry_rsi_threshold", 15)
        self.use_exhaust = config.get("entry_use_exhaust", True)
        self.exhaust_lookback = config.get("entry_exhaust_lookback", 3)
        self.use_wick = config.get("entry_use_wick", True)
        self.wick_ratio = config.get("entry_wick_ratio", 0.25)
        self.use_confirm = config.get("entry_use_confirm", True)
        self.confirm_lookback = config.get("entry_confirm_lookback", 2)
        self.use_volume = config.get("entry_use_volume", True)
        self.volume_mult = config.get("entry_volume_mult", 1.2)
        self.volume_lookback = config.get("entry_volume_lookback", 6)
        self.use_drawdown_filter = config.get("entry_use_drawdown_filter", True)

        self.tp_bearish = config.get("tp_bearish", 0.8)
        self.sl_bearish = config.get("sl_bearish", 3.0)
        self.tp_bullish = config.get("tp_bullish", 2.0)
        self.sl_bullish = config.get("sl_bullish", 3.0)
        self.lateral_enabled = config.get("lateral_enabled", False)

        # ----- Estratégias de tendência -----
        self.breakout_enabled = config.get("breakout_enabled", False)
        self.breakout_period = config.get("breakout_period", 20)
        self.breakout_trailing_atr_factor = config.get("breakout_trailing_atr_factor", 2.0)
        self.breakout_min_volume_ratio = config.get("breakout_min_volume_ratio", 1.2)

        self.pullback_enabled = config.get("pullback_enabled", False)
        self.pullback_ema_period = config.get("pullback_ema_period", 21)
        self.pullback_max_distance_pct = config.get("pullback_max_distance_pct", 1.5)
        self.pullback_confirmation = config.get("pullback_confirmation", True)
        self.pullback_trailing_atr_factor = config.get("pullback_trailing_atr_factor", 1.5)
        self.pullback_min_volume_ratio = config.get("pullback_min_volume_ratio", 1.0)

        self.momentum_enabled = config.get("momentum_enabled", False)
        self.momentum_rsi_threshold = config.get("momentum_rsi_threshold", 65)
        self.momentum_trailing_atr_factor = config.get("momentum_trailing_atr_factor", 2.5)
        self.momentum_min_volume_ratio = config.get("momentum_min_volume_ratio", 1.2)

        self.trend_capital_multiplier = config.get("trend_capital_multiplier", 1.0)
        self.trend_use_trailing = config.get("trend_use_trailing", True)
        self.tp_trend = config.get("tp_trend", 3.0)
        self.trend_trailing_activation_pct = config.get("trend_trailing_activation_pct", 0.5)

        # Minimo notional
        try:
            market = exchange.market(config["SYMBOL"])
            self.min_notional = market['limits']['cost']['min']
        except:
            self.min_notional = 5.0

        # Placar (será preenchido dinamicamente)
        self.entry_conditions = {}
        self.entry_mode = None
        self._last_volume_ratio = 0.0
        self._last_wick = 0.0

    # ---------- Indicadores (corrigidos) ----------
    def _update_rsi(self):
        if len(self.closes) < self.rsi_period + 1:
            self.rsi_value = 50.0
        else:
            self.rsi_value = compute_rsi(list(self.closes), self.rsi_period)

    def _update_atr(self, high: float, low: float, close: float):
        """ATR com seeding inicial e Wilder smoothing."""
        if self.last_close_for_atr is None:
            self.last_close_for_atr = close
            return

        tr = max(high - low, abs(high - self.last_close_for_atr), abs(low - self.last_close_for_atr))
        if not self.atr_initialized:
            # Acumula os primeiros `atr_period` TRs para seeding
            if not hasattr(self, '_tr_buffer'):
                self._tr_buffer = []
            self._tr_buffer.append(tr)
            if len(self._tr_buffer) == self.atr_period:
                self.atr_value = sum(self._tr_buffer) / self.atr_period
                self.atr_initialized = True
                delattr(self, '_tr_buffer')
        else:
            # Wilder smoothing correto: ATR[novo] = (ATR[antigo] * (period-1) + TR[novo]) / period
            self.atr_value = (self.atr_value * (self.atr_period - 1) + tr) / self.atr_period

        self.last_close_for_atr = close

    def _check_volume_ratio(self, volume_usd: float, min_ratio: float) -> bool:
        if len(self.volumes_usd) < self.volume_lookback + 1:
            return False
        vols = list(self.volumes_usd)[-self.volume_lookback-1:-1]
        if not vols:
            return False
        avg = sum(vols) / len(vols)
        self._last_volume_ratio = volume_usd / avg if avg > 0 else 0
        return volume_usd >= avg * min_ratio

    # ---------- Condições de tendência (corrigidas) ----------
    def _is_bullish_allowed(self) -> bool:
        regime = self.regime_detector.get_regime()
        if regime == REGIME_BULLISH:
            return True
        if regime == REGIME_LATERAL and self.lateral_enabled:
            return True
        return False

    def _get_higher_tf_high(self, n: int) -> float:
        candles = self.regime_detector.candles_1h
        if len(candles) < n:
            return 0.0
        recent = [c['high'] for c in candles[-n:]]
        return max(recent)

    def _get_ema(self, period: int) -> float:
        return compute_ema(list(self.closes), period)

    def _get_adx_current(self) -> float:
        """Retorna ADX atual baseado nos candles de 1h (para filtro de momentum)."""
        candles = self.regime_detector.candles_1h
        if len(candles) < 30:
            return 0.0
        highs = [c['high'] for c in candles[-30:]]
        lows  = [c['low'] for c in candles[-30:]]
        closes = [c['close'] for c in candles[-30:]]
        adx, _, _ = compute_adx(highs, lows, closes, 14)
        return adx

    def _check_breakout_entry(self, close: float, volume_usd: float) -> Tuple[bool, str]:
        if not self.breakout_enabled or not self._is_bullish_allowed():
            return False, ""
        max_n = self._get_higher_tf_high(self.breakout_period)
        if max_n == 0 or close <= max_n:
            return False, ""
        if not self._check_volume_ratio(volume_usd, self.breakout_min_volume_ratio):
            return False, ""
        return True, "BREAKOUT"

    def _check_pullback_entry(self, close: float, volume_usd: float, low: float, high: float) -> Tuple[bool, str]:
        if not self.pullback_enabled or not self._is_bullish_allowed():
            return False, ""
        ema21 = self._get_ema(self.pullback_ema_period)
        if ema21 == 0:
            return False, ""
        distance_pct = abs(close - ema21) / ema21 * 100
        if distance_pct > self.pullback_max_distance_pct:
            return False, ""
        if low > ema21:   # não tocou na EMA por baixo
            return False, ""
        if not self._check_volume_ratio(volume_usd, self.pullback_min_volume_ratio):
            return False, ""
        if self.pullback_confirmation:
            if len(self.candles_raw) < 2:
                return False, ""
            prev_close = self.candles_raw[-2]['c']
            # Confirmação: vela atual é de alta (close > open) e fechou acima do close anterior ou high anterior
            if close <= self.candles_raw[-1]['o']:   # não é vela de alta
                return False, ""
            if close <= prev_close:
                return False, ""
        return True, "PULLBACK"

    def _check_momentum_entry(self, close: float, volume_usd: float) -> Tuple[bool, str]:
        if not self.momentum_enabled or not self._is_bullish_allowed():
            return False, ""
        if self.rsi_value < self.momentum_rsi_threshold:
            return False, ""
        # Filtro adicional: ADX > 25 para garantir força da tendência
        adx = self._get_adx_current()
        if adx < 25:
            return False, ""
        if not self._check_volume_ratio(volume_usd, self.momentum_min_volume_ratio):
            return False, ""
        return True, "MOMENTUM"

    # ---------- Condições de reversão (original, sem mudanças) ----------
    def _check_exhaustion(self) -> bool:
        if not self.use_exhaust or len(self.candles_raw) < self.exhaust_lookback + 1:
            return not self.use_exhaust
        candles = list(self.candles_raw)
        idx = len(candles) - 1
        if idx < self.exhaust_lookback:
            return False
        recent_lows = [c['l'] for c in candles[idx - self.exhaust_lookback:idx]]
        prior_min = min(recent_lows)
        current = candles[idx]
        return current['l'] < prior_min and current['c'] > prior_min

    def _check_lower_wick(self) -> bool:
        if not self.use_wick:
            return True
        if not self.candles_raw:
            return False
        c = self.candles_raw[-1]
        rng = c['h'] - c['l']
        if rng <= 0:
            return False
        wick_lower = (min(c['o'], c['c']) - c['l']) / rng
        self._last_wick = wick_lower
        return wick_lower >= self.wick_ratio

    def _check_confirmation(self) -> bool:
        if not self.use_confirm or len(self.candles_raw) < 2:
            return not self.use_confirm
        candles = list(self.candles_raw)
        idx = len(candles) - 1
        prev = candles[idx-1]
        close = candles[idx]['c']
        if close > prev['h']:
            return True
        start = max(0, idx - self.confirm_lookback)
        recent_highs = [candles[i]['h'] for i in range(start, idx)]
        if recent_highs and close > max(recent_highs):
            return True
        return close > prev['o'] and close > prev['c']

    # ---------- Ações de mercado (corrigidas) ----------
    def _open_position(self, price: float, ts: int, strategy: str, regime: str):
        # Determina parâmetros conforme estratégia
        if strategy == "REVERSAL":
            tp = self.tp_bearish
            sl = self.sl_bearish
            capital_mult = 1.0
            use_trailing = False
            trailing_atr_factor = 0
            trailing_activation = 0
        else:  # estratégias de tendência
            tp = self.tp_trend if not self.trend_use_trailing else 0
            sl = 0  # será definido via ATR depois
            capital_mult = self.trend_capital_multiplier
            use_trailing = self.trend_use_trailing
            if strategy == "BREAKOUT":
                trailing_atr_factor = self.breakout_trailing_atr_factor
            elif strategy == "PULLBACK":
                trailing_atr_factor = self.pullback_trailing_atr_factor
            else:
                trailing_atr_factor = self.momentum_trailing_atr_factor
            trailing_activation = self.trend_trailing_activation_pct

        cost = self.base_capital * capital_mult
        if self.cash < cost:
            return False

        obs = f"estrat={strategy} regime={regime} rsi={self.rsi_value:.1f}"
        ordem = comprar_com_vault(cost, motivo=f"Entrada {strategy}", obs=obs)
        if not ordem['ok']:
            return False

        qty = ordem['v']
        custo_real = ordem['c']
        preco_real = ordem['p']
        self.cash -= custo_real

        self.position = {
            "avgCost": preco_real,
            "totalQty": qty,
            "totalCost": custo_real,
            "openTime": ts,
            "strategy": strategy,
            "regime": regime,
            "use_trailing": use_trailing,
            "trailing_atr_factor": trailing_atr_factor,
            "trailing_activation_pct": trailing_activation,
            "trailing_active": False,
            "trailing_stop": 0.0,
            "max_price": preco_real,
        }

        # Stop loss fixo (apenas para reversão)
        if strategy == "REVERSAL":
            self.position["sl_price"] = preco_real * (1 - sl/100)
            self.position["tp_price"] = preco_real * (1 + tp/100)
        else:
            # Stop loss inicial baseado em ATR (com fallback)
            atr_mult = 1.5
            if self.atr_initialized and self.atr_value > 0:
                stop_distance = self.atr_value * atr_mult
            else:
                # Fallback: 2% do preço
                stop_distance = preco_real * 0.02
            self.position["sl_price"] = preco_real - stop_distance
            self.position["tp_price"] = None  # sem TP fixo

        with self.state_lock:
            self.shared_state["em_operacao"] = True
            self.shared_state["marcha"] = f"POSIÇÃO ATIVA [{strategy}]"
            self.shared_state["preco_medio"] = preco_real

        self.entry_conditions = {}
        self.entry_mode = strategy
        salvar_estado_disco()
        return True

    def _close_position(self, exit_price: float, ts: int, reason: str):
        if self.position is None:
            return False
        qtd = self.position["totalQty"]
        custo = self.position["totalCost"]
        strategy = self.position["strategy"]
        obs = f"SAIDA: {reason} estrategia={strategy} price={exit_price:.6f}"
        res = vender_quantidade(self.exchange, qtd, custo, reason, self.config,
                                self.shared_state, self.state_lock, obs=obs)
        if res['ok']:
            self.cash += res['receita']
            with self.state_lock:
                self.shared_state["lucro_perc_atual"] = 0.0
                self.shared_state["perda_usd_atual"] = 0.0
        else:
            logging.error(f"Falha na venda ({reason}): {res.get('msg')}")
            return False

        self.position = None
        salvar_estado_disco()
        self.cooldown = self.config.get("ENTRY_COOLDOWN", 1)
        with self.state_lock:
            self.shared_state["em_operacao"] = False
            self.shared_state["marcha"] = "AGUARDANDO ENTRADA"
        return True

    def _update_trailing_stop(self, high: float, low: float, close: float, ts: int):
        if self.position is None or not self.position.get("use_trailing", False):
            return
        # Atualiza preço máximo
        if high > self.position["max_price"]:
            self.position["max_price"] = high
        # Ativa trailing após lucro mínimo
        if not self.position["trailing_active"]:
            profit_pct = (close - self.position["avgCost"]) / self.position["avgCost"] * 100
            if profit_pct >= self.position["trailing_activation_pct"]:
                self.position["trailing_active"] = True
                if self.atr_initialized and self.atr_value > 0:
                    self.position["trailing_stop"] = self.position["max_price"] - (self.atr_value * self.position["trailing_atr_factor"])
                else:
                    # Fallback: 1.5% de trailing
                    self.position["trailing_stop"] = self.position["max_price"] * 0.985
        else:
            if self.atr_initialized and self.atr_value > 0:
                new_stop = self.position["max_price"] - (self.atr_value * self.position["trailing_atr_factor"])
            else:
                new_stop = self.position["max_price"] * 0.985
            if new_stop > self.position["trailing_stop"]:
                self.position["trailing_stop"] = new_stop
            if low <= self.position["trailing_stop"]:
                self._close_position(low, ts, "TRAILING_STOP")

    # ---------- Processamento de candle (corrigido) ----------
    def on_candle(self, candle: Dict):
        ts = candle['t']
        open_p = candle['o']
        high = candle['h']
        low = candle['l']
        close = candle['c']
        volume_base = candle['v']
        volume_usd = volume_base * close

        # Históricos
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)
        self.volumes_usd.append(volume_usd)
        self.candles_raw.append(candle)
        self.last_candle_time = ts

        # Indicadores
        self._update_rsi()
        self._update_atr(high, low, close)
        self.regime_detector.update(ts, open_p, high, low, close)
        self.drawdown_filter.update(ts, high, close)

        # Cooldown
        if self.cooldown > 0:
            self.cooldown -= 1

        # Gerenciar posição existente
        if self.position is not None:
            # Verifica stop loss fixo (apenas reversão)
            if "sl_price" in self.position and low <= self.position["sl_price"]:
                self._close_position(self.position["sl_price"], ts, "STOP_LOSS")
                return
            if "tp_price" in self.position and self.position["tp_price"] and high >= self.position["tp_price"]:
                self._close_position(self.position["tp_price"], ts, "TAKE_PROFIT")
                return
            # Trailing stop para tendência
            if self.position.get("use_trailing", False):
                self._update_trailing_stop(high, low, close, ts)
            return

        # --- Verificar entrada (prioridade: reversão > breakout > pullback > momentum) ---
        if self.cooldown > 0:
            return

        regime = self.regime_detector.get_regime()
        regime_ok = regime is not None and regime != REGIME_CRASH
        if regime == REGIME_LATERAL and not self.lateral_enabled:
            regime_ok = False

        # Estratégia de reversão (apenas em BEARISH)
        if self.reversal_enabled and regime == REGIME_BEARISH and regime_ok:
            rsi_ok = self.rsi_value <= self.rsi_threshold
            exhaust_ok = self._check_exhaustion()
            wick_ok = self._check_lower_wick()
            confirm_ok = self._check_confirmation()
            volume_ok = self._check_volume_ratio(volume_usd, self.volume_mult)
            drawdown_ok = not self.use_drawdown_filter or self.drawdown_filter.allowed()
            if all([rsi_ok, exhaust_ok, wick_ok, confirm_ok, volume_ok, drawdown_ok]):
                self.entry_conditions = {
                    "RSI≤15": rsi_ok,
                    "Exaustão": exhaust_ok,
                    "Pavio≥25%": wick_ok,
                    "Confirmação": confirm_ok,
                    "Volume": volume_ok,
                    "Drawdown≤40%": drawdown_ok,
                }
                self._open_position(close, ts, "REVERSAL", regime)
                return

        # Estratégias de tendência (apenas em BULLISH ou LATERAL habilitado)
        if self._is_bullish_allowed():
            # Breakout
            ok, strat = self._check_breakout_entry(close, volume_usd)
            if ok:
                self.entry_conditions = {"Breakout": True, "Volume": True}
                self._open_position(close, ts, strat, regime)
                return
            # Pullback
            ok, strat = self._check_pullback_entry(close, volume_usd, low, high)
            if ok:
                self.entry_conditions = {"Pullback": True, "Volume": True}
                self._open_position(close, ts, strat, regime)
                return
            # Momentum
            ok, strat = self._check_momentum_entry(close, volume_usd)
            if ok:
                self.entry_conditions = {"Momentum": True, "Volume": True, "ADX>25": True}
                self._open_position(close, ts, strat, regime)
                return

        # Se nenhuma estratégia ativou, placar vazio
        self.entry_conditions = {}

    # ---------- Estado (com ATR persistente) ----------
    def get_state(self) -> Dict:
        return {
            "cash": self.cash,
            "position": self.position,
            "cooldown": self.cooldown,
            "last_candle_time": self.last_candle_time,
            "rsi_value": self.rsi_value,
            "atr_value": self.atr_value,
            "atr_initialized": self.atr_initialized,
            "last_close_for_atr": self.last_close_for_atr,
            "entry_conditions": self.entry_conditions,
            "entry_mode": self.entry_mode,
            "regime_detector": self.regime_detector.to_dict(),
            "drawdown_filter": self.drawdown_filter.to_dict(),
            "closes": list(self.closes)[-100:],
            "highs": list(self.highs)[-100:],
            "lows": list(self.lows)[-100:],
            "volumes_usd": list(self.volumes_usd)[-100:],
            "candles_raw": list(self.candles_raw)[-100:],
        }

    def from_dict(self, data: Dict):
        self.cash = data.get("cash", self.config["CAPITAL_TOTAL"])
        self.position = data.get("position")
        self.cooldown = data.get("cooldown", 0)
        self.last_candle_time = data.get("last_candle_time", 0)
        self.rsi_value = data.get("rsi_value", 50.0)
        self.atr_value = data.get("atr_value", 0.0)
        self.atr_initialized = data.get("atr_initialized", False)
        self.last_close_for_atr = data.get("last_close_for_atr")
        self.entry_conditions = data.get("entry_conditions", {})
        self.entry_mode = data.get("entry_mode")
        self.regime_detector.from_dict(data.get("regime_detector", {}))
        self.drawdown_filter.from_dict(data.get("drawdown_filter", {}))
        self.closes = deque(data.get("closes", []), maxlen=500)
        self.highs = deque(data.get("highs", []), maxlen=500)
        self.lows = deque(data.get("lows", []), maxlen=500)
        self.volumes_usd = deque(data.get("volumes_usd", []), maxlen=500)
        self.candles_raw = deque(data.get("candles_raw", []), maxlen=200)
        self._update_rsi()
        # Se o ATR não estava inicializado e temos dados suficientes, podemos tentar inicializar
        if not self.atr_initialized and len(self.highs) >= self.atr_period:
            # Recalcula ATR a partir dos históricos
            highs_list = list(self.highs)
            lows_list = list(self.lows)
            closes_list = list(self.closes)
            if len(highs_list) >= self.atr_period:
                self.atr_value = compute_atr(highs_list, lows_list, closes_list, self.atr_period)
                self.atr_initialized = True

    def get_ui_state(self) -> Dict:
        regime = self.regime_detector.get_regime() if self.regime_detector else "DESCONHECIDO"
        return {
            "current_regime": regime or "DESCONHECIDO",
            "drawdown_pct": self.drawdown_filter.current_drawdown(),
            "rsi": self.rsi_value,
            "entry_conditions": self.entry_conditions,
            "entry_mode": self.entry_mode,
            "cash": self.cash,
            "em_operacao": self.position is not None,
            "preco_medio": self.position["avgCost"] if self.position else 0.0,
            "position": self.position,
        }

# =============================================================================
# AUDITORIA, CONFIG, ORDENS (mantido igual à versão anterior)
# =============================================================================
class Auditoria:
    @staticmethod
    def configurar():
        root_logger = logging.getLogger()
        if not root_logger.handlers:
            with config_lock:
                max_bytes    = CONFIG.get("LOG_MAX_BYTES", 5_242_880)
                backup_count = CONFIG.get("LOG_BACKUP_COUNT", 5)
            handler = logging.handlers.RotatingFileHandler(
                ARQUIVO_LOG_SISTEMA,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            root_logger.setLevel(logging.INFO)
            root_logger.addHandler(handler)
        if not os.path.exists(ARQUIVO_LOG_TRADES):
            with open(ARQUIVO_LOG_TRADES, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([
                    "DATA", "PAR", "TIPO", "PRECO", "QTD",
                    "TOTAL_USD", "LUCRO_USD", "LUCRO_PERC", "SALDO_VAULT", "OBS"
                ])

    @staticmethod
    def log_sistema(msg, nivel="INFO"):
        nivel = {"INFO": "info", "AVISO": "warning", "ERRO": "error", "CRITICO": "critical"}.get(nivel, "info")
        getattr(logging, nivel)(msg)
        return msg

    @staticmethod
    def log_transacao(tipo, preco, qtd, total_usd, lucro_usd=0.0, lucro_perc=0.0, saldo_vault=0.0, obs=""):
        with config_lock:
            symbol = CONFIG["SYMBOL"]
        linha = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol, tipo,
            f"{preco:.8f}", f"{qtd:.8f}", f"{total_usd:.2f}",
            f"{lucro_usd:.2f}", f"{lucro_perc:.2f}%",
            f"{saldo_vault:.2f}", obs
        ]
        logging.info(f"LOG_TRANSACAO: {linha}")
        with csv_lock:
            with open(ARQUIVO_LOG_TRADES, 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(linha)

shared_state = {
    "preco": 0.0,
    "erros_consecutivos": 0,
    "marcha": "INICIALIZANDO...",
    "em_operacao": False,
    "preco_medio": 0.0,
    "lucro_perc_atual": 0.0,
    "perda_usd_atual": 0.0,
    "trailing_ativo": False,
    "max_p_trailing": 0.0,
    "stop_atual_trailing": 0.0,
    "circuit_breaker": False,
    "current_regime": "DESCONHECIDO",
    "drawdown_pct": 0.0,
    "rsi": 50.0,
    "entry_conditions": {},
    "entry_mode": None,
    "cash": 0.0,
    "msg_log": "Sistema Iniciado",
}

menu_ativo = False
exchange = None
trading_engine: Optional[TradingEngine] = None

CONFIG = {
    "API_KEY": "", "SECRET": "",
    "SYMBOL": "AXS/USDT", "MOEDA_BASE": "USDT",
    "CAPITAL_TOTAL": 100.0, "CAPITAL_BASE": 100.0,
    "FEE_PCT": 0.075, "COMPOUND": False,
    "ENTRY_COOLDOWN": 1,
    "CIRCUIT_BREAKER_ATIVO": False,
    "TIMEFRAME": "5m",
    "LOG_MAX_BYTES": 5_242_880, "LOG_BACKUP_COUNT": 5,
    "regime_adx_period": 14, "regime_ema_period": 200, "regime_adx_thresh": 25.0,
    "regime_crash_pct": 8.0, "regime_crash_lookback": 20, "lateral_enabled": False,
    "drawdown_lookback_days": 90, "drawdown_max_pct": 40.0,
    "entry_rsi_period": 14, "entry_rsi_threshold": 15,
    "entry_use_exhaust": True, "entry_exhaust_lookback": 3,
    "entry_use_wick": True, "entry_wick_ratio": 0.25,
    "entry_use_confirm": True, "entry_confirm_lookback": 2,
    "entry_use_volume": True, "entry_volume_mult": 1.2, "entry_volume_lookback": 6,
    "entry_use_drawdown_filter": True,
    "tp_bearish": 0.8, "sl_bearish": 3.0,
    "tp_bullish": 2.0, "sl_bullish": 3.0, "tp_lateral": 1.2, "sl_lateral": 2.0,
    "REVERSAL_ENABLED": True,
    "breakout_enabled": False, "breakout_period": 20, "breakout_trailing_atr_factor": 2.0, "breakout_min_volume_ratio": 1.2,
    "pullback_enabled": False, "pullback_ema_period": 21, "pullback_max_distance_pct": 1.5,
    "pullback_confirmation": True, "pullback_trailing_atr_factor": 1.5, "pullback_min_volume_ratio": 1.0,
    "momentum_enabled": False, "momentum_rsi_threshold": 65, "momentum_trailing_atr_factor": 2.5,
    "momentum_min_volume_ratio": 1.2,
    "trend_capital_multiplier": 1.0, "trend_use_trailing": True, "tp_trend": 3.0,
    "trend_trailing_activation_pct": 0.5,
    "atr_period": 14,
}

# =============================================================================
# FUNÇÕES DE CONEXÃO E ORDENS (mantidas da versão anterior)
# =============================================================================
def definir_status(msg, tipo="INFO"):
    hora = datetime.now().strftime("%H:%M:%S")
    cor = {"SUCESSO": Fore.GREEN, "ERRO": Fore.RED, "AVISO": Fore.YELLOW}.get(tipo, Fore.CYAN)
    with state_lock:
        shared_state["msg_log"] = f"{Fore.WHITE}[{hora}] {cor}{msg}"
    if tipo == "ERRO":
        Auditoria.log_sistema(msg, "ERRO")
    elif tipo == "SUCESSO":
        Auditoria.log_sistema(msg, "INFO")

def panico_sistema(mensagem):
    logging.critical(f"DISJUNTOR ATIVADO: {mensagem}")
    definir_status(f"ERRO CRÍTICO: {mensagem} — DESLIGANDO", "ERRO")
    salvar_estado_disco()
    sys.exit(1)

def get_saldo_fundos() -> float:
    with config_lock:
        moeda = CONFIG["MOEDA_BASE"]
    try:
        with exchange_lock:
            bal = exchange.fetch_balance({'type': 'funding'})
            return bal.get(moeda, {}).get('free', 0)
    except Exception:
        return 0

def transferir_para_spot(valor: float) -> bool:
    with config_lock:
        moeda = CONFIG["MOEDA_BASE"]
    try:
        with exchange_lock:
            exchange.transfer(moeda, valor, 'funding', 'spot')
        Auditoria.log_sistema(f"VAULT: ${valor:.2f} enviado ao Spot", "INFO")
        time.sleep(1)
        return True
    except Exception as e:
        definir_status(f"Erro Vault (Fundos→Spot): {e}", "ERRO")
        return False

def recolher_para_fundos() -> bool:
    with config_lock:
        moeda = CONFIG["MOEDA_BASE"]
    try:
        with exchange_lock:
            balance = exchange.fetch_balance()
            saldo_usdt = balance.get(moeda, {}).get('free', 0)
        if saldo_usdt > 0.5:
            with exchange_lock:
                exchange.transfer(moeda, saldo_usdt, 'spot', 'funding')
            Auditoria.log_sistema(f"VAULT: ${saldo_usdt:.2f} protegido em Fundos", "INFO")
        return True
    except Exception as e:
        definir_status(f"Erro Vault (Spot→Fundos): {e}", "ERRO")
        return False

def comprar_com_vault(valor_usd: float, motivo: str = "Entrada", obs: str = "") -> dict:
    with config_lock:
        moeda = CONFIG["MOEDA_BASE"]
    try:
        with exchange_lock:
            bal_funding = exchange.fetch_balance({'type': 'funding'})
            saldo_usdt_funding = bal_funding.get(moeda, {}).get('free', 0)
        if saldo_usdt_funding < valor_usd:
            logging.error(f"Saldo insuficiente no Funding: {saldo_usdt_funding:.2f} < {valor_usd:.2f}")
            return {'ok': False, 'msg': 'Saldo Funding insuficiente'}
    except Exception as e:
        logging.error(f"Erro ao verificar saldo Funding: {e}")
        return {'ok': False, 'msg': str(e)}

    if not transferir_para_spot(valor_usd):
        return {'ok': False, 'msg': 'Falha na transferência Fundos→Spot'}
    with config_lock:
        symbol = CONFIG['SYMBOL']
    try:
        with exchange_lock:
            ordem = exchange.create_order(
                symbol, 'market', 'buy', None,
                params={'quoteOrderQty': exchange.cost_to_precision(symbol, valor_usd)}
            )
        with state_lock:
            shared_state["erros_consecutivos"] = 0
        preco_exec = float(ordem.get('average') or ordem.get('price') or 0)
        res = {'ok': True, 'p': preco_exec,
               'v': float(ordem['amount']), 'c': float(ordem['cost'])}
        obs_final = obs if obs else motivo
        Auditoria.log_transacao("COMPRA", res['p'], res['v'], res['c'], obs=obs_final)
        return res
    except Exception as e:
        with state_lock:
            shared_state["erros_consecutivos"] += 1
            cnt = shared_state["erros_consecutivos"]
        logging.error(f"Erro Compra: {e} ({cnt}/5)")
        definir_status("Ordem falhou! Recolhendo ao Vault...", "AVISO")
        recolher_para_fundos()
        return {'ok': False, 'msg': str(e)}

def inicializar_vault():
    definir_status("Auditando Vault...", "INFO")
    recolher_para_fundos()
    try:
        saldo = get_saldo_fundos()
        nivel = "AVISO" if saldo < 120 else "SUCESSO"
        definir_status(f"Vault: ${saldo:.2f}", nivel)
    except Exception as e:
        definir_status(f"Erro Vault Init: {e}", "ERRO")

def vender_quantidade(exchange, qtd, custo, motivo, config, shared_state, state_lock, obs=""):
    symbol = config["SYMBOL"]
    try:
        base_currency = symbol.split('/')[0]
        with exchange_lock:
            real_balance = exchange.fetch_balance()[base_currency]['free']
        if qtd > real_balance:
            qtd = real_balance
            if qtd <= 0:
                return {'ok': False, 'msg': 'Saldo zero'}
        with exchange_lock:
            market = exchange.market(symbol)
            min_amount = market['limits']['amount']['min']
        if qtd < min_amount:
            return {'ok': False, 'msg': f'Qtd {qtd} abaixo do mínimo {min_amount}'}
        with exchange_lock:
            qtd_fmt = exchange.amount_to_precision(symbol, qtd)
            ordem = exchange.create_order(symbol, 'market', 'sell', qtd_fmt)
            preco = float(ordem.get('average') or ordem.get('price') or 0)
            receita = float(ordem.get('cost') or 0)
        pnl = receita - custo
        pnl_pct = (pnl / custo) * 100 if custo > 0 else 0
        obs_final = obs if obs else motivo
        Auditoria.log_transacao(tipo=motivo, preco=preco, qtd=qtd, total_usd=receita, lucro_usd=pnl, lucro_perc=pnl_pct, saldo_vault=0, obs=obs_final)
        return {'ok': True, 'preco': preco, 'receita': receita, 'pnl': pnl, 'pnl_pct': pnl_pct}
    except Exception as e:
        logging.error(f"Erro na venda ({motivo}): {e}")
        return {'ok': False, 'msg': str(e)}

def validate_config() -> None:
    if CONFIG["tp_bearish"] <= 0 or CONFIG["sl_bearish"] <= 0:
        raise ValueError("TP/SL bearish devem ser positivos")
    if CONFIG["CAPITAL_BASE"] <= 0:
        raise ValueError("CAPITAL_BASE deve ser > 0")
    if CONFIG["CAPITAL_TOTAL"] < CONFIG["CAPITAL_BASE"]:
        raise ValueError("CAPITAL_TOTAL não pode ser menor que CAPITAL_BASE")

STATE_SCHEMA_VERSION = 43

def salvar_estado_disco():
    try:
        with engine_lock, state_lock:
            if trading_engine:
                estado_motor = trading_engine.get_state()
            else:
                estado_motor = {}
            dados = {
                "schema_version": STATE_SCHEMA_VERSION,
                "em_operacao": shared_state["em_operacao"],
                "symbol": CONFIG["SYMBOL"],
                "motor": estado_motor,
                "timestamp": str(datetime.now()),
            }
        with open(ARQUIVO_ESTADO, "w") as f:
            json.dump(dados, f, indent=4, cls=NumpyEncoder)
        Auditoria.log_sistema("Estado salvo com sucesso.", "INFO")
    except Exception as e:
        logging.error(f"Erro ao salvar estado: {e}")

def carregar_estado_disco():
    if not os.path.exists(ARQUIVO_ESTADO):
        return False
    try:
        with open(ARQUIVO_ESTADO) as f:
            dados = json.load(f)
        if dados.get("em_operacao") and dados.get("symbol") == CONFIG["SYMBOL"]:
            with state_lock:
                shared_state["em_operacao"] = True
                shared_state["marcha"] = "RECUPERANDO..."
            if trading_engine and "motor" in dados:
                with engine_lock:
                    trading_engine.from_dict(dados["motor"])
                if trading_engine.position:
                    ui = trading_engine.get_ui_state()
                    with state_lock:
                        shared_state.update(ui)
                        shared_state["em_operacao"] = True
                        shared_state["marcha"] = "POSIÇÃO ATIVA (REC)"
            return True
    except Exception as e:
        Auditoria.log_sistema(f"Erro ao ler save: {e}", "ERRO")
    return False

def carregar_configuracoes():
    global exchange, CONFIG, trading_engine

    if not os.path.exists("config.ini"):
        Auditoria.log_sistema("Arquivo config.ini não encontrado. Usando configurações padrão.", "AVISO")
    else:
        cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        cp.read("config.ini")

        CONFIG["API_KEY"] = cp.get("binance", "api_key", fallback=CONFIG["API_KEY"])
        CONFIG["SECRET"]  = cp.get("binance", "secret", fallback=CONFIG["SECRET"])

        if "mercado" in cp:
            mg = cp["mercado"]
            CONFIG["SYMBOL"] = mg.get("symbol", CONFIG["SYMBOL"])
            CONFIG["MOEDA_BASE"] = CONFIG["SYMBOL"].split('/')[1] if '/' in CONFIG["SYMBOL"] else "USDT"
            CONFIG["CAPITAL_TOTAL"] = float(mg.get("capital_total", CONFIG["CAPITAL_TOTAL"]))
            CONFIG["CAPITAL_BASE"] = float(mg.get("capital_base", CONFIG["CAPITAL_BASE"]))
            CONFIG["FEE_PCT"] = float(mg.get("fee_pct", CONFIG["FEE_PCT"]))

        if "trading" in cp:
            tg = cp["trading"]
            CONFIG["TIMEFRAME"] = tg.get("timeframe", CONFIG["TIMEFRAME"])
            CONFIG["ENTRY_COOLDOWN"] = int(tg.get("entry_cooldown", CONFIG["ENTRY_COOLDOWN"]))
            CONFIG["atr_period"] = int(tg.get("atr_period", CONFIG["atr_period"]))

        if "regime" in cp:
            rg = cp["regime"]
            CONFIG["regime_adx_period"] = int(rg.get("adx_period", CONFIG["regime_adx_period"]))
            CONFIG["regime_ema_period"] = int(rg.get("ema_period", CONFIG["regime_ema_period"]))
            CONFIG["regime_adx_thresh"] = float(rg.get("adx_thresh", CONFIG["regime_adx_thresh"]))
            CONFIG["regime_crash_pct"] = float(rg.get("crash_pct", CONFIG["regime_crash_pct"]))
            CONFIG["regime_crash_lookback"] = int(rg.get("crash_lookback", CONFIG["regime_crash_lookback"]))
            CONFIG["lateral_enabled"] = rg.getboolean("lateral_enabled", CONFIG["lateral_enabled"])

        if "drawdown" in cp:
            dd = cp["drawdown"]
            CONFIG["drawdown_lookback_days"] = int(dd.get("lookback_days", CONFIG["drawdown_lookback_days"]))
            CONFIG["drawdown_max_pct"] = float(dd.get("max_pct", CONFIG["drawdown_max_pct"]))
            CONFIG["entry_use_drawdown_filter"] = dd.getboolean("enabled", CONFIG["entry_use_drawdown_filter"])

        if "entry" in cp:
            en = cp["entry"]
            CONFIG["entry_rsi_threshold"] = int(en.get("rsi_threshold", CONFIG["entry_rsi_threshold"]))
            CONFIG["entry_wick_ratio"] = float(en.get("wick_ratio", CONFIG["entry_wick_ratio"]))
            CONFIG["entry_volume_mult"] = float(en.get("volume_mult", CONFIG["entry_volume_mult"]))
            CONFIG["REVERSAL_ENABLED"] = en.getboolean("reversal_enabled", CONFIG["REVERSAL_ENABLED"])

        if "tp_sl" in cp:
            ts = cp["tp_sl"]
            CONFIG["tp_bearish"] = float(ts.get("tp_bearish", CONFIG["tp_bearish"]))
            CONFIG["sl_bearish"] = float(ts.get("sl_bearish", CONFIG["sl_bearish"]))
            CONFIG["tp_bullish"] = float(ts.get("tp_bullish", CONFIG["tp_bullish"]))
            CONFIG["sl_bullish"] = float(ts.get("sl_bullish", CONFIG["sl_bullish"]))
            CONFIG["tp_lateral"] = float(ts.get("tp_lateral", CONFIG["tp_lateral"]))
            CONFIG["sl_lateral"] = float(ts.get("sl_lateral", CONFIG["sl_lateral"]))

        if "trend" in cp:
            tr = cp["trend"]
            CONFIG["breakout_enabled"] = tr.getboolean("breakout_enabled", CONFIG["breakout_enabled"])
            CONFIG["breakout_period"] = int(tr.get("breakout_period", CONFIG["breakout_period"]))
            CONFIG["breakout_trailing_atr_factor"] = float(tr.get("breakout_trailing_atr_factor", CONFIG["breakout_trailing_atr_factor"]))
            CONFIG["breakout_min_volume_ratio"] = float(tr.get("breakout_min_volume_ratio", CONFIG["breakout_min_volume_ratio"]))

            CONFIG["pullback_enabled"] = tr.getboolean("pullback_enabled", CONFIG["pullback_enabled"])
            CONFIG["pullback_ema_period"] = int(tr.get("pullback_ema_period", CONFIG["pullback_ema_period"]))
            CONFIG["pullback_max_distance_pct"] = float(tr.get("pullback_max_distance_pct", CONFIG["pullback_max_distance_pct"]))
            CONFIG["pullback_confirmation"] = tr.getboolean("pullback_confirmation", CONFIG["pullback_confirmation"])
            CONFIG["pullback_trailing_atr_factor"] = float(tr.get("pullback_trailing_atr_factor", CONFIG["pullback_trailing_atr_factor"]))
            CONFIG["pullback_min_volume_ratio"] = float(tr.get("pullback_min_volume_ratio", CONFIG["pullback_min_volume_ratio"]))

            CONFIG["momentum_enabled"] = tr.getboolean("momentum_enabled", CONFIG["momentum_enabled"])
            CONFIG["momentum_rsi_threshold"] = int(tr.get("momentum_rsi_threshold", CONFIG["momentum_rsi_threshold"]))
            CONFIG["momentum_trailing_atr_factor"] = float(tr.get("momentum_trailing_atr_factor", CONFIG["momentum_trailing_atr_factor"]))
            CONFIG["momentum_min_volume_ratio"] = float(tr.get("momentum_min_volume_ratio", CONFIG["momentum_min_volume_ratio"]))

            CONFIG["trend_capital_multiplier"] = float(tr.get("trend_capital_multiplier", CONFIG["trend_capital_multiplier"]))
            CONFIG["trend_use_trailing"] = tr.getboolean("trend_use_trailing", CONFIG["trend_use_trailing"])
            CONFIG["tp_trend"] = float(tr.get("tp_trend", CONFIG["tp_trend"]))
            CONFIG["trend_trailing_activation_pct"] = float(tr.get("trend_trailing_activation_pct", CONFIG["trend_trailing_activation_pct"]))

    validate_config()
    Auditoria.configurar()

    try:
        print(f"{Fore.CYAN}🔌 Conectando à Binance...{Fore.WHITE}")
        exchange = ccxt.binance({
            'apiKey': CONFIG["API_KEY"] or '',
            'secret': CONFIG["SECRET"] or '',
            'enableRateLimit': True,
            'options': {'adjustForTimeDifference': True},
        })
        with exchange_lock:
            exchange.load_markets()
        print(f"{Fore.GREEN}✅ Conectado!{Fore.WHITE}")
        definir_status(f"Conectado: {CONFIG['SYMBOL']}", "SUCESSO")
    except Exception as e:
        print(f"❌ Erro Crítico ao conectar: {e}")
        sys.exit(1)

    trading_engine = TradingEngine(dict(CONFIG), exchange, state_lock, shared_state)

    print(f"{Fore.CYAN}📥 Carregando candles de 15m e 1h...{Fore.WHITE}")
    if trading_engine.regime_detector.carregar_candles_tf_superior(exchange, CONFIG["SYMBOL"], limite=250):
        regime_inicial = trading_engine.regime_detector.get_regime()
        definir_status(f"Regime inicial detectado: {regime_inicial}", "SUCESSO")
        with state_lock:
            shared_state["current_regime"] = regime_inicial or "DESCONHECIDO"
    else:
        definir_status("Não foi possível carregar candles superiores. Regime ficará indefinido até warmup.", "AVISO")

    carregar_estado_disco()

    try:
        with exchange_lock:
            ohlcv = exchange.fetch_ohlcv(CONFIG["SYMBOL"], timeframe=CONFIG["TIMEFRAME"], limit=200)
        for k in ohlcv:
            candle = {'t': k[0], 'o': k[1], 'h': k[2], 'l': k[3], 'c': k[4], 'v': k[5]}
            trading_engine.on_candle(candle)
    except Exception as e:
        logging.warning(f"Erro ao buscar dados históricos: {e}")

    if trading_engine and trading_engine.position:
        symbol = CONFIG["SYMBOL"]
        base_currency = symbol.split('/')[0]
        try:
            with exchange_lock:
                real_balance = exchange.fetch_balance()[base_currency]['free']
            expected_qty = trading_engine.position["totalQty"]
            if real_balance < expected_qty * 0.9:
                logging.warning(f"Saldo real ({real_balance}) muito menor que o esperado ({expected_qty}). Resetando posição.")
                trading_engine.position = None
                trading_engine.cash = CONFIG["CAPITAL_TOTAL"]
                with state_lock:
                    shared_state["em_operacao"] = False
                    shared_state["marcha"] = "AGUARDANDO ENTRADA"
                salvar_estado_disco()
        except Exception as e:
            logging.warning(f"Erro ao reconciliar saldo: {e}")

# =============================================================================
# THREADS (motor, visual, ticker) - com pequenas correções na interface
# =============================================================================
def thread_motor():
    global trading_engine
    with engine_lock:
        if trading_engine and trading_engine.cash == CONFIG["CAPITAL_TOTAL"] and not trading_engine.position:
            inicializar_vault()
    with config_lock:
        timeframe = CONFIG["TIMEFRAME"]
        symbol = CONFIG["SYMBOL"]
    with engine_lock:
        last_processed = trading_engine.last_candle_time

    while True:
        try:
            if last_processed == 0:
                with exchange_lock:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=1)
                if not ohlcv:
                    time.sleep(5)
                    continue
                last_processed = ohlcv[0][0]
                with engine_lock:
                    trading_engine.last_candle_time = last_processed
                logging.info(f"Motor sincronizado. Último candle em {datetime.fromtimestamp(last_processed/1000)}")
                with state_lock:
                    shared_state["marcha"] = "AGUARDANDO ENTRADA"
                time.sleep(5)
                continue

            with exchange_lock:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=last_processed + 1, limit=1000)

            if len(ohlcv) == 0:
                if timeframe.endswith('m'):
                    candle_duration = int(timeframe[:-1]) * 60
                elif timeframe.endswith('h'):
                    candle_duration = int(timeframe[:-1]) * 3600
                else:
                    candle_duration = 3600
                now = time.time()
                next_candle_time = (last_processed / 1000) + candle_duration
                if now > next_candle_time + 10:
                    logging.warning("Atraso detectado: forçando sincronização.")
                    with exchange_lock:
                        ultimo_candle = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=1)
                    if ultimo_candle:
                        last_processed = ultimo_candle[0][0]
                        with engine_lock:
                            trading_engine.last_candle_time = last_processed
                        continue
                else:
                    sleep_time = max(0, next_candle_time - now)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    else:
                        time.sleep(5)
                continue

            for k in ohlcv:
                candle_time = k[0]
                if candle_time <= last_processed:
                    continue
                candle = {
                    't': candle_time,
                    'o': k[1],
                    'h': k[2],
                    'l': k[3],
                    'c': k[4],
                    'v': k[5]
                }
                with engine_lock:
                    trading_engine.on_candle(candle)
                last_processed = candle_time
                with engine_lock:
                    trading_engine.last_candle_time = last_processed

                if timeframe.endswith('m'):
                    candle_duration = int(timeframe[:-1]) * 60
                elif timeframe.endswith('h'):
                    candle_duration = int(timeframe[:-1]) * 3600
                else:
                    candle_duration = 3600
                now = time.time()
                next_candle_time = (candle_time / 1000) + candle_duration
                sleep_time = max(0, next_candle_time - now)
                if sleep_time > 0:
                    time.sleep(sleep_time)

            with engine_lock:
                ui_state = trading_engine.get_ui_state()
            with state_lock:
                shared_state.update(ui_state)
                shared_state["cash"] = ui_state["cash"]
                if trading_engine.position:
                    shared_state["marcha"] = "POSIÇÃO ATIVA"
                elif shared_state["marcha"] in ("INICIALIZANDO...", "RECUPERANDO..."):
                    shared_state["marcha"] = "AGUARDANDO ENTRADA"

            time.sleep(1)
        except Exception as e:
            with state_lock:
                shared_state["erros_consecutivos"] += 1
                cnt = shared_state["erros_consecutivos"]
            logging.error(f"Erro no motor ({cnt}/5): {e}")
            if cnt >= 5:
                panico_sistema("5 erros consecutivos no Motor.")
            time.sleep(5)

def thread_visual():
    while True:
        if not menu_ativo:
            os.system('cls' if os.name == 'nt' else 'clear')
            uptime = str(datetime.now() - inicio_bot).split('.')[0]
            with state_lock:
                s = dict(shared_state)
            with config_lock:
                cfg_symbol = CONFIG['SYMBOL']
            print(f"{Fore.CYAN}🐦‍🔥 SNIPER PHOENIX v8.3.0 - HÍBRIDO CORRIGIDO {Fore.WHITE}| UPTIME: {uptime}")
            print(f"{Fore.YELLOW}{'='*80}")
            print(f"  MERCADO: {Fore.GREEN}{cfg_symbol} {Fore.YELLOW}${s['preco']:.8f}")
            print(f"   REGIME: {Fore.CYAN}{s.get('current_regime', 'DESCONHECIDO')}  |  "
                  f"Drawdown ATH: {s.get('drawdown_pct', 0):.1f}%  |  RSI: {s.get('rsi', 50):.1f}")
            print(f"   STATUS: {Fore.CYAN}{s['marcha']}")

            cond = s.get('entry_conditions', {})
            total = len(cond)
            if total > 0:
                atendidas = sum(1 for v in cond.values() if v)
                bar_len = 10
                filled = int(bar_len * atendidas / total) if total > 0 else 0
                bar = '█' * filled + '░' * (bar_len - filled)
                print(f"   PLACAR ENTRADA: {atendidas}/{total}  [{bar}]")
                faltam = [k for k, v in cond.items() if not v]
                if faltam and atendidas < total:
                    print(f"   FALTAM: {', '.join(faltam)}")
                elif atendidas == total:
                    print(f"   ✅ TODAS AS CONDIÇÕES ATENDIDAS! AGUARDANDO ENTRADA...")
            else:
                print(f"   ⏳ AGUARDANDO SINAL DE ESTRATÉGIA...")
            print()

            if s["em_operacao"] and trading_engine and trading_engine.position:
                pos = trading_engine.position
                pnl = ((s['preco'] / pos['avgCost']) - 1) * 100
                perda = pos['totalCost'] - (s['preco'] * pos['totalQty'])
                cor_pnl = Fore.GREEN if pnl > 0 else Fore.RED
                print(f"{Fore.YELLOW}{'-'*80}")
                print(f" ESTRATÉGIA: {Fore.CYAN}{pos.get('strategy', 'DESCONHECIDA')}")
                print(f" P. MÉDIO: {Fore.WHITE}${pos['avgCost']:.8f} {cor_pnl}{pnl:+.2f}%  (${perda:.2f} USD)")
                if "tp_price" in pos and pos["tp_price"]:
                    print(f" TP: ${pos['tp_price']:.6f}  |  SL: ${pos['sl_price']:.6f}")
                elif pos.get("use_trailing", False):
                    if pos.get("trailing_active", False):
                        print(f" TRAILING STOP: ${pos['trailing_stop']:.6f} (ativo)")
                    else:
                        print(f" TRAILING STOP: aguardando ativação (lucro > {pos.get('trailing_activation_pct',0.5)}%)")
                print()

            print(f"{Fore.YELLOW}{'='*80}")
            print(f" LOG: {s['msg_log']}")
            print(f"{Fore.YELLOW}{'='*80}")
            print(f" {Fore.WHITE}[Ctrl+C] MENU | LOGS: {ARQUIVO_LOG_SISTEMA} | TRADES: {ARQUIVO_LOG_TRADES}")
        time.sleep(1)

def thread_ticker_ws():
    async def ws_loop():
        with config_lock:
            ultimo_symbol = CONFIG['SYMBOL']
        uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/', '').lower()}@ticker"
        while True:
            try:
                with config_lock:
                    cur = CONFIG['SYMBOL']
                if ultimo_symbol != cur:
                    ultimo_symbol = cur
                    uri = f"wss://stream.binance.com:9443/ws/{ultimo_symbol.replace('/', '').lower()}@ticker"
                async with websockets.connect(uri) as ws:
                    while True:
                        with config_lock:
                            cur = CONFIG['SYMBOL']
                        if ultimo_symbol != cur:
                            break
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            dados = json.loads(msg)
                            current_price = float(dados['c'])
                            with state_lock:
                                shared_state["preco"] = current_price
                                if shared_state.get("em_operacao", False) and trading_engine and trading_engine.position:
                                    pnl_pct = ((current_price / trading_engine.position['avgCost']) - 1) * 100
                                    shared_state["lucro_perc_atual"] = pnl_pct
                                    shared_state["perda_usd_atual"] = trading_engine.position['totalCost'] - (current_price * trading_engine.position['totalQty'])

                            if shared_state.get("em_operacao", False) and trading_engine and trading_engine.position:
                                with engine_lock:
                                    pos = trading_engine.position
                                    if pos and "tp_price" in pos and pos["tp_price"] and current_price >= pos["tp_price"]:
                                        trading_engine._close_position(current_price, int(time.time()*1000), "WS_TAKE_PROFIT")
                                    elif pos and "sl_price" in pos and current_price <= pos["sl_price"]:
                                        trading_engine._close_position(current_price, int(time.time()*1000), "WS_STOP_LOSS")
                                    elif pos and pos.get("use_trailing", False) and pos.get("trailing_active", False) and current_price <= pos["trailing_stop"]:
                                        trading_engine._close_position(current_price, int(time.time()*1000), "WS_TRAILING_STOP")
                        except asyncio.TimeoutError:
                            continue
            except Exception as e:
                logging.warning(f"WebSocket error: {e}")
                await asyncio.sleep(2)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_loop())

def acionar_menu(signum, frame):
    global menu_ativo
    menu_ativo = True
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{Fore.MAGENTA}╔══════════════════════════════════════╗")
    print(f"{Fore.MAGENTA}║    MENU DE CONTROLE SNIPER PHOENIX   ║")
    print(f"{Fore.MAGENTA}╠══════════════════════════════════════╣")
    print(f"{Fore.WHITE}║ 1. VOLTAR AO MONITORAMENTO           ║")
    print(f"{Fore.RED}║ 2. ENCERRAR (DESLIGAR BOT)           ║")
    print(f"{Fore.MAGENTA}╚══════════════════════════════════════╝")
    try:
        opt = input(f"\n{Fore.CYAN}➤ Escolha uma opção [1-2]: {Fore.WHITE}")
        if opt == '1':
            print(f"{Fore.GREEN}Retornando...")
            time.sleep(0.5)
        elif opt == '2':
            print(f"{Fore.RED}Salvando dados e encerrando...")
            salvar_estado_disco()
            sys.exit(0)
        else:
            print(f"{Fore.RED}Opção inválida.")
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{Fore.RED}Forçando saída...")
        salvar_estado_disco()
        sys.exit(0)
    except Exception as e:
        print(f"Erro no menu: {e}")
        time.sleep(1)
    menu_ativo = False

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    signal.signal(signal.SIGINT, acionar_menu)
    carregar_configuracoes()
    if exchange is None:
        print("Falha na inicialização da exchange. Encerrando.")
        sys.exit(1)
    threading.Thread(target=thread_motor, daemon=True, name="Motor").start()
    threading.Thread(target=thread_visual, daemon=True, name="Visual").start()
    threading.Thread(target=thread_ticker_ws, daemon=True, name="TickerWS").start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        salvar_estado_disco()
        sys.exit(0)