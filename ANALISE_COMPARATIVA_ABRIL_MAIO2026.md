     1	# 📊 ANÁLISE COMPARATIVA: ABRIL vs MAIO-JUNHO 2026
     2	
     3	## Resumo Executivo
     4	
     5	Esta análise compara o desempenho do trading bot `sniper_phoenix_v17_trend_rider.py` 
     6	em dois períodos distintos de 2026, revelando a importância crítica da adaptação 
     7	ao regime de mercado.
     8	
     9	---
    10	
    11	## 📈 Estatísticas dos Períodos
    12	
    13	| Métrica | Abril 2026 | Maio-Junho 2026 |
    14	|---------|------------|-----------------|
    15	| **Variação Total** | +20.56% | -32.70% |
    16	| **ATR Médio** | 0.371% | 0.292% |
    17	| **% Bearish** | 52.8% | 57.7% |
    18	| **RSI Médio** | 49.97 | 49.53 |
    19	| **Candles** | 8640 | 12097 |
    20	
    21	---
    22	
    23	## 🧪 Resultados da Simulação V17 Original
    24	
    25	### Abril 2026 (Bias BULLISH)
    26	- Retorno: 109.26%
    27	- Win Rate: 50.0%
    28	- Trades: 108
    29	- PnL: R$ 10925.59
    30	
    31	### Maio-Junho 2026 (Bias BULLISH - inadequado)
    32	- Retorno: -1.86%
    33	- Win Rate: 35.3%
    34	- Trades: 173
    35	- PnL: R$ -185.95
    36	
    37	### Maio-Junho 2026 (Bias BEARISH - otimizado)
    38	- Retorno: 489.03%
    39	- Win Rate: 45.5%
    40	- Trades: 358
    41	- PnL: R$ 48902.60
    42	
    43	---
    44	
    45	## 🏆 Melhores Configurações Encontradas
    46	
    47	### Para Abril 2026 (BULLISH)
    48	| Parâmetro | Valor |
    49	|-----------|-------|
    50	| Stop Loss | 2.0x ATR |
    51	| Trailing Stop | 3.5x ATR |
    52	| Limiar Trailing | 2.0x ATR |
    53	| ADX Mínimo | 20 |
    54	| RSI Máximo | 75 |
    55	| **Retorno** | **136.73%** |
    56	| Win Rate | 36.3% |
    57	
    58	### Para Maio-Junho 2026 (BEARISH)
    59	| Parâmetro | Valor |
    60	|-----------|-------|
    61	| Stop Loss | 1.2x ATR |
    62	| Trailing Stop | 1.5x ATR |
    63	| Limiar Trailing | 0.6x ATR |
    64	| ADX Mínimo | 15 |
    65	| RSI Máximo | 60 |
    66	| **Retorno** | **823.68%** |
    67	| Win Rate | 45.8% |
    68	
    69	---
    70	
    71	## 💡 Conclusões Principais
    72	
    73	1. **Mudança de Regime é Crítica**: 
    74	   - Abril teve tendência de alta (+20.56%) → Bias BULLISH ideal
    75	   - Maio-Junho teve tendência de baixa (-32.70%) → Bias BEARISH necessário
    76	
    77	2. **Parâmetros V17 Originais**:
    78	   - Funcionaram bem em Abril (regime favorável)
    79	   - Falharam em Maio-Junho quando mantidos com bias BULLISH
    80	
    81	3. **Necessidade de Adaptação Dinâmica**:
    82	   - Implementar detecção automática de regime
    83	   - Alternar bias baseado em preço vs EMA200 e EMA9 vs EMA21
    84	
    85	4. **Lições para Futuras Versões**:
    86	   - Criar sistema de detecção de tendência automática
    87	   - Ajustar parâmetros conforme volatilidade (ATR)
    88	   - Considerar filtros temporais/semanais
    89	
    90	---
    91	
    92	## ✅ Recomendações para V18
    93	
    94	```python
    95	# Detecção automática de regime
    96	def detectar_regime(df):
    97	    preco_vs_ema200 = df['close'].iloc[-1] > df['EMA200'].iloc[-1]
    98	    ema9_vs_ema21 = df['EMA9'].iloc[-1] > df['EMA21'].iloc[-1]
    99	    
   100	    if preco_vs_ema200 and ema9_vs_ema21:
   101	        return 'BULLISH'
   102	    elif not preco_vs_ema200 and not ema9_vs_ema21:
   103	        return 'BEARISH'
   104	    else:
   105	        return 'NEUTRO'
   106	
   107	# Ajuste dinâmico de parâmetros
   108	if regime == 'BULLISH':
   109	    bias = 'BULLISH'
   110	    sl_mult = 2.0
   111	    trail_dist = 2.5
   112	else:
   113	    bias = 'BEARISH'
   114	    sl_mult = 1.3
   115	    trail_dist = 1.8
   116	```
   117	
   118	---
   119	
   120	*Relatório gerado em 2026-06-13 16:06*
   121	*Dados: AXSUSDT_2026-04-01_2026-04-30_5m.csv e AXSUSDT_2026-05-11_2026-06-12_5m.csv*