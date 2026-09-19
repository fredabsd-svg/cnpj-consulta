"""Testes do normalizador."""

from __future__ import annotations

from datetime import date

from app.core.normalizer import (
    normalize_cep,
    normalize_name,
    normalize_name_for_search,
    normalize_phone,
    normalize_text,
    parse_date,
    parse_decimal,
)


class TestNormalizeText:
    def test_trims_spaces(self):
        assert normalize_text("  Ola  ") == "Ola"

    def test_collapses_internal_spaces(self):
        assert normalize_text("Joao    da    Silva") == "Joao da Silva"

    def test_empty(self):
        assert normalize_text("") is None
        assert normalize_text("   ") is None

    def test_none(self):
        assert normalize_text(None) is None


class TestNormalizeName:
    def test_title_case(self):
        assert normalize_name("JOAO DA SILVA") == "Joao da Silva"

    def test_keeps_prepositions_lowercase(self):
        assert normalize_name("MARIA DE FATIMA") == "Maria de Fatima"

    def test_empty(self):
        assert normalize_name("") is None


class TestNormalizeNameForSearch:
    def test_no_accents_uppercase(self):
        assert normalize_name_for_search("João da Silva") == "JOAO DA SILVA"

    def test_removes_punctuation(self):
        assert normalize_name_for_search("Maria, da Silva!") == "MARIA DA SILVA"


class TestNormalizePhone:
    def test_digits_only(self):
        assert normalize_phone("(11) 98888-7777") == "11988887777"

    def test_empty(self):
        assert normalize_phone("") is None


class TestNormalizeCep:
    def test_digits_only(self):
        assert normalize_cep("01311-902") == "01311902"

    def test_too_short(self):
        assert normalize_cep("01311") is None


class TestParseDate:
    def test_iso(self):
        assert parse_date("2024-01-15") == date(2024, 1, 15)

    def test_iso_with_time(self):
        assert parse_date("2024-01-15T10:30:00Z") == date(2024, 1, 15)

    def test_br_format(self):
        assert parse_date("15/01/2024") == date(2024, 1, 15)

    def test_invalid(self):
        assert parse_date("invalid") is None

    def test_none(self):
        assert parse_date(None) is None

    def test_empty(self):
        assert parse_date("") is None


class TestParseDecimal:
    def test_pt_br_format(self):
        assert parse_decimal("1.234.567,89") == 1234567.89

    def test_simple(self):
        assert parse_decimal("100,50") == 100.50

    def test_int(self):
        assert parse_decimal(42) == 42.0

    def test_float(self):
        assert parse_decimal(3.14) == 3.14

    def test_invalid(self):
        assert parse_decimal("invalid") is None

    # Regressao: "1000.00" (ReceitaWS) virava 100000.0
    def test_dot_decimal_from_api(self):
        assert parse_decimal("1000.00") == 1000.0
        assert parse_decimal("150000.00") == 150000.0

    def test_rfb_comma_decimal(self):
        assert parse_decimal("1000,00") == 1000.0

    def test_thousands_dots_only(self):
        assert parse_decimal("1.234.567") == 1234567.0

    def test_us_format(self):
        assert parse_decimal("1,234,567.89") == 1234567.89

    def test_currency_prefix(self):
        assert parse_decimal("R$ 1.500,00") == 1500.0

    def test_bool_is_not_number(self):
        assert parse_decimal(True) is None


class TestParseDateRfb:
    def test_rfb_compact_format(self):
        assert parse_date("20200101") == date(2020, 1, 1)

    def test_rfb_zero_date(self):
        assert parse_date("00000000") is None


class TestNonStringInputs:
    def test_normalize_text_accepts_int(self):
        assert normalize_text(37) == "37"

    def test_cep_int_keeps_leading_zero(self):
        assert normalize_cep(1311902) == "01311902"
