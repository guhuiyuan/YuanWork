"""Redactor - mask sensitive information before sending to LLM."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RedactionRule:
    """A regex-based redaction rule."""

    name: str
    pattern: str
    replacement: str


class Redactor:
    """Mask sensitive information in text."""

    DEFAULT_RULES = [
        RedactionRule(
            name="id_card_cn",
            pattern=r"\b[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
            replacement="[身份证号]",
        ),
        RedactionRule(
            name="phone_cn",
            pattern=r"\b1[3-9]\d{9}\b",
            replacement="[手机号]",
        ),
        RedactionRule(
            name="email",
            pattern=r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
            replacement="[邮箱]",
        ),
        RedactionRule(
            name="bank_card",
            pattern=r"\b\d{16,19}\b",
            replacement="[银行卡号]",
        ),
        RedactionRule(
            name="credit_card",
            pattern=r"\b(?:\d{4}[-\s]?){3}\d{4}\b",
            replacement="[信用卡号]",
        ),
    ]

    def __init__(self, enabled: bool = False, extra_rules: list[RedactionRule] | None = None):
        self.enabled = enabled
        self.rules = self.DEFAULT_RULES + (extra_rules or [])

    def redact(self, text: str) -> str:
        """Apply all redaction rules to text."""
        if not self.enabled:
            return text

        redacted = text
        for rule in self.rules:
            try:
                redacted = re.sub(rule.pattern, rule.replacement, redacted)
            except Exception as e:
                logger.warning(f"Redaction rule '{rule.name}' failed: {e}")

        return redacted
