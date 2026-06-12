#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BACKTEST - SNIPER PHOENIX v8.3.0
Simulação do robô usando dados históricos de AXSUSDT
"""

import pandas as pd
import numpy as np
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional, Tuple
import json

# =============================================================================
# CONFIGURAÇÕES DO BACKTEST
# =============================================================================
CONFIG = {
    "SYMBOL": "AXS/USDT",
    "TIMEFRAME": "5m",
    "CAPITAL_TOTAL": 1000.0,  # Capital inicial em USD
    "FEE_PCT": 0.1,  # Taxa da exchange (0.1%)
    
    # Configurações de entrada (reversão)
    "entry_rsi_period": 14,
    "entry_rsi_threshold": 15,
    "entry_use_exhaust": True,
    "entry_exhaust_lookback": 3,
    "entry_use_wick": True,
    "entry_wick_ratio": 0.25,
    "entry_use_confirm": True,
    "entry_confirm_lookback": 2,
    "entry_use_volume": True,
    "entry_volume_mult": 1.2,
    "entry_volume_lookback": 6,
    "entry_use_drawdown_filter": True,
    
    # Take Profit / Stop Loss
    "tp_bearish": 0.8,
    "sl_bearish": 3.0,
    "tp_bullish": 2.0,
    "sl_bullish": 3.0,
    "lateral_enabled": False,
    
    # Trend Following
    "breakout_enabled": True,
    "breakout_period": 20,
    "breakout_trailing_atr_factor": 2.0,
    "breakout_min_volume_ratio": 1.2,
    
    "pullback_enabled": True,
    "pullback_ema_period": 21,
    "pullback_max_distance_pct": 1.5,
    "pullback_confirmation": True,
    "pullback_trailing_atr_factor": 1.5,
    "pullback_min_volume_ratio": 1.0,
    
    "momentum_enabled": True,
    "momentum_rsi_threshold": 65,
    "momentum_trailing_atr_factor": 2.5,
    "momentum_min_volume_ratio": 1.2,
    
    # Regime Detection
    "regime_adx_period": 14,
    "regime_ema_period": 200,
    "regime_adx_thresh": 25.0,
    "regime_crash_pct": 8.0,
    "regime_crash_lookback": 20,
    
    # Drawdown Filter
    "drawdown_lookback_days": 90,
    "drawdown_max_pct": 40.0,
    
    # ATR
    "atr_period": 14,
}

# =============================================================================
# INDICADORES
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

def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    n = len(highs)
    if n < period:
        return 0.0
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].sum() / period
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    return atr[-1]

# =============================================================================
# DETECTOR DE REGIME
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
        self.candles = []

    def add_candle(self, ts: int, open_p: float, high: float, low: float, close: float):
        if self.candles and self.candles[-1]['timestamp'] == ts:
            self.candles[-1]['high'] = max(self.candles[-1]['high'], high)
            self.candles[-1]['low'] = min(self.candles[-1]['low'], low)
            self.candles[-1]['close'] = close
        else:
            self.candles.append({
                'timestamp': ts,
                'open': open_p,
                'high': high,
                'low': low,
                'close': close
            })
        # Manter apenas candles suficientes
        if len(self.candles) > 500:
            self.candles = self.candles[-500:]

    def get_regime(self) -> Optional[str]:
        if len(self.candles) < max(self.ema_period, self.adx_period * 2):
            return None
        
        highs = [c['high'] for c in self.candles]
        lows  = [c['low'] for c in self.candles]
        closes = [c['close'] for c in self.candles]
        
        adx, _, _ = compute_adx(highs, lows, closes, self.adx_period)
        ema = compute_ema(closes, self.ema_period)
        last_close = closes[-1]

        if adx < self.adx_thresh:
            return REGIME_LATERAL
        elif last_close > ema:
            return REGIME_BULLISH
        else:
            start = max(0, len(self.candles) - self.crash_lookback)
            ref_max = max(highs[start:])
            drop = (ref_max - last_close) / ref_max * 100 if ref_max > 0 else 0
            if drop >= self.crash_pct:
                return REGIME_CRASH
            else:
                return REGIME_BEARISH

# =============================================================================
# SIMULADOR DE BACKTEST
# =============================================================================

class BacktestEngine:
    def __init__(self, config: Dict):
        self.config = config
        self.cash = config["CAPITAL_TOTAL"]
        self.base_capital = config["CAPITAL_TOTAL"]
        self.fee = config["FEE_PCT"] / 100.0
        
        # Posição atual
        self.position = None
        
        # Histórico de preços
        self.closes = deque(maxlen=500)
        self.highs = deque(maxlen=500)
        self.lows = deque(maxlen=500)
        self.opens = deque(maxlen=500)
        self.volumes_usd = deque(maxlen=500)
        
        # Indicadores
        self.rsi_value = 50.0
        self.atr_value = 0.0
        self.atr_initialized = False
        self._tr_buffer = []
        self.last_close_for_atr = None
        
        # Detectores
        self.regime_detector = RegimeDetector(config)
        
        # Estatísticas
        self.trades = []
        self.equity_curve = []
        self.max_equity = self.cash
        self.max_drawdown = 0.0
        
        # Placar de condições
        self.entry_conditions = {}
        self.entry_mode = None
        
        # Variáveis auxiliares
        self._last_volume_ratio = 0.0
        self._last_wick = 0.0

    def _update_indicators(self, open_p: float, high: float, low: float, close: float, volume_usd: float):
        """Atualiza todos os indicadores"""
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)
        self.opens.append(open_p)
        self.volumes_usd.append(volume_usd)
        
        # RSI
        if len(self.closes) >= self.config.get("entry_rsi_period", 14) + 1:
            self.rsi_value = compute_rsi(list(self.closes), self.config.get("entry_rsi_period", 14))
        
        # ATR
        if self.last_close_for_atr is not None:
            tr = max(high - low, abs(high - self.last_close_for_atr), abs(low - self.last_close_for_atr))
            if not self.atr_initialized:
                self._tr_buffer.append(tr)
                if len(self._tr_buffer) == self.config.get("atr_period", 14):
                    self.atr_value = sum(self._tr_buffer) / self.config.get("atr_period", 14)
                    self.atr_initialized = True
                    self._tr_buffer = []
            else:
                period = self.config.get("atr_period", 14)
                self.atr_value = (self.atr_value * (period - 1) + tr) / period
        
        self.last_close_for_atr = close
        
        # Regime
        self.regime_detector.add_candle(
            int(datetime.now().timestamp() * 1000),  # Timestamp simulado
            open_p, high, low, close
        )

    def _check_volume_ratio(self, volume_usd: float, min_ratio: float) -> bool:
        if len(self.volumes_usd) < self.config.get("entry_volume_lookback", 6) + 1:
            return False
        lookback = self.config.get("entry_volume_lookback", 6)
        vols = list(self.volumes_usd)[-lookback-1:-1]
        if not vols:
            return False
        avg = sum(vols) / len(vols)
        self._last_volume_ratio = volume_usd / avg if avg > 0 else 0
        return volume_usd >= avg * min_ratio

    def _is_bullish_allowed(self) -> bool:
        regime = self.regime_detector.get_regime()
        if regime == REGIME_BULLISH:
            return True
        if regime == REGIME_LATERAL and self.config.get("lateral_enabled", False):
            return True
        return False

    def _is_bearish_allowed(self) -> bool:
        regime = self.regime_detector.get_regime()
        if regime == REGIME_BEARISH or regime == REGIME_CRASH:
            return True
        if regime == REGIME_LATERAL and self.config.get("lateral_enabled", False):
            return True
        return False

    def _check_reversal_long(self, open_p: float, high: float, low: float, close: float, volume_usd: float) -> Tuple[bool, Dict]:
        """Verifica condições para entrada Long por reversão"""
        conditions = {}
        
        # RSI oversold
        rsi_threshold = self.config.get("entry_rsi_threshold", 15)
        conditions["RSI Oversold"] = self.rsi_value <= rsi_threshold
        
        # Exaustão de baixa
        if self.config.get("entry_use_exhaust", True):
            lookback = self.config.get("entry_exhaust_lookback", 3)
            if len(self.closes) >= lookback + 1:
                recent_closes = list(self.closes)[-lookback-1:-1]
                conditions["Exaustão Baixa"] = all(recent_closes[i] > recent_closes[i+1] for i in range(len(recent_closes)-1))
            else:
                conditions["Exaustão Baixa"] = False
        
        # Wick inferior
        if self.config.get("entry_use_wick", True):
            wick_ratio = self.config.get("entry_wick_ratio", 0.25)
            body = close - open_p if close > open_p else open_p - close
            lower_wick = min(open_p, close) - low
            self._last_wick = lower_wick / body if body > 0 else 0
            conditions["Wick Inferior"] = self._last_wick >= wick_ratio
        else:
            conditions["Wick Inferior"] = True
        
        # Confirmação de alta
        if self.config.get("entry_use_confirm", True):
            lookback = self.config.get("entry_confirm_lookback", 2)
            if len(self.closes) >= lookback:
                prev_close = list(self.closes)[-lookback]
                conditions["Confirmação Alta"] = close > prev_close
            else:
                conditions["Confirmação Alta"] = False
        else:
            conditions["Confirmação Alta"] = True
        
        # Volume
        if self.config.get("entry_use_volume", True):
            mult = self.config.get("entry_volume_mult", 1.2)
            conditions["Volume"] = self._check_volume_ratio(volume_usd, mult)
        else:
            conditions["Volume"] = True
        
        # Drawdown filter
        if self.config.get("entry_use_drawdown_filter", True):
            # Simplificado: sempre permite no backtest
            conditions["Drawdown OK"] = True
        else:
            conditions["Drawdown OK"] = True
        
        all_met = all(conditions.values())
        return all_met, conditions

    def _check_breakout_long(self, open_p: float, high: float, low: float, close: float, volume_usd: float) -> Tuple[bool, Dict]:
        """Verifica condições para entrada Long por breakout"""
        conditions = {}
        
        period = self.config.get("breakout_period", 20)
        if len(self.closes) < period:
            return False, {}
        
        recent_highs = list(self.highs)[-period:]
        resistance = max(recent_highs[:-1])  # Máximo dos períodos anteriores
        
        # Rompimento
        conditions["Rompimento Resistência"] = close > resistance
        
        # Volume
        min_ratio = self.config.get("breakout_min_volume_ratio", 1.2)
        conditions["Volume Breakout"] = self._check_volume_ratio(volume_usd, min_ratio)
        
        # Regime bullish permitido
        conditions["Regime Bullish"] = self._is_bullish_allowed()
        
        all_met = all(conditions.values())
        return all_met, conditions

    def _check_pullback_long(self, open_p: float, high: float, low: float, close: float, volume_usd: float) -> Tuple[bool, Dict]:
        """Verifica condições para entrada Long por pullback"""
        conditions = {}
        
        ema_period = self.config.get("pullback_ema_period", 21)
        if len(self.closes) < ema_period:
            return False, {}
        
        ema = compute_ema(list(self.closes), ema_period)
        
        # Pullback para EMA
        max_distance = self.config.get("pullback_max_distance_pct", 1.5) / 100
        distance = abs(close - ema) / ema if ema > 0 else 999
        conditions["Pullback EMA"] = distance <= max_distance and close >= ema
        
        # Confirmação de vela de alta
        if self.config.get("pullback_confirmation", True):
            conditions["Vela Alta"] = close > open_p
        else:
            conditions["Vela Alta"] = True
        
        # Volume
        min_ratio = self.config.get("pullback_min_volume_ratio", 1.0)
        conditions["Volume Pullback"] = self._check_volume_ratio(volume_usd, min_ratio)
        
        # Regime bullish permitido
        conditions["Regime Bullish"] = self._is_bullish_allowed()
        
        all_met = all(conditions.values())
        return all_met, conditions

    def _check_momentum_long(self, open_p: float, high: float, low: float, close: float, volume_usd: float) -> Tuple[bool, Dict]:
        """Verifica condições para entrada Long por momentum"""
        conditions = {}
        
        # RSI acima do threshold
        rsi_threshold = self.config.get("momentum_rsi_threshold", 65)
        conditions["RSI Momentum"] = self.rsi_value >= rsi_threshold
        
        # Volume
        min_ratio = self.config.get("momentum_min_volume_ratio", 1.2)
        conditions["Volume Momentum"] = self._check_volume_ratio(volume_usd, min_ratio)
        
        # Regime bullish permitido
        conditions["Regime Bullish"] = self._is_bullish_allowed()
        
        all_met = all(conditions.values())
        return all_met, conditions

    def check_entry_signals(self, open_p: float, high: float, low: float, close: float, volume_usd: float) -> Optional[str]:
        """Verifica todos os sinais de entrada e retorna o modo de entrada"""
        if self.position is not None:
            return None
        
        # Verifica cada estratégia
        strategies = [
            ("Reversão", self.config.get("reversal_enabled", True), 
             lambda: self._check_reversal_long(open_p, high, low, close, volume_usd)),
            ("Breakout", self.config.get("breakout_enabled", False), 
             lambda: self._check_breakout_long(open_p, high, low, close, volume_usd)),
            ("Pullback", self.config.get("pullback_enabled", False), 
             lambda: self._check_pullback_long(open_p, high, low, close, volume_usd)),
            ("Momentum", self.config.get("momentum_enabled", False), 
             lambda: self._check_momentum_long(open_p, high, low, close, volume_usd)),
        ]
        
        for name, enabled, check_func in strategies:
            if enabled:
                met, conditions = check_func()
                if met:
                    self.entry_conditions = conditions
                    self.entry_mode = name
                    return name
        
        return None

    def execute_entry(self, strategy: str, open_p: float, high: float, low: float, close: float):
        """Executa entrada na posição"""
        if self.position is not None:
            return
        
        # Calcular quantidade baseada no capital
        capital_to_use = self.cash * 0.95  # Usa 95% do capital disponível
        quantity = capital_to_use / close
        
        # Definir TP e SL baseado na estratégia
        if strategy == "Reversão":
            tp_pct = self.config.get("tp_bullish", 2.0) / 100
            sl_pct = self.config.get("sl_bullish", 3.0) / 100
            use_trailing = False
        else:
            tp_pct = self.config.get("tp_trend", 3.0) / 100
            sl_pct = self.config.get("sl_bullish", 3.0) / 100
            use_trailing = self.config.get("trend_use_trailing", True)
        
        tp_price = close * (1 + tp_pct)
        sl_price = close * (1 - sl_pct)
        
        # Calcular trailing stop se aplicável
        trailing_atr_factor = {
            "Breakout": self.config.get("breakout_trailing_atr_factor", 2.0),
            "Pullback": self.config.get("pullback_trailing_atr_factor", 1.5),
            "Momentum": self.config.get("momentum_trailing_atr_factor", 2.5),
        }.get(strategy, 2.0)
        
        trailing_stop = close - (self.atr_value * trailing_atr_factor) if self.atr_initialized else sl_price
        
        self.position = {
            'strategy': strategy,
            'entry_price': close,
            'quantity': quantity,
            'avgCost': close,
            'totalQty': quantity,
            'totalCost': close * quantity,
            'tp_price': tp_price,
            'sl_price': sl_price,
            'use_trailing': use_trailing,
            'trailing_stop': trailing_stop,
            'trailing_active': False,
            'trailing_activation_pct': self.config.get("trend_trailing_activation_pct", 0.5),
            'max_profit_pct': 0.0,
        }

    def check_exit(self, high: float, low: float, close: float) -> Optional[str]:
        """Verifica condições de saída"""
        if self.position is None:
            return None
        
        pos = self.position
        pnl_pct = ((close / pos['avgCost']) - 1) * 100
        
        # Atualizar máximo lucro para trailing stop
        if pnl_pct > pos['max_profit_pct']:
            pos['max_profit_pct'] = pnl_pct
            
            # Ativar trailing stop se lucro > activation_pct
            if not pos['trailing_active'] and pnl_pct >= pos['trailing_activation_pct']:
                pos['trailing_active'] = True
                pos['trailing_stop'] = close - (self.atr_value * 2.0) if self.atr_initialized else pos['sl_price']
            
            # Atualizar trailing stop
            if pos['trailing_active'] and self.atr_initialized:
                new_trailing = close - (self.atr_value * 2.0)
                if new_trailing > pos['trailing_stop']:
                    pos['trailing_stop'] = new_trailing
        
        # Verificar Take Profit
        if high >= pos['tp_price']:
            return "TP"
        
        # Verificar Stop Loss
        if low <= pos['sl_price']:
            return "SL"
        
        # Verificar Trailing Stop
        if pos['trailing_active'] and low <= pos['trailing_stop']:
            return "Trailing"
        
        return None

    def execute_exit(self, exit_reason: str, close: float):
        """Executa saída da posição"""
        if self.position is None:
            return
        
        pos = self.position
        exit_value = pos['quantity'] * close * (1 - self.fee)  # Valor após taxa de venda
        entry_cost = pos['totalCost'] * (1 + self.fee)  # Custo inicial com taxa de compra
        
        profit = exit_value - entry_cost
        profit_pct = ((close / pos['entry_price']) - 1) * 100
        
        # Registrar trade
        trade = {
            'strategy': pos['strategy'],
            'entry_price': pos['entry_price'],
            'exit_price': close,
            'quantity': pos['quantity'],
            'profit_usd': profit,
            'profit_pct': profit_pct,
            'exit_reason': exit_reason,
        }
        self.trades.append(trade)
        
        # Atualizar cash
        self.cash += exit_value
        self.position = None

    def update_equity(self, close: float):
        """Atualiza curva de equity e drawdown"""
        if self.position:
            equity = self.cash + (self.position['quantity'] * close)
        else:
            equity = self.cash
        
        self.equity_curve.append(equity)
        
        if equity > self.max_equity:
            self.max_equity = equity
        
        drawdown = (self.max_equity - equity) / self.max_equity * 100
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
        
        return equity

    def run(self, df: pd.DataFrame) -> Dict:
        """Executa o backtest"""
        print(f"Iniciando backtest com {len(df)} candles...")
        print(f"Período: {df['open_time_brasilia'].iloc[0]} até {df['open_time_brasilia'].iloc[-1]}")
        print(f"Capital inicial: ${self.cash:.2f}")
        print("-" * 80)
        
        trades_by_strategy = {}
        
        for idx, row in df.iterrows():
            open_p = row['open']
            high = row['high']
            low = row['low']
            close = row['close']
            volume_usd = row['quote_volume']
            
            # Atualizar indicadores
            self._update_indicators(open_p, high, low, close, volume_usd)
            
            # Verificar saída se estiver em posição
            if self.position:
                exit_reason = self.check_exit(high, low, close)
                if exit_reason:
                    self.execute_exit(exit_reason, close)
                    if self.position is None:  # Confirmou saída
                        strat = self.trades[-1]['strategy']
                        if strat not in trades_by_strategy:
                            trades_by_strategy[strat] = []
                        trades_by_strategy[strat].append(self.trades[-1])
            
            # Verificar entrada se não estiver em posição
            if self.position is None and len(self.closes) > 50:  # Warmup
                strategy = self.check_entry_signals(open_p, high, low, close, volume_usd)
                if strategy:
                    self.execute_entry(strategy, open_p, high, low, close)
            
            # Atualizar equity
            self.update_equity(close)
        
        # Fechar posição aberta no final (se houver)
        if self.position:
            last_close = df['close'].iloc[-1]
            self.execute_exit("FIM_BACKTEST", last_close)
            strat = self.trades[-1]['strategy']
            if strat not in trades_by_strategy:
                trades_by_strategy[strat] = []
            trades_by_strategy[strat].append(self.trades[-1])
        
        # Calcular estatísticas finais
        results = self.calculate_results(trades_by_strategy)
        return results

    def calculate_results(self, trades_by_strategy: Dict) -> Dict:
        """Calcula estatísticas finais do backtest"""
        total_trades = len(self.trades)
        winning_trades = [t for t in self.trades if t['profit_usd'] > 0]
        losing_trades = [t for t in self.trades if t['profit_usd'] <= 0]
        
        win_rate = len(winning_trades) / total_trades * 100 if total_trades > 0 else 0
        
        total_profit = sum(t['profit_usd'] for t in self.trades)
        gross_profit = sum(t['profit_usd'] for t in winning_trades) if winning_trades else 0
        gross_loss = abs(sum(t['profit_usd'] for t in losing_trades)) if losing_trades else 0
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        avg_win = gross_profit / len(winning_trades) if winning_trades else 0
        avg_loss = gross_loss / len(losing_trades) if losing_trades else 0
        
        final_equity = self.equity_curve[-1] if self.equity_curve else self.cash
        total_return = (final_equity - self.base_capital) / self.base_capital * 100
        
        # Estatísticas por estratégia
        strategy_stats = {}
        for strat, trades in trades_by_strategy.items():
            strat_wins = [t for t in trades if t['profit_usd'] > 0]
            strat_losses = [t for t in trades if t['profit_usd'] <= 0]
            strat_profit = sum(t['profit_usd'] for t in trades)
            
            strategy_stats[strat] = {
                'total_trades': len(trades),
                'wins': len(strat_wins),
                'losses': len(strat_losses),
                'win_rate': len(strat_wins) / len(trades) * 100 if trades else 0,
                'total_profit': strat_profit,
            }
        
        return {
            'total_trades': total_trades,
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': win_rate,
            'total_profit_usd': total_profit,
            'gross_profit': gross_profit,
            'gross_loss': gross_loss,
            'profit_factor': profit_factor,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'final_equity': final_equity,
            'total_return_pct': total_return,
            'max_drawdown_pct': self.max_drawdown,
            'strategy_stats': strategy_stats,
        }


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 80)
    print("BACKTEST - SNIPER PHOENIX v8.3.0")
    print("Simulação com dados históricos de AXSUSDT")
    print("=" * 80)
    print()
    
    # Carregar dados históricos
    csv_file = "/workspace/AXSUSDT_2026-06-09_2026-06-11_5m.csv"
    print(f"Carregando dados de: {csv_file}")
    
    df = pd.read_csv(csv_file)
    print(f"Dados carregados: {len(df)} candles")
    print(f"Colunas disponíveis: {list(df.columns)}")
    print()
    
    # Criar e executar backtest
    engine = BacktestEngine(CONFIG)
    results = engine.run(df)
    
    # Imprimir resultados
    print()
    print("=" * 80)
    print("RESULTADOS DO BACKTEST")
    print("=" * 80)
    print()
    
    print(f"📊 ESTATÍSTICAS GERAIS:")
    print(f"   Total de Trades:        {results['total_trades']}")
    print(f"   Trades Vencedores:      {results['winning_trades']}")
    print(f"   Trades Perdedores:      {results['losing_trades']}")
    print(f"   Win Rate:               {results['win_rate']:.2f}%")
    print()
    
    print(f"💰 RESULTADOS FINANCEIROS:")
    print(f"   Capital Inicial:        ${CONFIG['CAPITAL_TOTAL']:.2f}")
    print(f"   Capital Final:          ${results['final_equity']:.2f}")
    print(f"   Lucro Total:            ${results['total_profit_usd']:.2f}")
    print(f"   Retorno Total:          {results['total_return_pct']:.2f}%")
    print()
    
    print(f"📈 MÉTRICAS DE PERFORMANCE:")
    print(f"   Profit Factor:          {results['profit_factor']:.2f}")
    print(f"   Ganho Médio:            ${results['avg_win']:.2f}")
    print(f"   Perda Média:            ${results['avg_loss']:.2f}")
    print(f"   Max Drawdown:           {results['max_drawdown_pct']:.2f}%")
    print()
    
    print(f"📊 RESULTADOS POR ESTRATÉGIA:")
    for strat, stats in results['strategy_stats'].items():
        print(f"   {strat}:")
        print(f"      Trades: {stats['total_trades']} | Vitórias: {stats['wins']} | Derrotas: {stats['losses']}")
        print(f"      Win Rate: {stats['win_rate']:.2f}% | Lucro Total: ${stats['total_profit']:.2f}")
    
    print()
    print("=" * 80)
    
    # Salvar trades em CSV
    if engine.trades:
        trades_df = pd.DataFrame(engine.trades)
        trades_csv = "/workspace/backtest_trades_axs.csv"
        trades_df.to_csv(trades_csv, index=False)
        print(f"✅ Trades salvos em: {trades_csv}")
    
    # Salvar equity curve em CSV
    if engine.equity_curve:
        equity_df = pd.DataFrame({'equity': engine.equity_curve})
        equity_csv = "/workspace/backtest_equity_axs.csv"
        equity_df.to_csv(equity_csv, index=False)
        print(f"✅ Equity curve salvo em: {equity_csv}")
    
    print()
    print("Backtest concluído!")


if __name__ == "__main__":
    main()
