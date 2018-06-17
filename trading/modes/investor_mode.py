"""
WhaleBot Extension

$extension_description: {
    "name": "investor_mode",
    "type": "trading",
    "subtype": "modes",
    "version": "1.0.0",
    "requirements": [],
    "config_files": ["InvestorModeCreator.json"]
}
"""

import logging

from config.constants import *
from utils.evaluators_util import check_valid_eval_note
from utils.symbol_util import split_symbol
from core.trader.modes.abstract_mode_creator import AbstractTradingModeCreator
from core.trader.modes.abstract_mode_decider import AbstractTradingModeDecider
from core.trader.modes.abstract_trading_mode import AbstractTradingMode
from config.constants import ExchangeConstantsMarketStatusColumns as Ecmsc
from evaluators.strategies import FullMixedStrategiesEvaluator


class InvestorMode(AbstractTradingMode):
    def __init__(self, config, symbol_evaluator, exchange):
        super().__init__(config, symbol_evaluator, exchange)

        self.add_creator(InvestorModeCreator(self))
        self.set_decider(InvestorModeDecider(self, symbol_evaluator, exchange))

    @staticmethod
    def get_required_strategies():
        return [FullMixedStrategiesEvaluator]


class InvestorModeCreator(AbstractTradingModeCreator):
    def __init__(self, trading_mode):
        super().__init__(trading_mode)

    """
    Starting point : self.SELL_LIMIT_ORDER_MIN_PERCENT or self.BUY_LIMIT_ORDER_MAX_PERCENT
    1 - abs(eval_note) --> confirmation level --> high : sell less expensive / buy more expensive
    1 - trader.get_risk() --> high risk : sell / buy closer to the current price
    1 - abs(eval_note) + 1 - trader.get_risk() --> result between 0 and 2 --> self.MAX_SUM_RESULT
    self.QUANTITY_ATTENUATION --> try to contains the result between self.XXX_MIN_PERCENT and self.XXX_MAX_PERCENT
    """

    def _get_limit_price_from_risk(self, eval_note, trader):
        if eval_note > 0:
            factor = self.BUY_LIMIT_ORDER_MAX_PERCENT - (
                    (1 - abs(eval_note) + 1 - trader.get_risk()) * self.LIMIT_ORDER_ATTENUATION)
            return self.check_factor(self.BUY_LIMIT_ORDER_MIN_PERCENT,
                                     self.BUY_LIMIT_ORDER_MAX_PERCENT,
                                     factor)
        else:
            factor = self.SELL_LIMIT_ORDER_MIN_PERCENT + (
                    (1 - abs(eval_note) + 1 - trader.get_risk()) * self.LIMIT_ORDER_ATTENUATION)
            return self.check_factor(self.SELL_LIMIT_ORDER_MIN_PERCENT,
                                     self.SELL_LIMIT_ORDER_MAX_PERCENT,
                                     factor)

    """
    Starting point : self.STOP_LOSS_ORDER_MAX_PERCENT
    trader.get_risk() --> low risk : stop level close to the current price
    self.STOP_LOSS_ORDER_ATTENUATION --> try to contains the result between self.STOP_LOSS_ORDER_MIN_PERCENT and self.STOP_LOSS_ORDER_MAX_PERCENT
    """

    def _get_stop_price_from_risk(self, trader):
        factor = self.STOP_LOSS_ORDER_MAX_PERCENT - (trader.get_risk() * self.STOP_LOSS_ORDER_ATTENUATION)
        return self.check_factor(self.STOP_LOSS_ORDER_MIN_PERCENT,
                                 self.STOP_LOSS_ORDER_MAX_PERCENT,
                                 factor)

    """
    Starting point : self.QUANTITY_MIN_PERCENT
    abs(eval_note) --> confirmation level --> high : sell/buy more quantity
    trader.get_risk() --> high risk : sell / buy more quantity
    abs(eval_note) + trader.get_risk() --> result between 0 and 2 --> self.MAX_SUM_RESULT
    self.QUANTITY_ATTENUATION --> try to contains the result between self.QUANTITY_MIN_PERCENT and self.QUANTITY_MAX_PERCENT
    """

    def _get_limit_quantity_from_risk(self, eval_note, trader, quantity):
        factor = self.QUANTITY_MIN_PERCENT + ((abs(eval_note) + trader.get_risk()) * self.QUANTITY_ATTENUATION)
        return self.check_factor(self.QUANTITY_MIN_PERCENT,
                                 self.QUANTITY_MAX_PERCENT,
                                 factor) * quantity

    """
    Starting point : self.QUANTITY_MARKET_MIN_PERCENT
    abs(eval_note) --> confirmation level --> high : sell/buy more quantity
    trader.get_risk() --> high risk : sell / buy more quantity
    abs(eval_note) + trader.get_risk() --> result between 0 and 2 --> self.MAX_SUM_RESULT
    self.QUANTITY_MARKET_ATTENUATION --> try to contains the result between self.QUANTITY_MARKET_MIN_PERCENT and self.QUANTITY_MARKET_MAX_PERCENT
    """

    def _get_market_quantity_from_risk(self, eval_note, trader, quantity, buy=False):
        factor = self.QUANTITY_MARKET_MIN_PERCENT + (
                (abs(eval_note) + trader.get_risk()) * self.QUANTITY_MARKET_ATTENUATION)

        # if buy market --> limit market usage
        if buy:
            factor *= self.QUANTITY_BUY_MARKET_ATTENUATION

        return self.check_factor(self.QUANTITY_MARKET_MIN_PERCENT, self.QUANTITY_MARKET_MAX_PERCENT, factor) * quantity

    def can_create_order(self, symbol, exchange, state, portfolio):
        currency, market = split_symbol(symbol)

        # get symbol min amount when creating order
        symbol_limit = exchange.get_market_status(symbol)[Ecmsc.LIMITS.value]
        symbol_min_amount = symbol_limit[Ecmsc.LIMITS_AMOUNT.value][Ecmsc.LIMITS_AMOUNT_MIN.value]
        order_min_amount = symbol_limit[Ecmsc.LIMITS_COST.value][Ecmsc.LIMITS_COST_MIN.value]

        # short cases => market increase => buy => need this currency
        if state == EvaluatorStates.VERY_SHORT or state == EvaluatorStates.SHORT:
            return portfolio.get_currency_portfolio(market) > order_min_amount

        # long cases => market decrease => sell => need money(aka other currency in the pair) to buy this currency
        elif state == EvaluatorStates.LONG or state == EvaluatorStates.VERY_LONG:
           return portfolio.get_currency_portfolio(currency) > symbol_min_amount

        # other cases like neutral state or unfulfilled previous conditions
        return False

    # creates a new order (or multiple split orders), always check EvaluatorOrderCreator.can_create_order() first.
    def create_new_order(self, eval_note, symbol, exchange, trader, portfolio, state):
        try:
            last_prices = exchange.get_recent_trades(symbol)
            reference_sum = 0

            for last_price in last_prices[-ORDER_CREATION_LAST_TRADES_TO_USE:]:
                reference_sum += float(last_price["price"])

            reference = reference_sum / ORDER_CREATION_LAST_TRADES_TO_USE

            currency, market = split_symbol(symbol)

            current_portfolio = portfolio.get_currency_portfolio(currency)
            current_market_quantity = portfolio.get_currency_portfolio(market)

            market_quantity = current_market_quantity / reference

            price = reference
            symbol_market = exchange.get_market_status(symbol)

            created_orders = []

            if state == EvaluatorStates.VERY_SHORT:
                quantity = self._get_market_quantity_from_risk(eval_note,
                                                               trader,
                                                               market_quantity)
                for order_quantity, order_price in self.check_and_adapt_order_details_if_necessary(quantity, price,
                                                                                                   symbol_market):
                    market = trader.create_order_instance(order_type=TraderOrderType.BUY_MARKET,
                                                          symbol=symbol,
                                                          current_price=order_price,
                                                          quantity=order_quantity,
                                                          price=order_price)
                    trader.create_order(market, portfolio)
                    created_orders.append(market)
                return created_orders

            elif state == EvaluatorStates.SHORT:
                quantity = self._get_limit_quantity_from_risk(eval_note,
                                                              trader,
                                                              market_quantity)
                limit_price = self.adapt_price(symbol_market, price * self._get_limit_price_from_risk(eval_note, trader))
                # limit_price = price
                # stop_price = self.adapt_price(symbol_market, price * self._get_stop_price_from_risk(trader))
                for order_quantity, order_price in self.check_and_adapt_order_details_if_necessary(quantity,
                                                                                                   limit_price,
                                                                                                   symbol_market):
                    limit = trader.create_order_instance(order_type=TraderOrderType.BUY_LIMIT,
                                                         symbol=symbol,
                                                         current_price=price,
                                                         quantity=order_quantity,
                                                         price=order_price)
                    updated_limit = trader.create_order(limit, portfolio)
                    created_orders.append(updated_limit)

                return created_orders

            elif state == EvaluatorStates.NEUTRAL:
                pass

            # TODO : stop loss
            elif state == EvaluatorStates.LONG:
                quantity = self._get_limit_quantity_from_risk(eval_note,
                                                              trader,
                                                              current_portfolio)
                limit_price = self.adapt_price(symbol_market, price * self._get_limit_price_from_risk(eval_note, trader))
                # limit_price = price
                stop_price = self.adapt_price(symbol_market, price * self._get_stop_price_from_risk(trader))
                for order_quantity, order_price in self.check_and_adapt_order_details_if_necessary(quantity,
                                                                                                   limit_price,
                                                                                                   symbol_market):
                    limit = trader.create_order_instance(order_type=TraderOrderType.SELL_LIMIT,
                                                         symbol=symbol,
                                                         current_price=price,
                                                         quantity=order_quantity,
                                                         price=order_price)
                    updated_limit = trader.create_order(limit, portfolio)
                    created_orders.append(updated_limit)

                    stop = trader.create_order_instance(order_type=TraderOrderType.STOP_LOSS,
                                                        symbol=symbol,
                                                        current_price=price,
                                                        quantity=order_quantity,
                                                        price=stop_price,
                                                        linked_to=updated_limit)
                    trader.create_order(stop, portfolio)
                return created_orders

            elif state == EvaluatorStates.VERY_LONG:
                quantity = self._get_market_quantity_from_risk(eval_note,
                                                               trader,
                                                               current_portfolio,
                                                               True)

                stop_price = self.adapt_price(symbol_market, price * self._get_stop_price_from_risk(trader))

                for order_quantity, order_price in self.check_and_adapt_order_details_if_necessary(quantity, price,
                                                                                                   symbol_market):
                    market = trader.create_order_instance(order_type=TraderOrderType.SELL_MARKET,
                                                          symbol=symbol,
                                                          current_price=order_price,
                                                          quantity=order_quantity,
                                                          price=order_price)
                    updated_market = trader.create_order(market, portfolio)
                    created_orders.append(market)

                    stop = trader.create_order_instance(order_type=TraderOrderType.STOP_LOSS,
                                                        symbol=symbol,
                                                        current_price=price,
                                                        quantity=order_quantity,
                                                        price=stop_price,
                                                        linked_to=updated_market)
                    trader.create_order(stop, portfolio)

                return created_orders

            # if nothing go returned, return None
            return None

        except Exception as e:
            logging.getLogger(self.__class__.__name__).error("Failed to create order : {0}".format(e))
            return None

    def set_default_config(self):
        self.trading_creator_config = {
            CONFIG_MAX_SUM_RESULT: 2,
            CONFIG_STOP_LOSS_ORDER_MAX_PERCENT: 1,
            CONFIG_STOP_LOSS_ORDER_MIN_PERCENT: 0.998,
            CONFIG_QUANTITY_MIN_PERCENT: 0.99,
            CONFIG_QUANTITY_MAX_PERCENT: 1,
            CONFIG_QUANTITY_MARKET_MIN_PERCENT: 0.99,
            CONFIG_QUANTITY_MARKET_MAX_PERCENT: 1,
            CONFIG_QUANTITY_BUY_MARKET_ATTENUATION: 0.2,
            CONFIG_BUY_LIMIT_ORDER_MAX_PERCENT: 1,
            CONFIG_BUY_LIMIT_ORDER_MIN_PERCENT: 0.999
        }


class InvestorModeDecider(AbstractTradingModeDecider):
    def __init__(self, trading_mode, symbol_evaluator, exchange):
        super().__init__(trading_mode, symbol_evaluator, exchange)

    def set_final_eval(self):
        strategies_analysis_note_counter = 0
        # Strategies analysis
        for evaluated_strategies in self.symbol_evaluator.get_strategies_eval_list(self.exchange):
            strategy_eval = evaluated_strategies.get_eval_note()
            if check_valid_eval_note(strategy_eval):
                self.final_eval += strategy_eval * evaluated_strategies.get_pertinence()
                strategies_analysis_note_counter += evaluated_strategies.get_pertinence()

        if strategies_analysis_note_counter > 0:
            self.final_eval /= strategies_analysis_note_counter
        else:
            self.final_eval = INIT_EVAL_NOTE

    def _get_delta_risk(self):
        return self.RISK_THRESHOLD * self.symbol_evaluator.get_trader(self.exchange).get_risk()

    def create_state(self):
        delta_risk = self._get_delta_risk()

        if self.final_eval < self.VERY_LONG_THRESHOLD + delta_risk:
            self._set_state(EvaluatorStates.VERY_LONG)

        elif self.final_eval < self.LONG_THRESHOLD + delta_risk:
            self._set_state(EvaluatorStates.LONG)

        elif self.final_eval < self.NEUTRAL_THRESHOLD - delta_risk:
            self._set_state(EvaluatorStates.NEUTRAL)

        elif self.final_eval < self.SHORT_THRESHOLD - delta_risk:
            self._set_state(EvaluatorStates.SHORT)

        else:
            self._set_state(EvaluatorStates.VERY_SHORT)

    def _set_state(self, new_state):
        if new_state != self.state:
            # previous_state = self.state
            self.state = new_state
            self.logger.info("{0} ** NEW FINAL STATE ** : {1}".format(self.symbol, self.state))

            # if new state is not neutral --> cancel orders and create new else keep orders
            if new_state is not EvaluatorStates.NEUTRAL:

                # cancel open orders
                if self.symbol_evaluator.get_trader(self.exchange).is_enabled():
                    self.symbol_evaluator.get_trader(self.exchange).cancel_open_orders(self.symbol)
                if self.symbol_evaluator.get_trader_simulator(self.exchange).is_enabled():
                    self.symbol_evaluator.get_trader_simulator(self.exchange).cancel_open_orders(self.symbol)

                # create notification
                evaluator_notification = None
                if self.notifier.enabled(CONFIG_NOTIFICATION_PRICE_ALERTS):
                    evaluator_notification = self.notifier.notify_state_changed(
                        self.final_eval,
                        self.symbol_evaluator.get_crypto_currency_evaluator(),
                        self.symbol_evaluator.get_symbol(),
                        self.symbol_evaluator.get_trader(self.exchange),
                        self.state,
                        self.symbol_evaluator.get_matrix(self.exchange).get_matrix())

                # call orders creation method
                self.create_final_state_orders(evaluator_notification, self.trading_mode.get_only_creator_key())
