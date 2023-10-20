import math
import time
from decimal import Decimal

import pandas_ta as ta  # noqa: F401

from hummingbot.core.data_type.common import TradeType
from hummingbot.smart_components.executors.position_executor.data_types import PositionConfig, TrailingStop
from hummingbot.smart_components.executors.position_executor.position_executor import PositionExecutor
from hummingbot.smart_components.strategy_frameworks.data_types import OrderLevel, TripleBarrierConf
from hummingbot.smart_components.strategy_frameworks.market_making.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)


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



class TrendFollowerV1Config(MarketMakingControllerConfigBase):
    strategy_name: str = "trend_follower_v1"
    bb_length: int = 100
    bb_std: float = 2.0
    side_filter: bool = False
    smart_activation: bool = False
    activation_threshold: Decimal = Decimal("0.001")
    dynamic_target_spread: bool = False


class TrendFollowerV1(MarketMakingControllerBase):
    def __init__(self, config: TrendFollowerV1Config):
        super().__init__(config)
        self.target_prices = {}
        self.config = config

    @property
    def order_levels_targets(self):
        return self.target_prices

    def refresh_order_condition(self, executor: PositionExecutor, order_level: OrderLevelFixed) -> bool:
        """
        Checks if the order needs to be refreshed.
        You can reimplement this method to add more conditions.
        """
        if executor.position_config.timestamp + order_level.order_refresh_time > time.time():
            return False
        return True

    def early_stop_condition(self, executor: PositionExecutor, order_level: OrderLevelFixed) -> bool:
        """
        If an executor has an active position, should we close it based on a condition.
        """
        return False

    def cooldown_condition(self, executor: PositionExecutor, order_level: OrderLevelFixed) -> bool:
        """
        After finishing an order, the executor will be in cooldown for a certain amount of time.
        This prevents the executor from creating a new order immediately after finishing one and execute a lot
        of orders in a short period of time from the same side.
        """
        if executor.close_timestamp and executor.close_timestamp + order_level.cooldown_time > time.time():
            return True
        return False

    def get_processed_data(self):
        """
        Gets the price and spread multiplier from the last candlestick.
        """
        candles_df = self.candles[0].candles_df
        bbp = ta.bbands(candles_df["close"], length=self.config.bb_length, std=self.config.bb_std)

        candles_df["price_multiplier"] = bbp[f"BBM_{self.config.bb_length}_{self.config.bb_std}"]
        candles_df["spread_multiplier"] = bbp[f"BBB_{self.config.bb_length}_{self.config.bb_std}"] / 200
        return candles_df

    def get_position_config(self, order_level: OrderLevelFixed) -> PositionConfig:
        """
        Creates a PositionConfig object from an OrderLevel object.
        Here you can use technical indicators to determine the parameters of the position config.
        """
        close_price = self.get_close_price(self.config.exchange, self.config.trading_pair)
        amount = order_level.order_amount_usd / close_price
        bollinger_mid_price, spread_multiplier = self.get_price_and_spread_multiplier()
        # This side multiplier is only to get the correct bollingrid side
        side_multiplier = 1 if order_level.side == TradeType.BUY else -1
        order_price = bollinger_mid_price * (1 + order_level.spread_factor * spread_multiplier * side_multiplier)
        tolerance = self.config.activation_threshold * spread_multiplier
        order_upper_limit = order_price * (1 + tolerance)
        order_lower_limit = order_price * (1 - tolerance)
        # This side will replace the original order level side if the order is placed from the opposite side
        if close_price < order_price * (1 + tolerance):
            fixed_side = TradeType.BUY
        elif close_price > order_price * (1 - tolerance):
            fixed_side = TradeType.SELL
        else:
            fixed_side = order_level.side

        # Avoid placing the order from the opposite side
        side_filter_condition = self.config.side_filter and (
            (close_price < bollinger_mid_price and order_level.side == TradeType.BUY) or
            (close_price > bollinger_mid_price and order_level.side == TradeType.SELL))
        if side_filter_condition:
            return

        # Update target prices for format status
        self.target_prices[f"{order_level.level}"] = {"side": fixed_side,
                                                      "close_price": close_price,
                                                      "upper_limit": order_upper_limit,
                                                      "order_price": order_price,
                                                      "lower_limit": order_lower_limit}

        # Smart activation of orders
        smart_activation_condition = self.config.smart_activation and math.isclose(close_price, order_price,
                                                                                   rel_tol=tolerance)
        if not smart_activation_condition:
            return

        # Dynamic trailing stop
        target_spread = spread_multiplier if self.config.dynamic_target_spread else 1

        if fixed_side == order_level.side:
            if order_level.next_triple_barrier_conf.trailing_stop_trailing_delta and order_level.next_triple_barrier_conf.trailing_stop_trailing_delta:
                trailing_stop = TrailingStop(
                    activation_price_delta=order_level.next_triple_barrier_conf.trailing_stop_activation_price_delta * target_spread,
                    trailing_delta=order_level.next_triple_barrier_conf.trailing_stop_trailing_delta * target_spread,
                )
            else:
                trailing_stop = None
            position_config = PositionConfig(
                timestamp=time.time(),
                trading_pair=self.config.trading_pair,
                exchange=self.config.exchange,
                side=fixed_side,
                amount=amount,
                take_profit=order_level.next_triple_barrier_conf.take_profit * target_spread,
                stop_loss=order_level.next_triple_barrier_conf.stop_loss * target_spread,
                time_limit=order_level.next_triple_barrier_conf.time_limit * target_spread,
                entry_price=Decimal(order_price),
                open_order_type=order_level.next_triple_barrier_conf.open_order_type,
                take_profit_order_type=order_level.next_triple_barrier_conf.take_profit_order_type,
                trailing_stop=trailing_stop,
                leverage=self.config.leverage
            )
        else:
            if order_level.prev_triple_barrier_conf.trailing_stop_trailing_delta and order_level.prev_triple_barrier_conf.trailing_stop_trailing_delta:
                trailing_stop = TrailingStop(
                    activation_price_delta=order_level.prev_triple_barrier_conf.trailing_stop_activation_price_delta * target_spread,
                    trailing_delta=order_level.prev_triple_barrier_conf.trailing_stop_trailing_delta * target_spread,
                )
            else:
                trailing_stop = None
            position_config = PositionConfig(
                timestamp=time.time(),
                trading_pair=self.config.trading_pair,
                exchange=self.config.exchange,
                side=fixed_side,
                amount=amount,
                take_profit=order_level.prev_triple_barrier_conf.take_profit * target_spread,
                stop_loss=order_level.prev_triple_barrier_conf.stop_loss * target_spread,
                time_limit=order_level.prev_triple_barrier_conf.time_limit * target_spread,
                entry_price=Decimal(order_price),
                open_order_type=order_level.prev_triple_barrier_conf.open_order_type,
                take_profit_order_type=order_level.prev_triple_barrier_conf.take_profit_order_type,
                trailing_stop=trailing_stop,
                leverage=self.config.leverage
            )
        return position_config
