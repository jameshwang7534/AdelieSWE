"""Normalize snake/camel identifiers, acronyms, paths and ordinary text consistently."""

import re
import unicodedata


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", text)
    text = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", text)
    return re.findall(r"[^\W_]+", text.casefold())
