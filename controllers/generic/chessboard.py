"""
Chessboard Controller — tablero de grillas con pares LONG+SHORT y relevo asimétrico.

Modelo (docs/chessboard_strategy_v1.md):
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

    # Target de campaña: %BTC de la SUB-CUENTA al que se quiere llegar. El controller
    # CORTA la descarga (no crea más SHORT) cuando el %BTC de su sub-cuenta <= target.
    # La carga (LONG) sigue habilitada. None = sin corte (cubre todo el rango).
    target_pct_btc: Optional[Decimal] = Field(default=None, json_schema_extra={"is_updatable": True})

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

    def _capital_for(self, side: TradeType) -> Decimal:
        """Capital a asignar a una grilla de este lado: el nominal (total/N/2) capado
        al balance LIBRE real del lado. Nunca pide más de lo que hay -> evita
        INSUFFICIENT_BALANCE. Si el libre es chico, la grilla nace más flaca."""
        return min(self._capital_per_grid(), self._available_quote_for(side))

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
            total_amount_quote=self._capital_for(side),
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
            level_id = getattr(ex.config, "level_id", None)
            self._net_btc_from_grids += delta_btc
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

    def _current_pct_btc(self) -> Optional[Decimal]:
        """%BTC de la SUB-CUENTA del tablero, medido sobre el inventario FIRME con
        signo (base_assigned + net_btc_from_grids). Aislado del portfolio global
        que comparten otras estrategias. None si no se puede calcular.
        """
        try:
            price = self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            if not price or price <= 0:
                return None
            btc_subcuenta = self.config.base_assigned + self._net_btc_from_grids
            nav = btc_subcuenta * price + self.config.quote_assigned
            if nav <= 0:
                return None
            return (btc_subcuenta * price) / nav
        except Exception:
            return None  # ante falla, no bloqueamos (deja operar)

    def _target_reached(self) -> bool:
        """True si la descarga llegó al target (no crear más SHORT)."""
        if self.config.target_pct_btc is None:
            return False
        pct = self._current_pct_btc()
        if pct is None:
            return False  # sin lectura confiable, no cortar
        return pct <= self.config.target_pct_btc

    def _create_if_possible(self, idx: int, side: TradeType, active: set,
                            actions: list, mid_price: Decimal):
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
        # CORTE EN TARGET: si la descarga ya alcanzó el target, no crear más SHORT.
        # Las LONG siguen habilitadas (pueden recargar si el precio baja).
        if side == TradeType.SELL and self._target_reached():
            return
        level_id = self._level_id(idx, side)
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
        capital = self._capital_for(side)
        if capital < self.config.min_order_amount_quote:
            self._open_no_capital(level_id, idx, step, side, capital)
            return
        # Hay munición: si este slot tenía un evento no_capital abierto, ciérralo.
        self._close_no_capital(level_id)
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
        self._no_capital_open[level_id] = {
            "type": "no_capital",
            "level_id": level_id,
            "step": {"idx": idx, "low": float(step["low"]), "high": float(step["high"])},
            "side": "LONG" if side == TradeType.BUY else "SHORT",
            "asset_needed": asset_needed,
            "needed_quote": float(self._capital_per_grid()),
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
            self._create_if_possible(idx + direction, side, active, actions, mid_price)

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
        pct = self._current_pct_btc()
        self.processed_data.update({
            "net_btc_from_grids": self._net_btc_from_grids,
            "spot_btc_firme": spot_btc_firme,
            "btc_subcuenta": spot_btc_firme,
            "pct_btc": pct,
            "rebalance_events": self._rebalance_events,
            **recon,
        })

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
