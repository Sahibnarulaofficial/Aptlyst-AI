"""Background worker processes for Aptlyst AI."""

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.workers.razorpay_webhook_worker import RazorpayWebhookWorker
    from app.workers.stripe_webhook_worker import StripeWebhookWorker, run_worker

__all__ = ["RazorpayWebhookWorker", "StripeWebhookWorker", "run_worker"]


def __getattr__(name: str) -> Any:
    if name == "RazorpayWebhookWorker":
        from app.workers import razorpay_webhook_worker

        return getattr(razorpay_webhook_worker, name)
    if name in ("StripeWebhookWorker", "run_worker"):
        from app.workers import stripe_webhook_worker

        return getattr(stripe_webhook_worker, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
