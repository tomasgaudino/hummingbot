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


class PriceFollowerV2Config(MarketMakingControllerConfigBase):
    strategy_name: str = "price_follower_v2"
    bb_length: int = 100
    bb_std: float = 2.0
    smart_activation: bool = False
    debug_mode: bool = False
    activation_threshold: Decimal = Decimal("0.001")
    price_band: bool = False
    price_band_long_filter: Decimal = Decimal("0.8")
    price_band_short_filter: Decimal = Decimal("0.8")
    dynamic_target_spread: bool = False
    dynamic_spread_factor: bool = True
    step_between_orders: Decimal = Decimal("0.01")


class PriceFollowerV2(MarketMakingControllerBase):
    """
    Directional Market Making Strategy making use of NATR indicator to make spreads dynamic and shift the mid price.
    """

    def __init__(self, config: PriceFollowerV2Config):
        super().__init__(config)
        self.config = config
        self.target_prices = {}

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
        bbp = ta.bbands(candles_df["close"], length=self.config.bb_length, std=self.config.bb_std)

        candles_df["price_multiplier"] = bbp[f"BBM_{self.config.bb_length}_{self.config.bb_std}"]
        candles_df["spread_multiplier"] = bbp[f"BBB_{self.config.bb_length}_{self.config.bb_std}"] / 200
        return candles_df

    def get_position_config(self, order_level: OrderLevel) -> PositionConfig:
        """
        Creates a PositionConfig object from an OrderLevel object.
        Here you can use technical indicators to determine the parameters of the position config.
        """
        # Get the close price of the trading pair
        close_price = self.get_close_price(self.config.exchange, self.config.trading_pair)

        # Get bollinger mid price and spread multiplier
        bollinger_mid_price, spread_multiplier = self.get_price_and_spread_multiplier()

        # Use this to use percentage of price instead of bollinger spread
        if not self.config.dynamic_spread_factor:
            spread_multiplier = 1

        # This side multiplier is only to get the correct bollingrid side
        bollinger_side_multiplier = 1 if order_level.side == TradeType.BUY else -1
        side_name = "UPPER" if order_level.side == TradeType.BUY else "LOWER"

        # Calculate order price
        order_price = bollinger_mid_price * (1 + order_level.spread_factor * spread_multiplier * bollinger_side_multiplier)

        # Get base amount from order level
        amount = order_level.order_amount_usd / order_price

        # Calculate gap tolerance for the order (because we're using market orders)
        tolerance = self.config.activation_threshold * self.config.step_between_orders
        order_upper_limit = order_price * (1 + tolerance)
        order_lower_limit = order_price * (1 - tolerance)

        # This side will replace the original order level side if the order is placed from the opposite side
        fixed_side = TradeType.BUY if close_price < order_price else TradeType.SELL
        fixed_side_multiplier = 1 if fixed_side == TradeType.BUY else -1

        # Get triple barrier pcts
        stop_loss_pct = order_level.triple_barrier_conf.stop_loss
        take_profit_pct = order_level.triple_barrier_conf.take_profit
        trailing_stop_activation_pct = order_level.triple_barrier_conf.trailing_stop_activation_price_delta
        trailing_stop_trailing_pct = order_level.triple_barrier_conf.trailing_stop_trailing_delta

        # Calculate target prices according to the fixed side
        take_profit_price = order_price * (1 + take_profit_pct * fixed_side_multiplier)
        stop_loss_price = order_price * (1 - stop_loss_pct * fixed_side_multiplier)
        trailing_stop_activation_price = order_price * (1 + trailing_stop_activation_pct * fixed_side_multiplier)
        trailing_stop_trailing_price = order_price * (1 + (trailing_stop_activation_pct - trailing_stop_trailing_pct)
                                                      * fixed_side_multiplier)

        # Update target prices for format status
        self.target_prices[f"{order_level.level}_{side_name}_{fixed_side.name}"] = {
            "side": fixed_side.name,
            "status": "Waiting",
            "close_price": close_price,
            "order_price": order_price,
            "lower_limit": order_lower_limit,
            "upper_limit": order_upper_limit,
          }

        # Avoid placing orders according to certain bounds of the bollinger band
        if self.config.price_band:
            max_buy_price = bollinger_mid_price * (1 + self.config.price_band_long_filter * spread_multiplier)
            min_sell_price = bollinger_mid_price * (1 - self.config.price_band_short_filter * spread_multiplier)
            price_band_condition = ((order_price > max_buy_price and order_level.side == TradeType.BUY)
                                    or (order_price < min_sell_price and order_level.side == TradeType.SELL))
            if price_band_condition:
                return

        # Smart activation of orders
        smart_activation_condition = self.config.smart_activation and (
                fixed_side == TradeType.BUY and order_lower_limit <= close_price <= order_price
        ) or (
                fixed_side == TradeType.SELL and order_upper_limit >= close_price >= order_price
        )
        if not smart_activation_condition:
            return

        # This option is set to avoid placing orders during debugging
        if self.config.debug_mode:
            return

        # Update triple barrier conf after opening or reinforcing position
        # TODO: check why fixed_side creates a problem here
        self.target_prices[f"{order_level.level}_{side_name}_{fixed_side.name}"] = {"status": "Active"}

        if order_level.triple_barrier_conf.trailing_stop_trailing_delta and order_level.triple_barrier_conf.trailing_stop_trailing_delta:
            trailing_stop = TrailingStop(
                activation_price_delta=trailing_stop_activation_pct,
                trailing_delta=trailing_stop_trailing_pct,
            )
        else:
            trailing_stop = None

        position_config = PositionConfig(
            timestamp=time.time(),
            trading_pair=self.config.trading_pair,
            exchange=self.config.exchange,
            side=fixed_side,
            amount=amount,
            take_profit=take_profit_pct,
            stop_loss=stop_loss_pct,
            time_limit=order_level.triple_barrier_conf.time_limit,
            entry_price=Decimal(order_price),
            open_order_type=order_level.triple_barrier_conf.open_order_type,
            take_profit_order_type=order_level.triple_barrier_conf.take_profit_order_type,
            trailing_stop=trailing_stop,
            leverage=self.config.leverage
        )
        return position_config
