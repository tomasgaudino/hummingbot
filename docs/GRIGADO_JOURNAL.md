# Grigado — Journal de desarrollo de la estrategia

> **Qué es este archivo:** el ABC del proyecto. Objetivo, diseño, decisiones
> tomadas, estado actual, y brechas conocidas. Sirve para recargar contexto en
> una sesión nueva (ver comando `/grigado`) y para chequear que no hagamos
> cosas contradictorias.
>
> **Última actualización:** 2026-06-02
> **Estado:** controller corriendo en producción con capital real (primera campaña).
> Fase 1 del hedge (inventario firme con signo + conciliación + KPI log) completada
> en código y testeada; pendiente validación en datos reales (Fase 2) y hedge perp
> (Fase 3). Ver §6 y §9.

---

## 1. Objetivo

Sistema de trading de grillas para **BTC-BRL en Binance** que **gestiona el ratio
de inventario del portfolio** (cuánto tener en BTC vs BRL), explotando que la
cuenta tiene **rebate de +0.015% por orden maker**.

- **El ratio manda, no el PnL.** El objetivo es mover el %BTC hacia un target
  (ej. de 80% → 60%), capturando rebates en el camino. El PnL en BRL es subproducto.
- **Maker o nada.** Toda orden debe ser LIMIT_MAKER. Una ejecución a market
  rompe el modelo económico (pierde rebate + paga fee).
- **Capital aislado.** El tablero opera sobre una sub-cuenta lógica (base+quote
  asignados), porque otras estrategias usan el mismo portfolio.

---

## 2. La estrategia: "tablero de ajedrez" (chessboard)

El rango entre un soporte A y una resistencia B se divide en **N escalones
contiguos**. Cada escalón tiene un **par LONG + SHORT** operando simultáneamente
en esa banda de precio.

- **Un solo par activo a la vez:** el del escalón que contiene el precio.
- **Relevo asimétrico:** cuando una grilla cierra, se activa la consecutiva
  según el `close_type`:
  - `TAKE_PROFIT` (favorable) → releva en la dirección del movimiento favorable.
  - `POSITION_HOLD` (desfavorable, por limit_price con keep_position) → releva
    en la dirección desfavorable.
  - Tabla: LONG TP → LONG +1 (arriba) · LONG limit → LONG -1 (abajo) ·
    SHORT TP → SHORT -1 (abajo) · SHORT limit → SHORT +1 (arriba).
- **Dos cadenas independientes** (LONG y SHORT) que nunca se pisan.
- **Corte en target:** el controller deja de crear grillas SHORT cuando el
  %BTC de su sub-cuenta llega al target. La carga (LONG) sigue habilitada.

Detalle técnico clave: con `keep_position=true`, el grid executor cierra por
limit_price como `POSITION_HOLD` (no STOP_LOSS), y por TP global como
`TAKE_PROFIT`. Esa es la distinción que usa el relevo asimétrico.

---

## 3. Las dos perillas de calibración

1. **Ancho / N grillas:** define el capital por grilla (`total / N / 2` por los
   dos slots del par) y el ancho de cada escalón.
2. **Distancia del limit_price (`limit_distance_pct`):** define la zona muerta
   y el centro de masa del rebalanceo (a qué precio promedio queda el inventario).

---

## 4. Arquitectura: routine (diseño) + controller (ejecución)

**Routine `chessboard_lab`** (en Condor): el laboratorio. Iterás configs,
te muestra tabla comparativa (viabilidad min_notional por N) + gráficos
(candles con niveles, curva de inventario cortada en target, NAV estrategia vs
hold, volumen de rebates). Genera un **bloque copiable "Config resuelta"** para
pasar al controller.
- Path: `condor/trading_agents/grigado/routines/chessboard_lab.py`

**Controller `chessboard`** (en Hummingbot): la ejecución real. Corre en el
control loop de Hummingbot (sin agente externo, sin delay). Despliega el par
del escalón actual, releva por close_type, corta en target midiendo el %BTC
de su sub-cuenta de forma aislada.
- Path: `hummingbot/controllers/generic/chessboard.py`
- Tests: `hummingbot/test/hummingbot/strategy_v2/controllers/test_chessboard.py` (16 tests)
- Backtest: `hummingbot/scripts/backtest_chessboard.py`
- Config YAML: `hummingbot/conf/controllers/chessboard-btc-brl-1.yml`
- Wrapper: `hummingbot/conf/scripts/conf_v2_chessboard_btc_brl.yml`

**Lanzar:** `start --v2 conf_v2_chessboard_btc_brl.yml` (en consola Hummingbot dev-2.15.0).

---

## 5. Decisiones tomadas (cronológico, las que importan)

1. **El target es punto de corte, no decoración.** La curva de inventario se
   aplana en el target (routine), y el controller deja de descargar al llegar
   (no crea más SHORT). La carga sigue.
2. **Corte medido sobre la sub-cuenta, no el balance global.** Autogestionado:
   el controller parte de base+quote asignados y suma el BTC en posiciones/held
   de SUS grillas. No toca el balance del connector (que comparten otras
   estrategias). Simplificación v1: no reconcilia exacto las ventas SHORT contra
   el base inicial — suficiente para el corte, afinar en v2 si difiere mucho.
3. **Pares LONG+SHORT por escalón** (no un solo executor). Resolvió un bug donde
   dos grillas del mismo lado se pisaban.
4. **Relevo asimétrico fiel** (TP vs limit), no el "relevo simple" inicial.
5. **Capital por grilla = total / N / 2** (÷2 por los dos slots). La routine
   refleja esto (igual que el controller).
6. **S/R por proximidad al precio**, no por score (el código se cambió).
7. **`inverse_siding`** existe como flag pero se dejó de lado por ahora.
8. **El hedge se ajusta por EVENTO de cierre firme, no por seguimiento continuo**
   (2026-06-02). El plan completo es: las grillas spot (binance) mueven BTC y un
   short perpetual (binance_perpetual) lo cubre, de modo que la única variación
   del NAV sea el PnL de rebates. El short NO sigue el inventario tick a tick (es
   entrópico y falla): se ajusta SOLO cuando una grilla cierra dejando inventario
   firme. Cierre por TAKE_PROFIT (inventario neutro) → el short NO se toca. Cierre
   por POSITION_HOLD → el short se ajusta por el ΔBTC firme exacto de ese cierre.
9. **Inventario firme con signo como señal-setpoint del hedge** (2026-06-02). El
   controller reconstruye el ΔBTC firme CON SIGNO desde `held_position_orders`
   (BUY=+, SELL=−), porque `held_position_value` viene sin signo y sumarlo en
   positivo era un error fatal para el hedge (una SHORT que vendió BTC sumaba como
   si comprara). El short sigue el BTC spot firme (`base_assigned + Σ ΔBTC`), NO el
   capital de las grillas. Esto además corrige el corte en target (cierra §7.4).
10. **Asegurar el par en el escalón del precio** (2026-06-03). Bug observado en
    producción: el escalón del precio quedaba con un solo lado del par (ej. `cb_2`
    con SHORT activa pero sin LONG). Causa: el relevo propaga cada cadena (LONG /
    SHORT) por separado, así que en un movimiento sostenido un escalón termina con
    un solo lado → hueco de cobertura. Fix: una 3ª fase en `determine_executor_actions`
    crea el lado faltante del escalón que contiene el precio (invariante CB4).
11. **Guarda de no-doble-relevo por executor.id, no por level_id** (2026-06-03).
    Bug peor observado después: **0 grillas activas**. Causa raíz: `_closed_handled`
    (por level_id) bloqueaba recrear un slot PARA SIEMPRE, pero el precio re-visita
    escalones. Cuando el precio volvía a un escalón cuyos dos lados ya habían sido
    relevados, ni el relevo ni la Fase 3 creaban nada → tablero vacío. Fix: la guarda
    de no-doble-relevo ahora es por `executor.id` (cada cierre es único, como
    `_inventory_handled`), y `_create_if_possible` solo evita duplicar lo ACTIVO (o
    ya encolado en el tick), no lo relevado. Así un slot se repuebla cuando el precio
    vuelve. `_closed_handled` queda solo para el display "·". Tests de regresión:
    `test_pair_repopulated_when_price_returns`.
12. **Guarda de banda: solo crear grillas con el precio DENTRO del escalón**
    (2026-06-03). Bug peor de todos: loop de ~1000 cierres en una banda en 1.4h,
    grillas con `filled=0` cerrando por TAKE_PROFIT al instante. Causa raíz: el grid
    executor cierra al NACER (en `on_start`, antes de colocar órdenes) una grilla
    cuyo precio ya está fuera de su banda (`mid > end` para LONG, `mid < start` para
    SHORT) — emite TAKE_PROFIT con inventario 0. El relevo + asegurar-par del
    controller la recreaban en loop, sin cooldown. Fix: `_create_if_possible` ahora
    NO crea una grilla si el precio está fuera de `[low, high]` del escalón. Solo
    nacen grillas en el escalón que contiene el precio → ninguna nace muerta → loop
    cortado. Implica que el modelo opera el par del escalón del precio (no grillas
    dormidas en escalones vecinos); el relevo sigue como intención pero su grilla
    destino solo se crea cuando el precio entra a esa banda (vía Fase 3). Verificado:
    grilla fuera de banda recreada 0 veces. Tests actualizados (precio en el escalón
    destino del relevo).
13. **Capital por grilla según balance LIBRE + zona sin munición** (2026-06-04).
    Bug observado: grillas mueren por INSUFFICIENT_BALANCE. Causa: el controller
    asignaba `total/N/2` (con total = NAV total BTC+BRL), pero una LONG solo gasta
    BRL y una SHORT solo BTC. Con sub-cuenta 79% BTC / 21% BRL, las LONG no tenían
    munición (entra 1, la 2ª falla). Es estructural: con capital fijo y precio
    fluctuando, un lado se agota (bajando se acaba el BRL; vendiendo/target 0% el
    BTC). Decisión del usuario: priorizar descarga (SHORT). Fix:
    - `_capital_for(side)` = min(nominal, balance LIBRE del lado) vía
      `get_available_balance` (LONG→quote, SHORT→base valuado a quote). Nunca pide
      más de lo que hay.
    - Guarda de munición en `_create_if_possible`: si el capital libre < min_order
      (R$20), NO crea la grilla → la marca como **zona sin munición** (`⊘` en el
      status) y abre un evento `no_capital` (estadía con inicio/fin por relevos, no
      por tick). Se cierra cuando el precio sale del escalón o vuelve el capital.
    - **JSONL de eventos** (`data/chessboard_events_<id>.jsonl`) reemplaza el KPI
      CSV: dos tipos `rebalance` (capital AISLADO de la grilla: assigned/filled/held/
      pnl/bep/delta_btc, sin sesgo del connector global) y `no_capital`. El hedge se
      ajusta por evento discreto, no por curva continua → no necesita el CSV.
    - Status: marca `⊘` (L⊘/S⊘) + línea "Capital insuficiente: cb_X LADO (falta
      asset)". Es resultado de la corrida para mejora continua: muestra dónde el
      tablero se quedó sin munición (límite natural de la campaña).
    Tests: `_capital_for` capa al libre; LONG sin BRL marca zona; recuperación;
    cierre por salida del precio; persistencia JSONL. 37 verdes.

---

## 6. Estado actual (2026-06-02)

**Corriendo en producción**, primera campaña con capital real:
- Sub-cuenta: ~0.00668 BTC + R$ 550 = NAV ~R$ 2784.
- %BTC inicial 80% → target 60%.
- Rango: 300.732 → 359.151, n_grids=4. Precio al lanzar ~334.6k (cae en cb_2).
- Confirmado en runtime: par LONG+SHORT desplegado en cb_2, todas Limit Maker,
  primeros trades en positivo (+0.14%), relevo aún no disparado.

**Fase 1 del hedge COMPLETADA en código** (2026-06-02): el controller ahora mide
el inventario firme con signo, registra cada cierre como evento discreto
(`_rebalance_events`), concilia (`tp_residual_btc`, `hold_btc`,
`unexpected_close_btc`, `in_flight_btc`) y emite un KPI log CSV
(`data/chessboard_kpi_<id>.csv`) que registra fila SOLO en cambios de inventario
firme (para graficar spot vs short y ver los cruces en cada rebalanceo). 26 tests
verdes. El status muestra la línea de inventario firme con banderas ⚠ si el TP
deja residual o aparece un cierre inesperado. Pendiente: validar en datos reales
(Fase 2) y construir el hedge perp (Fase 3).

---

## 7. Brechas teórico-vs-real conocidas (input de benchmark)

Cosas donde el modelo teórico (routine) difiere de la ejecución real. NO son
bugs, son simplificaciones aceptadas. Importa trackearlas para no sacar
conclusiones erradas:

1. **Distribución de órdenes no uniforme.** El executor comprime los levels
   cerca del precio (por `safe_extra_spread` reprice maker), mientras la routine
   asume distribución uniforme. Afecta break-even y volumen real vs proyectado.
2. **Drift operativo.** Parámetros que desvían del teórico determinístico:
   `max_orders_per_batch`, `activation_bounds`, `order_frequency`,
   `max_open_orders`, `safe_extra_spread`. El teórico es la baseline; el drift
   se mide con grillas reales.
3. **Backtest sin rebate.** El backtest usa `trade_cost` (cobra fee), pero la
   cuenta real tiene rebate +0.015%. El backtest es PESIMISTA respecto a la
   economía real. No descartar configs por PnL chico en backtest.
4. **%BTC del corte simplificado.** ~~El controller suma BTC en posiciones/held
   pero no reconcilia exacto las ventas SHORT contra el base inicial.~~
   **CERRADA (2026-06-02):** el controller ahora mide el inventario firme con
   signo desde `held_position_orders` (BUY=+, SELL=−), no suma quotes sin signo.
   El %BTC se calcula sobre `base_assigned + Σ ΔBTC firme`. Queda como límite
   conocido: el inventario *en vuelo* (position_size_base de grillas activas) no
   se cuenta como firme — es transitorio y el hedge lo ignora a propósito.
5. **Centro de masa = punto medio.** La routine modela el rebalanceo al punto
   medio del escalón, no a la distribución real de órdenes.

---

## 8. Reglas duras (no contradecir)

- R1: Toda orden LIMIT_MAKER (open y TP). Verificar en Binance que entren maker.
- R2: `keep_position=true` obligatorio (hace que limit_price cierre como
  POSITION_HOLD, necesario para el relevo asimétrico).
- R3: Nunca `stop_loss` en el triple_barrier (cierra a market, rompe maker).
- R4: El corte en target solo aplica a SHORT (descarga). La carga (LONG) sigue.
- R5: Capital por grilla debe superar min_notional de Binance (R$ 20).
- R6: El %BTC se mide sobre la sub-cuenta asignada, nunca el balance global.

---

## 9. Roadmap / pendientes

### Hedge perpetual (NAV neutro) — el camino crítico

- **Fase 1 ✅ (2026-06-02):** inventario firme con signo + eventos de rebalanceo +
  conciliación + KPI log en el controller. Ver §6.
- **Fase 2 (pendiente, datos reales):** validar empíricamente que `tp_residual_btc`
  ≈ 0 tras cierres por TAKE_PROFIT. El executor solo liquida la posición de un TP
  si `position_size_base >= min_order_size`; un residual chico podría quedar sin
  liquidar y, con la regla "TP no toca el short", quedaría descubierto. Si el
  residual no es ≈0, el hedge debe cubrir también el residual de TP (cambia el
  trigger de Fase 3). Mirar el status enriquecido y el KPI log con la campaña viva.
- **Fase 3 (pendiente, hedge perp):** controller que consume `net_btc_from_grids`
  (o los eventos discretos) y mantiene un short en binance_perpetual igual al BTC
  spot firme. Patrón de referencia: `controllers/generic/hedge_asset.py`. Ajuste
  por evento POSITION_HOLD (cantidad exacta), LIMIT_MAKER para cobrar rebate también
  en el perp, conciliación cruzada `short_perp_btc == net_btc_spot`. Decisiones
  abiertas: ¿un controller con dos patas o dos coordinados? ¿ajuste por evento o
  por gap agregado con cooldown? Se resuelven con los datos de Fase 2.

### Otros pendientes

- **v2 del corte en target:** reconciliar exacto el %BTC con los fills reales.
- **Asimetría de densidad:** modelar el reprice maker en la routine para que la
  curva teórica coincida mejor.
- **Benchmark formal:** correr el teórico vs el real y medir el drift de cada
  parámetro operativo, decidir cuáles usar/desestimar.
- **inverse_siding:** evaluar si aporta (está como flag, sin usar).
- **Columnas de la routine:** "%BTC rango" hardcodeada y "Break-even"
  redundante (mismo valor toda fila) — pendientes de arreglo.
- **Re-planificación al cierre de campaña:** cuando se alcanza el target, qué
  hace el sistema (¿nueva campaña? ¿para?).

---

## 10. Docs relacionados

Tras el refactor de 2026-06-02, `docs/` quedó con 4 documentos (lo demás se borró
por redundante/histórico — recuperable en git history):

- `docs/MANIFIESTO.md` — la tesis de la estrategia: tablero, reglas duras, pares
  hedged (futuro), hedge perpetual. Consolida los ex-docs de estrategia.
- `docs/CONTROLLER.md` — lógica pura del controller con diagramas mermaid
  (auditable): control loop, relevo asimétrico, inventario firme, corte en target.
- `docs/ROUTINES.md` — manual consolidado de las routines de Condor (laboratorio).
- `docs/GRIGADO_JOURNAL.md` — este archivo: bitácora de decisiones y estado.

---

## Apéndice — Cómo mirar el estado en producción

- **Consola Hummingbot:** comando `status` muestra el tablero (escalones,
  grillas activas L✓ S✓, relevadas), executors recientes, performance.
- **Binance:** verificar que las órdenes abiertas digan "Limit Maker" / "Post Only".
- **Barra inferior del status:** `Trades / Total P&L / Return %` — el P&L real
  incluyendo fills (distinto del unrealized que muestra el controller arriba).
