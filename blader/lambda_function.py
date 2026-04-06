"""Lambda entry point for Blader."""

from blader.blader import agent
from mailroom.poll import make_lambda_handler

lambda_handler = make_lambda_handler(agent)
