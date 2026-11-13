"""
ADNA validation service: the AgentDNA checks for gateways that can't run them
themselves. validator.py shows what a gateway sends and gets back.
"""

from adna_validation.validator import answered, check, create_app

__all__ = ["answered", "check", "create_app"]
