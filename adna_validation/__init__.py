"""
ADNA validation service: the AgentDNA checks for gateways that can't run them
themselves. gate.py shows what a gateway sends and gets back.
"""

from adna_validation.gate import answered, check, create_app

__all__ = ["answered", "check", "create_app"]
