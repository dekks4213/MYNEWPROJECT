"""Every user-facing key exists in both Russian and English."""

import re
from pathlib import Path

from fitcoach.domain.fields import FieldType
from fitcoach.i18n import CATALOGS
from fitcoach.services.users import GOALS

SRC = Path(__file__).resolve().parents[1] / "src" / "fitcoach"
CODE_RE = re.compile(r"(?:ServiceError|ParseError|Conflict)\(\"([a-z_]+)\"\)")
AI_RE = re.compile(r"AIUnavailableError\(\"([a-z_]+)\"\)")
KEY_RE = re.compile(r"\btr\(\s*\"([a-z_]+(?:\.[a-z_]+)*)\"")


def _source() -> str:
    return "\n".join(p.read_text("utf-8") for p in SRC.rglob("*.py"))


def required_keys() -> set[str]:
    src = _source()
    keys = set(KEY_RE.findall(src))
    keys |= {f"err.{c}" for c in CODE_RE.findall(src)} | {"err.not_found", "err.conflict"}
    keys |= {f"ai.{c}" for c in AI_RE.findall(src)}
    keys |= {f"ftype.{t.value}" for t in FieldType} | {f"goal.{g}" for g in GOALS}
    keys |= {f"precision.{p}" for p in ("measured", "approximate", "unknown")}
    keys |= {
        "menu.food",
        "menu.weight",
        "menu.training",
        "menu.today",
        "menu.fix",
        "menu.settings",
        "hint.decimal",
        "hint.integer",
        "hint.boolean",
        "hint.text",
        "hint.selection",
        "hint.duration_mmss",
        "unit.kcal",
        "unit.g",
        "unit.kg",
    }
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


def test_menu_labels_are_unique() -> None:
    for catalog in CATALOGS.values():
        labels = [v for k, v in catalog.items() if k.startswith("menu.")]
        assert len(labels) == len(set(labels))
