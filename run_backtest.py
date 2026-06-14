#!/usr/bin/env python3
"""Script dedicado para backtest automatizado do Sniper Phoenix v831"""

import pandas as pd
import numpy as np
from datetime import datetime
import sys
import os
import configparser

# Adicionar caminho do workspace
sys.path.insert(0, '/workspace')

# Imports necessários
from sniper_phoenix_v831 import RegimeDetector, DrawdownFilter, TradingEngine

def carregar_config(config_path='/workspace/config.ini'):
    """Carrega configurações do arquivo INI"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    
    # Converter para dicionário aninhado
    config_dict = {}
    for section in config.sections():
        config_dict[section] = {}
        for key, value in config.items(section):
            # Tentar converter para tipos apropriados
            try:
                if '.' in value:
                    config_dict[section][key] = float(value)
                elif value.isdigit() or (value.startswith('-') and value[1:].isdigit()):
                    config_dict[section][key] = int(value)
                elif value.lower() in ['true', 'false']:
                    config_dict[section][key] = value.lower() == 'true'
                else:
                    config_dict[section][key] = value
            except:
                config_dict[section][key] = value
    
    # Achatar o dicionário para facilitar acesso
    flat_config = {}
    for section, values in config_dict.items():
        for key, value in values.items():
            flat_config[key] = value
    
    return flat_config

def run_backtest(csv_file, capital_inicial=10000):
    """Executa backtest completo"""
    
    print(f"🔍 CARREGANDO DADOS: {csv_file}")
    print("=" * 80)
    
    # Carregar configurações
    config = carregar_config()
    print(f"📋 CONFIGURAÇÕES CARREGADAS:")
    print(f"   Símbolo: {config.get('symbol', 'AXS/USDT')}")
    print(f"   Timeframe: {config.get('timeframe', '5m')}")
    print(f"   Capital: R$ {capital_inicial:,.2f}")
    print()
    
    # Carregar dados
    df = pd.read_csv(csv_file)
    
    # Normalizar colunas - adaptar para diferentes formatos de CSV
    col_mapping = {
        'open_time_brasilia': 'timestamp',
        'close_time_brasilia': 'close_time',
        'open': 'open',
        'high': 'high',
        'low': 'low',
        'close': 'close',
        'volume': 'volume'
    }
    
    # Renomear colunas se necessário
    for old_name, new_name in col_mapping.items():
        if old_name in df.columns and new_name not in df.columns:
            df[new_name] = df[old_name]
    
    # Verificar colunas obrigatórias
    required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise ValueError(f"Colunas obrigatórias ausentes: {missing_cols}")
    
    # Converter timestamp se necessário
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
    
    # Inicializar componentes com configurações
    regime_detector = RegimeDetector(config)
    drawdown_filter = DrawdownFilter(max_drawdown_pct=config.get('max_drawdown_pct', 0.15))
    trading_engine = TradingEngine(capital_inicial=capital_inicial)
    
    # Variáveis de controle
    capital = capital_inicial
    posicao = None
    trades = []
    equity_curve = [capital_inicial]
    regimes_count = {'BULLISH': 0, 'BEARISH': 0, 'NEUTRO': 0}
    
    print("🚀 INICIANDO SIMULAÇÃO...")
    print("-" * 80)
    
    # Loop principal de backtest
    for i in range(200, len(df)):  # Começar após warmup das EMAs
        candle_atual = df.iloc[i]
        
        # Dados OHLCV
        ohlcv = {
            'timestamp': candle_atual['timestamp'],
            'open': candle_atual['open'],
            'high': candle_atual['high'],
            'low': candle_atual['low'],
            'close': candle_atual['close'],
            'volume': candle_atual['volume']
        }
        
        # Calcular indicadores (simplificado para backtest)
        closes = df['close'].iloc[:i+1].values
        highs = df['high'].iloc[:i+1].values
        lows = df['low'].iloc[:i+1].values
        
        # Detectar regime
        regime = regime_detector.detect_regime(closes)
        regimes_count[regime] += 1
        
        # Verificar drawdown
        if not drawdown_filter.can_trade(capital, capital_inicial):
            continue
        
        # Processar sinal de trading
        if posicao is None:
            # Procurar entrada
            sinal = trading_engine.process_signal(ohlcv, closes, highs, lows, regime)
            if sinal and sinal.get('acao') == 'COMPRA':
                preco_entrada = ohlcv['close']
                tamanho_pos = (capital * 0.95) / preco_entrada  # Usar 95% do capital
                posicao = {
                    'tipo': 'COMPRA',
                    'preco': preco_entrada,
                    'tamanho': tamanho_pos,
                    'entrada_time': ohlcv['timestamp'],
                    'stop_loss': sinal.get('stop_loss', preco_entrada * 0.98),
                    'take_profit': sinal.get('take_profit', preco_entrada * 1.03),
                    'regime': regime
                }
                capital -= tamanho_pos * preco_entrada  # Compra
                
        else:
            # Gerenciar posição aberta
            resultado = trading_engine.manage_position(posicao, ohlcv, regime)
            
            if resultado.get('acao') == 'FECHAR':
                # Fechar posição
                preco_saida = ohlcv['close']
                valor_saida = posicao['tamanho'] * preco_saida
                capital += valor_saida
                
                pnl = valor_saida - (posicao['tamanho'] * posicao['preco'])
                pnl_pct = (pnl / (posicao['tamanho'] * posicao['preco'])) * 100
                
                trades.append({
                    'data_entrada': posicao['entrada_time'],
                    'data_saida': ohlcv['timestamp'],
                    'tipo': posicao['tipo'],
                    'preco_entrada': posicao['preco'],
                    'preco_saida': preco_saida,
                    'tamanho': posicao['tamanho'],
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'regime': posicao['regime'],
                    'motivo': resultado.get('motivo', 'SINAL')
                })
                
                posicao = None
        
        equity_curve.append(capital + (posicao['tamanho'] * ohlcv['close'] if posicao else 0))
    
    # Fechar qualquer posição aberta no final
    if posicao and len(df) > 0:
        candle_final = df.iloc[-1]
        preco_saida = candle_final['close']
        valor_saida = posicao['tamanho'] * preco_saida
        capital += valor_saida
        
        pnl = valor_saida - (posicao['tamanho'] * posicao['preco'])
        trades.append({
            'data_entrada': posicao['entrada_time'],
            'data_saida': candle_final['timestamp'],
            'tipo': posicao['tipo'],
            'preco_entrada': posicao['preco'],
            'preco_saida': preco_saida,
            'tamanho': posicao['tamanho'],
            'pnl': pnl,
            'pnl_pct': (pnl / (posicao['tamanho'] * posicao['preco'])) * 100,
            'regime': posicao['regime'],
            'motivo': 'FIM_BACKTEST'
        })
    
    # Resultados
    capital_final = capital
    retorno_total = ((capital_final - capital_inicial) / capital_inicial) * 100
    
    print("\n" + "=" * 80)
    print("📈 RESULTADOS DO BACKTEST")
    print("=" * 80)
    print(f"💰 Capital Inicial:     R$ {capital_inicial:,.2f}")
    print(f"💰 Capital Final:       R$ {capital_final:,.2f}")
    print(f"📊 Retorno Total:       {retorno_total:+.2f}%")
    print(f"📊 Buy & Hold:          {((df['close'].iloc[-1] / df['close'].iloc[0]) - 1) * 100:+.2f}%")
    print(f"📊 Alpha vs Buy&Hold:   {(retorno_total - (((df['close'].iloc[-1] / df['close'].iloc[0]) - 1) * 100)):+.2f}%")
    print()
    
    if trades:
        wins = [t for t in trades if t['pnl'] > 0]
        losses = [t for t in trades if t['pnl'] <= 0]
        win_rate = (len(wins) / len(trades)) * 100
        
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
        print(f"   Gain Médio:           R$ {np.mean([t['pnl'] for t in wins]):,.2f}" if wins else "   Gain Médio:           N/A")
        print(f"   Loss Médio:           R$ {np.mean([t['pnl'] for t in losses]):,.2f}" if losses else "   Loss Médio:           N/A")
        print()
        
        # Distribuição por regime
        print(f"📊 Distribuição de Regimes:")
        total_periodos = sum(regimes_count.values())
        for regime, count in regimes_count.items():
            pct = (count / total_periodos) * 100
            print(f"   {regime}: {count} períodos ({pct:.1f}%)")
        print()
        
        # Detalhar trades
        print("📋 DETALHAMENTO DOS TRADES:")
        print("-" * 80)
        for idx, trade in enumerate(trades, 1):
            status = "✅" if trade['pnl'] > 0 else "❌"
            print(f"{idx}. {trade['data_entrada']} → {trade['data_saida']} | {trade['tipo']} @ R$ {trade['preco_entrada']:.4f} → R$ {trade['preco_saida']:.4f} | PnL: R$ {trade['pnl']:+,.2f} ({trade['pnl_pct']:+.2f}%) [{trade['regime']}] {status}")
    else:
        print("⚠️ Nenhum trade executado durante o backtest.")
    
    print("\n" + "=" * 80)
    
    # Salvar resultados
    if trades:
        trades_df = pd.DataFrame(trades)
        trades_df.to_csv('/workspace/backtest_trades_axs_abril2026.csv', index=False)
        print(f"✅ Trades salvos em: backtest_trades_axs_abril2026.csv")
    
    equity_df = pd.DataFrame({'timestamp': df['timestamp'].iloc[200:], 'equity': equity_curve})
    equity_df.to_csv('/workspace/backtest_equity_axs_abril2026.csv', index=False)
    print(f"✅ Curva de equity salva em: backtest_equity_axs_abril2026.csv")
    
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
