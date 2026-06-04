# Grigado — Manual de las routines (Condor)

> **Qué es este documento:** el manual consolidado de las routines de Condor que
> dan soporte a la estrategia. Condor es el agente externo (en
> `/Users/tomasgaudino/PycharmProjects/condor/`) que corre routines de análisis y
> diseño — el **laboratorio** de la estrategia. La **ejecución** real vive en el
> controller de Hummingbot (`docs/CONTROLLER.md`), sin agente externo.
>
> **Última actualización:** 2026-06-02

---

## 1. El framework de routines

Toda routine de Condor sigue la misma estructura técnica:

- **`Config` (pydantic)** + función **`run` asíncrona**. Tipos: *one-shot* (default,
  corre y termina) o *continuous* (loop interno).
- **`context`** da acceso a Telegram y al servidor de Hummingbot vía cliente.
- **Métodos verificados del cliente:** `get_prices`, `get_candles_last_days`,
  `get_state` (balances), `get_history`.
- **Salida:** `ReportBuilder` (HTML en la UI) + notificación corta a Telegram.
  Gráficos con Plotly.
- **Normalización de velas** y manejo de errores robusto son obligatorios.

> Detalle exhaustivo del framework: el manual técnico original de Condor (referencia
> histórica, ahora resumido acá). Las routines vivas se listan abajo.

---

## 2. Mapa de routines y su flujo

```mermaid
flowchart TD
    sr["soporte_resistencia v3.1<br/>CORE · detecta S/R + escenarios"]:::core
    lab["chessboard_lab v2<br/>laboratorio del tablero"]:::main
    anl["grid_portfolio_analyzer v2<br/>propone portfolio"]:::tool
    tst["target_scenario_table v1<br/>tabla precio × target"]:::tool
    rec["grid_portfolio_reconciler v1<br/>feedback post-cierre"]:::tool
    ctrl["controller chessboard<br/>(ejecución real, Hummingbot)"]:::exec

    sr --> lab
    sr --> anl
    sr --> tst
    lab --> ctrl
    anl --> ctrl
    ctrl -.cierre de grilla.-> rec
    rec -.re-planificación.-> anl

    classDef core fill:#1d3557,color:#fff
    classDef main fill:#2d6a4f,color:#fff
    classDef tool fill:#457b9d,color:#fff
    classDef exec fill:#9d0208,color:#fff
```

Routine **principal viva**: `chessboard_lab`. Routine **núcleo**:
`soporte_resistencia`. Las demás (analyzer, reconciler, target_table) son
herramientas de soporte de distintos grados de madurez.

---

## 3. `chessboard_lab` (v2) — el laboratorio del tablero

**La routine principal.** Simulador determinístico del tablero; **no lanza nada**.

- **Path:** `condor/trading_agents/grigado/routines/chessboard_lab.py`
- Lee el portfolio real e infiere el inventario de la sub-cuenta (`base_assigned`,
  `quote_assigned`). Obtiene S/R y define el rango `[A, B]` por índices de S/R
  (`border_a_sr_idx`, `border_b_sr_idx`) o valores fijos.
- **Tabla comparativa (núcleo):** una fila por cantidad de grillas (`min`..`max`),
  mostrando niveles, spread, capital por grilla, viabilidad de `min_notional`, %BTC
  alcanzable, break-even. Marca la fila `selected_grid`.
- **Análisis profundo del `selected_grid`:** candles con niveles punteados + S/R en
  negrita, curva de inventario cortada en el target, doble NAV (BRL/USDT), volumen
  de rebates.
- **Salida:** genera un bloque copiable "Config resuelta" para pasar al controller.

**Pendientes conocidos:**
- Orden de S/R por proximidad al precio (hoy por score).
- Columna "%BTC rango" hardcodeada (no discrimina por fila).
- "Break-even" redundante (mismo valor toda la fila).
- `inverse_siding` es un no-op (flag sin efecto).
- Modela el rebalanceo al punto medio del escalón, no al limit_price / distribución
  real de órdenes.

---

## 4. `soporte_resistencia` (v3.1) — el núcleo

Detector de soportes y resistencias. **Alimenta a casi todas las demás.**

- Doble pasada de pivots (window=10 estructural, window=3 táctico).
- Clustering ponderado por touches; scoring multifactor (touches, recency con curva
  inverted-U, structural, proximidad).
- Selección top N con **cobertura garantizada de extremos** (máx/mín absoluto si
  score suficiente).
- **v3 agregó** análisis de escenarios de posicionamiento (matriz NAV BRL × USDT por
  `(%BTC, precio_SR)`), con asunciones explícitas (USDT-BRL constante, rebalance sin
  fees, cash rinde 0).
- **v3.1 (patch de ploteo):** muestra TODOS los niveles en el gráfico (sin filtro de
  score), distingue structural (sólida) vs tactical (punteada), evita labels pisados.

**Evolución:** v1 (pierde extremos, ruido) → v2 (clustering + scoring + cobertura de
extremos) → v3 (escenarios + doble NAV) → **v3.1 (fix de visualización)**. Las
versiones previas son histórico.

**Pendientes conocidos:** scoring tiende a aplanarse (revisar pesos); tagging
structural/tactical a veces no discrimina. Sin volumen/POC/VWAP, sin detección de
régimen (futuro v4).

---

## 5. `grid_portfolio_analyzer` (v2) — propuesta de portfolio

Propone un portfolio de grillas como **combinación lineal de aportes esperados**.

- Lee estado (balance, grillas activas, precios), calcula NAV y el delta hacia el
  target. Invoca `soporte_resistencia` en daily e intraday.
- Diseña un vector de aportes (Δfavorable, Δdesfavorable, Δesperado) por grilla, con
  validación de que la suma converge al delta de campaña.
- Valida reglas duras (R1, R3, R5, R7...) contra la propuesta.

**No hace (futuro):** no ejecuta (`create_executor`), no persiste propuestas, no
confirma por Telegram, no auto-calibra por volatilidad. Es propuesta, no ejecución.

---

## 6. `target_scenario_table` (v1) — tabla de decisión de target

Construye una tabla **precio futuro × target %BTC** para decidir a qué % rebalancear.

- Filas: precios futuros (S/R + interpolados + actual). Columnas: targets (10%..100%)
  + una columna "Total" = NAV si NO rebalanceo.
- Cada celda: NAV resultante (en BRL y USDT) si rebalanceo hoy a ese % y el BTC va a
  ese precio. Énfasis visual en filas S/R y celdas con |Δ| grande.

Complementaria a las matrices de escenarios de `soporte_resistencia v3` — ofrece la
vista `(precio × target)` en lugar de `(%BTC × precio)`.

---

## 7. `grid_portfolio_reconciler` (v1 MVP) — feedback post-cierre

Se activa cuando una grilla cierra (event-driven).

- Lee el resultado de la grilla (`close_type`, ΔBTC real, ΔBRL real, ciclos, PnL,
  FER) y el snapshot del portfolio post-cierre.
- Calcula `aporte_real` (Δ%BTC) vs `aporte_esperado` del plan; aplica bandas de
  control (verde ≤3pp, amarilla ≤7pp, roja >7pp) y decide la acción por tabla
  `close_type × banda`.
- Mantiene contador de replans; sugiere abandono si supera el máximo. Dispara el
  Analyzer si corresponde re-planificar.

**No hace (futuro):** scan mode automático (hoy invocación manual por executor_id),
invocación automática del Analyzer, snapshots periódicos, estado en DB.

> **Nota:** el reconciler nació para el modelo de grillas sueltas/pares. Con el
> tablero, parte de su rol (medir el inventario movido por cada cierre) lo cumple
> ahora el propio controller con el inventario firme (`docs/CONTROLLER.md §6`). Su
> futuro depende de cómo evolucione la coexistencia controller-vs-routines.

---

## 8. Validación de sintaxis de una routine

Antes de dar por buena una edición de la routine, validar con el venv de Condor:

```bash
/Users/tomasgaudino/PycharmProjects/condor/.venv/bin/python -c \
  "import ast; ast.parse(open('trading_agents/grigado/routines/chessboard_lab.py').read())"
```

---

## 9. Routines vivas vs históricas

| Routine | Versión viva | Estado |
|---------|--------------|--------|
| `chessboard_lab` | v2 | **principal** |
| `soporte_resistencia` | v3.1 | **núcleo** |
| `grid_portfolio_analyzer` | v2 | soporte |
| `target_scenario_table` | v1 | soporte |
| `grid_portfolio_reconciler` | v1 MVP | soporte (rol en revisión) |

Las versiones previas de cada routine (S/R v1/v2/v3, analyzer v1, etc.) son
histórico superado; su lógica vigente quedó resumida acá.
