import asyncio
from decimal import Decimal
from typing import Any, Dict, Optional

from hummingbot.connector.gateway.gateway_base import GatewayBase
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.utils import async_ttl_cache
from hummingbot.core.utils.async_utils import safe_ensure_future


class GatewaySwap(GatewayBase):
    """
    Handles swap-specific functionality including price quotes and trade execution.
    Maintains order tracking and wallet interactions in the base class.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize transaction handler
        self._tx_handler = None

    @property
    def tx_handler(self):
        if self._tx_handler is None:
            self._tx_handler = self._get_gateway_instance()
        return self._tx_handler

    @async_ttl_cache(ttl=5, maxsize=10)
    async def get_quote_price(
            self,
            trading_pair: str,
            is_buy: bool,
            amount: Decimal,
            slippage_pct: Optional[Decimal] = None,
            pool_address: Optional[str] = None
    ) -> Optional[Decimal]:
        """
        Retrieves the volume weighted average price. For an AMM DEX connectors, this is the swap price for a given amount.

        :param trading_pair: The market trading pair
        :param is_buy: True for an intention to buy, False for an intention to sell
        :param amount: The amount required (in base token unit)
        :return: The quote price.
        """
        base, quote = trading_pair.split("-")
        side: TradeType = TradeType.BUY if is_buy else TradeType.SELL

        # Pull the price from gateway.
        try:
            # Build request parameters
            from hummingbot.connector.gateway.common_types import ConnectorType, get_connector_type

            connector_type = get_connector_type(self.connector_name)
            request_payload = {
                "network": self.network,
                "baseToken": base,
                "quoteToken": quote,
                "amount": float(amount),
                "side": side.name
            }
            if slippage_pct is not None:
                request_payload["slippagePct"] = float(slippage_pct)
            if connector_type in (ConnectorType.CLMM, ConnectorType.AMM) and pool_address is not None:
                request_payload["poolAddress"] = pool_address

            resp: Dict[str, Any] = await self._get_gateway_instance().connector_request(
                "get",
                self.connector_name,
                "quote-swap",
                request_payload
            )
            return self.parse_price_response(base, quote, amount, side, price_response=resp)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().network(
                f"Error getting quote price for {trading_pair} {side} order for {amount} amount.",
                exc_info=True,
                app_warning_msg=str(e)
            )

    async def get_order_price(
            self,
            trading_pair: str,
            is_buy: bool,
            amount: Decimal,
    ) -> Decimal:
        """
        Retreives the price required for an order of a given amount. For AMM DEX connectors, this equals the quote price.
        """
        return await self.get_quote_price(trading_pair, is_buy, amount)

    def parse_price_response(
        self,
        base: str,
        quote: str,
        amount: Decimal,
        side: TradeType,
        price_response: Dict[str, Any],
        process_exception: bool = True
    ) -> Optional[Decimal]:
        """
        Parses price response
        :param base: The base asset
        :param quote: The quote asset
        :param amount: amount
        :param side: trade side
        :param price_response: Price response from Gateway.
        :param process_exception: Flag to trigger error on exception
        """
        # Check if we have a valid price in the response
        if "price" not in price_response:
            if "info" in price_response.keys():
                self.logger().info(f"Unable to get price. {price_response['info']}")
            else:
                self.logger().info(f"Missing price in response. Response keys: {price_response.keys()}")
            return None

        price: Decimal = Decimal(str(price_response["price"]))
        return price

    def buy(self, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal, **kwargs) -> str:
        """
        Buys an amount of base token for a given price (or cheaper).
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param order_type: Any order type is fine, not needed for this.
        :param price: The maximum price for the order.
        :return: A newly created order id (internal).
        """
        return self.place_order(True, trading_pair, amount, price)

    def sell(self, trading_pair: str, amount: Decimal, order_type: OrderType, price: Decimal, **kwargs) -> str:
        """
        Sells an amount of base token for a given price (or at a higher price).
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param order_type: Any order type is fine, not needed for this.
        :param price: The minimum price for the order.
        :return: A newly created order id (internal).
        """
        return self.place_order(False, trading_pair, amount, price)

    def place_order(self, is_buy: bool, trading_pair: str, amount: Decimal, price: Decimal, **request_args) -> str:
        """
        Places an order.
        :param is_buy: True for buy order
        :param trading_pair: The market trading pair
        :param amount: The order amount (in base token unit)
        :param price: The minimum price for the order.
        :return: A newly created order id (internal).
        """
        side: TradeType = TradeType.BUY if is_buy else TradeType.SELL
        order_id: str = self.create_market_order_id(side, trading_pair)
        safe_ensure_future(self._create_order(side, order_id, trading_pair, amount, price, **request_args))
        return order_id

    async def _create_order(
            self,
            trade_type: TradeType,
            order_id: str,
            trading_pair: str,
            amount: Decimal,
            price: Decimal
    ):
        """
        Calls buy or sell API end point to place an order, starts tracking the order and triggers relevant order events.
        :param trade_type: BUY or SELL
        :param order_id: Internal order id (also called client_order_id)
        :param trading_pair: The market to place order
        :param amount: The order amount (in base token value)
        :param price: The order price (TO-DO: add limit_price to Gateway execute-swap schema)
        """

        amount = self.quantize_order_amount(trading_pair, amount)
        price = self.quantize_order_price(trading_pair, price)

        base, quote = trading_pair.split("-")
        self.start_tracking_order(order_id=order_id,
                                  trading_pair=trading_pair,
                                  trade_type=trade_type,
                                  price=price,
                                  amount=amount)

        try:
            # Execute transaction with retry logic (non-blocking)
            await self.tx_handler.execute_transaction(
                chain=self.chain,
                network=self.network,
                connector=self.connector_name,
                method="execute-swap",
                params={
                    "walletAddress": self.address,
                    "baseToken": base,
                    "quoteToken": quote,
                    "amount": float(amount),
                    "side": trade_type.name,
                },
                order_id=order_id,
                gateway_connector=self
            )

            # Transaction executes in background
            self.logger().info(f"Swap order {order_id} submitted")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._handle_operation_failure(order_id, trading_pair, f"submitting {trade_type.name} swap order", e)
