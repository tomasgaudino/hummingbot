import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Union

import pandas as pd
from motor.motor_asyncio import AsyncIOMotorClient

from hummingbot.client.config.config_helpers import load_client_config_map_from_file
from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.arbitrage_executor.data_types import ArbitrageExecutorConfig
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction


class MongoClient:
    _shared_instance: "MongoClient" = None

    def __init__(
            self,
            uri: str = None,
            database: str = "mongodb",
    ):
        self.client = None
        self.db = None

        # Connection parameters with env fallbacks
        self.uri = uri
        self.database = database
        MongoClient._shared_instance = self

    @classmethod
    def get_instance(cls, *args, **kwargs) -> "MongoClient":
        if cls._shared_instance is None:
            cls._shared_instance = MongoClient(*args, **kwargs)
        return cls._shared_instance

    async def connect(self):
        """Connect to MongoDB using provided or environment variables."""
        try:
            self.client = AsyncIOMotorClient(
                self.uri,
                serverSelectionTimeoutMS=5000
            )
            self.db = self.client[self.database]
            await self.db.command('ping')
            logging.info("Successfully connected to MongoDB")

        except Exception as e:
            print(f"Failed to connect to MongoDB: {str(e)}")
            raise

    async def disconnect(self):
        """Disconnect from MongoDB."""
        if self.client:
            self.client.close()
            print("Disconnected from MongoDB")

    async def create_database(self, db_name: str):
        """Create a new database."""
        self.client[db_name]  # MongoDB creates a database automatically when a collection is added
        logging.info(f"Database {db_name} is now available.")

    async def delete_database(self, db_name: str):
        """Delete a database."""
        self.client.drop_database(db_name)
        logging.info(f"Database {db_name} deleted.")

    async def create_collection(self, collection_name: str, db_name: Optional[str] = None):
        """Create a collection in a given database."""
        db = self.client[db_name] if db_name else self.db
        await db.create_collection(collection_name)
        logging.info(f"Collection {collection_name} created in {db_name or self.db.name}.")

    async def delete_collection(self, collection_name: str, db_name: Optional[str] = None):
        """Delete a collection from a given database."""
        db = self.client[db_name] if db_name else self.db
        await db[collection_name].drop()
        logging.info(f"Collection {collection_name} deleted from {db_name or self.db.name}.")

    async def insert_documents(self, collection_name: str, documents: Union[Dict[str, Any], List[Dict[str, Any]]],
                               db_name: Optional[str] = None, index: List[str] = []):
        """Insert one or multiple documents into a specified collection."""
        db = self.client[db_name] if db_name else self.db
        collection = db[collection_name]

        if isinstance(documents, dict):
            documents = [documents]

        try:
            if index:
                await collection.create_index(index)
            result = await collection.insert_many(documents)
            logging.info(f"Inserted {len(result.inserted_ids)} documents into {collection_name}.")
        except Exception as e:
            logging.error(f"Error inserting documents into {collection_name}: {str(e)}")
            raise

    async def get_documents(self, collection_name: str, query: Dict[str, Any] = None, db_name: Optional[str] = None,
                            limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Retrieve documents from a collection with an optional query."""
        db = self.client[db_name] if db_name else self.db
        collection = db[collection_name]
        query = query or {}

        try:
            cursor = collection.find(query).sort("timestamp", -1)
            if limit:
                cursor = cursor.limit(limit)
            documents = await cursor.to_list(length=None)
            logging.info(f"Retrieved {len(documents)} documents from {collection_name}.")
            return documents
        except Exception as e:
            logging.error(f"Error retrieving documents from {collection_name}: {str(e)}")
            raise

    async def delete_documents(self, collection_name: str, query: Dict[str, Any], db_name: Optional[str] = None):
        """Delete documents matching a query from a collection."""
        db = self.client[db_name] if db_name else self.db
        collection = db[collection_name]
        try:
            result = await collection.delete_many(query)
            logging.info(f"Deleted {result.deleted_count} documents from {collection_name}.")
        except Exception as e:
            logging.error(f"Error deleting documents from {collection_name}: {str(e)}")
            raise


class ArbitrageRecorderControllerConfig(ControllerConfigBase):
    controller_name: str = "arbitrage_recorder"
    candles_config: List[CandlesConfig] = []
    exchange_pair_1: ConnectorPair = ConnectorPair(connector_name="binance", trading_pair="PENGU-USDT")
    exchange_pair_2: ConnectorPair = ConnectorPair(connector_name="solana_jupiter_mainnet-beta", trading_pair="PENGU-USDC")
    min_profitability: Decimal = Decimal("0.01")
    delay_between_executors: int = 10  # in seconds
    max_executors_imbalance: int = 1
    rate_connector: str = "binance"
    quote_conversion_asset: str = "USDT"

    def update_markets(self, markets: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        if self.exchange_pair_1.connector_name == self.exchange_pair_2.connector_name:
            markets.update({
                self.exchange_pair_1.connector_name: {self.exchange_pair_1.trading_pair,
                                                      self.exchange_pair_2.trading_pair}
            })
        else:
            markets.update({
                self.exchange_pair_1.connector_name: {self.exchange_pair_1.trading_pair},
                self.exchange_pair_2.connector_name: {self.exchange_pair_2.trading_pair}
            })
        return markets


class ArbitrageRecorderController(ControllerBase):
    gas_token_by_network = {
        "ethereum": "ETH",
        "solana": "SOL",
        "binance-smart-chain": "BNB",
        "polygon": "POL",
        "avalanche": "AVAX",
        "dexalot": "AVAX"
    }
    client_config_map = load_client_config_map_from_file()

    def __init__(self, config: ArbitrageRecorderControllerConfig, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)
        self._imbalance = 0
        self._last_buy_closed_timestamp = 0
        self._last_sell_closed_timestamp = 0
        self._len_active_buy_arbitrages = 0
        self._len_active_sell_arbitrages = 0
        self.base_asset = self.config.exchange_pair_1.trading_pair.split("-")[0]
        self.initialize_rate_sources()
        self.initialize_trading_rules()
        self.mongo_client = MongoClient(self.client_config_map.hb_config.mongo_uri,
                                        self.client_config_map.hb_config.mongo_database)

    def initialize_trading_rules(self):
        trading_rules_1: TradingRule = self.market_data_provider.get_trading_rules(
            self.config.exchange_pair_1.connector_name,
            self.config.exchange_pair_1.trading_pair)
        trading_rules_2: TradingRule = self.market_data_provider.get_trading_rules(
            self.config.exchange_pair_2.connector_name,
            self.config.exchange_pair_2.trading_pair)
        self.min_notional_size = max(trading_rules_1.min_notional_size, trading_rules_2.min_notional_size)
        self.min_order_size = max(trading_rules_1.min_order_size, trading_rules_2.min_order_size)

    def initialize_rate_sources(self):
        rates_required = []
        for connector_pair in [self.config.exchange_pair_1, self.config.exchange_pair_2]:
            base, quote = connector_pair.trading_pair.split("-")
            # Add rate source for gas token
            if connector_pair.is_amm_connector():
                gas_token = self.get_gas_token(connector_pair.connector_name)
                if gas_token != quote:
                    rates_required.append(ConnectorPair(connector_name=self.config.rate_connector,
                                                        trading_pair=f"{gas_token}-{quote}"))

            # Add rate source for quote conversion asset
            if quote != self.config.quote_conversion_asset:
                rates_required.append(ConnectorPair(connector_name=self.config.rate_connector,
                                                    trading_pair=f"{quote}-{self.config.quote_conversion_asset}"))

            # Add rate source for trading pairs
            rates_required.append(ConnectorPair(connector_name=connector_pair.connector_name,
                                                trading_pair=connector_pair.trading_pair))
        if len(rates_required) > 0:
            self.market_data_provider.initialize_rate_sources(rates_required)

    def get_gas_token(self, connector_name: str) -> str:
        _, chain, _ = connector_name.split("_")
        return self.gas_token_by_network[chain]

    async def update_processed_data(self):
        pass

    def determine_executor_actions(self) -> List[ExecutorAction]:
        self.update_arbitrage_stats()
        executor_actions = []
        current_time = self.market_data_provider.time()
        if (abs(self._imbalance) >= self.config.max_executors_imbalance or
                self._last_buy_closed_timestamp + self.config.delay_between_executors > current_time or
                self._last_sell_closed_timestamp + self.config.delay_between_executors > current_time):
            return executor_actions
        if self._len_active_buy_arbitrages == 0:
            executor_actions.append(self.create_arbitrage_executor_action(self.config.exchange_pair_1,
                                                                          self.config.exchange_pair_2))
        if self._len_active_sell_arbitrages == 0:
            executor_actions.append(self.create_arbitrage_executor_action(self.config.exchange_pair_2,
                                                                          self.config.exchange_pair_1))
        return executor_actions

    def create_arbitrage_executor_action(self, buying_exchange_pair: ConnectorPair,
                                         selling_exchange_pair: ConnectorPair):
        try:
            if buying_exchange_pair.is_amm_connector():
                gas_token = self.get_gas_token(buying_exchange_pair.connector_name)
                pair = buying_exchange_pair.trading_pair.split("-")[0] + "-" + gas_token
                gas_conversion_price = self.market_data_provider.get_rate(pair)
            elif selling_exchange_pair.is_amm_connector():
                gas_token = self.get_gas_token(selling_exchange_pair.connector_name)
                pair = selling_exchange_pair.trading_pair.split("-")[0] + "-" + gas_token
                gas_conversion_price = self.market_data_provider.get_rate(pair)
            else:
                gas_conversion_price = None
            rate = self.market_data_provider.get_rate(self.base_asset + "-" + self.config.quote_conversion_asset)
            amount_quantized = self.market_data_provider.quantize_order_amount(
                buying_exchange_pair.connector_name, buying_exchange_pair.trading_pair,
                self.min_notional_size / rate)
            arbitrage_config = ArbitrageExecutorConfig(
                timestamp=self.market_data_provider.time(),
                buying_market=buying_exchange_pair,
                selling_market=selling_exchange_pair,
                order_amount=amount_quantized,
                min_profitability=self.config.min_profitability,
                gas_conversion_price=gas_conversion_price,
            )
            return CreateExecutorAction(
                executor_config=arbitrage_config,
                controller_id=self.config.id)
        except Exception as e:
            self.logger().error(
                f"Error creating executor to buy on {buying_exchange_pair.connector_name} and sell on {selling_exchange_pair.connector_name}, {e}")

    def update_arbitrage_stats(self):
        closed_executors = [e for e in self.executors_info if e.status == RunnableStatus.TERMINATED]
        active_executors = [e for e in self.executors_info if e.status != RunnableStatus.TERMINATED]
        buy_arbitrages = [arbitrage for arbitrage in closed_executors if
                          arbitrage.config.buying_market == self.config.exchange_pair_1]
        sell_arbitrages = [arbitrage for arbitrage in closed_executors if
                           arbitrage.config.buying_market == self.config.exchange_pair_2]
        self._imbalance = len(buy_arbitrages) - len(sell_arbitrages)
        self._last_buy_closed_timestamp = max([arbitrage.close_timestamp for arbitrage in buy_arbitrages]) if len(
            buy_arbitrages) > 0 else 0
        self._last_sell_closed_timestamp = max([arbitrage.close_timestamp for arbitrage in sell_arbitrages]) if len(
            sell_arbitrages) > 0 else 0
        self._len_active_buy_arbitrages = len([arbitrage for arbitrage in active_executors if
                                               arbitrage.config.buying_market == self.config.exchange_pair_1])
        self._len_active_sell_arbitrages = len([arbitrage for arbitrage in active_executors if
                                                arbitrage.config.buying_market == self.config.exchange_pair_2])

    def to_format_status(self) -> List[str]:
        all_executors_custom_info = pd.DataFrame(e.custom_info for e in self.executors_info)
        return [format_df_for_printout(all_executors_custom_info, table_format="psql", )]
