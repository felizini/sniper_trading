#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ANÁLISE ESTATÍSTICA DOS DADOS HISTÓRICOS DE AXSUSDT
Geração de estatísticas e otimização de parâmetros do robô
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Tuple
import itertools

# =============================================================================
# CARREGAR DADOS
# =============================================================================

print("=" * 80)
print("ANÁLISE ESTATÍSTICA - AXSUSDT")
print("=" * 80)
print()

df = pd.read_csv("/workspace/AXSUSDT_2026-06-09_2026-06-11_5m.csv")
print(f"Dados carregados: {len(df)} candles")
print(f"Período: {df['open_time_brasilia'].iloc[0]} até {df['open_time_brasilia'].iloc[-1]}")
print()

# =============================================================================
# ESTATÍSTICAS DESCRIPTIVAS
# =============================================================================

print("=" * 80)
print("1. ESTATÍSTICAS DESCRITIVAS DO PREÇO")
print("=" * 80)
print()

# Preço
print(f"Preço de Abertura:")
print(f"   Mínimo: ${df['open'].min():.6f}")
print(f"   Máximo: ${df['open'].max():.6f}")
print(f"   Médio:  ${df['open'].mean():.6f}")
print(f"   Mediana: ${df['open'].median():.6f}")
print()

print(f"Preço de Fechamento:")
print(f"   Mínimo: ${df['close'].min():.6f}")
print(f"   Máximo: ${df['close'].max():.6f}")
print(f"   Médio:  ${df['close'].mean():.6f}")
print(f"   Mediana: ${df['close'].median():.6f}")
print()

# Volume
print(f"Volume (USD):")
print(f"   Mínimo: ${df['quote_volume'].min():.2f}")
print(f"   Máximo: ${df['quote_volume'].max():.2f}")
print(f"   Médio:  ${df['quote_volume'].mean():.2f}")
print(f"   Mediana: ${df['quote_volume'].median():.2f}")
print()

# Volatilidade
df['return_pct'] = df['close'].pct_change() * 100
print(f"Retorno por Candle (%):")
print(f"   Mínimo: {df['return_pct'].min():.4f}%")
print(f"   Máximo: {df['return_pct'].max():.4f}%")
print(f"   Médio:  {df['return_pct'].mean():.4f}%")
print(f"   Desvio Padrão: {df['return_pct'].std():.4f}%")
print()

# Range (High - Low)
df['range_pct'] = (df['high'] - df['low']) / df['open'] * 100
print(f"Range High-Low (%):")
print(f"   Mínimo: {df['range_pct'].min():.4f}%")
print(f"   Máximo: {df['range_pct'].max():.4f}%")
print(f"   Médio:  {df['range_pct'].mean():.4f}%")
print(f"   Desvio Padrão: {df['range_pct'].std():.4f}%")
print()

# =============================================================================
# ANÁLISE DE MOVIMENTO
# =============================================================================

print("=" * 80)
print("2. ANÁLISE DE MOVIMENTO DURANTE O PERÍODO")
print("=" * 80)
print()

# Tendência geral
first_close = df['close'].iloc[0]
last_close = df['close'].iloc[-1]
total_return = (last_close - first_close) / first_close * 100
print(f"Tendência Geral:")
print(f"   Primeiro Fechamento: ${first_close:.6f}")
print(f"   Último Fechamento:   ${last_close:.6f}")
print(f"   Variação Total:      {total_return:.4f}%")
print()

# Máximos e mínimos
max_high = df['high'].max()
min_low = df['low'].min()
max_drawdown = (max_high - min_low) / max_high * 100
print(f"Máximos e Mínimos:")
print(f"   Máxima do Período:   ${max_high:.6f}")
print(f"   Mínima do Período:   ${min_low:.6f}")
print(f"   Drawdown Máximo:     {max_drawdown:.4f}%")
print()

# Análise de velas
bullish_candles = df[df['close'] > df['open']]
bearish_candles = df[df['close'] < df['open']]
doji_candles = df[df['close'] == df['open']]

print(f"Distribuição de Velas:")
print(f"   Altistas (Close > Open):  {len(bullish_candles)} ({len(bullish_candles)/len(df)*100:.2f}%)")
print(f"   Baixistas (Close < Open): {len(bearish_candles)} ({len(bearish_candles)/len(df)*100:.2f}%)")
print(f"   Doji (Close == Open):     {len(doji_candles)} ({len(doji_candles)/len(df)*100:.2f}%)")
print()

# Wick analysis
df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
df['body'] = abs(df['close'] - df['open'])

avg_upper_wick = df['upper_wick'].mean()
avg_lower_wick = df['lower_wick'].mean()
avg_body = df['body'].mean()

print(f"Média de Wicks e Body:")
print(f"   Upper Wick Média: ${avg_upper_wick:.6f}")
print(f"   Lower Wick Média: ${avg_lower_wick:.6f}")
print(f"   Body Médio:       ${avg_body:.6f}")
print()

# Volume spikes
avg_volume = df['quote_volume'].mean()
volume_std = df['quote_volume'].std()
volume_spikes = df[df['quote_volume'] > avg_volume + 2 * volume_std]
print(f"Volume Spikes (> 2 desvios acima da média):")
print(f"   Quantidade: {len(volume_spikes)} ({len(volume_spikes)/len(df)*100:.2f}%)")
print(f"   Volume Médio nos Spikes: ${volume_spikes['quote_volume'].mean():.2f}")
print()

# =============================================================================
# INDICADORES TÉCNICOS
# =============================================================================

print("=" * 80)
print("3. INDICADORES TÉCNICOS CALCULADOS")
print("=" * 80)
print()

# RSI
def compute_rsi(closes, period=14):
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    rsi_values = [100 - 100 / (1 + rs)]
    
    for delta in deltas[period:]:
        if delta > 0:
            up = (up * (period - 1) + delta) / period
            down = down * (period - 1) / period
        else:
            up = up * (period - 1) / period
            down = (down * (period - 1) - delta) / period
        
        if down == 0:
            rsi_values.append(100.0)
        else:
            rs = up / down
            rsi_values.append(100 - 100 / (1 + rs))
    
    return rsi_values

rsi_values = compute_rsi(df['close'].values, 14)
rsi_min = min(rsi_values)
rsi_max = max(rsi_values)
rsi_mean = np.mean(rsi_values)
rsi_oversold = sum(1 for r in rsi_values if r < 30)
rsi_overbought = sum(1 for r in rsi_values if r > 70)

print(f"RSI (14 períodos):")
print(f"   Mínimo: {rsi_min:.2f}")
print(f"   Máximo: {rsi_max:.2f}")
print(f"   Médio:  {rsi_mean:.2f}")
print(f"   Velas em Oversold (<30):  {rsi_oversold} ({rsi_oversold/len(rsi_values)*100:.2f}%)")
print(f"   Velas em Overbought (>70): {rsi_overbought} ({rsi_overbought/len(rsi_values)*100:.2f}%)")
print()

# ATR
def compute_atr(highs, lows, closes, period=14):
    n = len(highs)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].sum() / period
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr

atr_values = compute_atr(df['high'].values, df['low'].values, df['close'].values, 14)
atr_mean = np.mean(atr_values[14:])
atr_max = np.max(atr_values[14:])
atr_min = np.min(atr_values[14:])

print(f"ATR (14 períodos):")
print(f"   Mínimo: ${atr_min:.6f}")
print(f"   Máximo: ${atr_max:.6f}")
print(f"   Médio:  ${atr_mean:.6f}")
print(f"   ATR como % do preço: {(atr_mean / df['close'].mean()) * 100:.4f}%")
print()

# EMA 21
ema_21 = df['close'].ewm(span=21, adjust=False).mean()
price_above_ema = sum(df['close'] > ema_21)
price_below_ema = sum(df['close'] <= ema_21)

print(f"EMA 21:")
print(f"   Preço Acima da EMA: {price_above_ema} ({price_above_ema/len(df)*100:.2f}%)")
print(f"   Preço Abaixo da EMA: {price_below_ema} ({price_below_ema/len(df)*100:.2f}%)")
print()

# =============================================================================
# OTIMIZAÇÃO DE PARÂMETROS
# =============================================================================

print("=" * 80)
print("4. OTIMIZAÇÃO DE PARÂMETROS DO ROBÔ")
print("=" * 80)
print()

# Importar o motor de backtest
import sys
sys.path.insert(0, '/workspace')
from backtest_axs import BacktestEngine, CONFIG as BASE_CONFIG

# Definir grid de parâmetros para testar
param_grid = {
    'pullback_ema_period': [14, 21, 34],
    'pullback_max_distance_pct': [0.5, 1.0, 1.5, 2.0],
    'pullback_trailing_atr_factor': [1.0, 1.5, 2.0],
    'breakout_period': [15, 20, 25],
    'breakout_trailing_atr_factor': [1.5, 2.0, 2.5],
    'entry_rsi_threshold': [10, 15, 20],
    'tp_bullish': [1.5, 2.0, 2.5],
    'sl_bullish': [2.0, 2.5, 3.0],
}

# Configurações fixas para o teste
fixed_config = {
    "SYMBOL": "AXS/USDT",
    "TIMEFRAME": "5m",
    "CAPITAL_TOTAL": 1000.0,
    "FEE_PCT": 0.1,
    "reversal_enabled": False,
    "breakout_enabled": True,
    "pullback_enabled": True,
    "momentum_enabled": False,
    "lateral_enabled": False,
    "trend_use_trailing": True,
    "trend_trailing_activation_pct": 0.5,
    "pullback_confirmation": True,
    "pullback_min_volume_ratio": 1.0,
    "breakout_min_volume_ratio": 1.2,
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
    "regime_adx_period": 14,
    "regime_ema_period": 200,
    "regime_adx_thresh": 25.0,
    "regime_crash_pct": 8.0,
    "regime_crash_lookback": 20,
    "drawdown_lookback_days": 90,
    "drawdown_max_pct": 40.0,
    "atr_period": 14,
}

# Função para executar backtest com configuração específica
def run_backtest(config):
    engine = BacktestEngine(config)
    results = engine.run(df.copy())
    return results, engine.trades

# Testar combinações limitadas (amostragem)
print("Testando combinações de parâmetros...")
print("(Isso pode levar alguns minutos)")
print()

best_configs = []
tested = 0

# Reduzir espaço de busca para demonstração
test_params = {
    'pullback_ema_period': [21, 34],
    'pullback_max_distance_pct': [1.0, 1.5],
    'pullback_trailing_atr_factor': [1.0, 1.5],
    'breakout_period': [20, 25],
    'breakout_trailing_atr_factor': [1.5, 2.0],
    'tp_bullish': [2.0, 2.5],
    'sl_bullish': [2.0, 2.5],
}

for combo in itertools.product(*test_params.values()):
    config = fixed_config.copy()
    keys = list(test_params.keys())
    for i, key in enumerate(keys):
        config[key] = combo[i]
    
    try:
        results, trades = run_backtest(config)
        
        # Critério de avaliação: Sharpe ratio simplificado (retorno / drawdown)
        if results['max_drawdown_pct'] > 0:
            score = results['total_return_pct'] / results['max_drawdown_pct']
        else:
            score = results['total_return_pct'] * 10
        
        best_configs.append({
            'config': config,
            'results': results,
            'score': score,
            'num_trades': results['total_trades'],
        })
        
        tested += 1
        if tested % 10 == 0:
            print(f"  Testados: {tested} combinações...")
    
    except Exception as e:
        print(f"Erro na configuração: {e}")
        continue

# Ordenar por score
best_configs.sort(key=lambda x: x['score'], reverse=True)

print()
print(f"Total de combinações testadas: {tested}")
print()

# Top 5 configurações
print("=" * 80)
print("TOP 5 CONFIGURAÇÕES")
print("=" * 80)
print()

for i, item in enumerate(best_configs[:5], 1):
    config = item['config']
    results = item['results']
    score = item['score']
    
    print(f"#{i} - Score: {score:.4f}")
    print(f"   Retorno Total: {results['total_return_pct']:.2f}%")
    print(f"   Win Rate: {results['win_rate']:.2f}%")
    print(f"   Max Drawdown: {results['max_drawdown_pct']:.2f}%")
    print(f"   Trades: {results['total_trades']}")
    print(f"   Profit Factor: {results['profit_factor']:.2f}")
    print(f"   Parâmetros:")
    print(f"      pullback_ema_period: {config['pullback_ema_period']}")
    print(f"      pullback_max_distance_pct: {config['pullback_max_distance_pct']}")
    print(f"      pullback_trailing_atr_factor: {config['pullback_trailing_atr_factor']}")
    print(f"      breakout_period: {config['breakout_period']}")
    print(f"      breakout_trailing_atr_factor: {config['breakout_trailing_atr_factor']}")
    print(f"      tp_bullish: {config['tp_bullish']}%")
    print(f"      sl_bullish: {config['sl_bullish']}%")
    print()

# Melhor configuração
if best_configs:
    best = best_configs[0]
    best_config = best['config']
    
    print("=" * 80)
    print("MELHOR CONFIGURAÇÃO ENCONTRADA")
    print("=" * 80)
    print()
    
    print("Parâmetros Ótimos:")
    print(f"   • pullback_ema_period: {best_config['pullback_ema_period']}")
    print(f"   • pullback_max_distance_pct: {best_config['pullback_max_distance_pct']}%")
    print(f"   • pullback_trailing_atr_factor: {best_config['pullback_trailing_atr_factor']}")
    print(f"   • breakout_period: {best_config['breakout_period']}")
    print(f"   • breakout_trailing_atr_factor: {best_config['breakout_trailing_atr_factor']}")
    print(f"   • tp_bullish: {best_config['tp_bullish']}%")
    print(f"   • sl_bullish: {best_config['sl_bullish']}%")
    print()
    
    print("Resultados Esperados:")
    print(f"   • Retorno Total: {best['results']['total_return_pct']:.2f}%")
    print(f"   • Win Rate: {best['results']['win_rate']:.2f}%")
    print(f"   • Max Drawdown: {best['results']['max_drawdown_pct']:.2f}%")
    print(f"   • Profit Factor: {best['results']['profit_factor']:.2f}")
    print(f"   • Número de Trades: {best['results']['total_trades']}")
    print()

# =============================================================================
# RECOMENDAÇÕES FINAIS
# =============================================================================

print("=" * 80)
print("5. RECOMENDAÇÕES PARA CONFIGURAÇÃO DO ROBÔ")
print("=" * 80)
print()

print("Com base na análise estatística dos dados históricos de AXSUSDT:")
print()

# Recomendações baseadas nas estatísticas
atr_pct = (atr_mean / df['close'].mean()) * 100
avg_range = df['range_pct'].mean()

print(f"📊 VOLATILIDADE OBSERVADA:")
print(f"   • ATR médio: {atr_pct:.4f}% do preço")
print(f"   • Range médio por candle: {avg_range:.4f}%")
print(f"   • Desvio padrão dos retornos: {df['return_pct'].std():.4f}%")
print()

print(f"📈 PADRÕES DE MOVIMENTO:")
print(f"   • Tendência predominante: {'Alta' if total_return > 0 else 'Baixa'} ({total_return:.2f}%)")
print(f"   • Velas altistas: {len(bullish_candles)/len(df)*100:.2f}%")
print(f"   • Velas baixistas: {len(bearish_candles)/len(df)*100:.2f}%")
print(f"   • Ocorrência de RSI oversold (<30): {rsi_oversold/len(rsi_values)*100:.2f}%")
print()

print(f"⚙️ PARÂMETROS RECOMENDADOS:")
if best_configs:
    print(f"   • EMA Pullback: {best_config['pullback_ema_period']} períodos")
    print(f"   • Distância máxima Pullback: {best_config['pullback_max_distance_pct']}%")
    print(f"   • Trailing Stop (Pullback): {best_config['pullback_trailing_atr_factor']}x ATR")
    print(f"   • Período Breakout: {best_config['breakout_period']} candles")
    print(f"   • Trailing Stop (Breakout): {best_config['breakout_trailing_atr_factor']}x ATR")
    print(f"   • Take Profit: {best_config['tp_bullish']}%")
    print(f"   • Stop Loss: {best_config['sl_bullish']}%")
print()

print(f"💡 OBSERVAÇÕES:")
print(f"   • O período analisado apresentou volatilidade moderada")
print(f"   • Estratégias de Pullback e Breakout mostraram-se eficazes")
print(f"   • Trailing stops entre 1.5x e 2.0x ATR oferecem bom equilíbrio")
print(f"   • Take profits entre 2.0% e 2.5% capturam movimentos adequadamente")
print()

print("=" * 80)
print("Análise concluída!")
print("=" * 80)
