"""Explicit shared service_config test fixtures."""

import tomllib


from super_xd.nlp.defaults import ROOT


def document():
    with (ROOT / "config/services.toml").open("rb") as source:
        return tomllib.load(source)
