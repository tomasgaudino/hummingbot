"""
Backtest del chessboard controller (tablero de grillas con relevo).

Corre el controller completo contra candles reales: despliega las grillas que
rodean el precio y va relevando consecutivas a medida que el precio se mueve.
Usar un par volátil (ej BTC-USDT, ETH-USDT) para estresar el relevo.

Usage:
    python scripts/backtest_chessboard.py --days 3
    python scripts/backtest_chessboard.py --trading-pair ETH-USDT --days 3 --n-grids 7 --chart
    python scripts/backtest_chessboard.py --range 0.06 --n-grids 8 --output bt_chess.html

(Si conda run falla por permisos, usar el python del env directo:
 /opt/homebrew/Caskroom/miniconda/base/envs/hummingbot/bin/python scripts/backtest_chessboard.py ...)
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Patch broken optional dependency (injective proto mismatch)
try:
    from pyinjective.proto.injective.stream.v2 import query_pb2
    if not hasattr(query_pb2, "OrderFailuresFilter"):
        query_pb2.OrderFailuresFilter = type("OrderFailuresFilter", (), {})
except ImportError:
    pass

from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase  # noqa: E402
from hummingbot.strategy_v2.backtesting.backtesting_result import BacktestingResult  # noqa: E402


def build_config(connector: str, trading_pair: str, total_amount_quote: int,
                 border_a: float, border_b: float, n_grids: int,
                 limit_distance_pct: float, take_profit: float,
                 max_open_orders: int, leverage: int):
    config_data = {
        "id": "backtest_chessboard",
        "controller_name": "chessboard",
        "controller_type": "generic",
        "connector_name": connector,
        "trading_pair": trading_pair,
        "total_amount_quote": str(total_amount_quote),
        "leverage": leverage,
        "border_a": str(border_a),
        "border_b": str(border_b),
        "n_grids": n_grids,
        "limit_distance_pct": str(limit_distance_pct),
        "max_open_orders": max_open_orders,
        "max_orders_per_batch": 2,
        "order_frequency": 5,
        "min_spread_between_orders": "0.001",
        "min_order_amount_quote": "5",
        "keep_position": True,
        "triple_barrier_config": {
            "take_profit": str(take_profit),
            "open_order_type": 3,        # OrderType.LIMIT_MAKER
            "take_profit_order_type": 3,  # OrderType.LIMIT_MAKER
        },
    }
    return BacktestingEngineBase.get_controller_config_instance_from_dict(
        config_data, controllers_module="controllers"
    )


async def fetch_recent_price(connector: str, trading_pair: str, start: int, end: int) -> float:
    """Primer close del histórico para auto-derivar el rango A-B."""
    from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
    from hummingbot.strategy_v2.backtesting.backtesting_data_provider import BacktestingDataProvider

    provider = BacktestingDataProvider(connectors={})
    provider.update_backtesting_time(start, end)
    cfg = CandlesConfig(connector=connector, trading_pair=trading_pair, interval="1m")
    await provider.initialize_candles_feed(cfg)
    df = provider.get_candles_df(connector_name=connector, trading_pair=trading_pair, interval="1m")
    if df.empty:
        raise RuntimeError(f"No candle data for {connector} {trading_pair}")
    return float(df.iloc[0]["close"])


async def main(days, show_chart, output_path, connector, trading_pair,
               total_amount_quote, resolution, border_a, border_b, n_grids,
               limit_distance_pct, take_profit, max_open_orders, leverage, rng):
    end_ts = int(time.time())
    start_ts = end_ts - int(days * 24 * 3600)

    # Auto-derivar bordes A-B del precio real si no se pasaron.
    if border_a is None or border_b is None:
        ref = await fetch_recent_price(connector, trading_pair, start_ts, end_ts)
        half = rng / 2
        if border_a is None:
            border_a = round(ref * (1 - half), 6)
        if border_b is None:
            border_b = round(ref * (1 + half), 6)
        print(f"  Auto rango A-B desde precio {ref:.4f}: {border_a} -> {border_b}")

    config = build_config(connector, trading_pair, total_amount_quote,
                          border_a, border_b, n_grids, limit_distance_pct,
                          take_profit, max_open_orders, leverage)
    engine = BacktestingEngineBase()

    print(f"Running backtest: chessboard | {connector} {trading_pair} | {days}d | {resolution} ...")
    print(f"  Rango: {border_a} -> {border_b} | N grillas: {n_grids} | "
          f"limit_dist: {limit_distance_pct} | TP: {take_profit}")
    t0 = time.perf_counter()
    result = await engine.run_backtesting(
        config, start_ts, end_ts,
        backtesting_resolution=resolution,
        trade_cost=0.0002,
    )
    elapsed = time.perf_counter() - t0

    r = result["results"]
    n_candles = len(result["processed_data"].get("features", []))
    cps = n_candles / elapsed if elapsed > 0 else 0

    print(f"\n{'=' * 60}")
    print(f"  chessboard backtest ({days}d @ {resolution})")
    print(f"{'=' * 60}")
    print(f"  Duration:               {elapsed:.2f}s ({n_candles} candles, {cps:.0f} candles/s)")
    print(f"  Total executors:        {r['total_executors']}  <- cuántas grillas se crearon (inicial + relevos)")
    print(f"  With position:          {r['total_executors_with_position']}")
    print(f"  Net PnL:                {r['net_pnl_quote']:.4f} {trading_pair.split('-')[1]} ({r['net_pnl'] * 100:.2f}%)")
    print(f"  Accuracy:               {r['accuracy']:.2%}")
    print(f"  Sharpe ratio:           {r['sharpe_ratio']:.4f}")
    print(f"  Max drawdown:           {r['max_drawdown_pct']:.4%}")
    print(f"  Profit factor:          {r['profit_factor']:.4f}")
    print(f"  Close types:            {r['close_types']}  <- distribución TP/SL/TL/etc de las grillas")
    print(f"  Total volume:           {r['total_volume']:.4f}")
    print(f"  Win/Loss:               {r['win_signals']}/{r['loss_signals']}")

    bt_result = BacktestingResult(result, config)
    print(f"\n{bt_result.get_results_summary()}")

    if show_chart:
        try:
            fig = bt_result.get_backtesting_figure()
            if output_path:
                fig.write_html(output_path)
                print(f"\n  Chart saved to {output_path}")
            else:
                fig.show()
        except ImportError:
            print("\n  plotly not installed: pip install plotly")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest chessboard controller")
    parser.add_argument("--days", type=float, default=3, help="Días a backtestear (ej 0.5 = 12h)")
    parser.add_argument("--connector", type=str, default="binance")
    parser.add_argument("--trading-pair", type=str, default="BTC-USDT", help="Par volátil para estresar el relevo")
    parser.add_argument("--amount", type=int, default=1000, help="Capital total quote del tablero")
    parser.add_argument("--resolution", type=str, default="1m")
    parser.add_argument("--border-a", type=float, default=None, help="Borde inferior A (auto si se omite)")
    parser.add_argument("--border-b", type=float, default=None, help="Borde superior B (auto si se omite)")
    parser.add_argument("--range", type=float, default=0.06, help="Rango del tablero como fracción (default 0.06 = ±3%)")
    parser.add_argument("--n-grids", type=int, default=6, help="Cantidad de escalones del tablero")
    parser.add_argument("--limit-distance", type=float, default=0.005, help="Distancia extra del limit (fracción)")
    parser.add_argument("--take-profit", type=float, default=0.001, help="TP por nivel (0.001 = 0.1%)")
    parser.add_argument("--max-open-orders", type=int, default=5)
    parser.add_argument("--leverage", type=int, default=1, help="1 = spot")
    parser.add_argument("--chart", action="store_true")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    asyncio.run(main(
        args.days, args.chart, args.output, args.connector, args.trading_pair,
        args.amount, args.resolution, args.border_a, args.border_b, args.n_grids,
        args.limit_distance, args.take_profit, args.max_open_orders,
        args.leverage, args.range,
    ))
