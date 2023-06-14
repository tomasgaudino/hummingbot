from decimal import Decimal

import numpy as np

from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.strategy.directional_strategy_base import DirectionalStrategyBase


class MacdDiff(DirectionalStrategyBase):
    """
    MacdDiff strategy implementation based on the DirectionalStrategyBase.

    This strategy breaks down the MACD (Moving Average Convergence Divergence) in two main components: MACD histogram
    normalized representing trend and MACD Cum Strength, momentum. It defines the specific parameters and
    configurations for the MacdBB strategy.

    Parameters:
        directional_strategy_name (str): The name of the strategy.
        trading_pair (str): The trading pair to be traded.
        exchange (str): The exchange to be used for trading.
        order_amount_usd (Decimal): The amount of the order in USD.
        leverage (int): The leverage to be used for trading.

    Position Parameters:
        stop_loss (float): The stop-loss percentage for the position.
        take_profit (float): The take-profit percentage for the position.
        time_limit (int): The time limit for the position in seconds.
        trailing_stop_activation_delta (float): The activation delta for the trailing stop.
        trailing_stop_trailing_delta (float): The trailing delta for the trailing stop.

    Candlestick Configuration:
        candles (List[CandlesBase]): The list of candlesticks used for generating signals.

    Markets:
        A dictionary specifying the markets and trading pairs for the strategy.

    Inherits from:
        DirectionalStrategyBase: Base class for creating directional strategies using the PositionExecutor.
    """
    directional_strategy_name: str = "MACD_DIFF_V1"
    # Define the trading pair and exchange that we want to use and the csv where we are going to store the entries
    trading_pair: str = "DOGE-BUSD"
    exchange: str = "binance_perpetual"
    order_amount_usd = Decimal("20")
    leverage = 20

    # Configure the parameters for the position
    stop_loss: float = 0.0075
    take_profit: float = 0.1
    time_limit: int = 60 * 55
    trailing_stop_activation_delta = 0.0016
    trailing_stop_trailing_delta = 0.001

    candles = [CandlesFactory.get_candle(connector=exchange,
                                         trading_pair=trading_pair,
                                         interval="5m", max_records=150)]
    markets = {exchange: {trading_pair}}

    def get_signal(self):
        """
        Generates the trading signal based on the MACD Diff indicators.
        Returns:
            int: The trading signal (-1 for short, 0 for hold, 1 for long).
        """
        candles_df, macdh_col, macdh_norm_col = self.get_processed_df()
        delta_macd_thold = 0.0008
        macdh_norm_thold = 0.0
        target_thold = 0.0025

        last_candle = candles_df.iloc[-1]
        target = last_candle['TARGET']

        macd_change_cum = last_candle["MACD_CHANGE_CUM"]
        macdh_norm = last_candle[macdh_norm_col]

        if (macd_change_cum > delta_macd_thold) & (macdh_norm > macdh_norm_thold) & (target < target_thold):
            signal_value = 1
        elif (macd_change_cum < - delta_macd_thold) & (macdh_norm < - macdh_norm_thold) & (target < target_thold):
            signal_value = -1
        else:
            signal_value = 0
        return signal_value

    def get_processed_df(self):
        """
        Retrieves the processed dataframe with MACD and Bollinger Bands values.
        Returns:
            pd.DataFrame: The processed dataframe with MACD and Bollinger Bands values.
        """
        # Read candles
        candles_df = self.candles[0].candles_df

        # Add target
        std_span = 100
        candles_df['TARGET'] = candles_df["close"].rolling(std_span).std() / candles_df["close"]

        # Set up MACD config
        macd_fast, macd_slow, macd_signal = (12, 26, 9)
        candles_df.ta.macd(fast=macd_fast, slow=macd_slow, signal=macd_signal, append=True)

        # Standardize column names
        macdh_col = f"MACDh_{macd_fast}_{macd_slow}_{macd_signal}"
        macdh_norm_col = f"MACDh_{macd_fast}_{macd_slow}_{macd_signal}_norm"

        # Add new metrics
        candles_df[macdh_norm_col] = candles_df[macdh_col] / candles_df['close']
        candles_df['MACD_DIFF'] = candles_df[macdh_norm_col].diff()
        candles_df['MACD_CHANGE'] = np.sign(candles_df['MACD_DIFF']) != np.sign(candles_df['MACD_DIFF'].shift())
        candles_df['MACD_CHANGE_ID'] = candles_df['MACD_CHANGE'].cumsum()
        candles_df['MACD_CHANGE_CUM'] = candles_df.groupby('MACD_CHANGE_ID')['MACD_DIFF'].cumsum()

        return candles_df, macdh_col, macdh_norm_col

    def format_status(self) -> str:
        """
                Positions summary
        """
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []

        if len(self.stored_executors) > 0:
            net_profit = sum(x.net_pnl_quote for x in self.stored_executors)
            total_executors = len(self.stored_executors)
            total_positive_entries = sum(x.net_pnl_quote > 0 for x in self.stored_executors)
            total_profit = sum(x.net_pnl_quote for x in self.stored_executors if x.net_pnl_quote > 0)
            total_loss = sum(x.net_pnl_quote for x in self.stored_executors if x.net_pnl_quote < 0)

            lines.extend(["\n Execution Summary"])
            lines.extend([f"Net Profit: {net_profit:.2f}"])
            # TODO: add total traded volume
            lines.extend([f"N° Transactions: {total_executors}"])
            lines.extend([f"% Profitable: {(total_positive_entries / total_executors):.2f}"])
            lines.extend([f"Profit factor: {(total_profit / total_loss if total_loss != 0 else 1):.2f}"])
            lines.extend([f"Avg Profit: {(net_profit / total_executors):.4f}"])
            # TODO: register open_timestamp in position executor and calculate avg min
        return "\n".join(lines)

    def market_data_extra_info(self):
        """
        Provides additional information about the market data.
        Returns:
            List[str]: A list of formatted strings containing market data information.
        """
        candles_df, macdh_col, macdh_norm_col = self.get_processed_df()
        lines = []
        columns_to_show = ["timestamp", "open", "low", "high", "close", "volume", "TARGET", macdh_col, macdh_norm_col, "MACD_CHANGE_CUM"]
        lines.extend([f"Candles: {self.candles[0].name} | Interval: {self.candles[0].interval}\n"])
        lines.extend(self.candles_formatted_list(candles_df, columns_to_show))
        return lines
