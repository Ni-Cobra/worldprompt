"""The Fabric mod's mirrored Core limits must equal the constants in ``limits.py``."""
import re
from pathlib import Path

from worldprompt_core import limits

CORE_LIMITS = Path(__file__).resolve().parents[1] / "fabric/src/main/java/dev/worldprompt/CoreLimits.java"


def mirrored_constants():
    values = {}
    for name, expression in re.findall(r"static final int (\w+) = ([^;]+);", CORE_LIMITS.read_text(encoding="utf-8")):
        # Only integer literals, earlier constants and * / arithmetic, as Java int expressions.
        expression = re.sub(r"(?<=\d)_(?=\d)", "", expression).replace("/", "//")
        assert re.fullmatch(r"[\w\s*/+-]+", expression), name
        values[name] = eval(expression, {"__builtins__": {}}, dict(values))
    return values


def test_mod_limits_match_core():
    mirrored = mirrored_constants()
    shared = {name: value for name, value in mirrored.items() if hasattr(limits, name)}
    assert len(shared) >= 12
    assert shared == {name: getattr(limits, name) for name in shared}


def test_mod_prompt_characters_fit_core_bytes():
    assert mirrored_constants()["MAX_PROMPT_CHARACTERS"] * 4 <= limits.MAX_PROMPT_BYTES
