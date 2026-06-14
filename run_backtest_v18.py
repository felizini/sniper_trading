#!/usr/bin/env python3
"""Backtest simplificado da estratégia V18 Trend Rider Adaptativo"""

import pandas as pd
import numpy as np
import configparser
from datetime import datetime
import sys

def carregar_config(config_path='/workspace/config.ini'):
    """Carrega configurações do arquivo INI"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    
    flat_config = {}
    for section in config.sections():
        for key, value in config.items(section):
            try:
                if '.' in value:
                    flat_config[key] = float(value)
                elif value.lstrip('-').isdigit():
                    flat_config[key] = int(value)
                elif value.lower() in ['true', 'false']:
                    flat_config[key] = value.lower() == 'true'
                else:
                    flat_config[key] = value
            except:
                flat_config[key] = value
    
    return flat_config

def calcular_ema(series, period):
    """Calcula EMA"""
    return series.ewm(span=period, adjust=False).mean()

def calcular_atr(df, period=14):
    """Calcula ATR"""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    atr = true_range.rolling(window=period).mean()
    return atr

def calcular_rsi(series, period=14):
    """Calcula RSI"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calcular_adx(df, period=14):
    """Calcula ADX"""
    high = df['high']
    low = df['low']
    close = df['close']
    
    plus_dm = high.diff()
    minus_dm = -low.diff()
    
    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0)
    
    atr = calcular_atr(df, period)
    
    plus_di = 100 * pd.Series(plus_dm).rolling(window=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm).rolling(window=period).mean() / atr
    
    dx = 100 * np.abs(plus_di - minus_di) / (np.abs(plus_di + minus_di) + 1e-10)
    adx = dx.rolling(window=period).mean()
    
    return adx

def detectar_regime(row, ema9, ema21, ema200):
    """Detecta regime de mercado baseado em EMAs"""
    if pd.isna(ema9) or pd.isna(ema21) or pd.isna(ema200):
        return 'NEUTRO'
    
    # Regime BULLISH: EMA9 > EMA21 > EMA200
    if ema9 > ema21 and ema21 > ema200:
        return 'BULLISH'
    # Regime BEARISH: EMA9 < EMA21 < EMA200
    elif ema9 < ema21 and ema21 < ema200:
        return 'BEARISH'
    else:
        return 'NEUTRO'

def run_backtest(csv_file, capital_inicial=10000):
    """Executa backtest da estratégia V18 Trend Rider"""
    
    print(f"🔍 CARREGANDO DADOS: {csv_file}")
    print("=" * 80)
    
    # Carregar configurações
    config = carregar_config()
    print(f"📋 CONFIGURAÇÕES V18 TREND RIDER:")
    print(f"   Símbolo: {config.get('symbol', 'AXS/USDT')}")
    print(f"   Timeframe: {config.get('timeframe', '5m')}")
    print(f"   Capital: R$ {capital_inicial:,.2f}")
    print()
    
    # Parâmetros V18 do config
    bull_sl_mult = config.get('v18_bull_sl_multiplier', 2.0)
    bull_trail_mult = config.get('v18_bull_trail_multiplier', 3.5)
    bull_adx_min = config.get('v18_bull_adx_min', 20)
    bull_rsi_max = config.get('v18_bull_rsi_max', 75)
    
    bear_sl_mult = config.get('v18_bear_sl_multiplier', 1.2)
    bear_trail_mult = config.get('v18_bear_trail_multiplier', 1.5)
    bear_adx_min = config.get('v18_bear_adx_min', 15)
    bear_rsi_max = config.get('v18_bear_rsi_max', 60)
    
    # Carregar dados
    df = pd.read_csv(csv_file)
    
    # Normalizar colunas
    col_mapping = {
        'open_time_brasilia': 'timestamp',
        'open': 'open',
        'high': 'high',
        'low': 'low',
        'close': 'close',
        'volume': 'volume'
    }
    
    for old_name, new_name in col_mapping.items():
        if old_name in df.columns and new_name not in df.columns:
            df[new_name] = df[old_name]
    
    if df['timestamp'].dtype == 'object':
        df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    print(f"📊 DADOS CARREGADOS:")
    print(f"   Período: {df['timestamp'].min()} até {df['timestamp'].max()}")
    print(f"   Total de candles: {len(df)}")
    print(f"   Preço inicial: ${df['close'].iloc[0]:.6f}")
    print(f"   Preço final: ${df['close'].iloc[-1]:.6f}")
    print(f"   Variação Buy&Hold: {((df['close'].iloc[-1] / df['close'].iloc[0]) - 1) * 100:.2f}%")
    print()
    
    # Calcular indicadores
    print("📈 CALCULANDO INDICADORES...")
    df['ema9'] = calcular_ema(df['close'], 9)
    df['ema21'] = calcular_ema(df['close'], 21)
    df['ema200'] = calcular_ema(df['close'], 200)
    df['atr'] = calcular_atr(df, 14)
    df['rsi'] = calcular_rsi(df['close'], 14)
    df['adx'] = calcular_adx(df, 14)
    
    # Detectar regime
    df['regime'] = df.apply(lambda row: detectar_regime(row, row['ema9'], row['ema21'], row['ema200']), axis=1)
    
    regimes_count = df['regime'].value_counts().to_dict()
    print(f"   Indicadores calculados: EMA9, EMA21, EMA200, ATR(14), RSI(14), ADX(14)")
    print()
    
    # Simulação de trades
    print("🚀 INICIANDO SIMULAÇÃO V18 TREND RIDER...")
    print("-" * 80)
    
    capital = capital_inicial
    posicao = None
    trades = []
    equity_curve = []
    
    for i in range(250, len(df)):  # Warmup para indicadores
        row = df.iloc[i]
        prev_row = df.iloc[i-1]
        
        regime = row['regime']
        atr = row['atr'] if not pd.isna(row['atr']) else 0
        rsi = row['rsi'] if not pd.isna(row['rsi']) else 50
        adx = row['adx'] if not pd.isna(row['adx']) else 0
        
        if pd.isna(atr) or atr <= 0:
            continue
        
        # Selecionar parâmetros baseados no regime
        if regime == 'BULLISH':
            sl_mult = bull_sl_mult
            trail_mult = bull_trail_mult
            adx_min = bull_adx_min
            rsi_max = bull_rsi_max
        elif regime == 'BEARISH':
            sl_mult = bear_sl_mult
            trail_mult = bear_trail_mult
            adx_min = bear_adx_min
            rsi_max = bear_rsi_max
        else:  # NEUTRO
            sl_mult = 1.5
            trail_mult = 2.5
            adx_min = 18
            rsi_max = 70
        
        if posicao is None:
            # Condições de entrada V18
            # 1. Cruzamento de EMA9 sobre EMA21 (bullish crossover) ou pullback na EMA9
            crossover = prev_row['ema9'] <= prev_row['ema21'] and row['ema9'] > row['ema21']
            pullback = row['low'] <= row['ema9'] * 1.002 and row['close'] > row['ema9']
            
            # 2. Filtros
            adx_ok = adx >= adx_min
            rsi_ok = rsi <= rsi_max
            
            if (crossover or pullback) and adx_ok and rsi_ok and regime != 'BEARISH':
                # Entrada
                preco_entrada = row['close']
                stop_loss = preco_entrada - (atr * sl_mult)
                take_profit = preco_entrada + (atr * sl_mult * 1.5)  # TP = 1.5x o risco
                
                tamanho_pos = (capital * 0.95) / preco_entrada
                capital -= tamanho_pos * preco_entrada
                
                posicao = {
                    'tipo': 'COMPRA',
                    'preco': preco_entrada,
                    'tamanho': tamanho_pos,
                    'entrada_idx': i,
                    'entrada_time': row['timestamp'],
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'regime': regime,
                    'atr_entrada': atr,
                    'trailing_ativo': False,
                    'maximo_desde_entrada': preco_entrada
                }
        
        else:
            # Gerenciar posição
            maximo = max(posicao['maximo_desde_entrada'], row['high'])
            posicao['maximo_desde_entrada'] = maximo
            
            # Ativar trailing stop após lucro mínimo (1x ATR)
            lucro_atual = row['close'] - posicao['preco']
            if lucro_atual >= posicao['atr_entrada'] * 1.0:
                posicao['trailing_ativo'] = True
            
            # Calcular trailing stop dinâmico
            if posicao['trailing_ativo']:
                trailing_stop = maximo - (atr * trail_mult)
                stop_efetivo = max(posicao['stop_loss'], trailing_stop)
            else:
                stop_efetivo = posicao['stop_loss']
            
            # Verificar saída
            motivo_saida = None
            preco_saida = None
            
            if row['low'] <= stop_efetivo:
                preco_saida = stop_efetivo
                motivo_saida = 'STOP_LOSS'
            elif row['high'] >= posicao['take_profit']:
                preco_saida = posicao['take_profit']
                motivo_saida = 'TAKE_PROFIT'
            elif regime == 'BEARISH' and posicao['regime'] != 'BEARISH':
                # Mudança de regime para bearish - sair
                preco_saida = row['close']
                motivo_saida = 'MUDANCA_REGIME'
            
            if preco_saida:
                valor_saida = posicao['tamanho'] * preco_saida
                capital += valor_saida
                
                pnl = valor_saida - (posicao['tamanho'] * posicao['preco'])
                pnl_pct = (pnl / (posicao['tamanho'] * posicao['preco'])) * 100
                
                trades.append({
                    'data_entrada': posicao['entrada_time'],
                    'data_saida': row['timestamp'],
                    'tipo': posicao['tipo'],
                    'preco_entrada': posicao['preco'],
                    'preco_saida': preco_saida,
                    'tamanho': posicao['tamanho'],
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'regime_entrada': posicao['regime'],
                    'motivo': motivo_saida
                })
                
                posicao = None
        
        # Equity curve
        equity = capital
        if posicao:
            equity += posicao['tamanho'] * row['close']
        equity_curve.append(equity)
    
    # Fechar posição aberta no final
    if posicao and len(df) > 0:
        row_final = df.iloc[-1]
        preco_saida = row_final['close']
        valor_saida = posicao['tamanho'] * preco_saida
        capital += valor_saida
        
        pnl = valor_saida - (posicao['tamanho'] * posicao['preco'])
        trades.append({
            'data_entrada': posicao['entrada_time'],
            'data_saida': row_final['timestamp'],
            'tipo': posicao['tipo'],
            'preco_entrada': posicao['preco'],
            'preco_saida': preco_saida,
            'tamanho': posicao['tamanho'],
            'pnl': pnl,
            'pnl_pct': (pnl / (posicao['tamanho'] * posicao['preco'])) * 100,
            'regime_entrada': posicao['regime'],
            'motivo': 'FIM_BACKTEST'
        })
    
    # Resultados
    capital_final = capital
    retorno_total = ((capital_final - capital_inicial) / capital_inicial) * 100
    
    print("\n" + "=" * 80)
    print("📈 RESULTADOS DO BACKTEST - V18 TREND RIDER")
    print("=" * 80)
    print(f"💰 Capital Inicial:     R$ {capital_inicial:,.2f}")
    print(f"💰 Capital Final:       R$ {capital_final:,.2f}")
    print(f"📊 Retorno Total:       {retorno_total:+.2f}%")
    print(f"📊 Buy & Hold:          {((df['close'].iloc[-1] / df['close'].iloc[0]) - 1) * 100:+.2f}%")
    alpha = retorno_total - (((df['close'].iloc[-1] / df['close'].iloc[0]) - 1) * 100)
    print(f"📊 Alpha vs Buy&Hold:   {alpha:+.2f}%")
    print()
    
    if trades:
        wins = [t for t in trades if t['pnl'] > 0]
        losses = [t for t in trades if t['pnl'] <= 0]
        win_rate = (len(wins) / len(trades)) * 100 if trades else 0
        
        total_gains = sum(t['pnl'] for t in wins)
        total_losses = abs(sum(t['pnl'] for t in losses))
        profit_factor = total_gains / total_losses if total_losses > 0 else float('inf')
        
        print(f"📊 Estatísticas dos Trades:")
        print(f"   Total de Trades:      {len(trades)}")
        print(f"   Trades Vencedores:    {len(wins)} ({win_rate:.1f}%)")
        print(f"   Trades Perdedores:    {len(losses)} ({100-win_rate:.1f}%)")
        print(f"   Profit Factor:        {profit_factor:.2f}")
        print(f"   Maior Ganho:          R$ {max(t['pnl'] for t in trades):,.2f}")
        print(f"   Maior Perda:          R$ {min(t['pnl'] for t in trades):,.2f}")
        if wins:
            print(f"   Gain Médio:           R$ {np.mean([t['pnl'] for t in wins]):,.2f}")
        if losses:
            print(f"   Loss Médio:           R$ {np.mean([t['pnl'] for t in losses]):,.2f}")
        print()
        
        # Distribuição por regime
        print(f"📊 Distribuição de Regimes:")
        total_periodos = sum(regimes_count.values())
        for regime in ['BULLISH', 'BEARISH', 'NEUTRO']:
            count = regimes_count.get(regime, 0)
            pct = (count / total_periodos) * 100 if total_periodos > 0 else 0
            print(f"   {regime}: {count} períodos ({pct:.1f}%)")
        print()
        
        # Detalhar trades
        print("📋 DETALHAMENTO DOS TRADES:")
        print("-" * 80)
        for idx, trade in enumerate(trades, 1):
            status = "✅" if trade['pnl'] > 0 else "❌"
            print(f"{idx}. {trade['data_entrada']} → {trade['data_saida']} | {trade['tipo']} @ R$ {trade['preco_entrada']:.4f} → R$ {trade['preco_saida']:.4f} | PnL: R$ {trade['pnl']:+,.2f} ({trade['pnl_pct']:+.2f}%) [{trade['regime_entrada']}] {status} ({trade['motivo']})")
    else:
        print("⚠️ Nenhum trade executado durante o backtest.")
        print("   Sugestão: Verifique se os filtros (ADX, RSI) não estão muito restritivos.")
    
    print("\n" + "=" * 80)
    
    # Salvar resultados
    if trades:
        trades_df = pd.DataFrame(trades)
        output_file = '/workspace/backtest_trades_axs_abril2026_v18.csv'
        trades_df.to_csv(output_file, index=False)
        print(f"✅ Trades salvos em: {output_file}")
    
    equity_df = pd.DataFrame({
        'timestamp': df['timestamp'].iloc[250:len(df)].values[:len(equity_curve)],
        'equity': equity_curve
    })
    equity_file = '/workspace/backtest_equity_axs_abril2026_v18.csv'
    equity_df.to_csv(equity_file, index=False)
    print(f"✅ Curva de equity salva em: {equity_file}")
    
    return {
        'capital_final': capital_final,
        'retorno_total': retorno_total,
        'num_trades': len(trades),
        'win_rate': win_rate if trades else 0,
        'profit_factor': profit_factor if trades else 0,
        'trades': trades
    }

if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else '/workspace/AXSUSDT_2026-04-01_2026-04-30_5m.csv'
    capital = float(sys.argv[2]) if len(sys.argv) > 2 else 10000
    
    try:
        resultados = run_backtest(csv_file, capital)
    except Exception as e:
        print(f"❌ ERRO NA SIMULAÇÃO: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
