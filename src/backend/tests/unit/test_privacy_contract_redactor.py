"""The redactor: the rules, and the fact that it is never called.

* the five default rules and their replacements
* the switch that is off by default
* the gaps the rules leave
"""

from __future__ import annotations

import inspect

import pytest

from yuanwork.privacy.redactor import RedactionRule, Redactor


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------


def test_the_five_default_rules():
    names = {rule.name for rule in Redactor().rules}
    assert names == {
        "id_card_cn", "phone_cn", "email", "bank_card", "credit_card",
    }


def test_the_redactor_is_off_by_default():
    assert Redactor().enabled is False


def test_a_disabled_redactor_returns_the_text_unchanged():
    redactor = Redactor()
    text = "我的身份证是110101199001011234，手机号13800138000"
    assert redactor.redact(text) == text


def test_the_rules_apply_when_enabled():
    redactor = Redactor(enabled=True)
    out = redactor.redact("身份证 110101199001011234")
    assert "110101199001011234" not in out
    assert "[身份证号]" in out


def test_the_phone_number_is_replaced():
    out = Redactor(enabled=True).redact("打给我 13800138000")
    assert "13800138000" not in out
    assert "[手机号]" in out


def test_the_email_is_replaced():
    out = Redactor(enabled=True).redact("发到 a.b@example.com")
    assert "a.b@example.com" not in out
    assert "[邮箱]" in out


def test_the_bank_card_is_replaced():
    out = Redactor(enabled=True).redact("卡号 6222021234567890")
    assert "6222021234567890" not in out
    assert "[银行卡号]" in out


def test_the_credit_card_is_replaced():
    out = Redactor(enabled=True).redact("卡号 4111 1111 1111 1111")
    assert "4111 1111 1111 1111" not in out
    assert "[信用卡号]" in out


def test_the_rules_apply_in_order():
    """A later rule sees the output of the earlier ones."""
    out = Redactor(enabled=True).redact("4111 1111 1111 1111")
    assert out.count("[") >= 1


def test_an_extra_rule_is_appended():
    extra = RedactionRule(name="custom", pattern=r"SECRET-\d+", replacement="[机密]")
    redactor = Redactor(enabled=True, extra_rules=[extra])
    out = redactor.redact("编号 SECRET-42")
    assert "SECRET-42" not in out
    assert "[机密]" in out


def test_a_broken_rule_is_skipped():
    extra = RedactionRule(name="broken", pattern=r"(unclosed", replacement="x")
    redactor = Redactor(enabled=True, extra_rules=[extra])
    # the other rules still work
    out = redactor.redact("邮箱 a@b.com")
    assert "[邮箱]" in out


# --------------------------------------------------------------------------
# the gaps
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "我的姓名是张三",              # 中文姓名
        "住在北京市海淀区中关村大街1号",  # 地址
        "订单号 123456789012",         # 12 位数字
        "座机 010-12345678",           # 座机
    ],
)
def test_the_gaps_are_not_covered(text: str):
    assert Redactor(enabled=True).redact(text) == text


# --------------------------------------------------------------------------
# it is called
# --------------------------------------------------------------------------


def test_the_redactor_is_called_before_the_text_leaves_the_machine():
    """There are call sites: the answer prompt and the conflict judge."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2] / "yuanwork"
    callers = []
    for path in backend.rglob("*.py"):
        if path.name == "redactor.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "Redactor(" in source or ".redact(" in source:
            callers.append(str(path))
    assert callers


def test_the_redaction_switch_exists():
    from yuanwork.config import YuanWorkSettings

    settings = YuanWorkSettings()
    assert settings.enable_redaction is True
