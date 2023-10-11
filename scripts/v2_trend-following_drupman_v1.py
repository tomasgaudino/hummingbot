from decimal import Decimal
from typing import Dict

from hummingbot.connector.connector_base import ConnectorBase, TradeType
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionSide
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.controllers.drupman_v1 import DrupmanV1, DrupmanV1Config
from hummingbot.smart_components.strategy_frameworks.data_types import (
    ExecutorHandlerStatus,
    OrderLevel,
    TripleBarrierConf,
)
from hummingbot.smart_components.strategy_frameworks.market_making.market_making_executor_handler import (
    MarketMakingExecutorHandler,
)
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


def get_order_levels(trading_pairs: list[str],
                     n_levels: int,
                     max_price_pct_expectation: float,
                     sl_spread_pct: float,
                     tsl_spread_pct: float):
    order_levels = {trading_pair:
                        [
                            OrderLevel(level=i,
                                       side=TradeType.BUY,
                                       order_amount_usd=Decimal("5"),
                                       spread_factor=i / n_levels * max_price_pct_expectation,
                                       order_refresh_time=60 * 60,
                                       cooldown_time=15,
                                       triple_barrier_conf=TripleBarrierConf(
                                           stop_loss=sl_spread_pct,
                                           take_profit=Decimal("99"),
                                           time_limit=60 * 60 * 12,
                                           take_profit_order_type=OrderType.MARKET,
                                           trailing_stop_activation_price_delta=max_price_pct_expectation / n_levels,
                                           trailing_stop_trailing_delta=tsl_spread_pct * max_price_pct_expectation / n_levels,
                                           open_order_type=OrderType.MARKET,
                                       )
                                       )
                            for i in range(1, n_levels + 1)
                        ]
                        + [
                            OrderLevel(level=i,
                                       side=TradeType.SELL,
                                       order_amount_usd=Decimal("5"),
                                       spread_factor=i / n_levels * max_price_pct_expectation,
                                       order_refresh_time=60 * 60,
                                       cooldown_time=15,
                                       triple_barrier_conf=TripleBarrierConf(
                                           stop_loss=sl_spread_pct,
                                           take_profit=Decimal("99"),
                                           time_limit=60 * 60 * 12,
                                           take_profit_order_type=OrderType.MARKET,
                                           open_order_type=OrderType.MARKET,
                                           trailing_stop_activation_price_delta=n_levels * max_price_pct_expectation,
                                           trailing_stop_trailing_delta=tsl_spread_pct,
                                       )
                                       )
                            for i in range(1, n_levels + 1)
                        ]
                        for trading_pair in trading_pairs
    }
    return order_levels


class TrendGridMultiplePairs(ScriptStrategyBase):
    trading_pairs = ["TRB-USDT", "BCH-USDT", "LINK-USDT", "WLD-USDT", "HBAR-USDT"]
    exchange = "binance_perpetual"
    max_price_pct_expectation = 0.05
    n_levels = 5
    # sl needs to be greater than tsl since the tsl activation is at same price of next level start
    sl_spread_pct = 0.5
    tsl_spread_pct = 0.3

    # This is only for the perpetual markets
    leverage_by_trading_pair = {
        "HBAR-USDT": 5,
        "BCH-USDT": 5,
        # "CYBER-USDT": 20,
        # "ETH-USDT": 100,
        # "LPT-USDT": 10,
        # "UNFI-USDT": 20,
        # "BAKE-USDT": 20,
        # "YGG-USDT": 20,
        # "SUI-USDT": 50,
        # "TOMO-USDT": 25,
        # "RUNE-USDT": 25,
        # "STX-USDT": 25,
        # "API3-USDT": 20,
        # "LIT-USDT": 20,
        # "PERP-USDT": 16,
        # "HOOK-USDT": 20,
        # "AMB-USDT": 20,
        # "ARKM-USDT": 20,
        "TRB-USDT": 5,
        # "OMG-USDT": 25,
        "WLD-USDT": 5,
        # "PEOPLE-USDT": 25,
        # "AGLD-USDT": 20,
        # "BAT-USDT": 20,
        "LINK-USDT": 5,
    }

    order_levels_by_trading_pair = get_order_levels(trading_pairs=trading_pairs,
                                                    n_levels=n_levels,
                                                    max_price_pct_expectation=max_price_pct_expectation,
                                                    sl_spread_pct=sl_spread_pct,
                                                    tsl_spread_pct=tsl_spread_pct)
    controllers = {}
    markets = {}
    executor_handlers = {}

    for trading_pair in trading_pairs:
        config = DrupmanV1Config(
            exchange=exchange,
            trading_pair=trading_pair,
            order_levels=order_levels_by_trading_pair[trading_pair],
            candles_config=[
                CandlesConfig(connector=exchange, trading_pair=trading_pair, interval="15m", max_records=300),
            ],
            bb_length=100,
            bb_std=2.0,
            leverage=leverage_by_trading_pair.get(trading_pair, 1),
        )
        controller = DrupmanV1(config=config)
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
            if self.tsl_spread_pct >= self.sl_spread_pct:
                self.logger().error("TSL spread needs to be lower than SL spread")
            elif executor_handler.status == ExecutorHandlerStatus.NOT_STARTED:
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
