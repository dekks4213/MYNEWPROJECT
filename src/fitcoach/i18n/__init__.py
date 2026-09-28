"""Minimal dictionary-based localization. Russian is the default language."""

from __future__ import annotations

from fitcoach.i18n import en, ru

DEFAULT_LANGUAGE = "ru"
CATALOGS: dict[str, dict[str, str]] = {"ru": ru.TEXTS, "en": en.TEXTS}


class Translator:
    def __init__(self, language: str) -> None:
        self.language = language if language in CATALOGS else DEFAULT_LANGUAGE
        self._texts = CATALOGS[self.language]

    def __call__(self, key: str, **kwargs: object) -> str:
        template = self._texts.get(key) or CATALOGS[DEFAULT_LANGUAGE].get(key) or key
        return template.format(**kwargs) if kwargs else template

    def has(self, key: str) -> bool:
        return key in self._texts


def all_labels(key: str) -> set[str]:
    """A key's text in every language, for matching reply-keyboard buttons."""
    return {catalog[key] for catalog in CATALOGS.values() if key in catalog}
