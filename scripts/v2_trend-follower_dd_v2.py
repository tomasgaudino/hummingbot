from decimal import Decimal
from typing import Dict, List, Union, Any

from hummingbot.connector.connector_base import ConnectorBase, TradeType
from hummingbot.core.data_type.common import OrderType, PositionSide, PositionAction
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.controllers.trend_follower_v1 import TrendFollowerV1, TrendFollowerV1Config
from hummingbot.smart_components.strategy_frameworks.data_types import (
    ExecutorHandlerStatus,
    OrderLevel,
    TripleBarrierConf,
)
from hummingbot.smart_components.strategy_frameworks.market_making.market_making_executor_handler import (
    MarketMakingExecutorHandler,
)
from hummingbot.smart_components.utils.order_level_builder import OrderLevelBuilder
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.smart_components.strategy_frameworks.data_types import OrderLevel, TripleBarrierConf


class OrderLevelFixed(OrderLevel):
    level: int
    side: TradeType
    order_amount_usd: Decimal
    spread_factor: Decimal = Decimal("0.0")
    order_refresh_time: int = 60
    cooldown_time: int = 0
    next_triple_barrier_conf: TripleBarrierConf
    prev_triple_barrier_conf: TripleBarrierConf
    triple_barrier_conf: TripleBarrierConf = None


class OrderLevelBuilderFixed(OrderLevelBuilder):
    def __init__(self, n_levels: int):
        super().__init__(n_levels)

    def build_order_levels(self,
                           sides: List[TradeType],
                           amounts: Union[Decimal, List[Decimal], Dict[str, Any]],
                           spreads: Union[Decimal, List[Decimal], Dict[str, Any]],
                           triple_barrier_configs: Union[TripleBarrierConf, List[TripleBarrierConf]]) -> List[OrderLevelFixed]:
        resolved_amounts = self._resolve_input(amounts)
        resolved_spreads = self._resolve_input(spreads)
        next_triple_barrier_configs = triple_barrier_configs["next"]
        prev_triple_barrier_configs = triple_barrier_configs["prev"]
        if not isinstance(next_triple_barrier_configs, list):
            next_triple_barrier_configs = [next_triple_barrier_configs] * self.n_levels

        if not isinstance(prev_triple_barrier_configs, list):
            prev_triple_barrier_configs = [prev_triple_barrier_configs] * self.n_levels

        order_levels = []
        for i in range(self.n_levels):
            for side in sides:
                order_level = OrderLevelFixed(
                    level=i + 1,
                    side=side,
                    order_amount_usd=Decimal(resolved_amounts[i]),
                    spread_factor=Decimal(resolved_spreads[i]),
                    next_triple_barrier_conf=next_triple_barrier_configs[i],  # Replace triple_barrier_conf
                    prev_triple_barrier_conf=prev_triple_barrier_configs[i]  # New input
                )
                order_levels.append(order_level)
        return order_levels


def build_levels_tf(n_levels=8,
                    initial_value=0.3,
                    exp_factor=1.2,
                    take_profit_factor=999,
                    trailing_stop_factor=0.3,
                    stop_loss_factor=0.5,
                    time_limit=60 * 60 * 24 * 1,
                    open_order_type=OrderType.MARKET):
    order_level_builder = OrderLevelBuilderFixed(n_levels=n_levels)
    spreads = order_level_builder._resolve_input(
        {"method": "exponential",
         "params": {"base": exp_factor, "initial_value": initial_value}
         }
    )
    next_triple_barrier_confs = []
    prev_triple_barrier_confs = []

    for i, spread in enumerate(spreads):
        try:
            next_spread_factor = spreads[i + 1]
        except:
            next_spread_factor = spread * exp_factor

        next_gap = next_spread_factor - spread

        next_trailing_stop_activation_price = next_gap
        next_trailing_stop_trailing_delta = next_gap * trailing_stop_factor
        next_stop_loss = next_gap * stop_loss_factor
        next_triple_barrier_config = TripleBarrierConf(
            stop_loss=Decimal(next_stop_loss),
            time_limit=time_limit,
            take_profit=Decimal(take_profit_factor),
            trailing_stop_activation_price_delta=Decimal(next_trailing_stop_activation_price),
            trailing_stop_trailing_delta=Decimal(next_trailing_stop_trailing_delta),
            open_order_type=open_order_type,
        )
        next_triple_barrier_confs.append(next_triple_barrier_config)

        if i > 0:
            prev_spread_factor = spreads[i - 1]
        else:
            prev_spread_factor = spread * 0.5

        prev_gap = spread - prev_spread_factor

        prev_trailing_stop_activation_price = prev_gap
        prev_trailing_stop_trailing_delta = prev_gap * trailing_stop_factor
        prev_stop_loss = prev_gap * stop_loss_factor
        prev_triple_barrier_config = TripleBarrierConf(
            stop_loss=Decimal(prev_stop_loss),
            time_limit=time_limit,
            take_profit=Decimal(take_profit_factor),
            trailing_stop_activation_price_delta=Decimal(prev_trailing_stop_activation_price),
            trailing_stop_trailing_delta=Decimal(prev_trailing_stop_trailing_delta),
            open_order_type=open_order_type,
        )
        prev_triple_barrier_confs.append(prev_triple_barrier_config)

    triple_barrier_confs = {
        "next": next_triple_barrier_confs,
        "prev": prev_triple_barrier_confs

    }

    levels = order_level_builder.build_order_levels(sides=[TradeType.BUY, TradeType.SELL], amounts=Decimal("10"),
                                                    spreads=spreads, triple_barrier_configs=triple_barrier_confs)
    return levels


class TrendFollowerV1MultiplePairs(ScriptStrategyBase):
    # trading_pairs = ["BNX-USDT", "BNT-USDT", "IOTA-USDT", "WLD-USDT"]
    trading_pairs = ["TRB-USDT"]

    exchange = "binance_perpetual"

    leverage_by_trading_pair = {
        "HBAR-USDT": 25,
        "CYBER-USDT": 20,
        "ETH-USDT": 100,
        "LPT-USDT": 10,
        "UNFI-USDT": 20,
        "BAKE-USDT": 20,
        "YGG-USDT": 20,
        "SUI-USDT": 50,
        "TOMO-USDT": 25,
        "RUNE-USDT": 25,
        "STX-USDT": 25,
        "API3-USDT": 20,
        "LIT-USDT": 20,
        "PERP-USDT": 16,
        "HOOK-USDT": 20,
        "AMB-USDT": 20,
        "ARKM-USDT": 20,
        "TRB-USDT": 10,
        "OMG-USDT": 25,
        "WLD-USDT": 50,
        "PEOPLE-USDT": 25,
        "AGLD-USDT": 20,
        "BAT-USDT": 20,
        "AVAX-USDT": 50,
        "JOE-USDT": 20,
        "BNX-USDT": 20,
        "COTI-USDT": 25,
        "JASMY-USDT": 20,
        "LOOM-USDT": 20,
        "IOTA-USDT": 25,
        "BNT-USDT": 20,
    }

    order_levels_tf = build_levels_tf(n_levels=11,
                                      initial_value=0.3,
                                      exp_factor=1.2,
                                      trailing_stop_factor=0.3,
                                      stop_loss_factor=0.5,
                                      time_limit=60 * 60 * 24 * 1,
                                      open_order_type=OrderType.MARKET)

    controllers = {}
    markets = {}
    executor_handlers = {}

    for trading_pair in trading_pairs:
        config = TrendFollowerV1Config(
            exchange=exchange,
            trading_pair=trading_pair,
            order_levels=order_levels_tf,
            candles_config=[
                CandlesConfig(connector=exchange, trading_pair=trading_pair, interval="1h", max_records=300),
            ],
            bb_length=200,
            bb_std=2.0,
            side_filter=True,
            smart_activation=True,
            dynamic_target_spread=True,
            activation_threshold=Decimal("0.01"),
            leverage=leverage_by_trading_pair.get(trading_pair, 1),
        )
        controller = TrendFollowerV1(config=config)
        markets = controller.update_strategy_markets_dict(markets)
        controllers[trading_pair] = controller

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        for trading_pair, controller in self.controllers.items():
            self.executor_handlers[trading_pair] = MarketMakingExecutorHandler(strategy=self, controller=controller)

    @property
    def is_perpetual(self):
        """
        Checks if the exchange is a perpetual market.
        """
        return "perpetual" in self.exchange

    def on_stop(self):
        if self.is_perpetual:
            self.close_open_positions()
        for executor_handler in self.executor_handlers.values():
            executor_handler.stop()

    def close_open_positions(self):
        # we are going to close all the open positions when the bot stops
        for connector_name, connector in self.connectors.items():
            for trading_pair, position in connector.account_positions.items():
                if trading_pair in self.trading_pairs:
                    if position.position_side == PositionSide.LONG:
                        self.sell(connector_name=connector_name,
                                  trading_pair=position.trading_pair,
                                  amount=abs(position.amount),
                                  order_type=OrderType.MARKET,
                                  price=connector.get_mid_price(position.trading_pair),
                                  position_action=PositionAction.CLOSE)
                    elif position.position_side == PositionSide.SHORT:
                        self.buy(connector_name=connector_name,
                                 trading_pair=position.trading_pair,
                                 amount=abs(position.amount),
                                 order_type=OrderType.MARKET,
                                 price=connector.get_mid_price(position.trading_pair),
                                 position_action=PositionAction.CLOSE)

    def on_tick(self):
        """
        This shows you how you can start meta controllers. You can run more than one at the same time and based on the
        market conditions, you can orchestrate from this script when to stop or start them.
        """
        for executor_handler in self.executor_handlers.values():
            if executor_handler.status == ExecutorHandlerStatus.NOT_STARTED:
                executor_handler.start()

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []
        for trading_pair, executor_handler in self.executor_handlers.items():
            lines.extend(
                [f"Strategy: {executor_handler.controller.config.strategy_name} | Trading Pair: {trading_pair}",
                 executor_handler.to_format_status()])
            lines.extend([""])
            lines.extend(
                [f"Level {order_level}: Side: {prices['side']} - Close: {prices['close_price']:.3f} - Lower: {prices['lower_limit']:.3f} - Order: {prices['order_price']:.3f} - Upper: {prices['upper_limit']:.3f}" for order_level, prices in executor_handler.controller.target_prices.items()]
            )
        return "\n".join(lines)
