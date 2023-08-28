from decimal import Decimal
from typing import Dict

from hummingbot.connector.connector_base import ConnectorBase, TradeType
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.data_types import ExecutorHandlerStatus, OrderLevel, TripleBarrierConf
from hummingbot.smart_components.market_making.controllers.bb_cum_diff_mm import BBCumDiffV1, BBCumDiffV1Config
from hummingbot.smart_components.market_making.market_making_executor_handler import MarketMakingExecutorHandler
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class MarketMakingBBandCumDiffV1(ScriptStrategyBase):
    trading_pair = "HBAR-USDT"
    triple_barrier_conf = TripleBarrierConf(
        stop_loss=Decimal("0.015"),
        take_profit=Decimal("0.02"),
        time_limit=60 * 60 * 24,
        trailing_stop_activation_price_delta=Decimal("0.01"),
        trailing_stop_trailing_delta=Decimal("0.002")
    )
    config = BBCumDiffV1Config(
        exchange="binance_perpetual",
        trading_pair=trading_pair,
        order_levels=[
            OrderLevel(level=0, side=TradeType.BUY, order_amount_usd=Decimal(30),
                       spread_factor=Decimal(0.7), order_refresh_time=60 * 5,
                       cooldown_time=15, triple_barrier_conf=triple_barrier_conf),
            OrderLevel(level=0, side=TradeType.SELL, order_amount_usd=Decimal(30),
                       spread_factor=Decimal(0.7), order_refresh_time=60 * 5,
                       cooldown_time=15, triple_barrier_conf=triple_barrier_conf)
        ],
        candles_config=[
            CandlesConfig(connector="binance_perpetual", trading_pair=trading_pair, interval="3m", max_records=1000),
        ],
        leverage=10,
        natr_length=21
    )
    bb_cum_diff_v1 = BBCumDiffV1(config=config)

    empty_markets = {}
    markets = bb_cum_diff_v1.update_strategy_markets_dict(empty_markets)

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.bb_cum_diff_v1_executor = MarketMakingExecutorHandler(strategy=self, controller=self.bb_cum_diff_v1)

    def on_stop(self):
        self.bb_cum_diff_v1_executor.terminate_control_loop()

    def on_tick(self):
        """
        This shows you how you can start meta controllers. You can run more than one at the same time and based on the
        market conditions, you can orchestrate from this script when to stop or start them.
        """
        if self.bb_cum_diff_v1_executor.status == ExecutorHandlerStatus.NOT_STARTED:
            self.bb_cum_diff_v1_executor.start()

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []
        lines.extend(["BBAND_CUM_DIFF V1", self.bb_cum_diff_v1_executor.to_format_status()])
        lines.extend(["\n-----------------------------------------\n"])
        return "\n".join(lines)
