#!/usr/bin/env python3
"""
SNIPER PHOENIX V18 - TREND RIDER ADAPTATIVO
============================================

Versão otimizada baseada na análise comparativa Abril vs Maio-Junho 2026.

MELHORIAS PRINCIPAIS:
1. Detecção automática de regime (BULLISH/BEARISH)
2. Parâmetros adaptativos conforme volatilidade (ATR)
3. Filtros de entrada refinados por regime
4. Gestão de risco dinâmica

PARÂMETROS OTIMIZADOS:
- BULLISH: SL=2.0x ATR, Trailing=3.5x ATR, Limiar=2.0x ATR, ADX>20, RSI<75
- BEARISH: SL=1.2x ATR, Trailing=1.5x ATR, Limiar=0.6x ATR, ADX>15, RSI<60

Autor: Sniper Phoenix Team
Data: 2026-06-13
"""

import pandas as pd
import numpy as np
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', None)


class SniperPhoenixV18:
    """
    Trading Bot Sniper Phoenix V18 - Trend Rider Adaptativo
    
    Detecta automaticamente o regime de mercado e ajusta parâmetros
    para maximizar lucros em tendências de alta e baixa.
    """
    
    def __init__(self, capital_inicial=100, risk_per_trade=0.01):
        self.capital_inicial = capital_inicial
        self.capital = capital_inicial
        self.risk_per_trade = risk_per_trade
        
        # Posição atual
        self.pos = False
        self.entry = 0
        self.qty = 0
        self.sl = 0
        self.trailing = False
        self.best = 0
        self.trail_val = 0
        self.bias = 'NEUTRO'  # BULLISH, BEARISH ou NEUTRO
        
        # Histórico de trades
        self.trades = []
        
        # Parâmetros por regime (otimizados pela análise)
        self.parametros = {
            'BULLISH': {
                'sl_mult': 2.0,        # Stop Loss multiplier
                'trail_dist': 3.5,     # Trailing stop distance
                'trail_limiar': 2.0,   # Profit threshold to activate trailing
                'adx_min': 20,         # Minimum ADX for trend strength
                'rsi_max': 75,         # Maximum RSI (avoid overbought)
            },
            'BEARISH': {
                'sl_mult': 1.2,        # Stop Loss multiplier (mais apertado)
                'trail_dist': 1.5,     # Trailing stop distance (mais agressivo)
                'trail_limiar': 0.6,   # Profit threshold (ativa cedo)
                'adx_min': 15,         # Minimum ADX (menor threshold)
                'rsi_max': 60,         # Maximum RSI (mais conservador)
            }
        }
    
    def calcular_indicadores(self, df):
        """Calcula todos os indicadores técnicos necessários"""
        # EMAs
        df['EMA9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['EMA21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['EMA200'] = df['close'].ewm(span=200, adjust=False).mean()
        
        # RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['RSI'] = 100 - 100 / (1 + gain / loss)
        
        # ATR (Average True Range)
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        true_range = np.max(pd.concat([high_low, high_close, low_close], axis=1), axis=1)
        df['ATR'] = true_range.rolling(14).mean()
        
        # ADX (Average Directional Index)
        periodo = 14
        plus_dm = df['high'].diff().clip(lower=0)
        minus_dm = (-df['low'].diff()).clip(lower=0)
        tr_roll = df['ATR'] * periodo
        plus_di = 100 * plus_dm.rolling(periodo).mean() / tr_roll
        minus_di = 100 * minus_dm.rolling(periodo).mean() / tr_roll
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        df['ADX'] = dx.rolling(periodo).mean()
        
        return df
    
    def detectar_regime(self, df, i):
        """
        Detecta o regime de mercado atual baseado em múltiplos fatores.
        
        Critérios:
        - BULLISH: Preço > EMA200 E EMA9 > EMA21
        - BEARISH: Preço < EMA200 E EMA9 < EMA21
        - NEUTRO: Condições mistas ou sem tendência clara
        
        Retorna também a confiança do sinal (0-100)
        """
        row = df.iloc[i]
        
        preco_vs_ema200 = row['close'] > row['EMA200']
        ema9_vs_ema21 = row['EMA9'] > row['EMA21']
        
        # Calcular distâncias para força do sinal
        if preco_vs_ema200:
            dist_ema200 = (row['close'] - row['EMA200']) / row['close'] * 100
        else:
            dist_ema200 = (row['EMA200'] - row['close']) / row['close'] * 100
            
        if ema9_vs_ema21:
            dist_ema9_21 = (row['EMA9'] - row['EMA21']) / row['close'] * 100
        else:
            dist_ema9_21 = (row['EMA21'] - row['EMA9']) / row['close'] * 100
        
        # Determinar regime
        if preco_vs_ema200 and ema9_vs_ema21:
            regime = 'BULLISH'
            confianca = min(100, (dist_ema200 + dist_ema9_21) * 10)
        elif not preco_vs_ema200 and not ema9_vs_ema21:
            regime = 'BEARISH'
            confianca = min(100, (dist_ema200 + dist_ema9_21) * 10)
        else:
            regime = 'NEUTRO'
            confianca = 0
        
        return regime, confianca
    
    def verificar_condicoes_entrada(self, df, i, params):
        """
        Verifica se todas as condições para entrada estão satisfeitas.
        
        Condições:
        1. ADX >= adx_min (tendência forte o suficiente)
        2. RSI <= rsi_max (não sobrecomprado/vendido)
        3. Padrão de entrada (cruzamento ou pullback)
        """
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        
        # Filtros básicos
        if row['ATR'] <= 0 or np.isnan(row['ATR']):
            return False, None
            
        if row['ADX'] < params['adx_min']:
            return False, 'ADX baixo'
            
        if row['RSI'] > params['rsi_max']:
            return False, 'RSI alto'
        
        # Verificar padrão de entrada baseado no bias
        if self.bias == 'BULLISH':
            # Cruzamento de alta
            cross_up = prev['EMA9'] <= prev['EMA21'] and row['EMA9'] > row['EMA21']
            
            # Pullback na EMA9
            pullback = (row['low'] <= row['EMA9'] * 1.002 and 
                       row['close'] > row['EMA9'] and
                       row['EMA9'] > row['EMA21'])
            
            if cross_up:
                return True, 'CROSS_UP'
            elif pullback:
                return True, 'PULLBACK'
                
        elif self.bias == 'BEARISH':
            # Cruzamento de baixa
            cross_down = prev['EMA9'] >= prev['EMA21'] and row['EMA9'] < row['EMA21']
            
            # Pullback na EMA9
            pullback = (row['high'] >= row['EMA9'] * 0.998 and 
                       row['close'] < row['EMA9'] and
                       row['EMA9'] < row['EMA21'])
            
            if cross_down:
                return True, 'CROSS_DOWN'
            elif pullback:
                return True, 'PULLBACK'
        
        return False, None
    
    def entrar_posicao(self, row, tipo_entrada):
        """Abre uma nova posição com gestão de risco adequada"""
        atr_val = row['ATR']
        params = self.parametros[self.bias]
        
        if self.bias == 'BULLISH':
            self.entry = row['close']
            self.sl = self.entry - params['sl_mult'] * atr_val
            risk = self.entry - self.sl
            
            if risk > 0:
                self.qty = int(self.capital * self.risk_per_trade / risk)
                if self.qty > 0:
                    self.pos = True
                    self.best = self.entry
                    self.trailing = False
                    self.trail_val = self.sl
                    return True
                    
        elif self.bias == 'BEARISH':
            self.entry = row['close']
            self.sl = self.entry + params['sl_mult'] * atr_val
            risk = self.sl - self.entry
            
            if risk > 0:
                self.qty = int(self.capital * self.risk_per_trade / risk)
                if self.qty > 0:
                    self.pos = True
                    self.best = self.entry
                    self.trailing = False
                    self.trail_val = self.sl
                    return True
        
        return False
    
    def gerenciar_posicao(self, row):
        """
        Gerencia a posição aberta, incluindo trailing stop dinâmico.
        
        Retorna True se a posição foi fechada.
        """
        if not self.pos:
            return False
            
        close = row['close']
        params = self.parametros[self.bias]
        atr_val = max(row['ATR'], 0.001)
        exit_price = None
        motivo_saida = None
        
        if self.bias == 'BULLISH':
            # Atualizar melhor preço
            if close > self.best:
                self.best = close
            
            # Ativar trailing stop quando lucro suficiente
            if self.best >= self.entry + params['trail_limiar'] * atr_val:
                self.trailing = True
                new_trail = self.best - params['trail_dist'] * atr_val
                if new_trail > self.trail_val:
                    self.trail_val = new_trail
            
            # Verificar saída
            if not self.trailing and close <= self.sl:
                exit_price = self.sl
                motivo_saida = 'STOP_LOSS'
            elif self.trailing and close <= self.trail_val:
                exit_price = self.trail_val
                motivo_saida = 'TRAILING_STOP'
                
        elif self.bias == 'BEARISH':
            # Atualizar melhor preço
            if close < self.best:
                self.best = close
            
            # Ativar trailing stop quando lucro suficiente
            if self.best <= self.entry - params['trail_limiar'] * atr_val:
                self.trailing = True
                new_trail = self.best + params['trail_dist'] * atr_val
                if new_trail < self.trail_val or self.trail_val == self.sl:
                    self.trail_val = new_trail
            
            # Verificar saída
            if not self.trailing and close >= self.sl:
                exit_price = self.sl
                motivo_saida = 'STOP_LOSS'
            elif self.trailing and close >= self.trail_val:
                exit_price = self.trail_val
                motivo_saida = 'TRAILING_STOP'
        
        # Executar saída
        if exit_price:
            if self.bias == 'BULLISH':
                pnl = (exit_price - self.entry) * self.qty
            else:
                pnl = (self.entry - exit_price) * self.qty
                
            self.capital += pnl
            self.trades.append({
                'pnl': pnl,
                'entry': self.entry,
                'exit': exit_price,
                'qty': self.qty,
                'bias': self.bias,
                'motivo': motivo_saida
            })
            
            # Resetar posição
            self.pos = False
            self.entry = 0
            self.qty = 0
            self.sl = 0
            self.trailing = False
            self.best = 0
            self.trail_val = 0
            
            return True
        
        return False
    
    def executar_backtest(self, df, verbose=False):
        """
        Executa o backtest completo no dataframe fornecido.
        
        Args:
            df: DataFrame com colunas ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            verbose: Se True, imprime logs detalhados
            
        Returns:
            dict: Resultados do backtest
        """
        # Resetar estado
        self.capital = self.capital_inicial
        self.pos = False
        self.trades = []
        self.bias = 'NEUTRO'
        
        # Calcular indicadores
        df = df.copy()
        df = self.calcular_indicadores(df)
        df.dropna(inplace=True)
        
        # Variáveis para logging
        trades_no_periodo = {'BULLISH': 0, 'BEARISH': 0}
        pnl_no_periodo = {'BULLISH': 0, 'BEARISH': 0}
        mudancas_bias = 0
        ultimo_bias = 'NEUTRO'
        
        # Loop principal
        for i in range(200, len(df)):
            row = df.iloc[i]
            
            # Detectar regime atual
            novo_bias, confianca = self.detectar_regime(df, i)
            
            # Log mudança de regime
            if novo_bias != ultimo_bias and novo_bias != 'NEUTRO':
                mudancas_bias += 1
                if verbose:
                    print(f"[{row['timestamp']}] Mudança de regime: {ultimo_bias} -> {novo_bias} (confiança: {confianca:.1f}%)")
                ultimo_bias = novo_bias
            
            # Fechar posição se regime mudou contra nós
            if self.pos and novo_bias != 'NEUTRO' and novo_bias != self.bias:
                if verbose:
                    print(f"[{row['timestamp']}] Fechando posição {self.bias} devido mudança de regime")
                # Forçar saída ao preço atual
                if self.bias == 'BULLISH':
                    pnl = (row['close'] - self.entry) * self.qty
                else:
                    pnl = (self.entry - row['close']) * self.qty
                self.capital += pnl
                self.trades.append({
                    'pnl': pnl,
                    'entry': self.entry,
                    'exit': row['close'],
                    'qty': self.qty,
                    'bias': self.bias,
                    'motivo': 'MUDANCA_REGIME'
                })
                self.pos = False
            
            # Atualizar bias atual
            if novo_bias != 'NEUTRO':
                self.bias = novo_bias
            
            # Se temos posição, gerenciar
            if self.pos:
                self.gerenciar_posicao(row)
                continue
            
            # Se não temos posição e temos bias definido, procurar entrada
            if self.bias in ['BULLISH', 'BEARISH']:
                params = self.parametros[self.bias]
                pode_entrar, tipo = self.verificar_condicoes_entrada(df, i, params)
                
                if pode_entrar:
                    if self.entrar_posicao(row, tipo):
                        trades_no_periodo[self.bias] += 1
                        if verbose:
                            print(f"[{row['timestamp']}] Entrada {self.bias} via {tipo} @ {row['close']:.4f}")
        
        # Calcular resultados
        if self.trades:
            total_pnl = sum(t['pnl'] for t in self.trades)
            trades_vencedores = len([t for t in self.trades if t['pnl'] > 0])
            win_rate = trades_vencedores / len(self.trades) * 100
            retorno = total_pnl / self.capital_inicial * 100
            
            # Estatísticas por bias
            for bias in ['BULLISH', 'BEARISH']:
                trades_bias = [t for t in self.trades if t['bias'] == bias]
                if trades_bias:
                    pnl_no_periodo[bias] = sum(t['pnl'] for t in trades_bias)
                    trades_no_periodo[bias] = len(trades_bias)
            
            return {
                'retorno_pct': retorno,
                'pnl_total': total_pnl,
                'capital_final': self.capital,
                'total_trades': len(self.trades),
                'win_rate': win_rate,
                'trades_bullish': trades_no_periodo['BULLISH'],
                'trades_bearish': trades_no_periodo['BEARISH'],
                'pnl_bullish': pnl_no_periodo['BULLISH'],
                'pnl_bearish': pnl_no_periodo['BEARISH'],
                'mudancas_bias': mudancas_bias,
                'trades_detalhes': self.trades
            }
        
        return {
            'retorno_pct': 0,
            'pnl_total': 0,
            'capital_final': self.capital,
            'total_trades': 0,
            'win_rate': 0,
            'trades_bullish': 0,
            'trades_bearish': 0,
            'pnl_bullish': 0,
            'pnl_bearish': 0,
            'mudancas_bias': 0,
            'trades_detalhes': []
        }


def carregar_dados(caminho_arquivo):
    """Carrega dados CSV e padroniza formato"""
    df = pd.read_csv(caminho_arquivo)
    
    # Garantir coluna timestamp
    if 'timestamp' not in df.columns:
        df['timestamp'] = pd.date_range(start='2026-01-01', periods=len(df), freq='5min')
    
    # Garantir colunas OHLCV
    colunas_necessarias = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    for col in colunas_necessarias:
        if col not in df.columns:
            raise ValueError(f"Coluna {col} não encontrada no arquivo")
    
    return df


def analisar_resultados(resultados, nome_periodo=""):
    """Imprime análise detalhada dos resultados"""
    print(f"\n{'='*70}")
    print(f"📊 RESULTADOS DO BACKTEST - {nome_periodo}")
    print(f"{'='*70}")
    
    print(f"\n💰 Performance Geral:")
    print(f"   Retorno: {resultados['retorno_pct']:+.2f}%")
    print(f"   PnL Total: $ {resultados['pnl_total']:+.2f}")
    print(f"   Capital Final: $ {resultados['capital_final']:.2f}")
    print(f"   Total de Trades: {resultados['total_trades']}")
    print(f"   Win Rate: {resultados['win_rate']:.1f}%")
    print(f"   Mudanças de Regime: {resultados['mudancas_bias']}")
    
    print(f"\n📈 Performance por Regime:")
    print(f"   BULLISH: {resultados['trades_bullish']} trades, PnL: $ {resultados['pnl_bullish']:+.2f}")
    print(f"   BEARISH: {resultados['trades_bearish']} trades, PnL: $ {resultados['pnl_bearish']:+.2f}")
    
    # Análise adicional
    if resultados['total_trades'] > 0:
        trades_win = [t for t in resultados['trades_detalhes'] if t['pnl'] > 0]
        trades_loss = [t for t in resultados['trades_detalhes'] if t['pnl'] <= 0]
        
        if trades_win:
            avg_win = sum(t['pnl'] for t in trades_win) / len(trades_win)
            max_win = max(t['pnl'] for t in trades_win)
            print(f"\n✅ Trades Vencedores:")
            print(f"   Quantidade: {len(trades_win)}")
            print(f"   Média: $ {avg_win:+.2f}")
            print(f"   Máximo: $ {max_win:+.2f}")
        
        if trades_loss:
            avg_loss = sum(t['pnl'] for t in trades_loss) / len(trades_loss)
            max_loss = min(t['pnl'] for t in trades_loss)
            print(f"\n❌ Trades Perdedores:")
            print(f"   Quantidade: {len(trades_loss)}")
            print(f"   Média: $ {avg_loss:+.2f}")
            print(f"   Máximo: $ {max_loss:+.2f}")
        
        # Ratio profit/loss
        if trades_loss:
            ratio = abs(avg_win / avg_loss) if trades_win else 0
            print(f"\n📊 Profit/Loss Ratio: {ratio:.2f}")
    
    print(f"\n{'='*70}")


def main():
    """Função principal para demonstração"""
    print("="*70)
    print("🚀 SNIPER PHOENIX V18 - TREND RIDER ADAPTATIVO")
    print("="*70)
    print("\nVersão otimizada com detecção automática de regime de mercado.")
    print("Parâmetros calibrados baseado na análise Abril vs Maio-Junho 2026.\n")
    
    # Inicializar bot
    bot = SniperPhoenixV18(capital_inicial=100, risk_per_trade=0.01)
    
    # Exemplo de uso (descomente quando tiver os arquivos)
    df_abril = carregar_dados('AXSUSDT_2026-04-01_2026-04-30_5m.csv')
    resultados_abril = bot.executar_backtest(df_abril, verbose=False)
    analisar_resultados(resultados_abril, "ABRIL 2026")
    
    df_maio = carregar_dados('AXSUSDT_2026-05-11_2026-06-12_5m.csv')
    resultados_maio = bot.executar_backtest(df_maio, verbose=False)
    analisar_resultados(resultados_maio, "MAIO-JUNHO 2026")
    
    print("\n📋 Para executar o backtest:")
    print("   1. Carregue seus dados: df = carregar_dados('seu_arquivo.csv')")
    print("   2. Execute: resultados = bot.executar_backtest(df)")
    print("   3. Analise: analisar_resultados(resultados, 'Nome do Período')")
    
    print("\n⚙️  Parâmetros Configurados:")
    print("\n   REGIME BULLISH:")
    print(f"      • Stop Loss: {bot.parametros['BULLISH']['sl_mult']}x ATR")
    print(f"      • Trailing Stop: {bot.parametros['BULLISH']['trail_dist']}x ATR")
    print(f"      • Limiar Trailing: {bot.parametros['BULLISH']['trail_limiar']}x ATR")
    print(f"      • ADX Mínimo: {bot.parametros['BULLISH']['adx_min']}")
    print(f"      • RSI Máximo: {bot.parametros['BULLISH']['rsi_max']}")
    
    print("\n   REGIME BEARISH:")
    print(f"      • Stop Loss: {bot.parametros['BEARISH']['sl_mult']}x ATR")
    print(f"      • Trailing Stop: {bot.parametros['BEARISH']['trail_dist']}x ATR")
    print(f"      • Limiar Trailing: {bot.parametros['BEARISH']['trail_limiar']}x ATR")
    print(f"      • ADX Mínimo: {bot.parametros['BEARISH']['adx_min']}")
    print(f"      • RSI Máximo: {bot.parametros['BEARISH']['rsi_max']}")
    
    print("\n✅ Bot inicializado com sucesso!")
    return bot


if __name__ == "__main__":
    bot = main()
