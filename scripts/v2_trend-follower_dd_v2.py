from decimal import Decimal
from typing import Dict

from hummingbot.connector.connector_base import ConnectorBase, TradeType
from hummingbot.core.data_type.common import OrderType, PositionSide, PositionAction
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.controllers.dman_v3 import DManV3, DManV3Config
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


def build_levels_tf(n_levels=8,
                    initial_value=0.3,
                    exp_factor=1.2,
                    take_profit_factor=999,
                    trailing_stop_factor=0.3,
                    stop_loss_factor=0.5,
                    time_limit=60 * 60 * 24 * 1,
                    open_order_type=OrderType.MARKET):
    order_level_builder = OrderLevelBuilder(n_levels=n_levels)
    spreads = order_level_builder._resolve_input(
        {"method": "exponential",
         "params": {"base": exp_factor, "initial_value": initial_value}
         }
    )
    triple_barrier_confs = []
    for i, spread in enumerate(spreads):
        try:
            next_spread_factor = spreads[i + 1]
        except:
            next_spread_factor = spread * exp_factor

        trailing_stop_activation_price = next_spread_factor - spread
        trailing_stop_trailing_delta = (next_spread_factor - spread) * trailing_stop_factor
        stop_loss = (next_spread_factor - spread) * stop_loss_factor
        triple_barrier_config = TripleBarrierConf(
            stop_loss=Decimal(stop_loss),
            time_limit=time_limit,
            take_profit=Decimal(take_profit_factor),
            trailing_stop_activation_price_delta=Decimal(trailing_stop_activation_price),
            trailing_stop_trailing_delta=Decimal(trailing_stop_trailing_delta),
            open_order_type=open_order_type,
        )
        triple_barrier_confs.append(triple_barrier_config)
    levels = order_level_builder.build_order_levels(sides=[TradeType.BUY, TradeType.SELL], amounts=Decimal("10"),
                                                    spreads=spreads, triple_barrier_confs=triple_barrier_confs)
    return levels


class TrendFollowerV1MultiplePairs(ScriptStrategyBase):
    trading_pairs = ["BNX-USDT", "BNT-USDT", "IOTA-USDT", "WLD-USDT"]
    # trading_pairs = ["BNX-USDT"]

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

    order_levels_tf = build_levels_tf(n_levels=8,
                                      initial_value=0.3,
                                      exp_factor=1.25,
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
            bb_std=3.0,
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
        return "\n".join(lines)
