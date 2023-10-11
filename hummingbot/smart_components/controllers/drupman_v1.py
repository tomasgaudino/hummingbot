import time
from decimal import Decimal

import pandas_ta as ta  # noqa: F401

from hummingbot.core.data_type.common import TradeType
from hummingbot.smart_components.executors.position_executor.data_types import PositionConfig, TrailingStop
from hummingbot.smart_components.executors.position_executor.position_executor import PositionExecutor
from hummingbot.smart_components.strategy_frameworks.data_types import OrderLevel
from hummingbot.smart_components.strategy_frameworks.market_making.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)


class DrupmanV1Config(MarketMakingControllerConfigBase):
    strategy_name: str = "drupman_v1"
    macd_fast: int = 12
    macd_slow: float = 26
    macd_signal: int = 9
    macd_thold: float = 0.01
    macdh_thold: float = 0.0


class DrupmanV1(MarketMakingControllerBase):
    """
    Directional Trend Follower Strategy making profitable grids based on fixed percentages.
    """

    def __init__(self, config: DrupmanV1Config):
        super().__init__(config)
        self.config = config

    def refresh_order_condition(self, executor: PositionExecutor, order_level: OrderLevel) -> bool:
        """
        Checks if the order needs to be refreshed.
        You can reimplement this method to add more conditions.
        """
        if executor.position_config.timestamp + order_level.order_refresh_time > time.time():
            return False
        return True

    def early_stop_condition(self, executor: PositionExecutor, order_level: OrderLevel) -> bool:
        """
        If an executor has an active position, should we close it based on a condition.
        """
        return False

    def cooldown_condition(self, executor: PositionExecutor, order_level: OrderLevel) -> bool:
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
        candles_df.ta.macd(fast=self.config.macd_fast, slow=self.config.macd_slow, signal=self.config.macd_signal, append=True)
        return candles_df

    def get_position_config(self, order_level: OrderLevel) -> PositionConfig:
        """
        Creates a PositionConfig object from an OrderLevel object.
        Here you can use technical indicators to determine the parameters of the position config.
        """
        close_price = self.get_close_price(self.config.exchange, self.config.trading_pair)
        amount = order_level.order_amount_usd / close_price
        side_multiplier = 1 if order_level.side == TradeType.BUY else -1
        macd, macd_hist = self.get_macd_and_histogram()
        long_signal = macd_hist > self.config.macdh_thold and macd < - self.config.macd_thold and side_multiplier == 1
        short_signal = macd_hist < self.config.macdh_thold and macd > self.config.macd_thold and side_multiplier == -1
        order_price = close_price * (1 + order_level.spread_factor * side_multiplier)
        if (long_signal and close_price >= order_price) or (short_signal and close_price <= order_price):
            if order_level.triple_barrier_conf.trailing_stop_trailing_delta and order_level.triple_barrier_conf.trailing_stop_trailing_delta:
                trailing_stop = TrailingStop(
                    activation_price_delta=order_level.triple_barrier_conf.trailing_stop_activation_price_delta,
                    trailing_delta=order_level.triple_barrier_conf.trailing_stop_trailing_delta,
                )
            else:
                trailing_stop = None
            position_config = PositionConfig(
                timestamp=time.time(),
                trading_pair=self.config.trading_pair,
                exchange=self.config.exchange,
                side=order_level.side,
                amount=amount,
                take_profit=order_level.triple_barrier_conf.take_profit,
                stop_loss=order_level.triple_barrier_conf.stop_loss,
                time_limit=order_level.triple_barrier_conf.time_limit,
                entry_price=Decimal(order_price),
                open_order_type=order_level.triple_barrier_conf.open_order_type,
                take_profit_order_type=order_level.triple_barrier_conf.take_profit_order_type,
                trailing_stop=trailing_stop,
                leverage=self.config.leverage
            )
            return position_config

    def get_macd_and_histogram(self):
        candles_df = self.get_processed_data()
        macd = candles_df[f"MACD_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"].iloc[-1]
        macd_hist = candles_df[f"MACDh_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"].iloc[-1]
        return Decimal(macd), Decimal(macd_hist)
