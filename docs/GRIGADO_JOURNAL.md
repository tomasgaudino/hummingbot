# Grigado — Journal de desarrollo de la estrategia

> **Qué es este archivo:** el ABC del proyecto. Objetivo, diseño, decisiones
> tomadas, estado actual, y brechas conocidas. Sirve para recargar contexto en
> una sesión nueva (ver comando `/grigado`) y para chequear que no hagamos
> cosas contradictorias.
>
> **Última actualización:** 2026-06-04
> **Estado:** trabajo intenso en la ROUTINE (laboratorio de diseño) — modelo de
> dimensionamiento de grillas al recorrido de inventario (techo↔piso). El controller
> tiene los fixes de cobertura/capital aplicados y testeado (37 tests). Branch
> `grigado` pusheado al fork `drupman`. La sesión reciente NO tocó el controller:
> todo el avance fue en `condor/.../chessboard_lab.py`. Ver §11 (lo más fresco).

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

### Controller: capital por slot — ✅ HECHO (2026-06-04)

- ~~Capital por grilla/lado~~ **RESUELTO.** El controller dimensiona al recorrido
  techo↔piso en vivo (`_recorrido_quote` + `_capital_for(side, idx)`), coincide
  exacto con la routine. Ver §11.5. Nuevo campo config `techo_pct_btc`.
- **Pendiente de validar en datos reales:** que el dimensionamiento asimétrico
  funcione con capital real (las LONG piden mucho más que las SHORT → el balance
  libre puede capar; el ⊘ de zona sin munición cubre ese caso). Relanzar y mirar.

### Otros pendientes

- **v2 del corte en target:** reconciliar exacto el %BTC con los fills reales.
- **Asimetría de densidad:** modelar el reprice maker en la routine para que la
  curva teórica coincida mejor.
- **Benchmark formal:** correr el teórico vs el real y medir el drift de cada
  parámetro operativo, decidir cuáles usar/desestimar.
- **inverse_siding:** evaluar si aporta (está como flag, sin usar).
- ~~Columnas de la routine "%BTC rango" / "Break-even"~~ — **RESUELTO** (§11.1.5).
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

## 11. Laboratorio (routine `chessboard_lab`) — TRABAJO RECIENTE (2026-06-04)

> Toda esta sección es lo más fresco. La routine vive en
> `/Users/tomasgaudino/PycharmProjects/condor/trading_agents/grigado/routines/chessboard_lab.py`.
> Validar sintaxis: `condor/.venv/bin/python -c "import ast; ast.parse(open('trading_agents/grigado/routines/chessboard_lab.py').read())"`.
> El controller NO se tocó en esta sesión; todo el avance fue en la routine.

### 11.1 Fixes de la routine (cronológico)
1. **`''` (UI) → None.** La UI de Condor manda los campos vacíos como string `''`,
   no `None`; pydantic v2 con `Optional[float]` revienta al parsear `''`. Fix:
   `field_validator(mode="before")` que mapea `''`→None en base_assigned,
   quote_assigned, border_a, border_b, target_levels_per_grid.
2. **Vacío = balance.** base_assigned/quote_assigned default `None`; vacío toma del
   balance real (× portfolio_pct). Quita los balances reales hardcodeados.
3. **YAML 1:1 con el controller.** La routine genera el YAML con nombres EXACTOS de
   ChessboardConfig (spread_per_subrange→min_spread_between_orders, etc.), pegable
   directo. Sin traducción manual.
4. **Niveles objetivo → despeja spread.** Campo `target_levels_per_grid`: fijás N
   niveles por grilla y la routine DESPEJA el spread (`spread = ancho/(m·precio)`),
   verificado contra la fórmula real del executor. Resolvió el "1 nivel ridículo".
   La tabla ahora capa niveles por capital (min(ancho/spread, capital/min_order)),
   fiel al executor.
5. **Pulido cosmético:** sacada la estrella ⭐ (el %BTC real va en la anotación HOY),
   más altura a la curva, eliminada sección "Config resuelta — resumen", sacadas
   columnas "%BTC rango"/"Break-even" de la tabla y KPIs BTC/BRL tablero/% portfolio/
   Balance.

### 11.2 Curva de inventario — BUG REAL arreglado
El gráfico decía 100% donde el KPI decía 83% (divergencia >15pp). Causa: la curva
asumía "tablero completamente ejecutado desde A" (contaba TODAS las LONG por debajo
del precio), no tu inventario real. **Fix:** `_inventory_curve` ahora está ANCLADA
en el inventario actual y proyecta desde el precio actual: cuenta solo las grillas
que el precio cruzaría de current→P. En el precio actual da exactamente tu %BTC real
(coincide con el KPI). Subiendo → SHORT descargan; bajando → LONG cargan.

### 11.3 Dimensionamiento — el insight central de la sesión
**`total/N/2` está atado al NAV, NO al target.** Por eso sobredimensiona (en la
campaña real ~6×): una sola grilla ya cruza el target y el corte frena el resto →
1-2 saltos en vez de N. NO es una fuga; es que el capital por grilla debe ser
función del RECORRIDO de inventario deseado, no del NAV total.

**Matemática validada (clave: el NAV es invariante a comprar/vender** — intercambiás
BTC↔BRL al precio, así que `btc_para(%) = % · NAV / precio`, despeje cerrado, no
iterativo):
- Para descargar de %actual→piso: vender `x = (base·p − piso·NAV)/p`, repartido en N
  → N saltos parejos en %BTC.
- Recorrido entre DOS extremos (lo último que hicimos): `techo_pct_btc` (nuevo campo)
  = %BTC máx en A (carga máx, LONG) y `target_pct_btc` = piso en B (descarga máx,
  SHORT). La curva recorre techo→piso a lo largo de A→B.

**Helpers nuevos en la routine:**
- `_descarga_para_target(base, quote, price, target, n)` → descarga al piso.
- `_recorrido_inventario(base, quote, price, piso, techo)` → carga + descarga (dos
  extremos). Devuelve brl_descarga, brl_carga, btc_piso, btc_techo.
- `_build_grids(..., recorrido=, current_price=)` → dimensiona ASIMÉTRICO: reparte
  brl_descarga entre las SHORT arriba del precio y brl_carga entre las LONG abajo.

**Estado de la routine HOY:** la curva recorre el target↔techo (validado: 100% en A,
83.2% actual, 75.8% en B con techo=100%/piso=75%). La tabla muestra Cap. SHORT /
Cap. LONG (asimétrico) y Total grillas. El `total/N/2` se sacó de circulación.

### 11.4 Cómo se "juega" con la agresividad de la curva (para el usuario)
- **techo_pct_btc / target_pct_btc** = los dos extremos → cuánto inventario recorrés
  (más amplio = más agresivo en %BTC).
- **Rango A-B** = la pendiente: más angosto = curva más empinada (mismo movimiento de
  precio mueve más inventario) = más agresivo.
- **N** = en cuántos saltos se reparte.
- **El %BTC actual** parte la curva: define cuánto va a carga (abajo) vs descarga
  (arriba). Asimetría real cuando el precio actual está cerca de un borde.

### 11.5 CONTROLLER YA EJECUTA EL MODELO (2026-06-04, cierre de sesión)
**La brecha routine↔controller está CERRADA.** El controller ahora dimensiona cada
grilla al recorrido techo↔piso, igual que la routine:
- Config nuevo: `techo_pct_btc` (default 1.0) = %BTC máx en border_a (carga, LONG).
  `target_pct_btc` = piso en border_b (descarga, SHORT).
- `_recorrido_quote()` (en `chessboard.py`) replica `_recorrido_inventario` de la
  routine: NAV invariante, `btc_para(%) = %·NAV/precio`, devuelve (brl_descarga, brl_carga).
- `_capital_for(side, idx)` reparte brl_descarga entre las SHORT por encima del precio
  y brl_carga entre las LONG por debajo. Capado por balance libre (anti-INSUFFICIENT).
  Fallback a total/N/2 si no hay datos de recorrido.
- **Verificado: coincidencia EXACTA con la routine** (mismos números: descarga R$24.638
  + carga R$50.700; cap SHORT R$8.213, cap LONG R$50.700). 38 tests verdes.

El capital es asimétrico por slot (SHORT≠LONG, y por escalón vía centro de masa) — el
controller lo calcula en vivo al crear cada grilla, no lo recibe slot-por-slot. El
YAML de la routine sigue llevando `total_amount_quote` aprox (informativo); lo que
manda ahora es el dimensionamiento interno del controller a partir de target_pct_btc +
techo_pct_btc + base/quote_assigned.

### 11.6 TARGET LOCAL POR ESCALÓN + histéresis (2026-06-05) — BUG de oscilación
**Problema real observado en producción:** vendió MÁS BTC del previsto. Causa (hipótesis
del usuario, confirmada con el JSONL): cuando el precio OSCILA en un borde de relevo,
cada cruce dispara un ciclo completo — SHORT cierra por TP → releva SHORT abajo (vende
todo su capital al nacer) → sube → cierra HOLD → baja → compra todo → ... El corte de
target GLOBAL no protege porque el relevo recrea la grilla y ésta descarga al poblarse.
Dato: `cb_0_S` relevado 63 veces, `cb_0_L` 61 veces (oscilación pura). 269 TAKE_PROFIT
vs 20 POSITION_HOLD — el inventario se movía por ciclos de TP, no por rebalanceo firme.

**Fix (decisión del usuario): target LOCAL por escalón, no global.** Cada escalón tiene
su %BTC objetivo = la curva determinística en su precio medio (lineal techo en border_a
→ piso en border_b). Una SHORT de cb_i frena si %BTC ≤ target_local(i); una LONG si
%BTC ≥ target_local(i). Así en precios BAJOS (target local alto, ~98%) las SHORT NO
venden → no se liquida todo en el fondo; en precios ALTOS (target ~75%) sí descargan.
La descarga se distribuye por el rango. Helpers: `_target_local(idx)`,
`_blocked_by_local_target(idx, side)` (reemplazan `_target_reached` global).

**+ Histéresis** (`hysteresis_pct`, default 0.1 = 10% del ancho del ESCALÓN en cada
borde; 0.5 = bloquea todo): no poblar la grilla si el precio está pegado a un borde →
mata el churn de relevo. Helper `_in_hysteresis_band`. Solo frena CREACIÓN/relevo (no
el ciclo intra-grilla del executor por ahora).

**MATIZ CLAVE (2026-06-05): histéresis SOLO en relevo desde TAKE_PROFIT.** Observación
del usuario: la histéresis solo importa para la consecutiva que ABRE posición (vende/
compra TODO su capital al nacer), que es la que releva un cierre TAKE_PROFIT (SHORT TP
abajo → SHORT nueva abajo vende todo; LONG TP arriba → LONG nueva compra todo). El
relevo por POSITION_HOLD arranca operando normal (no abre de golpe) → NO necesita
histéresis. Despliegue inicial y asegurar-par tampoco. Implementado con flag
`apply_hysteresis` en `_create_if_possible`, =True solo en el relevo cuando
`close_type == TAKE_PROFIT`.

**Sin gap con la routine:** el valor `hysteresis_pct` viaja routine→YAML→controller
idéntico. La routine NO modela la histéresis en la curva (correcto: la curva proyecta
el recorrido determinístico IDEAL sin oscilación; la histéresis protege en producción
contra el churn real, no cambia la trayectoria ideal).

Verificado con la config real: targets locales cb_0=97.9% ... cb_5=77.1%; con %BTC 83%
la SHORT de cb_0 queda bloqueada (no vende en precio bajo) y la de cb_5 descarga. 38
tests verdes. PENDIENTE: validar en datos reales que la oscilación ya no fuga el target.

---

## Apéndice — Cómo mirar el estado en producción

- **Consola Hummingbot:** comando `status` muestra el tablero (escalones,
  grillas activas L✓ S✓, relevadas), executors recientes, performance.
- **Binance:** verificar que las órdenes abiertas digan "Limit Maker" / "Post Only".
- **Barra inferior del status:** `Trades / Total P&L / Return %` — el P&L real
  incluyendo fills (distinto del unrealized que muestra el controller arriba).
