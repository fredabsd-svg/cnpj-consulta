"""Testes do validador de CNPJ."""

from __future__ import annotations

import pytest

from app.core.cnpj_validator import (
    format,
    is_valid,
    is_well_formed,
    normalize,
    normalize_or_none,
    strip,
)


class TestStrip:
    def test_strip_with_mask(self):
        assert strip("19.131.243/0001-97") == "19131243000197"

    def test_strip_without_mask(self):
        assert strip("19131243000197") == "19131243000197"

    def test_strip_lowercase(self):
        assert strip("ab") == "AB"


class TestFormat:
    def test_format_valid(self):
        assert format("19131243000197") == "19.131.243/0001-97"

    def test_format_invalid_length(self):
        with pytest.raises(ValueError):
            format("123")


class TestIsWellFormed:
    def test_with_mask(self):
        assert is_well_formed("19.131.243/0001-97") is True

    def test_without_mask(self):
        assert is_well_formed("19131243000197") is True

    def test_too_short(self):
        assert is_well_formed("123") is False

    def test_too_long(self):
        assert is_well_formed("191312430001970") is False

    def test_with_letters_invalid(self):
        assert is_well_formed("AB") is False

    def test_empty(self):
        assert is_well_formed("") is False

    def test_none(self):
        assert is_well_formed(None) is False


class TestIsValid:
    """Testa o DV matematico."""

    def test_valid_cnpj_okb(self):
        # Open Knowledge Brasil
        assert is_valid("19.131.243/0001-97") is True
        assert is_valid("19131243000197") is True

    def test_invalid_dv(self):
        assert is_valid("19.131.243/0001-00") is False

    def test_all_zeros(self):
        assert is_valid("00000000000000") is False

    def test_all_same_digit(self):
        assert is_valid("11111111111111") is False

    def test_empty(self):
        assert is_valid("") is False

    def test_none(self):
        assert is_valid(None) is False

    def test_short(self):
        assert is_valid("123") is False


class TestAlphanumeric:
    """CNPJ alfanumerico (IN RFB 2.229/2024, vigente desde jul/2026)."""

    def test_official_example_valid(self):
        # Exemplo publicado pela Receita Federal
        assert is_valid("12.ABC.345/01DE-35") is True
        assert is_valid("12ABC34501DE35") is True

    def test_lowercase_is_accepted(self):
        assert normalize("12.abc.345/01de-35") == "12ABC34501DE35"

    def test_wrong_dv(self):
        assert is_valid("12ABC34501DE36") is False

    def test_letters_in_dv_rejected(self):
        assert is_well_formed("12ABC34501DEAB") is False

    def test_format_alphanumeric(self):
        assert format("12ABC34501DE35") == "12.ABC.345/01DE-35"


class TestNormalize:
    def test_normalize_valid(self):
        assert normalize("19.131.243/0001-97") == "19131243000197"

    def test_normalize_invalid_raises(self):
        with pytest.raises(ValueError):
            normalize("123")

    def test_normalize_or_none_valid(self):
        assert normalize_or_none("19131243000197") == "19131243000197"

    def test_normalize_or_none_invalid(self):
        assert normalize_or_none("invalid") is None
