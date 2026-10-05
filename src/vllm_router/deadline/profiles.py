"""Per-engine calibrated prefill surfaces and tokenizer adapters."""
import bisect
import math
from decimal import Decimal, ROUND_HALF_UP


def rounded_us(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def bracket(nodes, value):
    index = min(max(bisect.bisect_right(nodes, value) - 1, 0), len(nodes) - 2)
    left, right = nodes[index:index + 2]
    return index, (Decimal(str(value)) - Decimal(str(left))) / (Decimal(str(right)) - Decimal(str(left)))


class Surface:
    def __init__(self, specification):
        self.tokens = specification["tokens"]
        self.decodes = specification["decodes"]
        self.values = specification["service_us"]
        if any(len(x) < 2 or x[0] != 0 or any(a >= b for a, b in zip(x, x[1:]))
               for x in (self.tokens, self.decodes)):
            raise ValueError("Calibration axes must start at zero and strictly increase")
        if len(self.values) != len(self.decodes) or any(len(row) != len(self.tokens) for row in self.values):
            raise ValueError("Calibration surface shape does not match its axes")
        if any(not math.isfinite(v) or v < 0 for row in self.values for v in row):
            raise ValueError("Calibration values must be finite and nonnegative")

    def estimate(self, uncached_tokens, decodes):
        x, _ = bracket(self.tokens, max(0, uncached_tokens))
        y, _ = bracket(self.decodes, max(0, decodes))
        return float(self.values[y][x])


class Tokenizers:
    """Small local fixture tokenizer, independent of client length hints.

    The codepoint and UTF-8 byte vocabularies deliberately differ for non-ASCII
    prompts. Production can supply another async encode implementation.
    """
    async def encode(self, version, prompt):
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        if version == "codepoint-v1":
            return [ord(char) for char in prompt]
        if version == "utf8-v1":
            return list(prompt.encode("utf-8"))
        raise ValueError("Unknown tokenizer version")
