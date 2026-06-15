#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SNIPER PHOENIX v8.5.0 - V18 TREND RIDER (ENXUTO)
- Remove todos os elementos obsoletos (funções, chaves, atributos não utilizados)
- Mantém apenas o essencial para a estratégia V18
- ATR como SMA, trailing por ATR, sem drawdown filter e sem cooldown
- Transferências Funding ↔ Spot corrigidas
- Thread-safe com locks
- Pré-carga de 500 velas para melhor estabilidade da EMA200
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
# INDICADORES (APENAS OS USADOS)
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

# =============================================================================
# DETECTOR DE REGIME (APENAS PARA EXIBIÇÃO)
# =============================================================================

REGIME_BULLISH = "BULLISH"
REGIME_BEARISH = "BEARISH"

class RegimeDetector:
    def __init__(self, config: Dict):
        self.adx_period = config.get("regime_adx_period", 14)
        self.ema_period = config.get("regime_ema_period", 200)
        self.adx_thresh = config.get("regime_adx_thresh", 25.0)
        self.candles_15m = []
        self.candles_1h = []

    def carregar_candles_tf_superior(self, exchange, symbol, limite=250):
        try:
            ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=limite)
            self.candles_15m = [{'timestamp': k[0], 'open': k[1], 'high': k[2], 'low': k[3], 'close': k[4]} for k in ohlcv_15m]
            ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=limite)
            self.candles_1h = [{'timestamp': k[0], 'open': k[1], 'high': k[2], 'low': k[3], 'close': k[4]} for k in ohlcv_1h]
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
            candles_list.append({'timestamp': ts, 'open': open_p, 'high': high, 'low': low, 'close': close})

    def _compute_regime(self, candles: List) -> Optional[str]:
        if len(candles) < max(self.ema_period, self.adx_period * 2):
            return None
        highs = [c['high'] for c in candles]
        lows  = [c['low'] for c in candles]
        closes = [c['close'] for c in candles]
        adx, _, _ = compute_adx(highs, lows, closes, self.adx_period)
        ema = compute_ema(closes, self.ema_period)
        last_close = closes[-1]
        if adx < self.adx_thresh:
            return None
        return REGIME_BULLISH if last_close > ema else REGIME_BEARISH

    def update(self, ts_1m: int, open_1m: float, high_1m: float, low_1m: float, close_1m: float):
        dt = datetime.fromtimestamp(ts_1m / 1000)
        minute_15 = (dt.minute // 15) * 15
        ts_15m = int(datetime(dt.year, dt.month, dt.day, dt.hour, minute_15, 0).timestamp() * 1000)
        self._add_candle(self.candles_15m, ts_15m, open_1m, high_1m, low_1m, close_1m)
        ts_1h = int(datetime(dt.year, dt.month, dt.day, dt.hour, 0, 0).timestamp() * 1000)
        self._add_candle(self.candles_1h, ts_1h, open_1m, high_1m, low_1m, close_1m)

    def get_regime(self) -> Optional[str]:
        r15 = self._compute_regime(self.candles_15m)
        r1h = self._compute_regime(self.candles_1h)
        return r1h if r1h else r15

    def to_dict(self) -> Dict:
        return {'candles_15m': self.candles_15m[-200:], 'candles_1h': self.candles_1h[-200:]}

    def from_dict(self, data: Dict):
        self.candles_15m = data.get('candles_15m', [])
        self.candles_1h = data.get('candles_1h', [])

# =============================================================================
# MOTOR DE NEGOCIAÇÃO V18 (VERSÃO ENXUTA)
# =============================================================================
class TradingEngine:
    def __init__(self, config: Dict, exchange, state_lock, shared_state):
        self.config = config
        self.exchange = exchange
        self.state_lock = state_lock
        self.shared_state = shared_state

        self.cash = config["CAPITAL_TOTAL"]
        self.base_capital = config["CAPITAL_BASE"]

        self.position = None
        self.last_candle_time = 0

        self.closes = deque(maxlen=500)
        self.highs = deque(maxlen=500)
        self.lows = deque(maxlen=500)

        self.rsi_period = config.get("entry_rsi_period", 14)
        self.rsi_value = 50.0
        self.atr_period = config.get("atr_period", 14)
        self.atr_value = 0.0
        self.last_close_for_atr = None
        self._tr_buffer = []

        self.ema_fast = config.get("v18_regime_ema_fast", 9)
        self.ema_slow = config.get("v18_regime_ema_slow", 21)
        self.ema_trend = config.get("v18_regime_ema_trend", 200)
        self.ema9 = 0.0
        self.ema21 = 0.0
        self.ema200 = 0.0
        self.adx = 0.0

        self.bias = 'NEUTRO'
        self.bias_conf = 0.0

        self.parametros = {
            'BULLISH': {
                'sl_mult': config.get("v18_bullish_sl_mult", 2.0),
                'trail_dist': config.get("v18_bullish_trail_dist", 3.5),
                'trail_limiar': config.get("v18_bullish_trail_limiar", 2.0),
                'adx_min': config.get("v18_bullish_adx_min", 20),
                'rsi_max': config.get("v18_bullish_rsi_max", 75),
            },
            'BEARISH': {
                'sl_mult': config.get("v18_bearish_sl_mult", 1.2),
                'trail_dist': config.get("v18_bearish_trail_dist", 1.5),
                'trail_limiar': config.get("v18_bearish_trail_limiar", 0.6),
                'adx_min': config.get("v18_bearish_adx_min", 15),
                'rsi_max': config.get("v18_bearish_rsi_max", 60),
            }
        }

        self.entry_conditions = {}
        self.entry_mode = None

    # ---------- Indicadores ----------
    def _update_rsi(self):
        if len(self.closes) >= self.rsi_period + 1:
            self.rsi_value = compute_rsi(list(self.closes), self.rsi_period)

    def _update_atr(self, high: float, low: float, close: float):
        if self.last_close_for_atr is None:
            self.last_close_for_atr = close
            return
        tr = max(high - low, abs(high - self.last_close_for_atr), abs(low - self.last_close_for_atr))
        self._tr_buffer.append(tr)
        if len(self._tr_buffer) > self.atr_period:
            self._tr_buffer.pop(0)
        if len(self._tr_buffer) == self.atr_period:
            self.atr_value = sum(self._tr_buffer) / self.atr_period
        self.last_close_for_atr = close

    def _update_emas(self):
        if len(self.closes) >= self.ema_trend:
            closes = list(self.closes)
            self.ema9 = compute_ema(closes, self.ema_fast)
            self.ema21 = compute_ema(closes, self.ema_slow)
            self.ema200 = compute_ema(closes, self.ema_trend)

    def _update_adx(self):
        if len(self.closes) >= 30:
            self.adx, _, _ = compute_adx(list(self.highs)[-30:], list(self.lows)[-30:], list(self.closes)[-30:], 14)

    # ---------- Regime ----------
    def _detectar_regime(self):
        if len(self.closes) < self.ema_trend or self.ema200 == 0:
            return 'NEUTRO', 0.0
        close = self.closes[-1]
        bull = (close > self.ema200) and (self.ema9 > self.ema21)
        bear = (close < self.ema200) and (self.ema9 < self.ema21)
        if bull:
            conf = min(100, ((close - self.ema200)/close*100 + (self.ema9 - self.ema21)/close*100) * 10)
            return 'BULLISH', conf
        if bear:
            conf = min(100, ((self.ema200 - close)/close*100 + (self.ema21 - self.ema9)/close*100) * 10)
            return 'BEARISH', conf
        return 'NEUTRO', 0.0

    # ---------- Entrada ----------
    def _verificar_entrada(self, params):
        if self.atr_value <= 0 or self.adx < params['adx_min'] or self.rsi_value > params['rsi_max']:
            return False, None
        close = self.closes[-1]
        prev_close = self.closes[-2] if len(self.closes) >= 2 else close
        if self.bias == 'BULLISH':
            cruz = (prev_close <= self.ema21 and close > self.ema21 and self.ema9 > self.ema21)
            pull = (self.lows[-1] <= self.ema9 * 1.002 and close > self.ema9 and self.ema9 > self.ema21)
            return (cruz or pull), 'CROSS_UP' if cruz else 'PULLBACK' if pull else None
        elif self.bias == 'BEARISH':
            cruz = (prev_close >= self.ema21 and close < self.ema21 and self.ema9 < self.ema21)
            pull = (self.highs[-1] >= self.ema9 * 0.998 and close < self.ema9 and self.ema9 < self.ema21)
            return (cruz or pull), 'CROSS_DOWN' if cruz else 'PULLBACK' if pull else None
        return False, None

    def _abrir_posicao(self, price, ts, tipo, regime):
        params = self.parametros[self.bias]
        sl = price - params['sl_mult'] * self.atr_value if self.bias == 'BULLISH' else price + params['sl_mult'] * self.atr_value
        risk = abs(sl - price)
        if risk <= 0:
            return False
        qty_raw = (self.base_capital * self.config.get("RISK_PER_TRADE", 0.01)) / risk
        try:
            qty = float(self.exchange.amount_to_precision(self.config['SYMBOL'], qty_raw))
        except:
            qty = max(0.000001, qty_raw)
        if qty <= 0 or self.cash < qty * price:
            return False
        ordem = comprar_com_vault(qty * price, motivo=f"Entrada {tipo} ({regime})", obs=f"estr={tipo} rsi={self.rsi_value:.1f} adx={self.adx:.1f}")
        if not ordem['ok']:
            return False
        self.cash -= ordem['c']
        self.position = {
            "avgCost": ordem['p'], "totalQty": ordem['v'], "totalCost": ordem['c'],
            "openTime": ts, "strategy": tipo, "regime": regime, "bias": self.bias,
            "use_trailing": True, "trailing_atr_factor": params['trail_dist'],
            "trailing_activation_pct": params['trail_limiar'], "trailing_active": False,
            "trailing_stop": sl, "max_price": ordem['p'], "sl_price": sl
        }
        with self.state_lock:
            self.shared_state.update({"em_operacao": True, "marcha": f"POSIÇÃO ATIVA [{tipo} - {regime}]", "preco_medio": ordem['p']})
        self.entry_conditions = {"Bias": self.bias, "ADX": f"{self.adx:.1f}", "RSI": f"{self.rsi_value:.1f}", "Tipo": tipo}
        salvar_estado_disco()
        return True

    def _fechar_posicao(self, price, ts, motivo):
        if not self.position:
            return False
        res = vender_quantidade(self.exchange, self.position["totalQty"], self.position["totalCost"], motivo, self.config, obs=f"estr={self.position['strategy']} price={price:.6f}")
        if res['ok']:
            self.cash += res['receita']
        else:
            logging.error(f"Falha na venda: {res.get('msg')}")
            return False
        self.position = None
        salvar_estado_disco()
        with self.state_lock:
            self.shared_state.update({"em_operacao": False, "marcha": "AGUARDANDO ENTRADA", "lucro_perc_atual": 0.0, "perda_usd_atual": 0.0})
        return True

    def _atualizar_trailing(self, high, low, close, ts):
        if not self.position or not self.position.get("use_trailing"):
            return
        params = self.parametros[self.bias]
        atr = max(self.atr_value, 0.001)
        if self.bias == 'BULLISH':
            if high > self.position["max_price"]:
                self.position["max_price"] = high
            if not self.position["trailing_active"] and (self.position["max_price"] - self.position["avgCost"]) >= params['trail_limiar'] * atr:
                self.position["trailing_active"] = True
                self.position["trailing_stop"] = self.position["max_price"] - params['trail_dist'] * atr
            elif self.position["trailing_active"]:
                new_stop = self.position["max_price"] - params['trail_dist'] * atr
                if new_stop > self.position["trailing_stop"]:
                    self.position["trailing_stop"] = new_stop
                if low <= self.position["trailing_stop"]:
                    self._fechar_posicao(self.position["trailing_stop"], ts, "TRAILING_STOP")
        else:  # BEARISH
            if close < self.position["max_price"] or self.position["max_price"] == self.position["avgCost"]:
                if close < self.position["max_price"]:
                    self.position["max_price"] = close
            if not self.position["trailing_active"] and (self.position["avgCost"] - self.position["max_price"]) >= params['trail_limiar'] * atr:
                self.position["trailing_active"] = True
                self.position["trailing_stop"] = self.position["max_price"] + params['trail_dist'] * atr
            elif self.position["trailing_active"]:
                new_stop = self.position["max_price"] + params['trail_dist'] * atr
                if new_stop < self.position["trailing_stop"]:
                    self.position["trailing_stop"] = new_stop
                if high >= self.position["trailing_stop"]:
                    self._fechar_posicao(self.position["trailing_stop"], ts, "TRAILING_STOP")

    # ---------- Processamento de candle ----------
    def on_candle(self, candle: Dict):
        with engine_lock:
            ts, open_p, high, low, close, vol = candle['t'], candle['o'], candle['h'], candle['l'], candle['c'], candle['v']
            self.closes.append(close)
            self.highs.append(high)
            self.lows.append(low)
            self.last_candle_time = ts

            self._update_rsi()
            self._update_atr(high, low, close)
            self._update_emas()
            self._update_adx()

            novo_bias, conf = self._detectar_regime()
            if novo_bias != self.bias and novo_bias != 'NEUTRO':
                logging.info(f"Mudança de regime: {self.bias} -> {novo_bias} ({conf:.1f}%)")
            if self.position and novo_bias != 'NEUTRO' and novo_bias != self.bias:
                self._fechar_posicao(close, ts, "MUDANCA_REGIME")
                self.bias = novo_bias
                self.bias_conf = conf
                return
            if novo_bias != 'NEUTRO':
                self.bias = novo_bias
                self.bias_conf = conf

            if self.position:
                if self.bias == 'BULLISH' and low <= self.position["sl_price"]:
                    self._fechar_posicao(self.position["sl_price"], ts, "STOP_LOSS")
                elif self.bias == 'BEARISH' and high >= self.position["sl_price"]:
                    self._fechar_posicao(self.position["sl_price"], ts, "STOP_LOSS")
                elif self.position.get("use_trailing"):
                    self._atualizar_trailing(high, low, close, ts)
                return

            if self.bias in ['BULLISH', 'BEARISH']:
                pode, tipo = self._verificar_entrada(self.parametros[self.bias])
                if pode:
                    self._abrir_posicao(close, ts, tipo, f"{self.bias}_REGIME")
            else:
                self.entry_conditions = {}

    # ---------- Estado ----------
    def get_state(self) -> Dict:
        return {
            "cash": self.cash, "position": self.position, "last_candle_time": self.last_candle_time,
            "rsi_value": self.rsi_value, "atr_value": self.atr_value, "last_close_for_atr": self.last_close_for_atr,
            "bias": self.bias, "bias_confidence": self.bias_conf, "entry_conditions": self.entry_conditions,
            "entry_mode": self.entry_mode, "closes": list(self.closes)[-100:], "highs": list(self.highs)[-100:],
            "lows": list(self.lows)[-100:], "_tr_buffer": self._tr_buffer,
            "ema9": self.ema9, "ema21": self.ema21, "ema200": self.ema200, "adx": self.adx
        }

    def from_dict(self, data: Dict):
        self.cash = data.get("cash", self.config["CAPITAL_TOTAL"])
        self.position = data.get("position")
        self.last_candle_time = data.get("last_candle_time", 0)
        self.rsi_value = data.get("rsi_value", 50.0)
        self.atr_value = data.get("atr_value", 0.0)
        self.last_close_for_atr = data.get("last_close_for_atr")
        self.bias = data.get("bias", 'NEUTRO')
        self.bias_conf = data.get("bias_confidence", 0.0)
        self.entry_conditions = data.get("entry_conditions", {})
        self.entry_mode = data.get("entry_mode")
        self.closes = deque(data.get("closes", []), maxlen=500)
        self.highs = deque(data.get("highs", []), maxlen=500)
        self.lows = deque(data.get("lows", []), maxlen=500)
        self._tr_buffer = data.get("_tr_buffer", [])
        self.ema9 = data.get("ema9", 0.0)
        self.ema21 = data.get("ema21", 0.0)
        self.ema200 = data.get("ema200", 0.0)
        self.adx = data.get("adx", 0.0)
        if len(self._tr_buffer) >= self.atr_period:
            self.atr_value = sum(self._tr_buffer) / self.atr_period

    def get_ui_state(self) -> Dict:
        return {
            "current_regime": self.bias, "bias": self.bias, "bias_confidence": self.bias_conf,
            "rsi": self.rsi_value, "adx": self.adx, "entry_conditions": self.entry_conditions,
            "cash": self.cash, "em_operacao": self.position is not None,
            "preco_medio": self.position["avgCost"] if self.position else 0.0,
            "position": self.position,
        }

# =============================================================================
# AUDITORIA (SIMPLIFICADA)
# =============================================================================
class Auditoria:
    @staticmethod
    def configurar():
        root = logging.getLogger()
        if not root.handlers:
            with config_lock:
                handler = logging.handlers.RotatingFileHandler(ARQUIVO_LOG_SISTEMA, maxBytes=5_242_880, backupCount=5, encoding='utf-8')
            handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            root.setLevel(logging.INFO)
            root.addHandler(handler)
        if not os.path.exists(ARQUIVO_LOG_TRADES):
            with open(ARQUIVO_LOG_TRADES, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(["DATA", "PAR", "TIPO", "PRECO", "QTD", "TOTAL_USD", "LUCRO_USD", "LUCRO_PERC", "OBS"])

    @staticmethod
    def log_sistema(msg, nivel="INFO"):
        getattr(logging, nivel.lower())(msg)

    @staticmethod
    def log_transacao(tipo, preco, qtd, total_usd, lucro_usd=0.0, lucro_perc=0.0, obs=""):
        with config_lock:
            symbol = CONFIG["SYMBOL"]
        linha = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, tipo, f"{preco:.8f}", f"{qtd:.8f}",
                 f"{total_usd:.2f}", f"{lucro_usd:.2f}", f"{lucro_perc:.2f}%", obs]
        with csv_lock:
            with open(ARQUIVO_LOG_TRADES, 'a', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(linha)

shared_state = {
    "preco": 0.0, "erros_consecutivos": 0, "marcha": "INICIALIZANDO...",
    "em_operacao": False, "preco_medio": 0.0, "lucro_perc_atual": 0.0, "perda_usd_atual": 0.0,
    "current_regime": "DESCONHECIDO", "rsi": 50.0, "entry_conditions": {}, "cash": 0.0, "msg_log": "Sistema Iniciado",
}

menu_ativo = False
exchange = None
trading_engine = None

CONFIG = {
    "API_KEY": "", "SECRET": "", "SYMBOL": "AXS/USDT", "MOEDA_BASE": "USDT",
    "CAPITAL_TOTAL": 100.0, "CAPITAL_BASE": 100.0, "FEE_PCT": 0.075,
    "TIMEFRAME": "5m", "atr_period": 14, "entry_rsi_period": 14,
    "v18_bullish_sl_mult": 2.0, "v18_bullish_trail_dist": 3.5, "v18_bullish_trail_limiar": 2.0,
    "v18_bullish_adx_min": 20, "v18_bullish_rsi_max": 75,
    "v18_bearish_sl_mult": 1.2, "v18_bearish_trail_dist": 1.5, "v18_bearish_trail_limiar": 0.6,
    "v18_bearish_adx_min": 15, "v18_bearish_rsi_max": 60,
    "v18_regime_ema_fast": 9, "v18_regime_ema_slow": 21, "v18_regime_ema_trend": 200,
    "RISK_PER_TRADE": 0.01,
    "regime_adx_period": 14, "regime_ema_period": 200, "regime_adx_thresh": 25.0,
}

# =============================================================================
# FUNÇÕES DE ORDEM (APENAS O ESSENCIAL)
# =============================================================================
def definir_status(msg, tipo="INFO"):
    hora = datetime.now().strftime("%H:%M:%S")
    cor = {"SUCESSO": Fore.GREEN, "ERRO": Fore.RED, "AVISO": Fore.YELLOW}.get(tipo, Fore.CYAN)
    with state_lock:
        shared_state["msg_log"] = f"{Fore.WHITE}[{hora}] {cor}{msg}"
    if tipo == "ERRO":
        Auditoria.log_sistema(msg, "ERRO")

def transferir_para_spot(valor: float) -> bool:
    moeda = CONFIG["MOEDA_BASE"]
    try:
        with exchange_lock:
            exchange.transfer(moeda, valor, 'funding', 'spot')
        return True
    except Exception as e:
        definir_status(f"Erro transferência Fundos→Spot: {e}", "ERRO")
        return False

def recolher_para_fundos() -> bool:
    moeda = CONFIG["MOEDA_BASE"]
    try:
        with exchange_lock:
            bal = exchange.fetch_balance()
            saldo = bal.get(moeda, {}).get('free', 0)
        if saldo > 0.5:
            with exchange_lock:
                exchange.transfer(moeda, saldo, 'spot', 'funding')
            Auditoria.log_sistema(f"VAULT: ${saldo:.2f} recolhido para Fundos", "INFO")
        return True
    except Exception as e:
        definir_status(f"Erro recolher para Fundos: {e}", "ERRO")
        return False

def comprar_com_vault(valor_usd: float, motivo: str = "", obs: str = "") -> dict:
    moeda = CONFIG["MOEDA_BASE"]
    symbol = CONFIG["SYMBOL"]
    try:
        with exchange_lock:
            bal_funding = exchange.fetch_balance({'type': 'funding'})
            if bal_funding.get(moeda, {}).get('free', 0) < valor_usd:
                return {'ok': False, 'msg': 'Saldo Funding insuficiente'}
        if not transferir_para_spot(valor_usd):
            return {'ok': False, 'msg': 'Falha na transferência Fundos→Spot'}
        with exchange_lock:
            ordem = exchange.create_order(symbol, 'market', 'buy', None, params={'quoteOrderQty': exchange.cost_to_precision(symbol, valor_usd)})
        preco = float(ordem.get('average') or ordem.get('price') or 0)
        res = {'ok': True, 'p': preco, 'v': float(ordem['amount']), 'c': float(ordem['cost'])}
        Auditoria.log_transacao("COMPRA", res['p'], res['v'], res['c'], obs=obs)
        return res
    except Exception as e:
        logging.error(f"Erro Compra: {e}")
        return {'ok': False, 'msg': str(e)}

def vender_quantidade(exchange, qtd, custo, motivo, config, obs=""):
    symbol = config["SYMBOL"]
    try:
        base_currency = symbol.split('/')[0]
        with exchange_lock:
            real_balance = exchange.fetch_balance()[base_currency]['free']
        qtd = min(qtd, real_balance)
        if qtd <= 0:
            return {'ok': False, 'msg': 'Saldo zero'}
        with exchange_lock:
            market = exchange.market(symbol)
            if qtd < market['limits']['amount']['min']:
                return {'ok': False, 'msg': f'Qtd abaixo do mínimo'}
            qtd_fmt = exchange.amount_to_precision(symbol, qtd)
            ordem = exchange.create_order(symbol, 'market', 'sell', qtd_fmt)
            preco = float(ordem.get('average') or ordem.get('price') or 0)
            receita = float(ordem.get('cost') or 0)
        pnl = receita - custo
        pnl_pct = (pnl / custo) * 100 if custo > 0 else 0
        Auditoria.log_transacao(motivo, preco, qtd, receita, lucro_usd=pnl, lucro_perc=pnl_pct, obs=obs)
        # Transferir para Funding
        moeda = config["MOEDA_BASE"]
        if receita > 0.5:
            try:
                with exchange_lock:
                    exchange.transfer(moeda, receita, 'spot', 'funding')
            except Exception as e:
                logging.error(f"Erro transferência pós-venda: {e}")
        return {'ok': True, 'preco': preco, 'receita': receita, 'pnl': pnl, 'pnl_pct': pnl_pct}
    except Exception as e:
        logging.error(f"Erro venda: {e}")
        return {'ok': False, 'msg': str(e)}

def inicializar_vault():
    definir_status("Auditando Vault...", "INFO")
    recolher_para_fundos()
    try:
        with exchange_lock:
            bal = exchange.fetch_balance({'type': 'funding'})
            saldo = bal.get(CONFIG["MOEDA_BASE"], {}).get('free', 0)
        definir_status(f"Vault: ${saldo:.2f}", "SUCESSO" if saldo >= 100 else "AVISO")
    except Exception as e:
        definir_status(f"Erro Vault: {e}", "ERRO")

def salvar_estado_disco():
    try:
        with engine_lock, state_lock:
            dados = {"schema_version": 47, "em_operacao": shared_state["em_operacao"],
                     "symbol": CONFIG["SYMBOL"], "motor": trading_engine.get_state() if trading_engine else {},
                     "timestamp": str(datetime.now())}
        with open(ARQUIVO_ESTADO, "w") as f:
            json.dump(dados, f, indent=4, cls=NumpyEncoder)
    except Exception as e:
        logging.error(f"Erro salvar estado: {e}")

def carregar_estado_disco():
    if not os.path.exists(ARQUIVO_ESTADO):
        return False
    try:
        with open(ARQUIVO_ESTADO) as f:
            dados = json.load(f)
        if dados.get("em_operacao") and dados.get("symbol") == CONFIG["SYMBOL"] and trading_engine:
            with state_lock:
                shared_state["em_operacao"] = True
                shared_state["marcha"] = "RECUPERANDO..."
            trading_engine.from_dict(dados["motor"])
            if trading_engine.position:
                ui = trading_engine.get_ui_state()
                with state_lock:
                    shared_state.update(ui)
                    shared_state["em_operacao"] = True
            return True
    except Exception as e:
        Auditoria.log_sistema(f"Erro ler save: {e}", "ERRO")
    return False

def carregar_configuracoes():
    global exchange, trading_engine
    if os.path.exists("config.ini"):
        cp = configparser.ConfigParser()
        cp.read("config.ini")
        if "binance" in cp:
            CONFIG["API_KEY"] = cp.get("binance", "api_key", fallback="")
            CONFIG["SECRET"]  = cp.get("binance", "secret", fallback="")
        if "mercado" in cp:
            mg = cp["mercado"]
            CONFIG["SYMBOL"] = mg.get("symbol", CONFIG["SYMBOL"])
            CONFIG["MOEDA_BASE"] = CONFIG["SYMBOL"].split('/')[1] if '/' in CONFIG["SYMBOL"] else "USDT"
            CONFIG["CAPITAL_TOTAL"] = float(mg.get("capital_total", CONFIG["CAPITAL_TOTAL"]))
            CONFIG["CAPITAL_BASE"] = float(mg.get("capital_base", CONFIG["CAPITAL_BASE"]))
        if "v18_params" in cp:
            v18 = cp["v18_params"]
            for k in ["v18_bullish_sl_mult", "v18_bullish_trail_dist", "v18_bullish_trail_limiar",
                      "v18_bullish_adx_min", "v18_bullish_rsi_max", "v18_bearish_sl_mult",
                      "v18_bearish_trail_dist", "v18_bearish_trail_limiar", "v18_bearish_adx_min",
                      "v18_bearish_rsi_max", "v18_regime_ema_fast", "v18_regime_ema_slow",
                      "v18_regime_ema_trend"]:
                if k in v18:
                    CONFIG[k] = float(v18[k]) if "mult" in k or "dist" in k or "limiar" in k else int(v18[k])
        if "risk" in cp:
            CONFIG["RISK_PER_TRADE"] = float(cp["risk"].get("risk_per_trade", 0.01))
    try:
        print(f"{Fore.CYAN}🔌 Conectando à Binance...{Fore.WHITE}")
        exchange = ccxt.binance({'apiKey': CONFIG["API_KEY"], 'secret': CONFIG["SECRET"], 'enableRateLimit': True})
        exchange.load_markets()
        print(f"{Fore.GREEN}✅ Conectado à Binance{Fore.WHITE}")
    except Exception as e:
        print(f"❌ Erro conectar: {e}")
        sys.exit(1)

    trading_engine = TradingEngine(CONFIG, exchange, state_lock, shared_state)
    # Pré-carregar velas (500 para melhor estabilidade da EMA200)
    print(f"{Fore.CYAN}📥 Carregando candles...{Fore.WHITE}")
    try:
        ohlcv = exchange.fetch_ohlcv(CONFIG["SYMBOL"], timeframe=CONFIG["TIMEFRAME"], limit=500)
        for k in ohlcv:
            trading_engine.on_candle({'t': k[0], 'o': k[1], 'h': k[2], 'l': k[3], 'c': k[4], 'v': k[5]})
    except Exception as e:
        logging.warning(f"Erro pré-carga: {e}")
    carregar_estado_disco()
    if trading_engine.position:
        base = CONFIG["SYMBOL"].split('/')[0]
        with exchange_lock:
            real = exchange.fetch_balance()[base]['free']
        if real < trading_engine.position["totalQty"] * 0.9:
            trading_engine.position = None
            trading_engine.cash = CONFIG["CAPITAL_TOTAL"]
            with state_lock:
                shared_state["em_operacao"] = False
            salvar_estado_disco()

# =============================================================================
# THREADS (MOTOR, VISUAL, WEBSOCKET)
# =============================================================================
def thread_motor():
    global trading_engine
    with engine_lock:
        if trading_engine and not trading_engine.position:
            inicializar_vault()
    timeframe = CONFIG["TIMEFRAME"]
    symbol = CONFIG["SYMBOL"]
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
                time.sleep(5)
                continue
            with exchange_lock:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=last_processed+1, limit=1000)
            if not ohlcv:
                duracao = int(timeframe[:-1]) * 60 if timeframe.endswith('m') else 3600
                agora = time.time()
                prox_candle = (last_processed / 1000) + duracao
                if agora > prox_candle + 10:
                    with exchange_lock:
                        ultimo = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=1)
                    if ultimo:
                        last_processed = ultimo[0][0]
                        with engine_lock:
                            trading_engine.last_candle_time = last_processed
                    continue
                else:
                    time.sleep(max(0, prox_candle - agora) or 5)
                continue
            for k in ohlcv:
                if k[0] <= last_processed:
                    continue
                candle = {'t': k[0], 'o': k[1], 'h': k[2], 'l': k[3], 'c': k[4], 'v': k[5]}
                with engine_lock:
                    trading_engine.on_candle(candle)
                last_processed = k[0]
                with engine_lock:
                    trading_engine.last_candle_time = last_processed
                duracao = int(timeframe[:-1]) * 60 if timeframe.endswith('m') else 3600
                time.sleep(max(0, (last_processed/1000 + duracao) - time.time()))
            with engine_lock:
                ui = trading_engine.get_ui_state()
            with state_lock:
                shared_state.update(ui)
                shared_state["cash"] = ui["cash"]
                if trading_engine.position:
                    shared_state["marcha"] = "POSIÇÃO ATIVA"
                elif shared_state["marcha"] in ("INICIALIZANDO...", "RECUPERANDO..."):
                    shared_state["marcha"] = "AGUARDANDO ENTRADA"
            time.sleep(1)
        except Exception as e:
            with state_lock:
                shared_state["erros_consecutivos"] += 1
                cnt = shared_state["erros_consecutivos"]
            logging.error(f"Erro motor ({cnt}/5): {e}")
            if cnt >= 5:
                logging.critical("5 erros consecutivos no motor")
                sys.exit(1)
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
            print(f"{Fore.CYAN}🐦‍🔥 SNIPER PHOENIX v8.5.0 - V18 TREND RIDER (ENXUTO){Fore.WHITE} | UPTIME: {uptime}")
            print(f"{Fore.YELLOW}{'='*80}")
            print(f"  MERCADO: {Fore.GREEN}{cfg_symbol} {Fore.YELLOW}${s['preco']:.8f}")
            print(f"   REGIME: {Fore.CYAN}{s.get('current_regime', 'DESCONHECIDO')}  |  RSI: {s.get('rsi', 50):.1f}  |  ADX: {s.get('adx', 0):.1f}")
            print(f"   STATUS: {Fore.CYAN}{s['marcha']}")
            cond = s.get('entry_conditions', {})
            if cond:
                atendidas = sum(1 for v in cond.values() if v)
                print(f"   PLACAR ENTRADA: {atendidas}/{len(cond)}")
                if atendidas == len(cond):
                    print(f"   ✅ TODAS AS CONDIÇÕES ATENDIDAS! AGUARDANDO ENTRADA...")
            else:
                print(f"   ⏳ AGUARDANDO SINAL DE ESTRATÉGIA...")
            print()
            if s["em_operacao"] and trading_engine and trading_engine.position:
                pos = trading_engine.position
                pnl = ((s['preco'] / pos['avgCost']) - 1) * 100
                cor_pnl = Fore.GREEN if pnl > 0 else Fore.RED
                print(f"{Fore.YELLOW}{'-'*80}")
                print(f" ESTRATÉGIA: {Fore.CYAN}{pos.get('strategy', 'DESCONHECIDA')}")
                print(f" P. MÉDIO: {Fore.WHITE}${pos['avgCost']:.8f} {cor_pnl}{pnl:+.2f}%")
                if pos.get("use_trailing"):
                    if pos.get("trailing_active"):
                        print(f" TRAILING STOP: ${pos['trailing_stop']:.6f} (ativo)")
                    else:
                        print(f" TRAILING STOP: aguardando ativação (lucro > {pos.get('trailing_activation_pct',0):.1f}x ATR)")
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
                                if shared_state.get("em_operacao") and trading_engine and trading_engine.position:
                                    pnl_pct = ((current_price / trading_engine.position['avgCost']) - 1) * 100
                                    shared_state["lucro_perc_atual"] = pnl_pct
                                    shared_state["perda_usd_atual"] = trading_engine.position['totalCost'] - (current_price * trading_engine.position['totalQty'])
                            if shared_state.get("em_operacao") and trading_engine and trading_engine.position:
                                with engine_lock:
                                    pos = trading_engine.position
                                    if pos and "sl_price" in pos:
                                        if (trading_engine.bias == 'BULLISH' and current_price <= pos["sl_price"]) or \
                                           (trading_engine.bias == 'BEARISH' and current_price >= pos["sl_price"]):
                                            trading_engine._fechar_posicao(current_price, int(time.time()*1000), "WS_STOP_LOSS")
                                        elif pos.get("use_trailing") and pos.get("trailing_active"):
                                            if (trading_engine.bias == 'BULLISH' and current_price <= pos["trailing_stop"]) or \
                                               (trading_engine.bias == 'BEARISH' and current_price >= pos["trailing_stop"]):
                                                trading_engine._fechar_posicao(current_price, int(time.time()*1000), "WS_TRAILING_STOP")
                        except asyncio.TimeoutError:
                            continue
            except Exception as e:
                logging.warning(f"WebSocket erro: {e}")
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