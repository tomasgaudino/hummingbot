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


class TrendFollowerV1Config(MarketMakingControllerConfigBase):
    strategy_name: str = "trend_follower_v1"
    bb_length: int = 100
    bb_std: float = 2.0
    side_filter: bool = False
    smart_activation: bool = False
    activation_threshold: Decimal = Decimal("0.001")
    dynamic_target_spread: bool = False
    intra_spread_pct: float = 0.005


class TrendFollowerV1(MarketMakingControllerBase):
    def __init__(self, config: TrendFollowerV1Config):
        super().__init__(config)
        self.target_prices = {}
        self.config = config
        self.price_pct_between_levels: float = 0.0
        self.take_profit_pct = None
        self.stop_loss_pct: float = None
        self.trailing_stop_activation_pct: float = None
        self.trailing_stop_trailing_pct: float = None

    @property
    def order_levels_targets(self):
        return self.target_prices

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
        # TODO: Think about this
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

        # Bollinger mid-price. Used as reference starting point for target prices
        candles_df["price_multiplier"] = bbp[f"BBM_{self.config.bb_length}_{self.config.bb_std}"]

        # Used to get the price percentage over the bollinger mid-price
        candles_df["spread_multiplier"] = bbp[f"BBB_{self.config.bb_length}_{self.config.bb_std}"] / 200

        # Calculate estimated price pct between levels
        candles_df["price_pct_between_levels"] = candles_df["spread_multiplier"] * self.config.intra_spread_pct

        # Update intra spread pct
        self.price_pct_between_levels = Decimal(candles_df["price_pct_between_levels"].iloc[-1])
        return candles_df

    def get_position_config(self, order_level: OrderLevel) -> PositionConfig:
        """
        Creates a PositionConfig object from an OrderLevel object.
        Here you can use technical indicators to determine the parameters of the position config.
        """
        # Get the close price of the trading pair
        close_price = self.get_close_price(self.config.exchange, self.config.trading_pair)

        # Get base amount from order level
        amount = order_level.order_amount_usd / close_price

        # Get bollinger mid-price and spread multiplier
        bollinger_mid_price, spread_multiplier = self.get_price_and_spread_multiplier()

        # This side multiplier is only to get the correct bollingrid side
        side_multiplier = 1 if order_level.side == TradeType.BUY else -1

        # Avoid placing the order from the opposite side
        side_filter_condition = self.config.side_filter and (
                (close_price < bollinger_mid_price and order_level.side == TradeType.BUY) or
                (close_price > bollinger_mid_price and order_level.side == TradeType.SELL))
        if side_filter_condition:
            return

        # Get triple barrier configs from the order level. Updated here to get the correct target prices.
        self.take_profit_pct = self.price_pct_between_levels * order_level.triple_barrier_conf.take_profit
        self.stop_loss_pct = self.price_pct_between_levels * order_level.triple_barrier_conf.stop_loss
        self.trailing_stop_activation_pct = self.price_pct_between_levels * order_level.triple_barrier_conf.trailing_stop_activation_price_delta
        self.trailing_stop_trailing_pct = self.price_pct_between_levels * order_level.triple_barrier_conf.trailing_stop_trailing_delta

        # Calculate order price and limits
        order_price = bollinger_mid_price * (1 + order_level.spread_factor * spread_multiplier * side_multiplier)
        tolerance = self.config.activation_threshold * spread_multiplier
        order_upper_limit = order_price * (1 + tolerance)
        order_lower_limit = order_price * (1 - tolerance)

        # This side will replace the original order level side if the order is placed from the opposite side
        if close_price < order_price:
            fixed_side = TradeType.BUY
            fixed_side_multiplier = 1
        elif close_price > order_price:
            fixed_side = TradeType.SELL
            fixed_side_multiplier = -1
        else:
            fixed_side = order_level.side
            fixed_side_multiplier = side_multiplier

        # Calculate target prices according to the fixed side
        take_profit_price = order_price * (1 + self.take_profit_pct * fixed_side_multiplier)
        stop_loss_price = order_price * (1 - self.stop_loss_pct * fixed_side_multiplier)
        trailing_stop_activation_price = order_price * (1 + self.trailing_stop_activation_pct * fixed_side_multiplier)
        trailing_stop_trailing_price = order_price * (1 - self.trailing_stop_trailing_pct * fixed_side_multiplier)

        # Update target prices for format status
        self.target_prices[f"{order_level.level}"] = {
            "side": fixed_side.name,
            "status": "Pending",
            "close_price": close_price,
            "order_price": order_price,
            "lower_limit": order_lower_limit,
            "upper_limit": order_upper_limit,
            "take_profit_price": take_profit_price,
            "stop_loss_price": stop_loss_price,
            "stop_loss_pct": f"{100 * self.stop_loss_pct:.3f}%",
            "trailing_stop_activation_price": trailing_stop_activation_price,
            "trailing_stop_activation_pct": f"{100 * self.trailing_stop_activation_pct:.3f}%",
            "trailing_stop_trailing_price": trailing_stop_trailing_price,
            "trailing_stop_trailing_pct": f"{100 * self.trailing_stop_trailing_pct:.3f}%",
          }

        # Smart activation of orders
        smart_activation_condition = self.config.smart_activation and (
                fixed_side == TradeType.BUY and order_lower_limit <= close_price <= order_price
        ) or (
                fixed_side == TradeType.SELL and order_upper_limit >= close_price >= order_price
        )
        if not smart_activation_condition:
            return

        # Mark as active
        self.target_prices[f"{order_level.level}"]["status"] = "Active"

        # Set up trailing stop
        if order_level.triple_barrier_conf.trailing_stop_trailing_delta and order_level.triple_barrier_conf.trailing_stop_trailing_delta:
            trailing_stop = TrailingStop(
                activation_price_delta=self.trailing_stop_activation_pct,
                trailing_delta=self.trailing_stop_trailing_pct,
            )
        else:
            trailing_stop = None

        # Build position config
        position_config = PositionConfig(
            timestamp=time.time(),
            trading_pair=self.config.trading_pair,
            exchange=self.config.exchange,
            side=fixed_side,
            amount=amount,
            take_profit=self.take_profit_pct,
            stop_loss=self.stop_loss_pct,
            time_limit=order_level.triple_barrier_conf.time_limit,
            entry_price=Decimal(order_price),
            open_order_type=order_level.triple_barrier_conf.open_order_type,
            take_profit_order_type=order_level.triple_barrier_conf.take_profit_order_type,
            trailing_stop=trailing_stop,
            leverage=self.config.leverage
        )
        return position_config
