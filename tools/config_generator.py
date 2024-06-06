import uuid

import pandas as pd
import yaml

from hummingbot.strategy_v2.utils.distributions import Distributions


def compute_dca_metrics(config):
    # Extract relevant parameters
    buy_spreads = config.get('buy_spreads', [])
    sell_spreads = config.get('sell_spreads', [])
    dca_amounts = config.get('dca_amounts', [])
    total_amount_quote = config.get('total_amount_quote', 0)
    stop_loss = config.get('stop_loss', 0)
    take_profit = config.get('take_profit', 0)

    # Calculate metrics
    min_spread = min(buy_spreads + sell_spreads)
    max_spread = max(buy_spreads + sell_spreads)
    average_entry_price = sum(dca_amounts) / len(dca_amounts) if dca_amounts else 0
    global_stop_loss_pct = stop_loss
    dca_max_loss_in_quote = total_amount_quote * stop_loss
    dca_max_profit_in_quote = total_amount_quote * take_profit
    min_order_amount = min(dca_amounts) if dca_amounts else 0
    first_order_amount = dca_amounts[0] if dca_amounts else 0
    total_dca_amount = sum(dca_amounts)
    dif_max_spread_vs_global_stop_loss = max_spread - global_stop_loss_pct
    dif_max_spread_vs_take_profit = max_spread - take_profit
    dif_max_spread_vs_average_entry_price = max_spread - average_entry_price
    n_levels = len(dca_amounts)

    # Create a dictionary of metrics
    metrics = {
        'config_id': config.get('config_id', ''),
        'min_spread': min_spread,
        'max_spread': max_spread,
        'average_entry_price': average_entry_price,
        'global_stop_loss_pct': global_stop_loss_pct,
        'dca_max_loss_in_quote': dca_max_loss_in_quote,
        'dca_max_profit_in_quote': dca_max_profit_in_quote,
        'min_order_amount': min_order_amount,
        'first_order_amount': first_order_amount,
        'total_dca_amount': total_dca_amount,
        'dif_max_spread_vs_global_stop_loss': dif_max_spread_vs_global_stop_loss,
        'dif_max_spread_vs_take_profit': dif_max_spread_vs_take_profit,
        'dif_max_spread_vs_average_entry_price': dif_max_spread_vs_average_entry_price,
        'n_levels': n_levels
    }

    # Convert the metrics dictionary to a DataFrame
    metrics_df = pd.DataFrame([metrics])

    return metrics_df


# Example usage
def load_config_from_yaml(yaml_file):
    with open(yaml_file, 'r') as file:
        config = yaml.safe_load(file)
    return config


def save_config_to_yaml(config, yaml_file):
    with open(yaml_file, 'w') as file:
        yaml.dump(config, file)


def generate_configs():
    # static values
    controller_name = "dman_maker_v2"
    connector_name = "binance"
    trading_pair = "BTC-USDT"
    total_amount_quote = 1000
    buy_spreads = [0.0]
    sell_spreads = [0.0]
    buy_amounts_pct = [0.5]
    sell_amounts_pct = [0.5]
    executor_refresh_time = 3600
    leverage = 20
    n_levels = 2
    position_mode = "HEDGE"
    top_executor_refresh_time = 3600
    executor_activation_bounds = None
    # won't be using this because currently there is no way to backtest it
    ts_ap = 99.9
    ts_delta = 0.02

    # variable values
    end_spread_list = [0.5, 1.0, 1.5, 2.0]
    dca_amounts_list = [[0.05, 0.95], [0.1, 0.9], [0.15, 0.85], [0.2, 0.8]]
    sl_list = [0.0005 + i / 1000 for i in range(10)]
    tp_list = [0.0005 + i / 1000 for i in range(10)]
    # cooldown_time_list = [i * 60 * 60 for i in range(2, 10, 2)]
    # time_limit_list = [i * 60 * 60 for i in range(2, 10, 2)]
    cooldown_time_list = [0]
    time_limit_list = [0]

    configs = []
    for cooldown_time in cooldown_time_list:
        for end_spread in end_spread_list:
            for dca_amounts in dca_amounts_list:
                for sl in sl_list:
                    for tp in tp_list:
                        for time_limit in time_limit_list:
                            user_inputs = {
                                "config_base": f"{controller_name}-{connector_name}-{trading_pair.split('-')[0]}",
                                "config_id": str(uuid.uuid4()),
                                "controller_name": "dman_maker_v2",
                                "controller_type": "market_making",
                                "manual_kill_switch": None,
                                "candles_config": [],
                                "connector_name": connector_name,
                                "trading_pair": trading_pair,
                                "total_amount_quote": total_amount_quote,
                                "buy_spreads": buy_spreads,
                                "sell_spreads": sell_spreads,
                                "buy_amounts_pct": buy_amounts_pct,
                                "sell_amounts_pct": sell_amounts_pct,
                                "executor_refresh_time": executor_refresh_time,
                                "cooldown_time": cooldown_time,
                                "leverage": leverage,
                                "position_mode": position_mode,
                                "top_executor_refresh_time": top_executor_refresh_time,
                                "executor_activation_bounds": [executor_activation_bounds]
                            }

                            dca_inputs = {
                                "dca_spreads": [spread / 100 for spread in
                                                Distributions.linear(n_levels=n_levels, start=0.0, end=end_spread)],
                                "dca_amounts": dca_amounts,
                                "stop_loss": sl,
                                "take_profit": tp,
                                "time_limit": time_limit,
                                "trailing_stop": {
                                    "activation_price": ts_ap,
                                    "trailing_delta": ts_delta
                                },
                            }
                            configs.append({**user_inputs, **dca_inputs})

    configs_df = pd.DataFrame(configs)

    metrics_df = pd.DataFrame()
    for index, row in configs_df.iterrows():
        df0 = compute_dca_metrics(row)
        metrics_df = pd.concat([metrics_df, df0], ignore_index=True)
    return configs_df, metrics_df
