from decimal import Decimal
import pandas as pd
from typing import Dict

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionSide, TradeType
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.executors.position_executor.data_types import TrailingStop
from hummingbot.smart_components.controllers.price_follower_v2 import PriceFollowerV2, PriceFollowerV2Config
from hummingbot.smart_components.strategy_frameworks.data_types import ExecutorHandlerStatus
from hummingbot.smart_components.utils.distributions import Distributions
from hummingbot.smart_components.strategy_frameworks.market_making.market_making_executor_handler import MarketMakingExecutorHandler
from hummingbot.smart_components.utils.order_level_builder import OrderLevelBuilder
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.smart_components.strategy_frameworks.data_types import TripleBarrierConf
from hummingbot.client.ui.interface_utils import format_df_for_printout


class PriceFollowerV2MultiplePairs(ScriptStrategyBase):
    debug_mode = False

    # Account configuration
    exchange = "binance_perpetual"
    trading_pairs = ["DYDX-USDT"]
    leverage = 20

    # Candles configuration
    candles_exchange = "binance_perpetual"
    candles_interval = "1h"
    candles_max_records = 300
    bollinger_band_length = 200
    bollinger_band_std = 2.0

    # Orders configuration
    order_amount = Decimal("20")
    n_levels = 20
    step_between_orders = 0.03
    start_spread = step_between_orders / 2
    order_refresh_time = 60 * 60 * 3  # 3 hours
    cooldown_time = 5

    # Set up triple barrier confs. Should be coefficients that will be multiplied by the spread multiplier to get the target prices
    stop_loss = Decimal("0.2")
    take_profit = Decimal("0.06")
    time_limit = 60 * 60 * 24 * 1
    trailing_stop_activation_price_delta = Decimal("0.01")
    trailing_stop_trailing_delta = Decimal("0.002")

    # Global Trailing Stop configuration
    global_trailing_stop_activation_price_delta = Decimal(str(step_between_orders))
    global_trailing_stop_trailing_delta = Decimal(str(step_between_orders / 4))

    # Advanced configurations
    side_filter = False
    dynamic_spread_factor = False
    dynamic_target_spread = False
    smart_activation = True
    activation_threshold = Decimal("0.01")
    price_band = False
    price_band_long_filter = Decimal("0.8")
    price_band_short_filter = Decimal("0.8")

    # Applying the configuration
    order_level_builder = OrderLevelBuilder(n_levels=n_levels)
    order_levels = order_level_builder.build_order_levels(
        amounts=order_amount,
        spreads=Distributions.arithmetic(n_levels=n_levels, start=start_spread, step=step_between_orders),
        triple_barrier_confs=TripleBarrierConf(
            stop_loss=stop_loss, take_profit=take_profit, time_limit=time_limit,
            trailing_stop_activation_price_delta=trailing_stop_activation_price_delta,
            trailing_stop_trailing_delta=trailing_stop_trailing_delta),
        order_refresh_time=order_refresh_time,
        cooldown_time=cooldown_time,
    )
    controllers = {}
    markets = {}
    executor_handlers = {}

    for trading_pair in trading_pairs:
        config = PriceFollowerV2Config(
            exchange=exchange,
            trading_pair=trading_pair,
            order_levels=order_levels,
            candles_config=[
                CandlesConfig(connector=candles_exchange, trading_pair=trading_pair,
                              interval=candles_interval, max_records=candles_max_records),
            ],
            bb_length=bollinger_band_length,
            bb_std=bollinger_band_std,
            price_band=price_band,
            price_band_long_filter=price_band_long_filter,
            price_band_short_filter=price_band_short_filter,
            dynamic_spread_factor=dynamic_spread_factor,
            dynamic_target_spread=dynamic_target_spread,
            smart_activation=smart_activation,
            activation_threshold=activation_threshold,
            leverage=leverage,
            global_trailing_stop_config={
                TradeType.BUY: TrailingStop(activation_price_delta=global_trailing_stop_activation_price_delta,
                                            trailing_delta=global_trailing_stop_trailing_delta),
                TradeType.SELL: TrailingStop(activation_price_delta=global_trailing_stop_activation_price_delta,
                                             trailing_delta=global_trailing_stop_trailing_delta),
            }
        )
        controller = PriceFollowerV2(config=config)
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
        """
        This is a method that will be called by the UI to show the status of the strategy.

        Shows a table with the target prices for each level and the estimated stop loss and trailing stop prices.

        Every table has the following metrics:
        - Side: Fixed side of the order
        - Status: Status of the order. Can be Pending or Active
        - Close Price: Current close price
        - Upper Limit: Upper limit of the order
        - Order Price: Order price
        - Lower Limit: Lower limit of the order
        - Stop Loss: Estimated stop loss price
        - Trailing Stop Activation: Estimated trailing stop activation price
        - Trailing Stop Delta: Estimated trailing stop delta

        As the strategy uses market orders, the orders will be activated when the close price is between the lower and
        upper limits. Once they are active, the status will change to Active and the other metrics will be freezed.

        """
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []
        for trading_pair, executor_handler in self.executor_handlers.items():
            lines.extend([""])
            if executor_handler.controller.target_prices:
                lines.extend(
                    [f"Strategy: {executor_handler.controller.config.strategy_name} | Trading Pair: {trading_pair} | Step Between Levels: {executor_handler.controller.config.step_between_orders:.3%}",
                     ""])

                closed_executors_info = executor_handler.closed_executors_info()
                active_executors_info = executor_handler.active_executors_info()
                unrealized_pnl = float(active_executors_info["net_pnl"])
                realized_pnl = closed_executors_info["net_pnl"]
                total_pnl = unrealized_pnl + realized_pnl
                total_volume = closed_executors_info["total_volume"] + float(active_executors_info["total_volume"])
                total_long = closed_executors_info["total_long"] + float(active_executors_info["total_long"])
                total_short = closed_executors_info["total_short"] + float(active_executors_info["total_short"])
                accuracy_long = closed_executors_info["accuracy_long"]
                accuracy_short = closed_executors_info["accuracy_short"]
                total_accuracy = (accuracy_long * total_long + accuracy_short * total_short) \
                                 / (total_long + total_short) if (total_long + total_short) > 0 else 0
                lines.extend([f"Unrealized PNL: {unrealized_pnl * 100:.2f} % | Realized PNL: {realized_pnl * 100:.2f} % | Total PNL: {total_pnl * 100:.2f} % | Total Volume: {total_volume} | Total positions: {total_short + total_long} --> Accuracy: {total_accuracy:.2%} ",
                              "",
                              f"Long: {total_long} --> Accuracy: {accuracy_long:.2%} | Short: {total_short} --> Accuracy: {accuracy_short:.2%}"])
                df = pd.DataFrame(executor_handler.controller.target_prices).T
                df["level"] = df.index
                df.insert(0, "level", df.pop("level"))
                df.drop(columns=["side"], inplace=True)
                levels_str = format_df_for_printout(df.sort_values(by=["order_price"], ascending=False), table_format="psql")
                lines.extend([f"{levels_str}"])
                lines.extend([""])
        return "\n".join(lines)
