"""CPF completo de socios e titulares, so para o administrador.

A Receita publica o CPF de socio pessoa fisica mascarado (***.456.789-**): o
numero completo NAO existe na base publica e o app nunca o deduz. Ele so passa
a existir aqui quando:

- o administrador o cadastra a partir de documentos do proprio cliente
  (contrato social, RG...), com fonte e finalidade; ou
- uma fonte externa consultada (API) o devolve completo.

Em ambos os casos o CPF fica num "cofre": criptografado no banco do app (chave
em data/cpf.key ou CPF_CHAVE no .env), mostrado so ao administrador e com cada
visualizacao registrada na auditoria (sem o numero). Ao cadastrar, o app confere
se o CPF bate com os digitos que a Receita publica para aquele socio.

O CPF do titular de empresario individual/MEI e outra coisa: a Receita o publica
completo dentro da razao social; aqui ele so e extraido como campo proprio.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.config import get_settings
from app.core.normalizer import normalize_name_for_search
from app.db import get_session_factory, init_database
from app.models.base_cnpj import SocioCpf

NATUREZA_EMPRESARIO_INDIVIDUAL = 2135  # 213-5 Empresario (Individual), inclusive MEI
_MASCARA = re.compile(r"\*{3}\.?(\d{3})\.?(\d{3})-?\*{2}")
_CPF_FIM_RAZAO = re.compile(r"(?<!\d)(\d{11})\s*$")
_lock = threading.Lock()


class CpfInvalido(ValueError):
    pass


# ---------------------------------------------------------------- utilitarios


def so_digitos(cpf: str | None) -> str:
    return re.sub(r"\D", "", cpf or "")


def cpf_valido(cpf: str | None) -> bool:
    d = so_digitos(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        dv = (soma * 10) % 11 % 10
        if dv != int(d[n]):
            return False
    return True


def formatar(cpf: str) -> str:
    d = so_digitos(cpf)
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else cpf


def mascarar(cpf: str) -> str:
    """Como a Receita publica: so os digitos 4 a 9."""
    d = so_digitos(cpf)
    return f"***.{d[3:6]}.{d[6:9]}-**" if len(d) == 11 else "***"


def digitos_visiveis(documento_publicado: str | None) -> str | None:
    """'***456789**' ou '***.456.789-**' -> '456789'."""
    m = _MASCARA.fullmatch((documento_publicado or "").strip())
    return m[1] + m[2] if m else None


def chave_pessoa(nome: str | None, documento_publicado: str | None) -> str:
    """Identifica a pessoa como a Receita a publica: nome + digitos visiveis."""
    return f"{normalize_name_for_search(nome) or ''}|{digitos_visiveis(documento_publicado) or ''}"


def cpf_do_titular(razao_social: str | None, natureza: int | None) -> str | None:
    """CPF do titular de empresario individual/MEI, publicado no fim da razao social."""
    if natureza is not None and int(natureza) != NATUREZA_EMPRESARIO_INDIVIDUAL:
        return None
    m = _CPF_FIM_RAZAO.search(razao_social or "")
    if not m:
        return None
    # Com a natureza 213-5 confirmada, os 11 digitos finais SAO o CPF (convencao da
    # Receita); sem ela, so aceita numero com digito verificador valido.
    return formatar(m[1]) if natureza is not None or cpf_valido(m[1]) else None


# ---------------------------------------------------------------- criptografia


def _arquivo_chave() -> Path:
    return Path(get_settings().receita_data_dir).expanduser().parent / "cpf.key"


def _chave() -> bytes:
    configurada = get_settings().cpf_chave.strip()
    if configurada:
        return configurada.encode()
    path = _arquivo_chave()
    with _lock:
        try:
            atual = path.read_bytes().strip()
            if atual:
                return atual
        except OSError:
            pass
        nova = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(nova + b"\n")
        return nova


def _cifrar(cpf: str) -> str:
    return Fernet(_chave()).encrypt(so_digitos(cpf).encode()).decode()


def _decifrar(token: str) -> str | None:
    try:
        return Fernet(_chave()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _indice(cpf: str) -> str:
    """HMAC do CPF: permite achar pelo numero sem guarda-lo em claro."""
    return hmac.new(_chave(), b"cpf|" + so_digitos(cpf).encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- cofre


def _publico(r: SocioCpf) -> dict[str, Any]:
    """Registro SEM o numero completo (para listas)."""
    return {"id": r.id, "nome": r.nome, "cpf_mascarado": r.cpf_mascarado, "fonte": r.fonte,
            "finalidade": r.finalidade, "origem": r.origem, "conferido": r.conferido,
            "criado_em": r.criado_em.isoformat() if r.criado_em else None}


def cadastrar(nome: str, cpf: str, *, fonte: str, finalidade: str, documento_publicado: str | None = None,
              origem: str = "manual") -> dict[str, Any]:
    """Guarda o CPF completo de uma pessoa. Confere o digito verificador e, se a
    Receita publica os digitos 4-9 dessa pessoa, que eles batem."""
    nome = (nome or "").strip()
    fonte = (fonte or "").strip()
    finalidade = (finalidade or "").strip()
    if not nome:
        raise CpfInvalido("Informe o nome da pessoa.")
    if not cpf_valido(cpf):
        raise CpfInvalido("CPF inválido (confira os 11 dígitos e os dígitos verificadores).")
    if len(fonte) < 3:
        raise CpfInvalido("Informe a fonte do CPF (ex.: contrato social do cliente, RG).")
    if len(finalidade) < 10:
        raise CpfInvalido("Informe a finalidade do uso (mínimo 10 caracteres).")
    visiveis = digitos_visiveis(documento_publicado)
    if visiveis and so_digitos(cpf)[3:9] != visiveis:
        raise CpfInvalido(f"Este CPF não confere com o publicado pela Receita para {nome} "
                          f"({documento_publicado}). Confira o número e a pessoa.")
    pessoa = f"{normalize_name_for_search(nome) or ''}|{so_digitos(cpf)[3:9]}"
    init_database()
    with get_session_factory()() as s:
        r = s.scalars(select(SocioCpf).where(SocioCpf.pessoa == pessoa)).first() or SocioCpf(pessoa=pessoa)
        r.nome = nome[:200]
        r.cpf_cifrado = _cifrar(cpf)
        r.cpf_indice = _indice(cpf)
        r.cpf_mascarado = mascarar(cpf)
        r.fonte = fonte[:300]
        r.finalidade = finalidade[:300]
        r.origem = origem
        r.conferido = bool(visiveis)
        r.criado_em = datetime.now()
        s.add(r)
        s.commit()
        return _publico(r)


def remover(registro_id: int) -> dict[str, Any]:
    with get_session_factory()() as s:
        r = s.get(SocioCpf, registro_id)
        if r is None:
            raise LookupError(registro_id)
        info = _publico(r)
        s.delete(r)
        s.commit()
        return info


def listar(limite: int = 500) -> list[dict[str, Any]]:
    init_database()
    with get_session_factory()() as s:
        q = select(SocioCpf).order_by(SocioCpf.nome).limit(limite)
        return [_publico(r) for r in s.scalars(q)]


def revelar(pessoas: list[tuple[str | None, str | None]]) -> dict[str, dict[str, Any]]:
    """Para cada (nome, documento publicado) com CPF no cofre: {chave: {cpf, fonte, ...}}.
    Quem chama registra a visualizacao na auditoria."""
    chaves = {chave_pessoa(n, d) for n, d in pessoas if digitos_visiveis(d)}
    if not chaves:
        return {}
    init_database()
    with get_session_factory()() as s:
        out = {}
        for r in s.scalars(select(SocioCpf).where(SocioCpf.pessoa.in_(chaves))):
            numero = _decifrar(r.cpf_cifrado)
            if numero:
                out[r.pessoa] = {**_publico(r), "cpf": formatar(numero)}
        return out


def por_cpf(cpf: str) -> dict[str, Any] | None:
    if not cpf_valido(cpf):
        return None
    init_database()
    with get_session_factory()() as s:
        r = s.scalars(select(SocioCpf).where(SocioCpf.cpf_indice == _indice(cpf))).first()
        return {**_publico(r), "cpf": formatar(cpf)} if r else None


def capturar_de_fontes(cnpj: str, fontes: list[tuple[str, list[dict]]]) -> int:
    """Guarda no cofre o CPF completo que uma fonte externa devolveu.
    `fontes`: [(provedor, [{nome, documento}])]. Retorna quantos foram guardados."""
    n = 0
    for provedor, socios in fontes:
        for sc in socios:
            doc = so_digitos(str(sc.get("documento") or ""))
            nome = str(sc.get("nome") or "")
            if len(doc) == 11 and cpf_valido(doc) and nome:
                try:
                    cadastrar(nome, doc, fonte=f"API {provedor} (consulta do CNPJ {cnpj})",
                              finalidade="Consulta cadastral de CNPJ no app", origem="fonte_externa")
                    n += 1
                except CpfInvalido:
                    continue
    return n


# ---------------------------------------------------------------- busca por CPF completo


def buscar(cpf: str, limite: int = 200) -> dict[str, Any]:
    """Empresas ligadas a um CPF completo:
    - titular de empresario individual/MEI (CPF publicado na razao social): CERTO;
    - socio cadastrado no cofre com esse CPF: CERTO;
    - socios com os mesmos digitos visiveis (4 a 9): PROVAVEL (varias pessoas
      podem ter os mesmos 6 digitos; confira o nome)."""
    from app.receita.duck import conectar_leitura, rows_as_dicts
    from app.services.ficha import _abrir

    if not cpf_valido(cpf):
        raise CpfInvalido("CPF inválido (confira os 11 dígitos e os dígitos verificadores).")
    d = so_digitos(cpf)
    cadastrado = por_cpf(d)
    ativa = _abrir()
    with conectar_leitura(ativa.caminho) as conn:
        titular = rows_as_dicts(conn, conn.execute(
            """
            SELECT cnpj_completo AS cnpj, razao_social, nome_fantasia, situacao_cadastral, uf
            FROM estabelecimentos
            WHERE natureza_juridica = ? AND razao_social LIKE ? LIMIT ?
            """, [NATUREZA_EMPRESARIO_INDIVIDUAL, f"%{d}", limite]).fetchall())
        socios = rows_as_dicts(conn, conn.execute(
            """
            SELECT r.socio_chave AS chave, r.nome_socio AS nome, r.cpf_cnpj_socio AS documento,
                   r.qtd_empresas
            FROM socio_resumo r
            WHERE r.identificador_de_socio = 2 AND r.cpf_cnpj_socio = ?
            ORDER BY r.nome_socio LIMIT ?
            """, [f"***{d[3:9]}**", limite]).fetchall())
    for x in socios:
        x["confirmado"] = bool(cadastrado and chave_pessoa(x["nome"], x["documento"]) ==
                               f"{normalize_name_for_search(cadastrado['nome']) or ''}|{d[3:9]}")
        x["documento"] = f"***.{d[3:6]}.{d[6:9]}-**"
    socios.sort(key=lambda x: (not x["confirmado"], x["nome"] or ""))
    return {"cpf_mascarado": mascarar(d), "cadastrado": cadastrado, "titular": titular, "socios": socios}
