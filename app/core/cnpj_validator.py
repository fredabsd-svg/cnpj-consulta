"""Validacao matematica do CNPJ (numerico e alfanumerico).

Referencias:
- https://www.gov.br/receitafederal/pt-br/assuntos/cnpj
- IN RFB 2.229/2024: a partir de julho/2026 a raiz (8 posicoes) e a ordem
  (4 posicoes) do CNPJ podem conter letras A-Z; os 2 digitos verificadores
  continuam numericos.

Algoritmo do DV (unico para os dois formatos): cada caractere vale
``ord(c) - 48`` (digitos 0-9 valem 0-9; letras A-Z valem 17-42) e aplica-se
o modulo 11 com os pesos tradicionais. Para CNPJs so numericos o resultado
e identico ao algoritmo antigo.

Este modulo NAO consulta nenhuma fonte externa -- apenas valida o formato.
"""

from __future__ import annotations

import re

# 12 posicoes alfanumericas (raiz + ordem) + 2 digitos verificadores.
_CNPJ_PATTERN = re.compile(r"^[A-Z0-9]{12}\d{2}$")

# Pesos para calculo do DV.
_WEIGHTS_DV1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_WEIGHTS_DV2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def strip(cnpj: str | None) -> str:
    """Remove mascara (pontos, barra, hifen). Retorna string vazia se None."""
    if cnpj is None:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", cnpj).upper()


def format(cnpj: str) -> str:
    """Aplica mascara XX.XXX.XXX/XXXX-XX."""
    s = strip(cnpj)
    if len(s) != 14:
        raise ValueError(f"CNPJ deve ter 14 caracteres, recebido {len(s)}: {cnpj!r}")
    return f"{s[:2]}.{s[2:5]}.{s[5:8]}/{s[8:12]}-{s[12:14]}"


def is_well_formed(cnpj: str | None) -> bool:
    """True se CNPJ esta em formato valido (com ou sem mascara).

    Nao confere digitos verificadores.
    """
    return bool(_CNPJ_PATTERN.match(strip(cnpj)))


def is_alphanumeric(cnpj: str | None) -> bool:
    """True se o CNPJ usa o novo formato com letras."""
    s = strip(cnpj)
    return is_well_formed(s) and not s.isdigit()


def _calc_dv(chars: str, weights: tuple[int, ...]) -> int:
    """Calcula um digito verificador (valor de cada caractere = ASCII - 48)."""
    total = sum((ord(c) - 48) * w for c, w in zip(chars, weights, strict=True))
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def is_valid(cnpj: str | None) -> bool:
    """True se CNPJ e bem-formado E tem digitos verificadores corretos."""
    s = strip(cnpj)
    if not is_well_formed(s):
        return False
    # Rejeita sequencias repetidas (000..., 111..., ...)
    if len(set(s)) == 1:
        return False
    base = s[:12]
    dv1 = _calc_dv(base, _WEIGHTS_DV1)
    dv2 = _calc_dv(base + str(dv1), _WEIGHTS_DV2)
    return s[12:14] == f"{dv1}{dv2}"


def normalize(cnpj: str | None) -> str:
    """Retorna o CNPJ normalizado (14 caracteres, sem mascara, maiusculo).

    Levanta ValueError se invalido.
    """
    if not is_valid(cnpj):
        raise ValueError(f"CNPJ invalido: {cnpj!r}")
    return strip(cnpj)


def normalize_or_none(cnpj: str | None) -> str | None:
    """Versao que retorna None em vez de levantar excecao."""
    try:
        return normalize(cnpj)
    except ValueError:
        return None
