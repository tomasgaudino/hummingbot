import asyncio
import datetime
import pickle

import numpy as np
import pandas as pd
import plotly.graph_objs as go
import streamlit as st

import hummingbot.connector.derivative.okx_perpetual.okx_perpetual_constants as CONSTANTS
from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.connector.derivative.okx_perpetual.okx_perpetual_derivative import OkxPerpetualDerivative
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.backtesting import BacktestingDataProvider
from tools.config_generator import generate_configs

st.set_page_config(page_title="Market Selection Tool for OKX Perpetuals", layout="wide")
if "data_providers" not in st.session_state:
    try:
        with open('backtesting_data.pkl', 'rb') as file:
            st.session_state.data_providers = pickle.load(file)
    except FileNotFoundError:
        st.session_state.data_providers = {}


class OkxPerpetualDerivativeDev(OkxPerpetualDerivative):
    def __init__(self):
        super().__init__(ClientConfigMap())

    async def _get_tickers_info(self) -> float:
        params = {"instType": "SWAP"}

        resp_json = await self._api_get(
            path_url=CONSTANTS.REST_LATEST_SYMBOL_INFORMATION[CONSTANTS.ENDPOINT],
            params=params,
        )

        self.tickers = resp_json["data"]
        return self.tickers


def ms_to_duration_str(ms):
    seconds = ms // 1000
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24
    hours = hours % 24
    minutes = minutes % 60

    return f"{days}d {hours}h {minutes}m"


def calculate_drawdowns_and_runups(df, gamma):
    df["peak"] = 0.0
    df["peak_timestamp"] = np.nan

    ascending = True if df["close"].iloc[0] < df["open"].iloc[0] else False
    threshold = df["open"].iloc[0] * (1 - gamma) if ascending else df["open"].iloc[0] * (1 + gamma)

    current_peak = df["low"].iloc[0] if ascending else df["high"].iloc[0]

    for i in range(1, len(df)):
        openp = df["open"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        close = df["close"].iloc[i]
        if ascending:
            candle_breaks_run_up_by_itself = (low - openp) / openp <= -gamma
            if candle_breaks_run_up_by_itself:
                df.at[i, "peak"] = current_peak
                current_peak = high
                ascending = True if close > openp else False
            else:
                if high > current_peak:
                    current_peak = high
                    threshold = current_peak * (1 - gamma)
                if low < threshold:
                    df.at[i, "peak"] = current_peak
                    current_peak = high
                    ascending = True if close > openp else False
        else:
            candle_breaks_draw_down_by_itself = (high - openp) / openp >= gamma
            if candle_breaks_draw_down_by_itself:
                df.at[i, "peak"] = current_peak
                current_peak = low
                ascending = True if close > openp else False
            else:
                if low < current_peak:
                    current_peak = min(current_peak, low)
                    threshold = current_peak * (1 + gamma)
                if high > threshold:
                    df.at[i, "peak"] = current_peak
                    current_peak = low
                    ascending = True if close > openp else False

    return df


async def get_tickers_df(connector):
    tickers_info = await connector._get_tickers_info()
    tickers_df = pd.DataFrame(tickers_info)
    tickers_df[["last", "open24h", "high24h", "low24h", "volCcy24h", "vol24h"]] = tickers_df[
        ["last", "open24h", "high24h", "low24h", "volCcy24h", "vol24h"]].apply(pd.to_numeric)
    trading_pairs = {}
    for trading_pair in tickers_df["instId"].to_list():
        trading_pairs[trading_pair] = await connector.trading_pair_associated_to_exchange_symbol(trading_pair)
    tickers_df["trading_pair"] = tickers_df["instId"].map(trading_pairs)
    return tickers_df


async def get_trading_rules_df(connector):
    await connector._update_trading_rules()
    data = [
        {
            'trading_pair': rule.trading_pair,
            'min_order_size': rule.min_order_size,
            'max_order_size': rule.max_order_size,
            'min_price_increment': rule.min_price_increment,
            'min_base_amount_increment': rule.min_base_amount_increment,
            'min_quote_amount_increment': rule.min_quote_amount_increment,
            'min_notional_size': rule.min_notional_size,
            'min_order_value': rule.min_order_value,
            'max_price_significant_digits': rule.max_price_significant_digits,
            'supports_limit_orders': rule.supports_limit_orders,
            'supports_market_orders': rule.supports_market_orders,
            'buy_order_collateral_token': rule.buy_order_collateral_token,
            'sell_order_collateral_token': rule.sell_order_collateral_token
        }
        for rule in connector._trading_rules.values()
    ]
    trading_rules = pd.DataFrame(data)
    return trading_rules


async def get_markets_data(connector):
    contract_sizes = pd.DataFrame(connector._contract_sizes.items(), columns=['trading_pair', 'contract_size'])
    trading_rules = await get_trading_rules_df(connector)
    tickers = await get_tickers_df(connector)
    markets_df = trading_rules.merge(contract_sizes, on="trading_pair")
    markets_df = markets_df.merge(tickers, on="trading_pair")
    markets_df[["min_order_size", "contract_size", "last"]] = markets_df[
        ["min_order_size", "contract_size", "last"]].apply(pd.to_numeric)
    markets_df["min_order_amount"] = markets_df["min_order_size"] * markets_df["last"]
    markets_df["min_base_amount_increment"] = (markets_df["min_base_amount_increment"].apply(pd.to_numeric) *
                                               markets_df["last"])
    markets_df["low_norm"] = 0
    markets_df["high_norm"] = markets_df["high24h"] / markets_df["low24h"] - 1
    markets_df["last_norm"] = markets_df["last"] / markets_df["low24h"] - 1
    markets_df["open_norm"] = markets_df["open24h"] / markets_df["low24h"] - 1
    markets_df["url"] = markets_df['instId'].apply(lambda x: f"https://www.okx.com/es-la/trade-swap/{x.lower()}")
    return markets_df


def get_normalized_returns_fig(df):
    fig = go.Figure()
    fig.add_trace(go.Ohlc(x=df["trading_pair"],
                          open=df["open_norm"],
                          high=df["high_norm"],
                          low=df["low_norm"],
                          close=df["last_norm"],
                          name="Normalized Returns"))
    fig.update_layout(title="Normalized Returns",
                      yaxis_title="Returns",
                      xaxis_title="Trading Pair",
                      xaxis_rangeslider_visible=False)
    return fig


def get_volume_vs_min_order_amount_fig(df):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["min_order_amount"],
                             y=df["volCcy24h"],
                             mode='markers+text',
                             text=df["trading_pair"],
                             textposition="top center", ))
    fig.update_layout(title="Volume vs Min Order Amount",
                      yaxis={
                          "title": "Volume",
                          "type": "log"
                      },
                      xaxis_title="Min Order Amount")
    return fig


async def main():
    st.subheader("Summary view")

    connector = OkxPerpetualDerivativeDev()
    await connector._update_trading_rules()
    markets = await get_markets_data(connector)

    col1, col2 = st.columns(2)
    with col1:
        min_volume = st.number_input("Minimum 24h volume (millons)", value=100.0)
    with col2:
        max_order_amount = st.number_input("Maximum order amount", value=1.0)

    filtered_markets = markets[(markets["min_order_amount"] <= max_order_amount) &
                               (markets["volCcy24h"] / 1_000_000 >= min_volume) & (markets["last"] > 1e-6)]

    with st.expander("Tickers info"):
        cols_to_show = ["trading_pair", "min_order_amount", "last", "open24h", "high24h", "low24h", "volCcy24h"]
        st.dataframe(filtered_markets[cols_to_show])

    col1, col2 = st.columns(2)
    with col1:
        sorted_returns = filtered_markets.sort_values("high_norm", ascending=False).copy()
        st.plotly_chart(get_normalized_returns_fig(sorted_returns), use_container_width=True)
    with col2:
        st.plotly_chart(get_volume_vs_min_order_amount_fig(filtered_markets), use_container_width=True)

    # ---------------------------
    st.subheader("Select your markets")
    selected_markets = st.multiselect("Markets", filtered_markets["trading_pair"].to_list())
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    today = datetime.datetime.today().date()
    with col1:
        connector_name = st.text_input("Candles Feed", "okx_perpetual")
    with col2:
        start_date = st.date_input("Select start date:", today - datetime.timedelta(days=3))
    with col3:
        start_time = st.time_input("Select start time:", datetime.time())
    with col4:
        end_date = st.date_input("Select end date:", today)
    with col5:
        end_time = st.time_input("Select end time:", datetime.time())
    with col6:
        interval = st.selectbox("Select interval", ["1m", "5m", "15m", "1h", "4h", "1d"])
    start_datetime = datetime.datetime.combine(start_date, start_time)
    end_datetime = datetime.datetime.combine(end_date, end_time)
    start_timestamp = start_datetime.timestamp() * 1000
    end_timestamp = end_datetime.timestamp() * 1000

    if st.button("Update Data Providers"):
        for trading_pair in selected_markets:
            data_provider = BacktestingDataProvider(connectors={})
            data_provider.update_backtesting_time(start_time=start_timestamp, end_time=end_timestamp)
            candles_config = CandlesConfig(connector="okx_perpetual", trading_pair=trading_pair, interval=interval)
            await data_provider.get_candles_feed(candles_config)
            st.session_state.data_providers[trading_pair] = data_provider
    if st.session_state.data_providers != {}:
        if st.button("Dump Data Providers"):
            with open('backtesting_data.pkl', 'wb') as file:
                pickle.dump(st.session_state.data_providers, file)
    else:
        st.stop()

    # ---------------------------
    st.subheader("Peaks")
    col1, col2 = st.columns(2)
    with col1:
        selected_candles = st.selectbox("Select candles", st.session_state.data_providers.keys())
        key = f"{connector_name}_{selected_candles}_{interval}"
        df = st.session_state.data_providers[selected_candles].candles_feeds[key]
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    with col2:
        gamma = st.number_input("Gamma", value=0.01, step=0.001, format="%.3f")

    df = calculate_drawdowns_and_runups(df, gamma)
    peaks = df[df["peak"] != 0]
    fig = go.Figure(data=[go.Candlestick(x=df['datetime'],
                                         open=df['open'],
                                         high=df['high'],
                                         low=df['low'],
                                         close=df['close'])])

    fig.add_trace(go.Scatter(x=peaks['datetime'],
                             y=peaks["peak"],
                             mode='lines',
                             name='peak',
                             line={"width": 4, "color": "yellow"}))
    fig.update_layout(xaxis_rangeslider_visible=False)

    col1, col2 = st.columns([3, 1])
    with col1:
        st.subheader("Suggested metrics")
        cola, colb, colc, cold, cole, colf = st.columns(6)
        suggested_metrics = {
            "max_sell_spread": 100 * peaks['peak'].pct_change().min(),
            "max_buy_spread": 100 * peaks['peak'].pct_change().max(),
            "min_order_amount": markets[markets['trading_pair'] == selected_candles]['min_order_amount'].values[0],
            "max_final_return": 100 * gamma,
            "time_limit": peaks["timestamp"].diff().max() / 1000 / 60,
            "total_trades": len(peaks)
        }
        with cola:
            st.metric("Max Sell Spread", f"{suggested_metrics['max_sell_spread']:.2f}%")
        with colb:
            st.metric("Max Buy Spread", f"{suggested_metrics['max_buy_spread']:.2f}%")
        with colc:
            st.metric("Min Order Amount", f"{suggested_metrics['min_order_amount']:.2f}")
        with cold:
            st.metric("Max Final Return", f"{suggested_metrics['max_final_return']:.2f}%")
        with cole:
            st.metric("Time Limit (Min)", ms_to_duration_str(suggested_metrics['time_limit']))
        with colf:
            st.metric("Total Trades", suggested_metrics['total_trades'])
        st.plotly_chart(fig, use_container_width=True)
        st.link_button("OKX Official Page",
                       markets[markets["trading_pair"] == selected_candles]["url"].values[0])
        configs_df, metrics_df = generate_configs()
        metrics_df_filtered = metrics_df[
            (metrics_df["min_order_amount"] <= suggested_metrics["min_order_amount"]) &
            (metrics_df["dif_max_spread_vs_global_stop_loss"] >= suggested_metrics["max_sell_spread"]) &
            (metrics_df["dif_max_spread_vs_global_stop_loss"] >= suggested_metrics["max_buy_spread"]) &
            (metrics_df["dif_max_spread_vs_take_profit"] >= suggested_metrics["max_final_return"])
        ]
        st.write(f"Wow! You found {len(metrics_df_filtered)} configs")

    with col2:
        fig = go.Figure()
        fig.add_trace(go.Histogram(x=peaks["peak"].pct_change()))
        fig.update_layout(title="Peak Returns",
                          xaxis_title="Returns",
                          yaxis_title="Frequency")
        st.plotly_chart(fig, use_container_width=True)

        fig = go.Figure()
        fig.add_trace(go.Scatter(y=peaks["timestamp"].diff() / 1000 / 60,  # minutes
                                 x=peaks["peak"].pct_change(),
                                 mode="markers")
                      )
        fig.update_layout(title="Drawdowns and Run-ups Duration",
                          xaxis_title="Returns",
                          yaxis_title="Duration (minutes)")
        st.plotly_chart(fig, use_container_width=True)


if __name__ == "__main__":
    asyncio.run(main())
