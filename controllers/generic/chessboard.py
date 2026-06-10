"""
Chessboard Controller — tablero de grillas con pares LONG+SHORT y relevo asimétrico.

Modelo (docs/CONTROLLER.md; tesis en docs/MANIFIESTO.md; bitácora en docs/GRIGADO_JOURNAL.md):
  - El rango [A, B] se divide en N escalones contiguos.
  - Cada escalón tiene DOS slots: una grilla LONG y una grilla SHORT.
  - En todo momento opera UN solo par: el del escalón que CONTIENE el precio
    (1 LONG + 1 SHORT simultáneas en esa banda).
  - Cuando una grilla del par cierra, se activa su consecutiva según close_type
    (relevo ASIMÉTRICO). Las cadenas LONG y SHORT avanzan independientes.

Relevo asimétrico (con keep_position=True el grid executor cierra así):
  - close_type TAKE_PROFIT  = favorable (precio salió del rango a favor).
  - close_type POSITION_HOLD = desfavorable (precio cruzó el limit_price; mantiene inventario).

  | grilla | cierre favorable (TP) | cierre desfavorable (POSITION_HOLD) |
  |--------|-----------------------|-------------------------------------|
  | LONG   | relevar LONG  +1 (arriba) | relevar LONG  -1 (abajo)            |
  | SHORT  | relevar SHORT -1 (abajo)  | relevar SHORT +1 (arriba)           |

  Regla: TP releva hacia el movimiento favorable de ese side; limit hacia el desfavorable.
"""

import json
import os
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, OrderType, PriceType, TradeType
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType


class ChessboardConfig(ControllerConfigBase):
    """Configuración del tablero de grillas (pares LONG+SHORT por escalón)."""
    controller_type: str = "generic"
    controller_name: str = "chessboard"

    connector_name: str = "binance"
    trading_pair: str = "BTC-BRL"
    leverage: int = 1  # spot

    # Bordes del tablero
    border_a: Decimal = Field(json_schema_extra={"is_updatable": True})
    border_b: Decimal = Field(json_schema_extra={"is_updatable": True})

    # Inventario asignado al tablero (sub-cuenta lógica). El controller mide su
    # %BTC sobre ESTO, no sobre el balance global del connector (que comparten
    # otras estrategias). Autogestionado: parte de acá y suma lo que sus grillas operan.
    base_assigned: Decimal = Field(json_schema_extra={"is_updatable": True})
    quote_assigned: Decimal = Field(json_schema_extra={"is_updatable": True})

    # Los DOS extremos del recorrido de inventario (definen el dimensionamiento de
    # cada grilla, igual que la routine chessboard_lab):
    #   target_pct_btc = PISO  (en border_b, precio alto): las SHORT descargan hasta acá.
    #   techo_pct_btc  = TECHO (en border_a, precio bajo): las LONG cargan hasta acá.
    # El controller CORTA la descarga (no crea más SHORT) cuando el %BTC <= target.
    # None en target = sin corte. El capital por grilla se dimensiona al recorrido
    # techo->piso (NO total/N/2): cada SHORT descarga su porción de (actual-piso),
    # cada LONG carga su porción de (techo-actual). Ver _capital_for.
    target_pct_btc: Optional[Decimal] = Field(default=None, json_schema_extra={"is_updatable": True})
    techo_pct_btc: Decimal = Field(default=Decimal("1.0"), json_schema_extra={"is_updatable": True})

    # Histéresis: franja (fracción del ancho del escalón) pegada a cada borde donde NO
    # se puebla la grilla. Evita el churn de relevo cuando el precio oscila en un borde.
    # 0 = sin histéresis. 0.1 = no crear si el precio está en el 10% más cercano a un borde.
    hysteresis_pct: Decimal = Field(default=Decimal("0.1"), json_schema_extra={"is_updatable": True})

    # Geometría
    n_grids: int = Field(default=10, json_schema_extra={"is_updatable": True})
    total_amount_quote: Decimal = Field(default=Decimal("1000"), json_schema_extra={"is_updatable": True})

    # Perilla del limit (zona muerta / centro de masa)
    limit_distance_pct: Decimal = Field(default=Decimal("0.005"), json_schema_extra={"is_updatable": True})

    # Parámetros comunes de cada grilla
    min_spread_between_orders: Decimal = Field(default=Decimal("0.001"), json_schema_extra={"is_updatable": True})
    min_order_amount_quote: Decimal = Field(default=Decimal("20"), json_schema_extra={"is_updatable": True})
    max_open_orders: int = Field(default=5, json_schema_extra={"is_updatable": True})
    max_orders_per_batch: Optional[int] = Field(default=2, json_schema_extra={"is_updatable": True})
    order_frequency: int = Field(default=5, json_schema_extra={"is_updatable": True})
    activation_bounds: Optional[Decimal] = Field(default=None, json_schema_extra={"is_updatable": True})

    # keep_position=True es OBLIGATORIO para el modelo: hace que el cierre por
    # limit_price sea POSITION_HOLD (desfavorable) en vez de liquidar a market,
    # y permite distinguirlo del TAKE_PROFIT en el relevo asimétrico.
    keep_position: bool = Field(default=True, json_schema_extra={"is_updatable": True})

    triple_barrier_config: TripleBarrierConfig = TripleBarrierConfig(
        take_profit=Decimal("0.001"),
        open_order_type=OrderType.LIMIT_MAKER,
        take_profit_order_type=OrderType.LIMIT_MAKER,
    )

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class Chessboard(ControllerBase):
    def __init__(self, config: ChessboardConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        # Escalones del rango. Cada escalón tiene dos slots identificados por
        # level_id: "cb_{i}_L" (LONG) y "cb_{i}_S" (SHORT).
        self._steps: List[dict] = self._build_steps()

        # level_id -> executor_id de las grillas activas (se repopula cada tick).
        self._level_executor: Dict[str, str] = {}
        # executor.id cuyo cierre ya RELEVAMOS (para no relevar dos veces el mismo
        # cierre). Clave por id (único), NO por level_id: un slot se re-visita cuando
        # el precio vuelve, y debe poder recrearse. Indexar por level_id bloqueaba
        # el slot para siempre -> dejaba el escalón del precio sin grillas.
        self._relayed_executor_ids: set[str] = set()
        # Slots cuyo relevo-desde-TP fue bloqueado por histéresis. Mientras el precio
        # siga en la franja, NINGUNA vía los crea (la Fase 3 asegurar-par tampoco:
        # sin esto la recreaba en el mismo tick y la histéresis era inefectiva).
        # Se libera cuando el precio llega al interior del escalón (fuera de la franja).
        self._tp_hysteresis_pending: set[str] = set()
        # level_ids que fueron relevados ALGUNA vez (solo para el display "·").
        self._closed_handled: set[str] = set()

        # ── Inventario firme (señal-setpoint para el hedge perp) ─────────────
        # executor.id cuyo cierre ya contabilizamos para el inventario. OJO: clave
        # por executor.id (único), NO por level_id (un level se recrea en relevos).
        self._inventory_handled: set[str] = set()
        # Eventos discretos de rebalanceo (cada cierre que dejó inventario firme).
        # {executor_id, level_id, close_type, delta_btc, timestamp}
        self._rebalance_events: List[dict] = []
        # BTC firme neto acumulado por las grillas (con signo: LONG hold +, SHORT hold -).
        self._net_btc_from_grids: Decimal = Decimal("0")
        # Quote firme neto acumulado, espejo del BTC (SELL +quote, BUY -quote). Permite
        # el %BTC con NAV invariante: al vender base, el quote sube (no se encoge el NAV).
        self._net_quote_from_grids: Decimal = Decimal("0")

        # ── Zonas sin munición (capital insuficiente para abrir la grilla) ───
        # level_id -> evento no_capital ABIERTO (estadía vigente del precio en una
        # zona donde no se pudo abrir la grilla). Se cierra cuando el precio sale o
        # vuelve el capital. Es el estado vivo para el display ⊘.
        self._no_capital_open: Dict[str, dict] = {}
        # Contador de zonas sin munición ya cerradas (para el status).
        self._no_capital_closed_count: int = 0

        # Registro único de eventos (JSONL): rebalance + no_capital. Reemplaza el
        # KPI CSV. El hedge se ajusta por evento discreto, no por curva continua.
        self._events_path = os.path.join("data", f"chessboard_events_{self.config.id}.jsonl")

        # Snapshots periódicos teórico-vs-real (JSONL) para graficar el drift en el
        # tiempo. Uno cada ~60s. Ver _maybe_snapshot / _build_snapshot.
        self._snapshots_path = os.path.join("data", f"chessboard_snapshots_{self.config.id}.jsonl")
        self._last_snapshot_ts: Optional[float] = None
        self._snapshot_interval_s = 60.0

        # Timestamp de arranque del controller (base de la tasa de rotación).
        self._start_timestamp: Optional[float] = None

        self.initialize_rate_sources()

    def initialize_rate_sources(self):
        self.market_data_provider.initialize_rate_sources([
            ConnectorPair(connector_name=self.config.connector_name,
                          trading_pair=self.config.trading_pair)
        ])

    # ── Construcción del tablero ─────────────────────────────────────────────
    def _build_steps(self) -> List[dict]:
        a, b, n = self.config.border_a, self.config.border_b, self.config.n_grids
        ancho = (b - a) / n
        steps = []
        for i in range(n):
            low = a + ancho * i
            high = low + ancho
            steps.append({"idx": i, "low": low, "high": high})
        return steps

    def _level_id(self, idx: int, side: TradeType) -> str:
        return f"cb_{idx}_{'L' if side == TradeType.BUY else 'S'}"

    def _parse_level_id(self, level_id: str):
        """'cb_3_L' -> (3, TradeType.BUY). Devuelve (None, None) si no matchea."""
        try:
            _, idx_s, side_s = level_id.split("_")
            side = TradeType.BUY if side_s == "L" else TradeType.SELL
            return int(idx_s), side
        except (ValueError, AttributeError):
            return None, None

    def _limit_for(self, step: dict, side: TradeType) -> Decimal:
        d = self.config.limit_distance_pct
        if side == TradeType.BUY:
            return step["low"] * (Decimal("1") - d)
        return step["high"] * (Decimal("1") + d)

    def _capital_per_grid(self) -> Decimal:
        # total / N escalones / 2 slots (LONG + SHORT por escalón). Nominal objetivo.
        return self.config.total_amount_quote / Decimal(self.config.n_grids) / Decimal("2")

    def _side_assets(self):
        """(base, quote) del trading_pair, ej 'BTC-BRL' -> ('BTC', 'BRL')."""
        base, quote = self.config.trading_pair.split("-")
        return base, quote

    def _available_quote_for(self, side: TradeType) -> Decimal:
        """Capital LIBRE disponible para una grilla de este lado, EN QUOTE.
        LONG gasta quote (BRL) -> mira quote libre.
        SHORT gasta base (BTC) -> mira base libre, valuado a quote al precio actual.
        """
        base_asset, quote_asset = self._side_assets()
        try:
            if side == TradeType.BUY:
                free = self.market_data_provider.get_available_balance(
                    self.config.connector_name, quote_asset)
                return Decimal(str(free or 0))
            # SHORT: BTC libre valuado en quote
            free_base = self.market_data_provider.get_available_balance(
                self.config.connector_name, base_asset)
            price = self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            if not price or price <= 0:
                return Decimal("0")
            return Decimal(str(free_base or 0)) * price
        except Exception:
            return Decimal("0")

    def _capital_for(self, side: TradeType, idx: int) -> Decimal:
        """Capital de UNA grilla = lo EXACTO para llevar el %BTC de la sub-cuenta al
        TARGET LOCAL de su escalón (un salto por grilla, aterrizando en la curva
        determinística). Como el NAV es invariante (cada fill intercambia
        base<->quote al precio), Δ%BTC = BRL_movido / NAV, entonces:

            cap SHORT(idx) = NAV · (pct_actual − target_local(idx))   [vende hasta SU target]
            cap LONG(idx)  = NAV · (target_local(idx) − pct_actual)   [compra hasta SU target]

        Sin reparto por lado: el modelo viejo (descarga/n_short) concentraba TODO el
        recorrido en las grillas por encima del precio (con el precio arriba del
        rango, una sola grilla absorbía todo y atravesaba su target local hasta el
        piso). Con esto cada grilla mueve su porción y la siguiente se dimensiona en
        vivo con el %BTC ya actualizado (autocorrectivo). Capado por balance LIBRE
        (anti-INSUFFICIENT). Fallback a total/N/2 si no hay target o inventario.
        """
        nominal = None
        tl = self._target_local(idx) if 0 <= idx < len(self._steps) else None
        if tl is not None:
            inv = self._real_inventory_from_fills()
            if inv is not None:
                delta = (inv["pct"] - tl) if side == TradeType.SELL else (tl - inv["pct"])
                # delta <= 0 -> el target-local ya bloquea esta creación; capital 0.
                nominal = inv["nav"] * max(Decimal("0"), delta)
        if nominal is None:
            nominal = self._capital_per_grid()  # fallback histórico
        if nominal <= 0:
            return Decimal("0")
        return min(nominal, self._available_quote_for(side))

    def _make_grid_config(self, step: dict, side: TradeType) -> GridExecutorConfig:
        return GridExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            start_price=step["low"],
            end_price=step["high"],
            limit_price=self._limit_for(step, side),
            side=side,
            leverage=self.config.leverage,
            total_amount_quote=self._capital_for(side, step["idx"]),
            min_spread_between_orders=self.config.min_spread_between_orders,
            min_order_amount_quote=self.config.min_order_amount_quote,
            max_open_orders=self.config.max_open_orders,
            max_orders_per_batch=self.config.max_orders_per_batch,
            order_frequency=self.config.order_frequency,
            activation_bounds=self.config.activation_bounds,
            triple_barrier_config=self.config.triple_barrier_config,
            level_id=self._level_id(step["idx"], side),
            keep_position=self.config.keep_position,
        )

    # ── Helpers de estado ────────────────────────────────────────────────────
    def active_level_ids(self) -> set:
        return {
            ex.config.level_id for ex in self.executors_info
            if ex.is_active and getattr(ex.config, "level_id", None)
        }

    def _step_containing(self, price: Decimal) -> Optional[dict]:
        for s in self._steps:
            if s["low"] <= price <= s["high"]:
                return s
        return None

    def _relay_direction(self, side: TradeType, close_type: CloseType) -> int:
        """Dirección del relevo según side y close_type (mecánica asimétrica).
        TAKE_PROFIT = favorable; POSITION_HOLD/STOP_LOSS = desfavorable (limit)."""
        favorable = close_type == CloseType.TAKE_PROFIT
        if side == TradeType.BUY:
            return 1 if favorable else -1      # LONG: TP arriba, limit abajo
        return -1 if favorable else 1          # SHORT: TP abajo, limit arriba

    # ── Inventario firme con signo (señal para el hedge perp) ───────────────
    @staticmethod
    def _held_btc_signed(custom_info: Optional[dict]) -> Decimal:
        """BTC firme que un cierre dejó en el portfolio, CON SIGNO.

        Se reconstruye desde custom_info['held_position_orders'] (poblado por el
        grid executor cuando cierra por POSITION_HOLD, persiste post-TERMINATED).
        Cada orden held aporta +executed_amount_base si fue BUY (cargó BTC) o
        -executed_amount_base si fue SELL (descargó BTC). Una LONG que hizo HOLD
        da +, una SHORT que hizo HOLD da -. Es el ΔBTC firme que el short debe cubrir.

        NOTA: held_position_value (custom_info) es la suma de quotes SIN signo; no
        sirve para el hedge. Por eso reconstruimos el signo desde las órdenes.
        """
        ci = custom_info or {}
        orders = ci.get("held_position_orders") or []
        total = Decimal("0")
        for o in orders:
            try:
                base = Decimal(str(o.get("executed_amount_base", 0) or 0))
            except Exception:
                continue
            side = str(o.get("trade_type", "")).upper()
            if side == "BUY":
                total += base
            elif side == "SELL":
                total -= base
        return total

    @staticmethod
    def _held_quote_signed(custom_info: Optional[dict]) -> Decimal:
        """ΔQuote firme que un cierre dejó, CON SIGNO, espejo de _held_btc_signed.
        Cada fill mueve base y quote en sentidos opuestos:
          - BUY (cargó base): gastó quote -> −executed_amount_quote
          - SELL (descargó base): cobró quote -> +executed_amount_quote
        Así, al vender BTC el quote SUBE (el NAV no se encoge: solo se intercambia
        base<->quote al precio del fill)."""
        ci = custom_info or {}
        orders = ci.get("held_position_orders") or []
        total = Decimal("0")
        for o in orders:
            try:
                quote = Decimal(str(o.get("executed_amount_quote", 0) or 0))
            except Exception:
                continue
            side = str(o.get("trade_type", "")).upper()
            if side == "BUY":
                total -= quote
            elif side == "SELL":
                total += quote
        return total

    def _position_base_signed(self, ex) -> Decimal:
        """BTC abierto-en-vuelo (transitorio, NO firme) de un executor activo, con
        signo según su side. El hedge lo IGNORA; solo informa el delta transitorio."""
        ci = ex.custom_info or {}
        try:
            pos_base = Decimal(str(ci.get("position_size_base", 0) or 0))
        except Exception:
            pos_base = Decimal("0")
        if pos_base == 0:
            return Decimal("0")
        _, side = self._parse_level_id(getattr(ex.config, "level_id", "") or "")
        sign = Decimal("1") if side == TradeType.BUY else Decimal("-1")
        return pos_base * sign

    def _register_rebalance_events(self) -> None:
        """Detecta cada cierre de executor UNA sola vez (por executor.id) y registra
        el ΔBTC firme con signo. Acumula el inventario neto del tablero."""
        for ex in self.executors_info:
            if ex.is_active or ex.close_type is None:
                continue
            ex_id = getattr(ex, "id", None)
            if ex_id is None or ex_id in self._inventory_handled:
                continue
            self._inventory_handled.add(ex_id)
            delta_btc = self._held_btc_signed(ex.custom_info)
            delta_quote = self._held_quote_signed(ex.custom_info)
            level_id = getattr(ex.config, "level_id", None)
            self._net_btc_from_grids += delta_btc
            self._net_quote_from_grids += delta_quote
            # Evento en memoria (lo usan status y _reconcile).
            self._rebalance_events.append({
                "executor_id": ex_id,
                "level_id": level_id,
                "close_type": ex.close_type,
                "delta_btc": delta_btc,
                "timestamp": self.market_data_provider.time(),
            })
            # Evento rico al JSONL: capital AISLADO de la grilla (del executor, no
            # del connector global -> sin sesgo). assigned al iniciar, lo operado,
            # lo dejado firme, PnL, BEP.
            self._append_event(self._build_rebalance_event(ex, level_id, delta_btc))

    def _build_rebalance_event(self, ex, level_id, delta_btc) -> dict:
        ci = ex.custom_info or {}
        cfg = ex.config
        idx, side = self._parse_level_id(level_id or "")
        step = self._steps[idx] if idx is not None and 0 <= idx < len(self._steps) else None

        def _f(v):
            try:
                return float(Decimal(str(v)))
            except Exception:
                return None

        return {
            "type": "rebalance",
            "ts": self.market_data_provider.time(),
            "executor_id": getattr(ex, "id", None),
            "level_id": level_id,
            "step": ({"idx": step["idx"], "low": float(step["low"]), "high": float(step["high"])}
                     if step else None),
            "side": ("LONG" if side == TradeType.BUY else "SHORT") if side else None,
            "close_type": ex.close_type.name if ex.close_type else None,
            "config": {
                "start_price": _f(getattr(cfg, "start_price", None)),
                "end_price": _f(getattr(cfg, "end_price", None)),
                "limit_price": _f(getattr(cfg, "limit_price", None)),
                "total_amount_quote": _f(getattr(cfg, "total_amount_quote", None)),
                "min_spread": _f(getattr(cfg, "min_spread_between_orders", None)),
            },
            "capital": {
                "assigned_quote": _f(getattr(cfg, "total_amount_quote", None)),
                "filled_quote": _f(ci.get("filled_amount_quote")),
                "held_quote": _f(ci.get("held_position_value")),
                "realized_pnl_quote": _f(ci.get("realized_pnl_quote")),
                "break_even_price": _f(ci.get("break_even_price")),
                "delta_btc": _f(delta_btc),
            },
            "net_btc_from_grids_after": _f(self._net_btc_from_grids),
        }

    def _reconcile(self) -> dict:
        """Métricas de conciliación: la prueba de que el inventario firme cuadra.
        - tp_residual_btc: held firme dejado por cierres TAKE_PROFIT. DEBE ser ~0
          (si no, el TP dejó inventario descubierto -> riesgo para el hedge).
        - hold_btc: held firme de cierres POSITION_HOLD (el rebalanceo real).
        - unexpected_close_btc: held firme de cierres fuera de {TP, HOLD}
          (EARLY_STOP/FAILED/etc.) -> no deben aparecer en operación normal.
        - in_flight_btc: BTC transitorio (no firme) de grillas activas.
        """
        tp_residual = Decimal("0")
        hold_btc = Decimal("0")
        unexpected = Decimal("0")
        for ev in self._rebalance_events:
            ct = ev["close_type"]
            d = ev["delta_btc"]
            if ct == CloseType.TAKE_PROFIT:
                tp_residual += d
            elif ct == CloseType.POSITION_HOLD:
                hold_btc += d
            else:
                unexpected += d
        in_flight = Decimal("0")
        for ex in self.executors_info:
            if ex.is_active and getattr(ex.config, "level_id", None):
                in_flight += self._position_base_signed(ex)
        return {
            "tp_residual_btc": tp_residual,
            "hold_btc": hold_btc,
            "unexpected_close_btc": unexpected,
            "in_flight_btc": in_flight,
        }

    def _real_inventory_from_fills(self, price: Optional[Decimal] = None) -> Optional[dict]:
        """Inventario REAL del tablero reconstruido desde los FILLS de TODOS los
        executors (activos + cerrados), no solo el held firme. Es lo más fidedigno:
        responde a lo que el bot efectivamente compró/vendió.

        Regla simple: el inventario parte de base_assigned/quote_assigned y CADA
        FILL firme lo altera 1:1 (al precio del fill, no al actual):
          - BUY  (cargó base): +base, −quote (gastó quote)
          - SELL (descargó):   −base, +quote (cobró quote)
        Los ΔBase/ΔQuote firmes de cada cierre se ACUMULAN por evento en
        _net_btc_from_grids / _net_quote_from_grids (ver _register_rebalance_events),
        robusto a que la lista de executors cambie entre ticks.
          base_real  = base_assigned  + net_btc_from_grids
          quote_real = quote_assigned + net_quote_from_grids
          nav        = base_real·precio + quote_real   (invariante salvo PnL real)

        Devuelve {base, quote, nav, pct} o None.
        """
        try:
            if price is None:
                price = self.market_data_provider.get_price_by_type(
                    self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            if not price or price <= 0:
                return None
            base_real = self.config.base_assigned + self._net_btc_from_grids
            quote_real = self.config.quote_assigned + self._net_quote_from_grids
            nav = base_real * price + quote_real
            if nav <= 0:
                return None
            return {
                "base": base_real, "quote": quote_real, "nav": nav,
                "pct": (base_real * price) / nav,
            }
        except Exception:
            return None

    def _current_pct_btc(self) -> Optional[Decimal]:
        """%BTC de la SUB-CUENTA del tablero. Medido desde los FILLS reales
        (base/quote reconstruidos): refleja exactamente lo que el bot operó, con
        NAV invariante (al vender base sube el quote, no solo baja el base).
        Aislado del portfolio global. None si falla (ante falla no bloqueamos)."""
        inv = self._real_inventory_from_fills()
        return inv["pct"] if inv is not None else None

    def _target_local(self, idx: int) -> Optional[Decimal]:
        """Target de %BTC LOCAL del escalón idx = la curva determinística en su precio
        medio (lineal techo en border_a -> piso en border_b). Ej: cb_0 ~techo,
        cb_N-1 ~piso. None si no hay piso definido (sin corte)."""
        if self.config.target_pct_btc is None:
            return None
        piso = self.config.target_pct_btc
        techo = self.config.techo_pct_btc
        a, b = self.config.border_a, self.config.border_b
        if b <= a:
            return piso
        step = self._steps[idx]
        mid = (step["low"] + step["high"]) / Decimal("2")
        frac = (mid - a) / (b - a)  # 0 en A, 1 en B
        frac = max(Decimal("0"), min(Decimal("1"), frac))
        return techo - (techo - piso) * frac

    def _blocked_by_local_target(self, idx: int, side: TradeType) -> bool:
        """True si la grilla (idx, side) NO debe crearse porque el %BTC firme ya
        cruzó el target LOCAL de ese escalón. SHORT frena si %BTC <= target_local
        (ya descargó lo que toca acá); LONG frena si %BTC >= target_local (ya cargó).
        Esto distribuye la descarga por el rango y evita el churn de oscilación en
        un borde: en un escalón de precio bajo (target alto) las SHORT no venden."""
        tl = self._target_local(idx)
        if tl is None:
            return False
        pct = self._current_pct_btc()
        if pct is None:
            return False  # sin lectura confiable, no bloquear
        if side == TradeType.SELL:
            return pct <= tl   # ya por debajo del target local -> no descargar más acá
        return pct >= tl       # LONG: ya por encima -> no cargar más acá

    def _in_hysteresis_band(self, step: dict, mid_price: Decimal) -> bool:
        """True si el precio está dentro de la franja de histéresis pegada a los
        bordes del escalón (no poblar ahí). Margen = hysteresis_pct del ancho del
        escalón en cada borde. Evita el churn de relevo cuando el precio oscila
        justo en un borde."""
        h = self.config.hysteresis_pct
        if h <= 0:
            return False
        ancho = step["high"] - step["low"]
        margen = ancho * h
        return (mid_price < step["low"] + margen) or (mid_price > step["high"] - margen)

    def _create_if_possible(self, idx: int, side: TradeType, active: set,
                            actions: list, mid_price: Decimal,
                            apply_hysteresis: bool = False):
        """Crea la grilla del slot (idx, side) si el escalón existe, el precio está
        DENTRO de su banda, y no está ya activo."""
        if not (0 <= idx < self.config.n_grids):
            return
        step = self._steps[idx]
        # GUARDA DE BANDA (anti-loop): NO crear si el precio está fuera de [low, high]
        # del escalón. El grid executor cierra al instante por TAKE_PROFIT una grilla
        # que nace con el precio fuera de su banda (mid > end para LONG, mid < start
        # para SHORT) -> nacería muerta y el relevo + asegurar-par la recrearían en
        # loop. Solo creamos grillas en el escalón que efectivamente contiene el precio.
        if not (step["low"] <= mid_price <= step["high"]):
            return
        lid_log = self._level_id(idx, side)
        # CORTE EN TARGET LOCAL: cada escalón tiene su %BTC objetivo (curva techo->piso).
        # SHORT frena si ya descargó lo que toca acá; LONG si ya cargó. Evita liquidar
        # todo en precios bajos y distribuye el rebalanceo por el rango.
        if self._blocked_by_local_target(idx, side):
            tl = self._target_local(idx)
            pct = self._current_pct_btc()
            self.logger().info(
                f"[GRIGADO] BLOQUEO target-local {lid_log}: %BTC={pct} "
                f"{'<=' if side == TradeType.SELL else '>='} target_local={tl} (no se crea)")
            return
        level_id = self._level_id(idx, side)
        # HISTÉRESIS: aplica cuando el relevo viene de un TAKE_PROFIT (apply_hysteresis)
        # — esa consecutiva vende/compra TODO su capital al nacer, y si el precio
        # oscila en el borde ese churn descarga/carga de más. El bloqueo es PEGAJOSO
        # (_tp_hysteresis_pending): mientras el precio siga en la franja, NINGUNA vía
        # crea el slot (la Fase 3 asegurar-par tampoco — sin esto lo recreaba en el
        # mismo tick y la histéresis era inefectiva). Se libera cuando el precio
        # llega al interior del escalón. El relevo por POSITION_HOLD arranca operando
        # normal (no abre de golpe) -> sin histéresis, igual que el despliegue inicial.
        if apply_hysteresis or level_id in self._tp_hysteresis_pending:
            if self._in_hysteresis_band(step, mid_price):
                self._tp_hysteresis_pending.add(level_id)
                self.logger().info(
                    f"[GRIGADO] BLOQUEO histéresis {lid_log}: mid={mid_price} pegado al borde "
                    f"[{step['low']}-{step['high']}] (relevo desde TP, no se crea)")
                return
            self._tp_hysteresis_pending.discard(level_id)  # precio en el interior: libera
        # No recrear si ya hay un executor ACTIVO en ese slot (active), ni si ya
        # encolamos su creación en este mismo tick (evita duplicados relevo+par).
        if level_id in active:
            return
        already_queued = {
            a.executor_config.level_id for a in actions
            if isinstance(a, CreateExecutorAction)
        }
        if level_id in already_queued:
            return
        # GUARDA DE MUNICIÓN: si el capital LIBRE del lado no alcanza ni para una
        # orden mínima, NO crear (evita INSUFFICIENT_BALANCE). Marca la zona como
        # "sin munición" y abre un evento no_capital. Es el límite natural de la
        # campaña: un lado se agota (bajando se acaba el BRL, vendiendo el BTC).
        capital = self._capital_for(side, idx)
        if capital < self.config.min_order_amount_quote:
            self.logger().info(
                f"[GRIGADO] SIN-MUNICIÓN {level_id}: capital libre={capital:.2f} < "
                f"min={self.config.min_order_amount_quote} (⊘ zona sin munición)")
            self._open_no_capital(level_id, idx, step, side, capital)
            return
        # Hay munición: si este slot tenía un evento no_capital abierto, ciérralo.
        self._close_no_capital(level_id)
        self.logger().info(
            f"[GRIGADO] CREA {level_id} ({'LONG' if side == TradeType.BUY else 'SHORT'}): "
            f"capital={capital:.2f} target_local={self._target_local(idx)} mid={mid_price}")
        actions.append(CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=self._make_grid_config(step, side),
        ))

    # ── Zonas sin munición ───────────────────────────────────────────────────
    def _open_no_capital(self, level_id, idx, step, side, available):
        """Abre (una sola vez) un evento no_capital: el precio cayó en una zona donde
        no se pudo abrir la grilla por falta de capital. Estadía vigente hasta que
        el precio salga o vuelva el capital."""
        if level_id in self._no_capital_open:
            return  # ya abierto, no spamear
        base_asset, quote_asset = self._side_assets()
        asset_needed = quote_asset if side == TradeType.BUY else base_asset
        # mínimo necesario para abrir = una orden mínima (la guarda de munición
        # compara contra esto; el nominal por recorrido puede ser mucho mayor).
        self._no_capital_open[level_id] = {
            "type": "no_capital",
            "level_id": level_id,
            "step": {"idx": idx, "low": float(step["low"]), "high": float(step["high"])},
            "side": "LONG" if side == TradeType.BUY else "SHORT",
            "asset_needed": asset_needed,
            "needed_quote": float(self.config.min_order_amount_quote),
            "available_quote": float(available),
            "ts_open": self.market_data_provider.time(),
            "ts_close": None,
            "open": True,
        }

    def _close_no_capital(self, level_id):
        """Cierra el evento no_capital de un slot (el precio salió o volvió el capital)
        y lo persiste al JSONL."""
        ev = self._no_capital_open.pop(level_id, None)
        if ev is None:
            return
        ev["ts_close"] = self.market_data_provider.time()
        ev["open"] = False
        self._no_capital_closed_count += 1
        self._append_event(ev)

    def _close_orphan_no_capital(self, mid_price):
        """Cierra zonas sin munición HUÉRFANAS: si el precio ya salió del escalón de
        un evento abierto, esa estadía terminó. Corre SIEMPRE (antes de cualquier
        early-return), porque ese slot ya no se intenta crear."""
        for lid in list(self._no_capital_open.keys()):
            ev = self._no_capital_open[lid]
            lo = Decimal(str(ev["step"]["low"]))
            hi = Decimal(str(ev["step"]["high"]))
            if not (lo <= mid_price <= hi):
                self._close_no_capital(lid)

    def _append_event(self, event: dict):
        """Append de un evento (rebalance | no_capital) al JSONL. Robusto: nunca
        rompe el control loop si falla el disco."""
        try:
            os.makedirs(os.path.dirname(self._events_path), exist_ok=True)
            with open(self._events_path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass

    # ── Snapshots periódicos teórico-vs-real ─────────────────────────────────
    def _maybe_snapshot(self):
        """Cada ~snapshot_interval_s graba un snapshot teórico-vs-real al JSONL.
        Robusto: no rompe el control loop si falla."""
        try:
            now = self.market_data_provider.time()
            if (self._last_snapshot_ts is not None and
                    now - self._last_snapshot_ts < self._snapshot_interval_s):
                return
            snap = self._build_snapshot(now)
            if snap is None:
                return
            self._last_snapshot_ts = now
            os.makedirs(os.path.dirname(self._snapshots_path), exist_ok=True)
            with open(self._snapshots_path, "a") as f:
                f.write(json.dumps(snap) + "\n")
        except Exception:
            pass

    def _build_snapshot(self, ts: float) -> Optional[dict]:
        """Snapshot teórico-vs-real. NAV invariante a comprar/vender:
        nav = base·precio + quote. Para un %BTC objetivo: base = %·nav/precio,
        quote = nav·(1−%). El teórico por escalón usa target_local(idx) como %BTC.
        El real usa el inventario FIRME del tablero (aislado)."""
        price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        if not price or price <= 0:
            return None
        # REAL: inventario firme del tablero desde fills (base y quote acumulados con
        # signo). NAV invariante: al vender base, el quote sube (no se encoge el NAV).
        inv = self._real_inventory_from_fills(price)
        if inv is None:
            return None
        base_real, quote_real, nav, pct_real = inv["base"], inv["quote"], inv["nav"], inv["pct"]

        # TEÓRICO al precio actual = %BTC que la curva da en el escalón del precio.
        step_here = self._step_containing(price)
        tl_here = self._target_local(step_here["idx"]) if step_here else None
        if tl_here is None:
            tl_here = pct_real  # sin target -> teórico = real
        base_teo = tl_here * nav / price
        quote_teo = nav * (Decimal("1") - tl_here)

        # TEÓRICO por escalón: si el precio estuviera en cb_i, %BTC = target_local(i).
        escalones = []
        for s in self._steps:
            tl = self._target_local(s["idx"])
            pct = tl if tl is not None else pct_real
            escalones.append({
                "idx": s["idx"],
                "low": float(s["low"]), "high": float(s["high"]),
                "target_local_pct": float(pct),
                "base_teorico": float(pct * nav / price),
                "quote_teorico": float(nav * (Decimal("1") - pct)),
            })

        return {
            "ts": ts,
            "mid_price": float(price),
            "nav": float(nav),
            "real": {"base": float(base_real), "quote": float(quote_real),
                     "pct_btc": float(pct_real)},
            "teorico": {"base": float(base_teo), "quote": float(quote_teo),
                        "pct_btc": float(tl_here)},
            "drift": {"base": float(base_real - base_teo),
                      "pct_btc": float(pct_real - tl_here)},
            "escalones": escalones,
        }

    # ── Gráfico ASCII de inventario (curva objetivo + posición real) ─────────
    def _target_local_at_price(self, price: Decimal) -> Optional[Decimal]:
        """Versión continua de _target_local: %BTC objetivo en un precio arbitrario
        (lineal techo en border_a -> piso en border_b). Para dibujar la curva."""
        if self.config.target_pct_btc is None:
            return None
        a, b = self.config.border_a, self.config.border_b
        piso, techo = self.config.target_pct_btc, self.config.techo_pct_btc
        if b <= a:
            return piso
        frac = max(Decimal("0"), min(Decimal("1"), (price - a) / (b - a)))
        return techo - (techo - piso) * frac

    def _inventory_chart_lines(self, mid_price: Decimal) -> List[str]:
        """Gráfico ASCII: curva de %BTC objetivo (techo->piso) a lo largo del rango,
        con la posición REAL (●) según el inventario reconstruido desde fills.
        Eje X = precio [border_a, border_b], eje Y = %BTC. De un vistazo: dónde
        estás (●) vs dónde deberías estar (curva ·), y si toca cargar o descargar."""
        a, b = self.config.border_a, self.config.border_b
        piso = self.config.target_pct_btc
        techo = self.config.techo_pct_btc
        if piso is None or b <= a or not mid_price or mid_price <= 0:
            return []  # sin target o geometría inválida -> no hay curva que dibujar

        inv = self._real_inventory_from_fills(mid_price)
        if inv is None:
            return []
        pct_real = inv["pct"]
        obj = self._target_local_at_price(mid_price)

        W, H = 48, 9
        y_top = techo
        y_bot = piso - (techo - piso) * Decimal("0.15")  # margen abajo del piso

        def yrow(p: Decimal) -> int:
            if y_top == y_bot:
                return 0
            f = float((y_top - p) / (y_top - y_bot))
            return max(0, min(H - 1, int(round(f * (H - 1)))))

        def xcol(p: Decimal) -> int:
            f = float((p - a) / (b - a))
            return max(0, min(W - 1, int(round(f * (W - 1)))))

        grid = [[" "] * W for _ in range(H)]
        # curva objetivo (· en cada columna según el target_local de ese precio)
        for c in range(W):
            price = a + (b - a) * Decimal(c) / Decimal(W - 1)
            tl = self._target_local_at_price(price)
            if tl is not None:
                grid[yrow(tl)][c] = "·"
        # línea vertical tenue en el precio actual (donde no pise la curva)
        c_now = xcol(mid_price) if a <= mid_price <= b else None
        if c_now is not None:
            for r in range(H):
                if grid[r][c_now] == " ":
                    grid[r][c_now] = "┊"
            # posición REAL (●): %BTC real en el precio actual. Prioridad sobre todo.
            grid[yrow(pct_real)][c_now] = "●"

        def fp(p: Decimal) -> str:
            return f"{float(p) * 100:4.0f}%"

        diff = pct_real - obj if obj is not None else Decimal("0")
        if obj is None:
            diag = "sin target (no corta)"
        elif diff > Decimal("0.002"):
            diag = f"DESCARGAR (real {diff:+.1%} sobre objetivo)"
        elif diff < Decimal("-0.002"):
            diag = f"CARGAR (real {diff:+.1%} bajo objetivo)"
        else:
            diag = "EN OBJETIVO"

        lines = ["│ ┌ Inventario %BTC vs precio  (· objetivo · ● HOY real desde fills)"]
        for r in range(H):
            yp = y_top - (y_top - y_bot) * Decimal(r) / Decimal(H - 1)
            tag = ""
            if r == yrow(techo):
                tag = " ← techo"
            elif r == yrow(piso):
                tag = " ← piso/target"
            lines.append(f"│ │ {fp(yp)} │" + "".join(grid[r]) + f"│{tag}")
        lines.append("│ │       └" + "─" * W + "┘")
        lines.append(f"│ │        {float(a):<8.0f}" + " " * (W - 16) + f"{float(b):>8.0f}")
        obj_s = f"{float(obj):.1%}" if obj is not None else "n/a"
        lines.append(f"│ │ real {float(pct_real):.1%}  ·  objetivo@HOY {obj_s}  →  {diag}")
        lines.append(
            f"│ │ base {float(inv['base']):.5f} BTC ({float(inv['base'] * mid_price):,.0f}) · "
            f"quote {float(inv['quote']):,.0f} · NAV {float(inv['nav']):,.0f}")
        lines.append("│ └")
        return lines

    # ── Decisión principal ───────────────────────────────────────────────────
    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = []
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        active = self.active_level_ids()

        # Cerrar zonas sin munición huérfanas (precio ya salió). Corre SIEMPRE, antes
        # de cualquier return, porque esos slots ya no se intentan crear.
        self._close_orphan_no_capital(mid_price)

        # 1) Despliegue inicial: solo si NO existe ningún executor todavía (ni
        #    activo ni cerrado). El par (LONG+SHORT) del escalón que contiene el precio.
        any_executor = any(getattr(ex.config, "level_id", None) for ex in self.executors_info)
        if not any_executor and not self._relayed_executor_ids:
            step = self._step_containing(mid_price)
            if step is not None:
                self._create_if_possible(step["idx"], TradeType.BUY, active, actions, mid_price)
                self._create_if_possible(step["idx"], TradeType.SELL, active, actions, mid_price)
            return actions

        # 2) Relevo asimétrico: por cada grilla cerrada, marcar el cierre (para no
        #    re-relevarlo) e INTENTAR crear la consecutiva en la dirección de su
        #    cadena. La guarda de banda en _create_if_possible se encarga de que solo
        #    se cree si el precio ya entró a esa banda (si no, se creará vía Fase 3
        #    cuando el precio llegue). Esto preserva la intención del relevo
        #    asimétrico sin permitir grillas que nacen fuera de banda (loop).
        for ex in self.executors_info:
            level_id = getattr(ex.config, "level_id", None)
            if not level_id or ex.is_active or ex.close_type is None:
                continue
            ex_id = getattr(ex, "id", None)
            if ex_id is None or ex_id in self._relayed_executor_ids:
                continue
            idx, side = self._parse_level_id(level_id)
            if idx is None:
                continue
            self._relayed_executor_ids.add(ex_id)
            self._closed_handled.add(level_id)  # solo para el display "·"
            direction = self._relay_direction(side, ex.close_type)
            # Histéresis SOLO si el relevo viene de un TAKE_PROFIT: la consecutiva
            # abre posición (vende/compra todo) al nacer -> hay que evitar el churn
            # de borde. POSITION_HOLD releva una grilla que arranca operando normal.
            from_tp = ex.close_type == CloseType.TAKE_PROFIT
            self.logger().info(
                f"[GRIGADO] RELEVO {level_id} cerró por {ex.close_type.name} -> "
                f"intenta cb_{idx + direction}_{'L' if side == TradeType.BUY else 'S'} "
                f"(dir {direction:+d}, histéresis={'sí' if from_tp else 'no'})")
            self._create_if_possible(idx + direction, side, active, actions, mid_price,
                                     apply_hysteresis=from_tp)

        # 3) Asegurar el PAR en el escalón del precio (invariante CB4: siempre un
        #    par LONG+SHORT rodeando el precio). El relevo propaga cada cadena por
        #    separado; en un movimiento sostenido un escalón puede quedar con un
        #    solo lado, o con ambos lados relevados (si el precio volvió). Acá
        #    recreamos lo que falte. _create_if_possible solo evita duplicar lo
        #    ACTIVO (o ya encolado este tick), así que el slot se repuebla cuando
        #    el precio vuelve a un escalón que ya había sido relevado.
        step = self._step_containing(mid_price)
        if step is not None:
            self._create_if_possible(step["idx"], TradeType.BUY, active, actions, mid_price)
            self._create_if_possible(step["idx"], TradeType.SELL, active, actions, mid_price)

        return actions

    async def update_processed_data(self):
        if self._start_timestamp is None:
            self._start_timestamp = self.market_data_provider.time()

        for ex in self.executors_info:
            level_id = getattr(ex.config, "level_id", None)
            if level_id and ex.is_active:
                self._level_executor[level_id] = ex.id

        # Contabilizar cierres nuevos (inventario firme con signo) y conciliar.
        # _register_rebalance_events también persiste cada cierre al JSONL de eventos.
        self._register_rebalance_events()
        recon = self._reconcile()

        spot_btc_firme = self.config.base_assigned + self._net_btc_from_grids
        quote_firme = self.config.quote_assigned + self._net_quote_from_grids
        pct = self._current_pct_btc()
        self.processed_data.update({
            "net_btc_from_grids": self._net_btc_from_grids,
            "net_quote_from_grids": self._net_quote_from_grids,
            "spot_btc_firme": spot_btc_firme,
            "btc_subcuenta": spot_btc_firme,
            "quote_subcuenta": quote_firme,
            "pct_btc": pct,
            "rebalance_events": self._rebalance_events,
            **recon,
        })

        # Snapshot periódico teórico-vs-real (cada ~60s) para graficar el drift.
        self._maybe_snapshot()

    def to_format_status(self) -> List[str]:
        mid_price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        active = self.active_level_ids()
        lines = [
            f"┌ Chessboard {self.config.connector_name} {self.config.trading_pair}",
            f"│ Mid: {mid_price:.2f} │ Rango: [{self.config.border_a:.2f}, {self.config.border_b:.2f}] "
            f"│ Escalones: {self.config.n_grids} │ Grillas activas: {len(active)} "
            f"│ Relevadas: {len(self._closed_handled)}",
        ]

        # Inventario firme (señal-setpoint para el hedge perp) + conciliación.
        pd = self.processed_data
        net = pd.get("net_btc_from_grids", self._net_btc_from_grids)
        hold = pd.get("hold_btc", Decimal("0"))
        tp_res = pd.get("tp_residual_btc", Decimal("0"))
        unexp = pd.get("unexpected_close_btc", Decimal("0"))
        in_flight = pd.get("in_flight_btc", Decimal("0"))
        pct = pd.get("pct_btc")
        spot_firme = self.config.base_assigned + net
        pct_s = f"{pct:.1%}" if pct is not None else "n/a"
        warn = ""
        if tp_res != 0:
            warn += f" ⚠TP-residual!={tp_res:+.8f}"
        if unexp != 0:
            warn += f" ⚠cierre-inesperado={unexp:+.8f}"
        lines.append(
            f"│ Inventario firme: spot={spot_firme:.8f} BTC (base {self.config.base_assigned:.8f} "
            f"+ grillas {net:+.8f}) │ hold={hold:+.8f} │ in-flight={in_flight:+.8f} "
            f"│ %BTC={pct_s} │ eventos={len(self._rebalance_events)}{warn}"
        )

        # Zonas sin munición (capital insuficiente para abrir la grilla).
        if self._no_capital_open:
            zonas = ", ".join(
                f"{ev['level_id']} {ev['side']} (falta {ev['asset_needed']})"
                for ev in self._no_capital_open.values()
            )
            lines.append(
                f"│ ⊘ Capital insuficiente: {zonas} │ zonas cerradas={self._no_capital_closed_count}"
            )

        # Rotación de capital: cuántas veces movió el total_amount_quote (turnover)
        # y esa misma tasa por hora encendido. volume = Σ filled_amount_quote.
        volume = sum((Decimal(str(getattr(ex, "filled_amount_quote", 0) or 0))
                      for ex in self.executors_info), Decimal("0"))
        total_quote = self.config.total_amount_quote
        turnover = (volume / total_quote) if total_quote > 0 else Decimal("0")
        now = self.market_data_provider.time()
        hours = ((Decimal(str(now - self._start_timestamp)) / Decimal("3600"))
                 if self._start_timestamp else Decimal("0"))
        turnover_h = (turnover / hours) if hours > 0 else Decimal("0")
        lines.append(
            f"│ Rotación: volumen={volume:.2f} │ turnover={turnover:.2f}x "
            f"(volumen/total) │ tasa={turnover_h:.3f}x/h │ encendido={hours:.2f}h"
        )

        # ── Economía: capital, volumen proyectado y rebates estimados ────────
        _, quote_asset = self._side_assets()
        # conversión a USDT: si el quote ya es USDT, factor 1; si no, USDT-BRL.
        if quote_asset.upper() == "USDT":
            quote_to_usdt = Decimal("1")
        else:
            try:
                rate = self.market_data_provider.get_rate(f"USDT-{quote_asset}")  # ej USDT-BRL
                quote_to_usdt = (Decimal("1") / Decimal(str(rate))) if rate and Decimal(str(rate)) > 0 else None
            except Exception:
                quote_to_usdt = None
        total_usdt = (total_quote * quote_to_usdt) if quote_to_usdt is not None else None
        # volumen proyectado = capital × tasa de rotación (turnover observado).
        vol_h = total_quote * turnover_h               # volumen/hora en quote
        vol_d = vol_h * Decimal("24")
        REBATE = Decimal("0.00015")                    # +0.015% maker
        reb_h, reb_d, reb_m = vol_h * REBATE, vol_d * REBATE, vol_d * Decimal("30") * REBATE

        def _u(v):  # quote -> USDT (o '?' si no hay rate)
            return f"${v * quote_to_usdt:,.2f}" if (quote_to_usdt is not None and v is not None) else "n/a"

        usdt_s = f"${total_usdt:,.0f}" if total_usdt is not None else "n/a"
        lines.append(
            f"│ Capital: {total_quote:,.0f} {quote_asset} ({usdt_s} USDT) │ "
            f"Vol proyectado: {_u(vol_h)}/h · {_u(vol_d)}/d (USDT, según tasa actual)"
        )
        lines.append(
            f"│ Rebate {REBATE:.3%} → estimado: {_u(reb_h)}/h · {_u(reb_d)}/d · {_u(reb_m)}/mes (USDT)"
        )

        # Gráfico de inventario: curva objetivo (techo->piso) + posición real (●)
        # reconstruida desde fills. Muestra dónde estás vs dónde deberías estar.
        lines.extend(self._inventory_chart_lines(mid_price))

        # Contador de cuántas grillas CERRARON en cada escalón (rotación por banda).
        # Desde _rebalance_events: cada evento es un cierre con su level_id.
        passes_l: Dict[int, int] = {}
        passes_s: Dict[int, int] = {}
        for ev in self._rebalance_events:
            idx, side = self._parse_level_id(ev.get("level_id") or "")
            if idx is None:
                continue
            d = passes_l if side == TradeType.BUY else passes_s
            d[idx] = d.get(idx, 0) + 1

        # Mostrar de mayor a menor precio (resistencia arriba, soporte abajo),
        # como se lee un tablero. No altera _steps ni los idx (solo el display).
        for step in reversed(self._steps):
            i = step["idx"]
            lid_l, lid_s = self._level_id(i, TradeType.BUY), self._level_id(i, TradeType.SELL)
            # ⊘ = zona sin munición (capital insuficiente, no se pudo abrir la grilla).
            # Tiene prioridad: es el estado vivo del slot.
            l_state = "L⊘" if lid_l in self._no_capital_open else (
                "L✓" if lid_l in active else ("L·" if lid_l in self._closed_handled else "  "))
            s_state = "S⊘" if lid_s in self._no_capital_open else (
                "S✓" if lid_s in active else ("S·" if lid_s in self._closed_handled else "  "))
            here = " ←HOY" if step["low"] <= mid_price <= step["high"] else ""
            nl, ns = passes_l.get(i, 0), passes_s.get(i, 0)
            tot = nl + ns
            # Sesgo de la banda: ↑ si predominan SHORT (precio subió por acá =
            # descarga), ↓ si predominan LONG (precio bajó = carga), ↕ si parejo.
            if tot == 0:
                bias = " "
            elif ns > nl:
                bias = "↑"
            elif nl > ns:
                bias = "↓"
            else:
                bias = "↕"
            # barra visual proporcional a la rotación total del escalón
            bar = "▪" * min(tot, 20)
            passes = f" │ {bias} paso×{tot:>2} (L{nl} S{ns}) {bar}" if tot else " │   paso× 0"
            lines.append(
                f"│  cb_{i}: [{step['low']:.2f}–{step['high']:.2f}] {l_state} {s_state}{here}{passes}")
        lines.append("└")
        return lines
