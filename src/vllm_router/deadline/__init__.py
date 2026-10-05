"""Deadline routing pilot for the production-stack gateway.

This package is an independently authored exercise extension. Its public
contract is in docs/deadline-routing-contract.md, not an upstream guarantee.
"""
from .gateway import DeadlineGateway
from .clock import ManualClock, MonotonicClock

__all__ = ["DeadlineGateway", "ManualClock", "MonotonicClock"]
