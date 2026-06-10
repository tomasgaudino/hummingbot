"""
Unit tests del Chessboard controller (modelo fiel: pares LONG+SHORT + relevo asimétrico).

Valida SIN red ni capital:
  - construcción de escalones
  - despliegue inicial = par LONG+SHORT del escalón que contiene el precio
  - relevo asimétrico por close_type:
      LONG  TP -> LONG +1 ; LONG  POSITION_HOLD -> LONG -1
      SHORT TP -> SHORT -1 ; SHORT POSITION_HOLD -> SHORT +1
  - no-doble-relevo
"""

import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import AsyncMock, MagicMock

from controllers.generic.chessboard import Chessboard, ChessboardConfig
from hummingbot.core.data_type.common import TradeType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class TestChessboard(IsolatedAsyncioWrapperTestCase):

    def _make_controller(self, n_grids=10, a="340000", b="400000"):
        config = ChessboardConfig(
            id="cb-test", connector_name="binance", trading_pair="BTC-USDT",
            border_a=Decimal(a), border_b=Decimal(b),
            n_grids=n_grids, total_amount_quote=Decimal("50000"),
            base_assigned=Decimal("0.5"), quote_assigned=Decimal("25000"),
        )
        mdp = MagicMock(spec=MarketDataProvider)
        mdp.initialize_rate_sources = MagicMock()
        mdp.time = MagicMock(return_value=1640995200.0)
        # Balance libre amplio por default (para que las grillas se creen normal).
        # Los tests de capital lo override con _set_balance.
        mdp.get_available_balance = MagicMock(return_value=Decimal("1000000"))
        ctrl = Chessboard(config=config, market_data_provider=mdp,
                          actions_queue=AsyncMock(spec=asyncio.Queue))
        return ctrl, mdp

    def _set_price(self, mdp, price):
        mdp.get_price_by_type = MagicMock(return_value=Decimal(str(price)))

    def _set_balance(self, mdp, base=None, quote=None):
        """Setea el balance libre por asset (base=BTC, quote=BRL/USDT). Lo que no se
        pase queda en un valor amplio."""
        defaults = {"BTC": Decimal("1000"), "BRL": Decimal("1000000"),
                    "USDT": Decimal("1000000")}
        if base is not None:
            defaults["BTC"] = Decimal(str(base))
        if quote is not None:
            defaults["BRL"] = Decimal(str(quote))
            defaults["USDT"] = Decimal(str(quote))
        mdp.get_available_balance = MagicMock(
            side_effect=lambda conn, asset: defaults.get(asset, Decimal("1000000")))

    def _mock_executor(self, level_id, is_active=True,
                       status=RunnableStatus.RUNNING, close_type=None, executor_id=None):
        cfg = MagicMock()
        cfg.level_id = level_id
        ex = MagicMock(spec=ExecutorInfo)
        ex.id = executor_id or f"ex_{level_id}"
        ex.config = cfg
        ex.is_active = is_active
        ex.status = status
        ex.close_type = close_type
        ex.custom_info = {}  # sin posición/held -> btc_en_grillas = 0
        return ex

    def _created(self, actions):
        return {a.executor_config.level_id for a in actions if isinstance(a, CreateExecutorAction)}

    # ── Construcción ─────────────────────────────────────────────────────────
    def test_build_steps(self):
        ctrl, _ = self._make_controller(n_grids=10, a="340000", b="400000")
        ancho = (Decimal("400000") - Decimal("340000")) / 10
        print(f"\n=== build_steps: n_grids=10 rango=[340000, 400000] ancho={ancho} ===")
        print(f"{'idx':>3} | {'low':>12} | {'high':>12}")
        print("-" * 34)
        for s in ctrl._steps:
            print(f"{s['idx']:>3} | {s['low']:>12} | {s['high']:>12}")
        print(f"capital por grilla = {ctrl._capital_per_grid()} "
              f"(total {ctrl.config.total_amount_quote} / {ctrl.config.n_grids} escalones / 2 slots)")
        self.assertEqual(len(ctrl._steps), 10)
        self.assertEqual(ctrl._steps[0]["low"], Decimal("340000"))
        self.assertEqual(ctrl._steps[-1]["high"], Decimal("400000"))

    def test_capital_split_by_steps_and_slots(self):
        ctrl, _ = self._make_controller(n_grids=10)
        # 50000 / 10 escalones / 2 slots = 2500
        total = ctrl.config.total_amount_quote
        n = ctrl.config.n_grids
        print(f"\n=== capital_split: total={total} n_grids={n} slots=2 (LONG+SHORT) ===")
        print(f"  capital por grilla = {total} / {n} / 2 = {ctrl._capital_per_grid()}")
        print(f"  capital por escalón (par) = {ctrl._capital_per_grid() * 2}")
        print(f"  capital total desplegable = {ctrl._capital_per_grid() * 2 * n}")
        self.assertEqual(ctrl._capital_per_grid(), Decimal("2500"))

    # ── limit_price (perilla 2: zona muerta / centro de masa) ────────────────
    def test_limit_for_direction_and_distance(self):
        """LONG: limit DEBAJO del piso (low*(1-d)). SHORT: limit ENCIMA del techo (high*(1+d))."""
        ctrl, _ = self._make_controller(n_grids=4, a="300000", b="360000")
        ctrl.config.limit_distance_pct = Decimal("0.005")
        step = ctrl._steps[2]  # cb_2: [330000, 345000]
        limit_long = ctrl._limit_for(step, TradeType.BUY)
        limit_short = ctrl._limit_for(step, TradeType.SELL)
        d = ctrl.config.limit_distance_pct
        print(f"\n=== limit_for: cb_2 [{step['low']}, {step['high']}] d={d} ===")
        print(f"  LONG  -> {limit_long}  (= low {step['low']} * (1-{d}))   debe estar DEBAJO de low")
        print(f"  SHORT -> {limit_short}  (= high {step['high']} * (1+{d}))  debe estar ENCIMA de high")
        # LONG: limit por debajo del piso
        self.assertEqual(limit_long, step["low"] * (Decimal("1") - d))
        self.assertLess(limit_long, step["low"])
        # SHORT: limit por encima del techo
        self.assertEqual(limit_short, step["high"] * (Decimal("1") + d))
        self.assertGreater(limit_short, step["high"])

    def test_grid_config_carries_limit_and_bands(self):
        """El GridExecutorConfig que recibe el executor lleva start/end/limit correctos."""
        ctrl, _ = self._make_controller(n_grids=4, a="300000", b="360000")
        ctrl.config.limit_distance_pct = Decimal("0.005")
        step = ctrl._steps[2]  # [330000, 345000]
        gc_long = ctrl._make_grid_config(step, TradeType.BUY)
        gc_short = ctrl._make_grid_config(step, TradeType.SELL)
        print(f"\n=== make_grid_config: cb_2 [{step['low']}, {step['high']}] ===")
        print(f"  LONG : start={gc_long.start_price} end={gc_long.end_price} "
              f"limit={gc_long.limit_price} side={gc_long.side.name} keep={gc_long.keep_position}")
        print(f"  SHORT: start={gc_short.start_price} end={gc_short.end_price} "
              f"limit={gc_short.limit_price} side={gc_short.side.name} keep={gc_short.keep_position}")
        # bandas = piso/techo del escalón, idénticas para ambos lados
        self.assertEqual(gc_long.start_price, step["low"])
        self.assertEqual(gc_long.end_price, step["high"])
        self.assertEqual(gc_short.start_price, step["low"])
        self.assertEqual(gc_short.end_price, step["high"])
        # limit = el que calcula _limit_for, en cada dirección
        self.assertEqual(gc_long.limit_price, ctrl._limit_for(step, TradeType.BUY))
        self.assertEqual(gc_short.limit_price, ctrl._limit_for(step, TradeType.SELL))
        # keep_position=True es obligatorio para el modelo (POSITION_HOLD vs market)
        self.assertTrue(gc_long.keep_position)
        self.assertTrue(gc_short.keep_position)

    def test_level_id_format(self):
        ctrl, _ = self._make_controller()
        self.assertEqual(ctrl._level_id(3, TradeType.BUY), "cb_3_L")
        self.assertEqual(ctrl._level_id(3, TradeType.SELL), "cb_3_S")
        self.assertEqual(ctrl._parse_level_id("cb_3_L"), (3, TradeType.BUY))
        self.assertEqual(ctrl._parse_level_id("cb_7_S"), (7, TradeType.SELL))

    # ── Despliegue inicial ───────────────────────────────────────────────────
    def test_initial_deploy_creates_pair_at_price_step(self):
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        # precio 370000 cae en cb_5 [370000-376000]... ancho=6000: cb_5=[370000,376000]
        self._set_price(mdp, "373000")  # claramente dentro de cb_5
        ctrl.executors_info = []
        actions = ctrl.determine_executor_actions()
        self.assertEqual(self._created(actions), {"cb_5_L", "cb_5_S"})

    # ── Relevo asimétrico ────────────────────────────────────────────────────
    def _print_relay(self, closed, side, close_type, direction, created, expected):
        sentido = "favorable" if close_type == CloseType.TAKE_PROFIT else "desfavorable"
        flecha = "arriba (+1)" if direction == 1 else "abajo (-1)"
        print(f"\n=== relevo: {closed} ({side.name}) cierra por {close_type.name} [{sentido}] ===")
        print(f"  _relay_direction({side.name}, {close_type.name}) = {direction:+d}  -> releva {flecha}")
        print(f"  creado: {created}  (esperado: {{{expected!r}}})")

    # En cada caso el precio se pone en el escalón DESTINO del relevo (coherente
    # con la física: si una LONG cierra por TP el precio subió; si cierra por HOLD
    # bajó). Así el relevo crea su grilla y la Fase 3 asegura el par de ese escalón.
    def test_long_tp_relays_long_up(self):
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "379000")  # precio subió a cb_6 (LONG hizo TP)
        ctrl.executors_info = [self._mock_executor(
            "cb_5_L", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        created = self._created(ctrl.determine_executor_actions())
        self._print_relay("cb_5_L", TradeType.BUY, CloseType.TAKE_PROFIT,
                          ctrl._relay_direction(TradeType.BUY, CloseType.TAKE_PROFIT),
                          created, "cb_6_L (relevo) + cb_6_S (par)")
        self.assertIn("cb_6_L", created)            # relevo LONG +1
        self.assertEqual(created, {"cb_6_L", "cb_6_S"})  # + par del escalón del precio

    def test_long_limit_relays_long_down(self):
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "367000")  # precio bajó a cb_4 (LONG hizo HOLD)
        ctrl.executors_info = [self._mock_executor(
            "cb_5_L", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.POSITION_HOLD)]
        created = self._created(ctrl.determine_executor_actions())
        self._print_relay("cb_5_L", TradeType.BUY, CloseType.POSITION_HOLD,
                          ctrl._relay_direction(TradeType.BUY, CloseType.POSITION_HOLD),
                          created, "cb_4_L (relevo) + cb_4_S (par)")
        self.assertIn("cb_4_L", created)            # relevo LONG -1
        self.assertEqual(created, {"cb_4_L", "cb_4_S"})

    def test_short_tp_relays_short_down(self):
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "367000")  # precio bajó a cb_4 (SHORT hizo TP)
        ctrl.executors_info = [self._mock_executor(
            "cb_5_S", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        created = self._created(ctrl.determine_executor_actions())
        self._print_relay("cb_5_S", TradeType.SELL, CloseType.TAKE_PROFIT,
                          ctrl._relay_direction(TradeType.SELL, CloseType.TAKE_PROFIT),
                          created, "cb_4_S (relevo) + cb_4_L (par)")
        self.assertIn("cb_4_S", created)            # relevo SHORT -1
        self.assertEqual(created, {"cb_4_S", "cb_4_L"})

    def test_short_limit_relays_short_up(self):
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "379000")  # precio subió a cb_6 (SHORT hizo HOLD)
        ctrl.executors_info = [self._mock_executor(
            "cb_5_S", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.POSITION_HOLD)]
        created = self._created(ctrl.determine_executor_actions())
        self._print_relay("cb_5_S", TradeType.SELL, CloseType.POSITION_HOLD,
                          ctrl._relay_direction(TradeType.SELL, CloseType.POSITION_HOLD),
                          created, "cb_6_S (relevo) + cb_6_L (par)")
        self.assertIn("cb_6_S", created)            # relevo SHORT +1
        self.assertEqual(created, {"cb_6_S", "cb_6_L"})

    def test_chains_are_independent(self):
        """LONG y SHORT relevan por separado sin pisarse. Con la guarda de banda,
        el escalón del precio determina qué se crea: aquí el precio está en cb_6,
        así que el relevo LONG (cb_5_L TP -> cb_6_L) cae en banda y se crea junto
        a su par cb_6_S; el relevo SHORT (cb_5_S TP -> cb_4_S) queda fuera de banda
        y NO se crea (se crearía si el precio bajara a cb_4)."""
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "379000")  # cb_6 [376000-382000]
        ctrl.executors_info = [
            self._mock_executor("cb_5_L", is_active=False,
                                status=RunnableStatus.TERMINATED, close_type=CloseType.TAKE_PROFIT),
            self._mock_executor("cb_5_S", is_active=False,
                                status=RunnableStatus.TERMINATED, close_type=CloseType.TAKE_PROFIT),
        ]
        created = self._created(ctrl.determine_executor_actions())
        # par del escalón del precio (cb_6); relevo LONG coincide con cb_6_L.
        self.assertEqual(created, {"cb_6_L", "cb_6_S"})
        self.assertNotIn("cb_4_S", created)  # destino fuera de banda -> no nace muerta

    def test_pair_ensured_when_one_side_missing(self):
        """Regresión: en un movimiento sostenido, el escalón del precio puede quedar
        con un solo lado (la cadena que relevó). El controller debe crear el lado
        faltante para mantener el par (invariante CB4). Caso real observado:
        cb_2 con SHORT activa pero sin LONG."""
        ctrl, mdp = self._make_controller(n_grids=10, a="328423", b="347423")
        # precio en cb_2 [332223, 334123]
        self._set_price(mdp, "333000")
        # Solo la SHORT de cb_2 está activa; la LONG nunca se creó (hueco).
        ctrl.executors_info = [self._mock_executor(
            "cb_2_S", is_active=True, status=RunnableStatus.RUNNING)]
        created = self._created(ctrl.determine_executor_actions())
        print("\n=== par asegurado: cb_2 tenía solo S✓, falta L ===")
        print(f"  creado: {created}  (esperado: cb_2_L)")
        self.assertIn("cb_2_L", created)   # crea la LONG faltante
        self.assertNotIn("cb_2_S", created)  # no duplica la SHORT activa

    def test_pair_repopulated_when_price_returns(self):
        """Regresión del bug '0 grillas activas': si el precio vuelve a un escalón
        cuyos dos lados ya fueron relevados, el controller DEBE repoblar el par.
        La guarda de no-doble-relevo es por executor.id, no por level_id, así que
        el slot se recrea cuando el precio vuelve (no queda bloqueado para siempre)."""
        ctrl, mdp = self._make_controller(n_grids=10, a="328423", b="347423")
        self._set_price(mdp, "333000")  # cb_2
        # cb_2 fue relevado antes (marca histórica de display), pero NO hay executor
        # activo ahí ahora, y el precio volvió. Debe repoblar el par.
        ctrl._closed_handled.update({"cb_2_L", "cb_2_S"})
        ctrl._relayed_executor_ids.add("ex_old")  # ya hubo relevos -> no es arranque
        ctrl.executors_info = []  # nada activo en cb_2
        created = self._created(ctrl.determine_executor_actions())
        print("\n=== repoblar par: precio volvió a cb_2 (ya relevado) ===")
        print(f"  creado: {created}  (esperado: cb_2_L + cb_2_S)")
        self.assertEqual(created, {"cb_2_L", "cb_2_S"})  # par repoblado

    def test_no_double_relay(self):
        """El mismo cierre no se releva dos veces (guarda por executor.id). Precio en
        cb_6 para que el relevo LONG (cb_5_L -> cb_6_L) caiga en banda."""
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "379000")  # cb_6 (destino del relevo)
        ctrl.executors_info = [self._mock_executor(
            "cb_5_L", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        a1 = ctrl.determine_executor_actions()
        self.assertIn("cb_6_L", self._created(a1))  # primer relevo crea cb_6_L
        # 2º tick: cb_6_L ya está activo (se creó) y el cierre viejo sigue presente.
        # No debe re-relevar (su executor.id está en _relayed_executor_ids) ni
        # duplicar cb_6_L (ya activo).
        ctrl.executors_info.append(self._mock_executor(
            "cb_6_L", is_active=True, status=RunnableStatus.RUNNING, executor_id="ex_cb_6_L"))
        a2 = ctrl.determine_executor_actions()
        self.assertNotIn("cb_6_L", self._created(a2))  # no releva de nuevo

    def test_relay_at_edge_no_out_of_range(self):
        """El relevo no debe crear fuera de rango (cb_10 inválido). La Fase 3 sí
        asegura el par del escalón del precio, pero nunca un índice inexistente."""
        ctrl, mdp = self._make_controller(n_grids=10)
        self._set_price(mdp, "373000")  # escalón del precio = cb_5
        # cb_9_L cierra por TP -> +1 = cb_10 (inválido). NO debe crear cb_10.
        ctrl.executors_info = [self._mock_executor(
            "cb_9_L", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        created = self._created(ctrl.determine_executor_actions())
        # No se crea ningún índice fuera de [0, 10)
        self.assertNotIn("cb_10_L", created)
        self.assertTrue(all(0 <= int(c.split("_")[1]) < 10 for c in created))
        # El relevo a cb_10 no ocurrió; lo único creado es el par del escalón del
        # precio (cb_5), que estaba vacío.
        self.assertEqual(created, {"cb_5_L", "cb_5_S"})

    async def test_mapping_updates(self):
        ctrl, _ = self._make_controller()
        ctrl.executors_info = [self._mock_executor("cb_5_L", executor_id="EXEC1")]
        await ctrl.update_processed_data()
        self.assertEqual(ctrl._level_executor.get("cb_5_L"), "EXEC1")

    # ── Corte en target ──────────────────────────────────────────────────────
    # El %BTC se mide sobre la SUB-CUENTA (base_assigned + BTC en grillas), no el
    # balance global. Forzamos un %BTC bajo seteando base_assigned chico.
    def _set_subcuenta(self, ctrl, base, quote):
        ctrl.config.base_assigned = Decimal(str(base))
        ctrl.config.quote_assigned = Decimal(str(quote))

    # En estos tests el SHORT cierra en cb_5 por TP -> relevo a cb_4_S (abajo); el
    # precio se pone en cb_4 (367000) para que ese destino caiga EN BANDA (si no,
    # la guarda anti-loop lo bloquea). Idem LONG -> cb_6_L con precio en cb_6.
    def test_target_blocks_short_when_reached(self):
        """Con target alcanzado (%BTC sub-cuenta <= target), una SHORT que cerró NO releva otra SHORT."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        ctrl.config.target_pct_btc = Decimal("0.60")
        self._set_price(mdp, "367000")  # cb_4 (destino del relevo SHORT)
        # base chico -> %btc bajo < 60% -> bloquea descarga
        self._set_subcuenta(ctrl, "0.001", "700")
        ctrl.executors_info = [self._mock_executor(
            "cb_5_S", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        actions = ctrl.determine_executor_actions()
        self.assertNotIn("cb_4_S", self._created(actions))  # SHORT bloqueada por target

    def test_target_allows_long_even_when_reached(self):
        """Con target alcanzado, las LONG siguen habilitadas (pueden recargar)."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        ctrl.config.target_pct_btc = Decimal("0.60")
        self._set_price(mdp, "379000")  # cb_6 (destino del relevo LONG)
        self._set_subcuenta(ctrl, "0.001", "700")  # %btc bajo
        ctrl.executors_info = [self._mock_executor(
            "cb_5_L", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        actions = ctrl.determine_executor_actions()
        self.assertIn("cb_6_L", self._created(actions))  # LONG NO bloqueada

    def test_target_allows_short_when_above_target(self):
        """Con %BTC ARRIBA del target, la SHORT releva normal (todavía hay que descargar)."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        ctrl.config.target_pct_btc = Decimal("0.60")
        self._set_price(mdp, "367000")  # cb_4 (destino del relevo SHORT)
        # base grande -> %btc alto > 60% -> permite descarga
        self._set_subcuenta(ctrl, "0.05", "1000")
        ctrl.executors_info = [self._mock_executor(
            "cb_5_S", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        actions = ctrl.determine_executor_actions()
        self.assertIn("cb_4_S", self._created(actions))  # %btc alto -> sigue descargando

    def test_no_target_no_block(self):
        """Sin target (None), no se bloquea nada (comportamiento de cubrir rango)."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        ctrl.config.target_pct_btc = None
        self._set_price(mdp, "367000")  # cb_4 (destino del relevo SHORT)
        self._set_subcuenta(ctrl, "0.001", "700")  # %btc bajo, pero sin target no importa
        ctrl.executors_info = [self._mock_executor(
            "cb_5_S", is_active=False, status=RunnableStatus.TERMINATED,
            close_type=CloseType.TAKE_PROFIT)]
        actions = ctrl.determine_executor_actions()
        self.assertIn("cb_4_S", self._created(actions))  # sin target, releva normal

    # ── Inventario firme con signo ───────────────────────────────────────────
    @staticmethod
    def _held_orders(*pairs, price="1000"):
        """pairs = ("BUY", "0.01"), ("SELL", "0.003") -> custom_info held_position_orders.
        El quote de cada fill = base * price (cada fill mueve base y quote 1:1)."""
        p = Decimal(str(price))
        return [{"trade_type": side, "executed_amount_base": amt,
                 "executed_amount_quote": str(Decimal(str(amt)) * p)} for side, amt in pairs]

    def test_held_btc_signed_long_positive(self):
        ctrl, _ = self._make_controller()
        ci = {"held_position_orders": self._held_orders(("BUY", "0.01"), ("BUY", "0.005"))}
        self.assertEqual(ctrl._held_btc_signed(ci), Decimal("0.015"))

    def test_held_btc_signed_short_negative(self):
        ctrl, _ = self._make_controller()
        ci = {"held_position_orders": self._held_orders(("SELL", "0.012"))}
        self.assertEqual(ctrl._held_btc_signed(ci), Decimal("-0.012"))

    def test_held_btc_signed_mixed(self):
        ctrl, _ = self._make_controller()
        ci = {"held_position_orders": self._held_orders(("BUY", "0.01"), ("SELL", "0.003"))}
        self.assertEqual(ctrl._held_btc_signed(ci), Decimal("0.007"))

    def test_held_btc_signed_empty(self):
        ctrl, _ = self._make_controller()
        self.assertEqual(ctrl._held_btc_signed({}), Decimal("0"))
        self.assertEqual(ctrl._held_btc_signed(None), Decimal("0"))

    async def test_event_registered_once(self):
        """Un cierre se contabiliza una sola vez (clave por executor.id)."""
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "373000")
        ex = self._mock_executor("cb_5_L", is_active=False,
                                 status=RunnableStatus.TERMINATED,
                                 close_type=CloseType.POSITION_HOLD, executor_id="EX1")
        ex.custom_info = {"held_position_orders": self._held_orders(("BUY", "0.01"))}
        ctrl.executors_info = [ex]
        await ctrl.update_processed_data()
        await ctrl.update_processed_data()  # segundo tick, no debe duplicar
        self.assertEqual(len(ctrl._rebalance_events), 1)
        self.assertEqual(ctrl._net_btc_from_grids, Decimal("0.01"))

    async def test_net_btc_accumulates_with_sign(self):
        """LONG hold (+) y SHORT hold (-) acumulan el neto correcto."""
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "373000")
        ex_l = self._mock_executor("cb_5_L", is_active=False,
                                   status=RunnableStatus.TERMINATED,
                                   close_type=CloseType.POSITION_HOLD, executor_id="EXL")
        ex_l.custom_info = {"held_position_orders": self._held_orders(("BUY", "0.02"))}
        ex_s = self._mock_executor("cb_5_S", is_active=False,
                                   status=RunnableStatus.TERMINATED,
                                   close_type=CloseType.POSITION_HOLD, executor_id="EXS")
        ex_s.custom_info = {"held_position_orders": self._held_orders(("SELL", "0.005"))}
        ctrl.executors_info = [ex_l, ex_s]
        await ctrl.update_processed_data()
        self.assertEqual(ctrl._net_btc_from_grids, Decimal("0.015"))

    async def test_tp_residual_separated_from_hold(self):
        """TP con held cae en tp_residual_btc, NO en hold_btc."""
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "373000")
        ex_tp = self._mock_executor("cb_5_L", is_active=False,
                                    status=RunnableStatus.TERMINATED,
                                    close_type=CloseType.TAKE_PROFIT, executor_id="EXTP")
        ex_tp.custom_info = {"held_position_orders": self._held_orders(("BUY", "0.001"))}
        ex_hold = self._mock_executor("cb_4_L", is_active=False,
                                      status=RunnableStatus.TERMINATED,
                                      close_type=CloseType.POSITION_HOLD, executor_id="EXH")
        ex_hold.custom_info = {"held_position_orders": self._held_orders(("BUY", "0.02"))}
        ctrl.executors_info = [ex_tp, ex_hold]
        await ctrl.update_processed_data()
        self.assertEqual(ctrl.processed_data["tp_residual_btc"], Decimal("0.001"))
        self.assertEqual(ctrl.processed_data["hold_btc"], Decimal("0.02"))

    async def test_unexpected_close_type(self):
        """EARLY_STOP cae en unexpected_close_btc (no es TP ni HOLD)."""
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "373000")
        ex = self._mock_executor("cb_5_L", is_active=False,
                                 status=RunnableStatus.TERMINATED,
                                 close_type=CloseType.EARLY_STOP, executor_id="EXE")
        ex.custom_info = {"held_position_orders": self._held_orders(("BUY", "0.003"))}
        ctrl.executors_info = [ex]
        await ctrl.update_processed_data()
        self.assertEqual(ctrl.processed_data["unexpected_close_btc"], Decimal("0.003"))
        self.assertEqual(ctrl.processed_data["hold_btc"], Decimal("0"))

    # ── Eventos JSONL ─────────────────────────────────────────────────────────
    async def test_jsonl_rebalance_event(self):
        """Un cierre escribe un evento rebalance al JSONL con capital aislado de la grilla."""
        import json
        import os
        import tempfile
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "373000")
        with tempfile.TemporaryDirectory() as d:
            ctrl._events_path = os.path.join(d, "events.jsonl")
            ex = self._mock_executor("cb_5_L", is_active=False,
                                     status=RunnableStatus.TERMINATED,
                                     close_type=CloseType.POSITION_HOLD, executor_id="EX1")
            ex.custom_info = {
                "held_position_orders": self._held_orders(("BUY", "0.01")),
                "filled_amount_quote": "100", "realized_pnl_quote": "0.5",
                "held_position_value": "37", "break_even_price": "372000",
            }
            ex.config.total_amount_quote = Decimal("2500")
            ctrl.executors_info = [ex]
            await ctrl.update_processed_data()
            self.assertTrue(os.path.exists(ctrl._events_path))
            evs = [json.loads(line) for line in open(ctrl._events_path)]
            self.assertEqual(len(evs), 1)
            e = evs[0]
            self.assertEqual(e["type"], "rebalance")
            self.assertEqual(e["level_id"], "cb_5_L")
            self.assertEqual(e["side"], "LONG")
            self.assertEqual(e["close_type"], "POSITION_HOLD")
            self.assertEqual(e["capital"]["assigned_quote"], 2500.0)
            self.assertEqual(e["capital"]["delta_btc"], 0.01)
            self.assertEqual(e["net_btc_from_grids_after"], 0.01)

    async def test_jsonl_no_event_without_close(self):
        """Sin cierres no se escribe ningún evento."""
        import os
        import tempfile
        ctrl, mdp = self._make_controller()
        self._set_price(mdp, "373000")
        with tempfile.TemporaryDirectory() as d:
            ctrl._events_path = os.path.join(d, "events.jsonl")
            ex = self._mock_executor("cb_5_L", is_active=True,
                                     status=RunnableStatus.RUNNING, executor_id="EX1")
            ex.custom_info = {}
            ctrl.executors_info = [ex]
            await ctrl.update_processed_data()
            self.assertFalse(os.path.exists(ctrl._events_path))

    # ── Capital por balance libre + zona sin munición ────────────────────────
    def test_capital_for_caps_at_free_balance(self):
        """_capital_for capa al balance libre del lado (evita INSUFFICIENT_BALANCE)."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        self._set_price(mdp, "373000")
        # LONG con BRL libre chico -> capa a ese libre (el nominal del recorrido es mayor)
        self._set_balance(mdp, quote="800")
        self.assertEqual(ctrl._capital_for(TradeType.BUY), Decimal("800"))

    def test_capital_for_dimensiona_al_recorrido(self):
        """El capital se dimensiona al recorrido techo<->piso, asimétrico LONG/SHORT
        (no total/N/2). SHORT reparte la descarga; LONG reparte la carga."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        ctrl.config.target_pct_btc = Decimal("0.50")   # piso 50%
        ctrl.config.techo_pct_btc = Decimal("1.0")     # techo 100%
        self._set_price(mdp, "373000")
        self._set_balance(mdp)  # balance amplio: no capa, vemos el nominal del recorrido
        rec = ctrl._recorrido_quote()
        self.assertIsNotNone(rec)
        brl_descarga, brl_carga = rec
        # base 0.5 BTC @ 373000 = 186500 ; quote 25000 ; nav 211500 ; %BTC ~88%
        # piso 50% y techo 100% -> hay descarga Y carga, ambas > 0
        self.assertGreater(brl_descarga, 0)
        self.assertGreater(brl_carga, 0)
        cap_short = ctrl._capital_for(TradeType.SELL)
        cap_long = ctrl._capital_for(TradeType.BUY)
        # asimétrico: SHORT y LONG difieren (distinto nº de grillas por lado y distinto BRL)
        self.assertNotEqual(cap_short, cap_long)
        self.assertGreater(cap_short, 0)
        self.assertGreater(cap_long, 0)

    def test_long_no_capital_marks_zone(self):
        """LONG con BRL libre < min_order_amount no se crea, marca zona y abre evento."""
        import os
        import tempfile
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        self._set_price(mdp, "373000")  # cb_5
        with tempfile.TemporaryDirectory() as d:
            ctrl._events_path = os.path.join(d, "events.jsonl")
            self._set_balance(mdp, quote="5", base="1000")  # BRL casi 0 (<20), BTC sobra
            actions = ctrl.determine_executor_actions()
            created = self._created(actions)
            print("\n=== sin munición LONG: BRL libre=5 (<20) ===")
            print(f"  creado: {created}  | zonas sin cap: {list(ctrl._no_capital_open)}")
            self.assertNotIn("cb_5_L", created)            # LONG no se crea
            self.assertIn("cb_5_S", created)               # SHORT sí (BTC sobra)
            self.assertIn("cb_5_L", ctrl._no_capital_open)  # zona marcada

    def test_no_capital_zone_recovers(self):
        """Si vuelve el capital, la zona sin munición se cierra y la grilla se crea."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        self._set_price(mdp, "373000")
        self._set_balance(mdp, quote="5")  # sin BRL -> abre zona
        ctrl.determine_executor_actions()
        self.assertIn("cb_5_L", ctrl._no_capital_open)
        # ahora vuelve el BRL
        self._set_balance(mdp, quote="9000")
        created = self._created(ctrl.determine_executor_actions())
        self.assertIn("cb_5_L", created)                    # ya se crea
        self.assertNotIn("cb_5_L", ctrl._no_capital_open)   # zona cerrada

    async def test_no_capital_event_persisted_on_close(self):
        """Al cerrar la zona (recuperación o salida del precio) se persiste el evento."""
        import json
        import os
        import tempfile
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        self._set_price(mdp, "373000")
        with tempfile.TemporaryDirectory() as d:
            ctrl._events_path = os.path.join(d, "events.jsonl")
            self._set_balance(mdp, quote="5")
            ctrl.determine_executor_actions()                # abre zona cb_5_L
            self._set_balance(mdp, quote="9000")
            ctrl.determine_executor_actions()                # cierra zona -> persiste
            evs = [json.loads(line) for line in open(ctrl._events_path)]
            nocap = [e for e in evs if e["type"] == "no_capital"]
            self.assertEqual(len(nocap), 1)
            self.assertEqual(nocap[0]["level_id"], "cb_5_L")
            self.assertEqual(nocap[0]["side"], "LONG")
            self.assertEqual(nocap[0]["asset_needed"], "USDT")  # quote de BTC-USDT
            self.assertFalse(nocap[0]["open"])
            self.assertIsNotNone(nocap[0]["ts_close"])

    def test_no_capital_zone_closes_when_price_leaves(self):
        """Si el precio sale del escalón, la zona sin munición huérfana se cierra."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        self._set_price(mdp, "373000")  # cb_5
        self._set_balance(mdp, quote="5")
        ctrl.determine_executor_actions()
        self.assertIn("cb_5_L", ctrl._no_capital_open)
        # el precio se va a cb_8; cb_5 ya no contiene el precio -> zona huérfana se cierra
        self._set_price(mdp, "391000")  # cb_8
        self._set_balance(mdp, quote="9000")
        ctrl.determine_executor_actions()
        self.assertNotIn("cb_5_L", ctrl._no_capital_open)

    # ── Recorrido de precio: descarga monótona + corte en target ─────────────
    async def test_price_walk_up_discharges_to_target_and_cuts_shorts(self):
        """Recorrido SUBIENDO de A hacia B (lo que descarga BTC: 80% -> 60%).

        Modela el comportamiento real: cuando el precio sube y cruza el techo de un
        escalón, la SHORT de ese escalón cierra por POSITION_HOLD vendiendo BTC
        (held SELL, signo -). El controller debe:
          (1) acumular net_btc_from_grids NEGATIVO (descarga),
          (2) ver el %BTC bajar monótonamente hacia el target,
          (3) cuando el %BTC firme <= 60%, DEJAR DE CREAR SHORT (corte),
              sin frenar las LONG.

        Sub-cuenta DIDÁCTICA (no la real): base=80, quote=20000, precio=1000 ->
        %BTC inicial 80%. Cada SHORT POSITION_HOLD vende 20 BTC y cobra 20000 quote
        (NAV invariante en 100000: solo intercambia base<->quote al precio del fill).
          paso 1: net=-20 -> base=60 quote=40000 -> 60%   (sigue descargando)
          paso 2: net=-40 -> base=40 quote=60000 -> 40%   (<=60% -> corte activo)
          paso 3: net=-60 -> base=20 quote=80000 -> 20%   (cortado)
        """
        ctrl, mdp = self._make_controller(n_grids=10, a="800", b="1200")
        ctrl.config.target_pct_btc = Decimal("0.60")   # piso
        ctrl.config.techo_pct_btc = Decimal("1.0")     # techo
        ctrl.config.hysteresis_pct = Decimal("0")      # aislar: sin histéresis
        ctrl.config.base_assigned = Decimal("80")
        ctrl.config.quote_assigned = Decimal("20000")
        self._set_price(mdp, "1000")  # precio fijo para aislar el efecto del inventario

        def close_short(idx, executor_id):
            ex = self._mock_executor(
                f"cb_{idx}_S", is_active=False, status=RunnableStatus.TERMINATED,
                close_type=CloseType.POSITION_HOLD, executor_id=executor_id)
            ex.custom_info = {"held_position_orders": self._held_orders(("SELL", "20"))}
            return ex

        print("\n=== recorrido: precio sube, SHORTs descargan, %BTC baja ===")
        pcts = []
        for paso, idx in enumerate([3, 4, 5], start=1):
            ctrl.executors_info = [close_short(idx, f"EXS{idx}")]
            await ctrl.update_processed_data()
            ctrl.determine_executor_actions()
            pct = ctrl.processed_data["pct_btc"]
            net = ctrl.processed_data["net_btc_from_grids"]
            pcts.append(pct)
            print(f"  paso {paso}: SHORT cb_{idx}_S hold -> net={net:+} %BTC={pct:.1%}")
            # El inventario firme baja (descarga): net cada vez más negativo
            self.assertEqual(net, Decimal("-20") * paso)

        # (1) %BTC monótonamente decreciente
        self.assertTrue(all(pcts[i] > pcts[i + 1] for i in range(len(pcts) - 1)),
                        f"%BTC no es monótonamente decreciente: {[f'{p:.3f}' for p in pcts]}")
        # (2) el target LOCAL bloquea la SHORT cuando el %BTC firme cruza por debajo.
        #     A precio 1000 en rango [800,1200], target_local ~ interp(techo,piso,0.5)=0.80.
        #     Con %BTC ya en 50%, una SHORT en el escalón del precio está bloqueada.
        idx_precio = ctrl._step_containing(Decimal("1000"))["idx"]
        self.assertTrue(ctrl._blocked_by_local_target(idx_precio, TradeType.SELL))

    async def test_price_walk_down_loads_and_longs_keep_running(self):
        """Recorrido BAJANDO: las LONG cargan BTC (held BUY, +) y el %BTC SUBE.
        La carga NUNCA se corta, aunque el %BTC supere el target."""
        ctrl, mdp = self._make_controller(n_grids=10, a="800", b="1200")
        ctrl.config.target_pct_btc = Decimal("0.60")
        ctrl.config.techo_pct_btc = Decimal("1.0")
        ctrl.config.hysteresis_pct = Decimal("0")   # aislar el relevo
        ctrl.config.base_assigned = Decimal("20")    # %BTC bajo -> LONG NO bloqueada por target local
        ctrl.config.quote_assigned = Decimal("80000")
        # precio en el centro de cb_4 (no en borde) para que el relevo a cb_4_L caiga en banda
        self._set_price(mdp, "984")  # cb_4 = [960, 1000), centro 980

        ex = self._mock_executor("cb_5_L", is_active=False,
                                 status=RunnableStatus.TERMINATED,
                                 close_type=CloseType.POSITION_HOLD, executor_id="EXL")
        ex.custom_info = {"held_position_orders": self._held_orders(("BUY", "10"))}
        ctrl.executors_info = [ex]
        await ctrl.update_processed_data()
        actions = ctrl.determine_executor_actions()
        created = self._created(actions)
        net = ctrl.processed_data["net_btc_from_grids"]
        print("\n=== recorrido bajando: LONG cb_5_L hold carga BTC ===")
        print(f"  net={net:+} (carga, +) %BTC={ctrl.processed_data['pct_btc']:.1%} creado={created}")
        # carga: net positivo; LONG releva abajo (cb_4_L); las LONG no se cortan nunca
        self.assertEqual(net, Decimal("10"))
        self.assertIn("cb_4_L", created)

    # ── Inventario real desde fills (NAV invariante) ─────────────────────────
    def test_held_quote_signed(self):
        """Espejo del BTC: BUY gastó quote (−), SELL cobró quote (+)."""
        ctrl, _ = self._make_controller()
        ci = {"held_position_orders": self._held_orders(("BUY", "0.01"), ("SELL", "0.004"))}
        # BUY 0.01 -> −10 quote ; SELL 0.004 -> +4 quote (price=1000) => −6
        self.assertEqual(ctrl._held_quote_signed(ci), Decimal("-6"))
        self.assertEqual(ctrl._held_quote_signed({}), Decimal("0"))
        self.assertEqual(ctrl._held_quote_signed(None), Decimal("0"))

    async def test_real_inventory_nav_invariant(self):
        """Cada fill mueve base y quote 1:1 -> el NAV NO se encoge al vender.
        base=80, quote=20000, precio=1000 (NAV=100k). SELL de 20 BTC:
        base 60, quote 40000, NAV sigue 100k, %BTC 80%->60%."""
        ctrl, mdp = self._make_controller(n_grids=10, a="800", b="1200")
        ctrl.config.base_assigned = Decimal("80")
        ctrl.config.quote_assigned = Decimal("20000")
        self._set_price(mdp, "1000")
        ex = self._mock_executor("cb_5_S", is_active=False,
                                 status=RunnableStatus.TERMINATED,
                                 close_type=CloseType.POSITION_HOLD, executor_id="EX1")
        ex.custom_info = {"held_position_orders": self._held_orders(("SELL", "20"))}
        ctrl.executors_info = [ex]
        await ctrl.update_processed_data()
        inv = ctrl._real_inventory_from_fills()
        print(f"\n=== NAV invariante: base={inv['base']} quote={inv['quote']} "
              f"nav={inv['nav']} pct={inv['pct']:.1%} ===")
        self.assertEqual(inv["base"], Decimal("60"))
        self.assertEqual(inv["quote"], Decimal("40000"))
        self.assertEqual(inv["nav"], Decimal("100000"))
        self.assertEqual(inv["pct"], Decimal("0.6"))

    async def test_recorrido_quote_uses_real_quote(self):
        """_recorrido_quote dimensiona con el quote REAL acumulado (no el assigned
        fijo). Tras vender, la descarga restante se calcula sobre el NAV invariante."""
        ctrl, mdp = self._make_controller(n_grids=10, a="800", b="1200")
        ctrl.config.target_pct_btc = Decimal("0.60")
        ctrl.config.techo_pct_btc = Decimal("1.0")
        ctrl.config.base_assigned = Decimal("80")
        ctrl.config.quote_assigned = Decimal("20000")
        self._set_price(mdp, "1000")
        # Sin fills: NAV=100k, %BTC=80%. piso=60% -> btc_piso=60 -> descarga=(80-60)*1000=20000
        d0, c0 = ctrl._recorrido_quote()
        self.assertEqual(d0, Decimal("20000"))
        # SELL de 10 BTC: base=70, quote=30000, NAV sigue 100k -> descarga=(70-60)*1000=10000
        ex = self._mock_executor("cb_5_S", is_active=False,
                                 status=RunnableStatus.TERMINATED,
                                 close_type=CloseType.POSITION_HOLD, executor_id="EX1")
        ex.custom_info = {"held_position_orders": self._held_orders(("SELL", "10"))}
        ctrl.executors_info = [ex]
        await ctrl.update_processed_data()
        d1, c1 = ctrl._recorrido_quote()
        print(f"\n=== recorrido con quote real: descarga {d0} -> {d1} ===")
        self.assertEqual(d1, Decimal("10000"))

    # ── Histéresis ───────────────────────────────────────────────────────────
    def test_in_hysteresis_band(self):
        """Franja = hysteresis_pct del ancho en CADA borde. 0 = nunca; 0.5 = todo."""
        ctrl, _ = self._make_controller(n_grids=10, a="340000", b="400000")
        step = ctrl._steps[0]  # [340000, 346000], ancho 6000
        ctrl.config.hysteresis_pct = Decimal("0.1")  # margen 600 por borde
        self.assertTrue(ctrl._in_hysteresis_band(step, Decimal("340300")))   # pegado abajo
        self.assertTrue(ctrl._in_hysteresis_band(step, Decimal("345700")))   # pegado arriba
        self.assertFalse(ctrl._in_hysteresis_band(step, Decimal("343000")))  # centro
        ctrl.config.hysteresis_pct = Decimal("0")
        self.assertFalse(ctrl._in_hysteresis_band(step, Decimal("340001")))  # sin histéresis
        ctrl.config.hysteresis_pct = Decimal("0.5")
        self.assertTrue(ctrl._in_hysteresis_band(step, Decimal("342999")))   # bloquea todo

    async def test_tp_relay_blocked_at_band_edge(self):
        """Relevo desde TP con el precio pegado al borde del escalón destino: la
        histéresis lo bloquea (la consecutiva-de-TP abre posición al nacer).
        El mismo relevo desde POSITION_HOLD NO se bloquea (no abre de golpe)."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        ctrl.config.hysteresis_pct = Decimal("0.1")
        # cb_6 = [376000, 382000], margen 600. Precio pegado al borde bajo de cb_6.
        self._set_price(mdp, "376300")
        ex_tp = self._mock_executor("cb_5_L", is_active=False,
                                    status=RunnableStatus.TERMINATED,
                                    close_type=CloseType.TAKE_PROFIT, executor_id="EXTP")
        ctrl.executors_info = [ex_tp]
        actions = ctrl.determine_executor_actions()
        created = self._created(actions)
        print(f"\n=== TP-relay en borde: creado={created} (cb_6_L NO debe estar) ===")
        self.assertNotIn("cb_6_L", created)
        # Mismo escenario pero el cierre fue POSITION_HOLD -> sin histéresis, SÍ crea.
        ctrl2, mdp2 = self._make_controller(n_grids=10, a="340000", b="400000")
        ctrl2.config.hysteresis_pct = Decimal("0.1")
        self._set_price(mdp2, "376300")
        ex_hold = self._mock_executor("cb_7_L", is_active=False,
                                      status=RunnableStatus.TERMINATED,
                                      close_type=CloseType.POSITION_HOLD, executor_id="EXH")
        ctrl2.executors_info = [ex_hold]
        created2 = self._created(ctrl2.determine_executor_actions())
        self.assertIn("cb_6_L", created2)

    # ── Relevo en bordes del tablero ─────────────────────────────────────────
    def test_relay_out_of_range_low_border(self):
        """SHORT TP en cb_0 releva hacia abajo (cb_-1): fuera de rango, no crea."""
        ctrl, mdp = self._make_controller(n_grids=10, a="340000", b="400000")
        self._set_price(mdp, "343000")  # dentro de cb_0
        ex = self._mock_executor("cb_0_S", is_active=False,
                                 status=RunnableStatus.TERMINATED,
                                 close_type=CloseType.TAKE_PROFIT, executor_id="EX0")
        ctrl.executors_info = [ex]
        actions = ctrl.determine_executor_actions()
        created = self._created(actions)
        # el relevo cb_-1_S no existe; la Fase 3 igual repuebla el par del escalón
        self.assertNotIn("cb_-1_S", created)
        for lid in created:
            self.assertTrue(lid.startswith("cb_0"))

    # ── Snapshot teórico-vs-real ─────────────────────────────────────────────
    async def test_build_snapshot_content(self):
        """El snapshot trae real (desde fills), teórico (target_local del escalón
        del precio) y drift = real − teórico. NAV invariante."""
        ctrl, mdp = self._make_controller(n_grids=10, a="800", b="1200")
        ctrl.config.target_pct_btc = Decimal("0.60")
        ctrl.config.techo_pct_btc = Decimal("1.0")
        ctrl.config.base_assigned = Decimal("80")
        ctrl.config.quote_assigned = Decimal("20000")
        self._set_price(mdp, "1000")
        ctrl.executors_info = []
        snap = ctrl._build_snapshot(1640995200.0)
        print(f"\n=== snapshot: real={snap['real']} teorico={snap['teorico']} "
              f"drift={snap['drift']} ===")
        self.assertEqual(snap["nav"], 100000.0)
        self.assertEqual(snap["real"]["pct_btc"], 0.8)
        # precio 1000 matchea cb_4=[960,1000] (borde inclusivo): mid=980,
        # frac=(980-800)/400=0.45 -> tl = 1 - 0.4*0.45 = 0.82
        self.assertAlmostEqual(snap["teorico"]["pct_btc"], 0.82, places=10)
        self.assertAlmostEqual(snap["drift"]["pct_btc"], -0.02, places=10)
        self.assertEqual(len(snap["escalones"]), 10)

    # ── Gráfico ASCII de inventario ──────────────────────────────────────────
    async def test_inventory_chart_lines(self):
        """El gráfico trae la curva (·), la posición real (●) y el diagnóstico
        correcto según real vs objetivo local."""
        ctrl, mdp = self._make_controller(n_grids=10, a="800", b="1200")
        ctrl.config.target_pct_btc = Decimal("0.60")
        ctrl.config.techo_pct_btc = Decimal("1.0")
        # %BTC = 80000/95000 = 84.2% > objetivo@1000 = 80% -> DESCARGAR
        ctrl.config.base_assigned = Decimal("80")
        ctrl.config.quote_assigned = Decimal("15000")
        self._set_price(mdp, "1000")
        ctrl.executors_info = []
        lines = ctrl._inventory_chart_lines(Decimal("1000"))
        chart = "\n".join(lines)
        print(f"\n{chart}")
        self.assertTrue(lines, "el gráfico no debe ser vacío con target definido")
        self.assertIn("●", chart)
        self.assertIn("·", chart)
        self.assertIn("techo", chart)
        self.assertIn("piso", chart)
        self.assertIn("DESCARGAR", chart)
        # sin target -> sin gráfico
        ctrl.config.target_pct_btc = None
        self.assertEqual(ctrl._inventory_chart_lines(Decimal("1000")), [])
