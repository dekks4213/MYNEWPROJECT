"""Every user-facing key exists in both Russian and English."""

import re
from pathlib import Path

from fitcoach.bot.handlers.training import MEASURES, OTHER_MEASURES
from fitcoach.bot.ui import (
    EFFORT_PRESETS,
    LEGACY_MENU,
    MENU_KEYS,
    REMINDER_PRESETS,
    TIMEZONES,
)
from fitcoach.domain.food import MealType, Unit
from fitcoach.domain.starters import STARTER_KINDS
from fitcoach.domain.workout import BlockKind
from fitcoach.i18n import CATALOGS
from fitcoach.services.reminders import KINDS
from fitcoach.services.users import GOALS

SRC = Path(__file__).resolve().parents[1] / "src" / "fitcoach"
CODE_RE = re.compile(r"(?:ServiceError|ParseError|Conflict)\(\s*\"([a-z_]+)\"")
AI_RE = re.compile(r"AIUnavailableError\(\s*\"([a-z_]+)\"")
KEY_RE = re.compile(r"\btr\(\s*\"([a-z_]+(?:\.[a-z_]+)*)\"(?!\s*\+)")  # prefix+var: skip
STARTER_RE = re.compile(r"\bt\(\s*\"((?:starter|unit)\.[a-z_.]+)\"")


def _source() -> str:
    return "\n".join(p.read_text("utf-8") for p in SRC.rglob("*.py"))


def required_keys() -> set[str]:
    src = _source()
    keys = set(KEY_RE.findall(src)) | set(STARTER_RE.findall(src))
    keys |= {f"err.{c}" for c in CODE_RE.findall(src)} | {"err.not_found", "err.conflict"}
    keys |= {f"ai.{c}" for c in AI_RE.findall(src)}
    keys |= {f"goal.{g}" for g in GOALS}
    keys |= {f"meal.{m.value}" for m in MealType} | {f"funit.{u.value}" for u in Unit}
    keys |= {f"block.{b.value}" for b in BlockKind}
    keys |= {f"starter.{k.value}" for k in STARTER_KINDS}
    keys |= {f"rem.kind.{k}" for k in KINDS} | {f"rem.msg.{k}" for k in KINDS if k != "custom"}
    keys |= {f"wd.{d}" for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    keys |= {f"macro.{m}" for m in "PFC"} | {f"month.{m}" for m in range(1, 13)}
    keys |= {f"settings.media_{x}" for x in ("on", "off")}
    keys |= {f"settings.ai_text_btn_{x}" for x in ("on", "off")}
    keys |= {f"rem.turn_{x}" for x in ("on", "off")}
    keys |= {f"measure.{m}" for m in (*MEASURES, *OTHER_MEASURES)}
    keys |= {f"measure.{m}.label" for m in (*MEASURES, *OTHER_MEASURES) if m != "other"}
    keys |= {f"recent.btn.{n}" for n in range(3)} | {f"meal_l.{m.value}" for m in MealType}
    keys |= {
        f"home.{p}{s}" for p in ("morning", "afternoon", "evening", "night") for s in ("", "_named")
    }
    keys |= {key for _, key in EFFORT_PRESETS} | {key for key, _ in REMINDER_PRESETS}
    keys |= (
        {key for _, key in TIMEZONES}
        | set(MENU_KEYS)
        | set(LEGACY_MENU)
        | {
            "hint.decimal",
            "hint.integer",
            "hint.text",
            "hint.duration_mmss",
            "hint.duration_hmm",
            "food.text_ask",
            "food.text_ask_ai",
            "wo.text_ask",
            "wo.text_ask_ai",
            "imp.unit_kg",
            "imp.unit_lb",
            "imp.unit_unknown",
        }
    )
    return keys


def test_all_keys_present_in_every_language() -> None:
    required = required_keys()
    for lang, catalog in CATALOGS.items():
        missing = sorted(required - catalog.keys())
        assert not missing, f"{lang} missing: {missing}"


def test_catalogs_have_identical_keys_and_placeholders() -> None:
    ru, en = CATALOGS["ru"], CATALOGS["en"]
    assert ru.keys() == en.keys()
    for key in ru:
        assert set(re.findall(r"\{(\w+)\}", ru[key])) == set(re.findall(r"\{(\w+)\}", en[key])), key


def test_menu_labels_are_unique_and_branding_is_configurable() -> None:
    for catalog in CATALOGS.values():
        labels = [v for k, v in catalog.items() if k.startswith("menu.")]
        assert len(labels) == len(set(labels))
    assert "РИТМ" in CATALOGS["ru"]["ob.hello"] and "RITM" in CATALOGS["en"]["ob.hello"]


def test_no_guilt_language() -> None:
    banned = ("провал", "плохо поел", "лень", "накаж", "failure", "lazy", "punish", "cheat day")
    for catalog in CATALOGS.values():
        for text in catalog.values():
            assert not any(b in text.lower() for b in banned), text


def test_no_orphan_keys() -> None:
    """Every catalog key is used by the code (or is a dynamic family listed above)."""
    source = _source()
    literal = set(re.findall(r"""["']([a-z_0-9]+(?:\.[a-z_0-9]+)+)["']""", source))
    families = ("err.", "ai.", "funit.", "block.", "starter.", "unit.", "fmt.", "word.", "rem.msg.")
    required = required_keys()
    orphans = [
        k
        for k in CATALOGS["ru"]
        if "." in k and k not in literal and k not in required and not k.startswith(families)
    ]
    assert not orphans, orphans
