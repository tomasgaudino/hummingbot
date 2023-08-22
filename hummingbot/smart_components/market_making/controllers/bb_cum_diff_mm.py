import time
from decimal import Decimal

import numpy as np
import pandas_ta as ta  # noqa: F401

from hummingbot.core.data_type.common import TradeType
from hummingbot.smart_components.data_types import ControllerMode, OrderLevel
from hummingbot.smart_components.executors.position_executor.data_types import PositionConfig, TrailingStop
from hummingbot.smart_components.executors.position_executor.position_executor import PositionExecutor
from hummingbot.smart_components.market_making.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)


class BBCumDiffV1Config(MarketMakingControllerConfigBase):
    strategy_name: str = "bb_cum_diff_v1"
    bb_length: int = 100
    bb_std: float = 1.0
    bbp_long_thold: float = 0.6
    bbp_short_thold: float = 0.4
    bbp_cum_diff_long_thold: float = 0.05
    bbp_cum_diff_short_thold: float = - 0.05
    natr_length: int = 14


class BBCumDiffV1(MarketMakingControllerBase):
    """
    Directional Market Making Strategy making use of NATR indicator to make spreads dynamic and shift the mid price.
    """

    def __init__(self, config: BBCumDiffV1Config, mode: ControllerMode = ControllerMode.LIVE):
        super().__init__(config, mode)
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

    def get_candles_with_price_and_spread_multipliers(self):
        """
        Gets the price and spread multiplier from the last candlestick.
        """
        candles_df = self.candles[0].candles_df
        natr = ta.natr(candles_df["high"], candles_df["low"], candles_df["close"], length=self.config.natr_length) / 100
        bbands_output = ta.bbands(candles_df["close"], length=self.config.bb_length, std=self.config.bb_std)
        bbp = bbands_output[f"BBP_{self.config.bb_length}_{self.config.bb_std}"]
        bbands_output['BBP_DIFF'] = bbp.diff()
        bbands_output['BBP_CHANGE'] = np.sign(bbands_output['BBP_DIFF']) != np.sign(bbands_output['BBP_DIFF'].shift())
        bbands_output['BBP_CHANGE_ID'] = bbands_output['BBP_CHANGE'].cumsum()
        bbp_cum_diff = bbands_output.groupby('BBP_CHANGE_ID')['BBP_DIFF'].cumsum()

        def get_bbp_signal(x: float):
            if x is not None:
                if x < self.config.bbp_long_thold:
                    return 1
                elif x > self.config.bbp_short_thold:
                    return -1
                else:
                    return 0
            else:
                return None

        bbp_signal = bbp.apply(get_bbp_signal)

        def get_bbp_cum_diff_signal(x: float):
            if x is not None:
                if x > self.config.bbp_cum_diff_long_thold:
                    return 1
                elif x < self.config.bbp_short_thold:
                    return -1
                else:
                    return 0
            else:
                return None

        bbp_cum_diff_signal = bbp_cum_diff.apply(get_bbp_cum_diff_signal)

        max_price_shift = natr / 2
        price_multiplier = (0.5 * bbp_signal + 0.5 * bbp_cum_diff_signal) * max_price_shift

        candles_df["spread_multiplier"] = natr
        candles_df["price_multiplier"] = price_multiplier
        return candles_df

    def get_position_config(self, order_level: OrderLevel) -> PositionConfig:
        """
        Creates a PositionConfig object from an OrderLevel object.
        Here you can use technical indicators to determine the parameters of the position config.
        """
        close_price = self.get_close_price(self.config.exchange, self.config.trading_pair)
        amount = order_level.order_amount_usd / close_price
        price_multiplier, spread_multiplier = self.get_price_and_spread_multiplier()

        price_adjusted = close_price * (1 + price_multiplier)
        side_multiplier = -1 if order_level.side == TradeType.BUY else 1
        order_price = price_adjusted * (1 + order_level.spread_factor * spread_multiplier * side_multiplier)

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
            trailing_stop=TrailingStop(
                activation_price_delta=order_level.triple_barrier_conf.trailing_stop_activation_price_delta,
                trailing_delta=order_level.triple_barrier_conf.trailing_stop_trailing_delta,
            ),
            leverage=self.config.leverage
        )
        return position_config
