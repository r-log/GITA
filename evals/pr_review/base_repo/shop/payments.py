"""Charging customers through the payment provider."""

import random
import time


class PaymentError(Exception):
    pass


class TransientPaymentError(PaymentError):
    pass


def charge(client, amount_cents: int, card_token: str, *, max_attempts: int = 3) -> str:
    """Charge ``amount_cents``; return the provider's charge id.

    Retries transient provider errors with jittered backoff. Never retries
    a declined card.
    """
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    for attempt in range(1, max_attempts + 1):
        try:
            return client.create_charge(amount=amount_cents, source=card_token)
        except TransientPaymentError:
            if attempt == max_attempts:
                raise
            time.sleep(min(8.0, 0.5 * 2**attempt) * random.uniform(0.5, 1.0))
    raise AssertionError("unreachable")
