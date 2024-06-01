import asyncio
import logging
import time
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from hummingbot.core.network_iterator import NetworkStatus, safe_ensure_future
from hummingbot.core.utils.tracking_nonce import get_tracking_nonce
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.data_feed.candles_feed.candles_base import CandlesBase
from hummingbot.data_feed.candles_feed.kucoin_perpetual_candles import constants as CONSTANTS
from hummingbot.logger import HummingbotLogger


class KucoinPerpetualCandles(CandlesBase):
    _logger: Optional[HummingbotLogger] = None
    _last_ws_message_sent_timestamp = 0
    _ping_interval = 0

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, trading_pair: str, interval: str = "1min", max_records: int = 150):
        self.symbols_dict = {}
        super().__init__(trading_pair, interval, max_records)
        self.hb_base_asset = self._trading_pair.split("-")[0]
        self.kucoin_base_asset = self.get_kucoin_base_asset()
        self.quote_asset = self._trading_pair.split("-")[1]

    def get_kucoin_base_asset(self):
        for hb_asset, kucoin_value in CONSTANTS.HB_TO_KUCOIN_MAP.items():
            return kucoin_value if hb_asset == self.hb_base_asset else self.hb_base_asset

    @property
    def name(self):
        return f"kucoin_perpetual_{self._trading_pair}"

    @property
    def interval_in_seconds(self):
        return self.get_seconds_from_interval(self.interval)

    @property
    def rest_url(self):
        return CONSTANTS.REST_URL

    @property
    def wss_url(self):
        return CONSTANTS.WSS_URL

    @property
    def health_check_url(self):
        return self.rest_url + CONSTANTS.HEALTH_CHECK_ENDPOINT

    @property
    def candles_url(self):
        return self.rest_url + CONSTANTS.CANDLES_ENDPOINT

    @property
    def public_ws_url(self):
        return self.rest_url + CONSTANTS.PUBLIC_WS_DATA_PATH_URL

    @property
    def rate_limits(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def intervals(self):
        return CONSTANTS.INTERVALS

    @property
    def candles_df(self) -> pd.DataFrame:
        df = pd.DataFrame(self._candles, columns=self.columns, dtype=float)
        return df.sort_values(by="timestamp", ascending=True)

    async def check_network(self) -> NetworkStatus:
        rest_assistant = await self._api_factory.get_rest_assistant()
        await rest_assistant.execute_request(url=self.health_check_url,
                                             throttler_limit_id=CONSTANTS.HEALTH_CHECK_ENDPOINT)
        return NetworkStatus.CONNECTED

    @property
    def symbols_url(self):
        return self.rest_url + CONSTANTS.SYMBOLS_ENDPOINT

    async def generate_symbols_dict(self) -> Dict[str, Any]:
        try:
            rest_assistant = await self._api_factory.get_rest_assistant()
            response = await rest_assistant.execute_request(url=self.symbols_url,
                                                            throttler_limit_id=CONSTANTS.SYMBOLS_ENDPOINT)
            symbols = response["data"]
            symbols_dict = {}
            for symbol in symbols:
                symbols_dict[f"{symbol['baseCurrency']}-{symbol['quoteCurrency']}"] = symbol["symbol"]
            self.symbols_dict = symbols_dict
        except Exception:
            self.logger().error("Error fetching symbols from Kucoin.")
            raise

    def get_exchange_trading_pair(self, trading_pair):
        return f"{self.kucoin_base_asset}-{self.quote_asset}" if bool(self.symbols_dict) else None

    async def symbols_ready(self):
        while not bool(self.symbols_dict):
            await self.generate_symbols_dict()
            self._ex_trading_pair = self.get_exchange_trading_pair(self._trading_pair)
        return bool(self._ex_trading_pair)

    async def fetch_candles(self,
                            start_time: Optional[int] = None,
                            end_time: Optional[int] = None,
                            limit: Optional[int] = CONSTANTS.MAX_RECORDS_LIMIT):
        rest_assistant = await self._api_factory.get_rest_assistant()
        params = {
            "symbol": self.symbols_dict[f"{self.kucoin_base_asset}-{self.quote_asset}"],
            "granularity": CONSTANTS.GRANULARITIES[self.interval],
            "to": end_time if end_time else start_time + (limit * self.interval_in_seconds)
        }
        if start_time:
            params["from"] = start_time

        response = await rest_assistant.execute_request(url=self.candles_url,
                                                        throttler_limit_id=CONSTANTS.CANDLES_ENDPOINT,
                                                        params=params)
        candles = np.array([[row[0], row[1], row[2], row[3], row[4], row[5], 0., 0., 0., 0.]
                            for row in response['data']]).astype(float)
        return candles

    async def fill_historical_candles(self):
        max_request_needed = (self._candles.maxlen // CONSTANTS.MAX_RECORDS_LIMIT) + 1
        requests_executed = 0
        await self.symbols_ready()
        while not self.ready:
            missing_records = self._candles.maxlen - len(self._candles)
            end_timestamp = int(self._candles[0][0])
            try:
                if requests_executed < max_request_needed:
                    candles = await self.fetch_candles(end_time=end_timestamp, limit=missing_records + 1)
                    missing_records = self._candles.maxlen - len(self._candles)
                    self._candles.extendleft(candles[-(missing_records + 1):-1])
                    requests_executed += 1
                else:
                    self.logger().error(f"There is no data available for the quantity of "
                                        f"candles requested for {self.name}.")
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception(
                    "Unexpected error occurred when getting historical klines. Retrying in 1 seconds...",
                )
                await self._sleep(1.0)

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the candles events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        try:
            await self.symbols_ready()
            topic_candle = f"{self.symbols_dict[self._ex_trading_pair]}_{CONSTANTS.INTERVALS[self.interval]}"
            payload = {
                "id": str(get_tracking_nonce()),
                "type": "subscribe",
                "topic": f"/contractMarket/limitCandle:{topic_candle}",
                "privateChannel": False,
                "response": True,
            }
            subscribe_candles_request: WSJSONRequest = WSJSONRequest(payload=payload)

            await ws.send(subscribe_candles_request)
            self.logger().info("Subscribed to public klines...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to public klines...",
                exc_info=True
            )
            raise

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant):
        while True:
            try:
                seconds_until_next_ping = self._ping_interval - (self._time() - self._last_ws_message_sent_timestamp)
                await asyncio.wait_for(self._process_websocket_messages_from_candles(websocket_assistant=websocket_assistant),
                                       timeout=seconds_until_next_ping)
            except asyncio.TimeoutError:
                payload = {
                    "id": str(get_tracking_nonce()),
                    "type": "ping",
                }
                ping_request = WSJSONRequest(payload=payload)
                self._last_ws_message_sent_timestamp = self._time()
                await websocket_assistant.send(request=ping_request)

    async def _process_websocket_messages_from_candles(self, websocket_assistant: WSAssistant):
        async for ws_response in websocket_assistant.iter_messages():
            data: Dict[str, Any] = ws_response.data
            data = data.get("data", {})
            # TODO: If there are no trades, socket doesn't update.
            # TODO: This overrides fill_historical_candles, so it doesn't work properly.
            if "candles" in data:
                candles = data["candles"]
                timestamp = int(candles[0]) * 1000.0
                open = candles[1]
                close = candles[2]
                high = candles[3]
                low = candles[4]
                volume = candles[5]
                quote_asset_volume = 0.
                n_trades = 0.
                taker_buy_base_volume = 0.
                taker_buy_quote_volume = 0.
                candles_array = np.array([timestamp, open, high, low, close, volume, quote_asset_volume, n_trades,
                                          taker_buy_base_volume, taker_buy_quote_volume]).astype(float)
                if len(self._candles) == 0:
                    self._candles.append(candles_array)
                    safe_ensure_future(self.fill_historical_candles())
                elif timestamp > int(self._candles[-1][0]):
                    self._candles.append(candles_array)
                elif timestamp == int(self._candles[-1][0]):
                    self._candles.pop()
                    self._candles.append(candles_array)

    async def _connected_websocket_assistant(self) -> WSAssistant:
        rest_assistant = await self._api_factory.get_rest_assistant()
        connection_info = await rest_assistant.execute_request(
            url=self.public_ws_url,
            method=RESTMethod.POST,
            throttler_limit_id=CONSTANTS.PUBLIC_WS_DATA_PATH_URL,
        )

        ws_url = connection_info["data"]["instanceServers"][0]["endpoint"]
        self._ping_interval = int(connection_info["data"]["instanceServers"][0]["pingInterval"]) * 0.8 * 1e-3
        token = connection_info["data"]["token"]

        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(ws_url=f"{ws_url}?token={token}", message_timeout=self._ping_interval)
        return ws

    def _time(self):
        return time.time()
