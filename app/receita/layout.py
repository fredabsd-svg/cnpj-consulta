"""Leiaute oficial dos dados abertos do CNPJ (Receita Federal).

Referencia: "Metadados / Leiaute dos Dados Abertos do CNPJ", publicado pela
Receita junto com os arquivos. Os CSVs nao tem cabecalho, usam ';' como
separador, aspas duplas e codificacao ISO-8859-1 (latin-1).

Cada ZIP do mes pertence a um grupo pelo prefixo do nome (Empresas0.zip,
Estabelecimentos3.zip, Socios9.zip, Simples.zip, Cnaes.zip...). O numero de
partes NAO e fixo: vem da listagem do servidor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMPRESAS_COLS = [
    "cnpj_basico", "razao_social", "natureza_juridica", "qualificacao_responsavel",
    "capital_social", "porte_empresa", "ente_federativo_responsavel",
]
ESTABELECIMENTOS_COLS = [
    "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial",
    "nome_fantasia", "situacao_cadastral", "data_situacao_cadastral",
    "motivo_situacao_cadastral", "nome_cidade_exterior", "pais",
    "data_inicio_atividade", "cnae_fiscal_principal", "cnae_fiscal_secundaria",
    "tipo_logradouro", "logradouro", "numero", "complemento", "bairro", "cep",
    "uf", "municipio", "ddd_telefone_1", "numero_telefone_1", "ddd_telefone_2",
    "numero_telefone_2", "ddd_fax", "numero_fax", "correio_eletronico",
    "situacao_especial", "data_situacao_especial",
]
SOCIOS_COLS = [
    "cnpj_basico", "identificador_de_socio", "nome_socio", "cpf_cnpj_socio",
    "qualificacao_socio", "data_entrada_sociedade", "pais",
    "representante_legal", "nome_representante", "qualificacao_representante_legal",
    "faixa_etaria",
]
SIMPLES_COLS = [
    "cnpj_basico", "opcao_simples", "data_opcao_simples", "data_exclusao_simples",
    "opcao_mei", "data_opcao_mei", "data_exclusao_mei",
]
DOMINIO_COLS = ["codigo", "descricao"]


@dataclass(frozen=True)
class Grupo:
    nome: str  # tabela bruta de destino
    prefixo: str  # prefixo do ZIP no servidor
    colunas: tuple[str, ...]
    multiparte: bool  # Empresas0..N
    principal: bool  # tabela principal (obrigatoria, com contagem minima)


GRUPOS: tuple[Grupo, ...] = (
    # Dominios primeiro: sao pequenos e validam o acesso antes dos arquivos grandes.
    Grupo("cnaes", "Cnaes", tuple(DOMINIO_COLS), False, False),
    Grupo("motivos", "Motivos", tuple(DOMINIO_COLS), False, False),
    Grupo("municipios", "Municipios", tuple(DOMINIO_COLS), False, False),
    Grupo("naturezas", "Naturezas", tuple(DOMINIO_COLS), False, False),
    Grupo("paises", "Paises", tuple(DOMINIO_COLS), False, False),
    Grupo("qualificacoes", "Qualificacoes", tuple(DOMINIO_COLS), False, False),
    Grupo("empresas", "Empresas", tuple(EMPRESAS_COLS), True, True),
    Grupo("estabelecimentos", "Estabelecimentos", tuple(ESTABELECIMENTOS_COLS), True, True),
    Grupo("socios", "Socios", tuple(SOCIOS_COLS), True, True),
    Grupo("simples", "Simples", tuple(SIMPLES_COLS), False, True),
)
GRUPO_POR_NOME = {g.nome: g for g in GRUPOS}
ORDEM_GRUPOS = {g.nome: i for i, g in enumerate(GRUPOS)}

_ZIP_RE = re.compile(r"^(?P<prefixo>[A-Za-z]+)(?P<parte>\d*)\.zip$")
MES_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def grupo_do_arquivo(nome: str) -> Grupo | None:
    """Grupo de um ZIP pelo nome (None = arquivo desconhecido, ignorado)."""
    m = _ZIP_RE.match(nome)
    if not m:
        return None
    for g in GRUPOS:
        if m["prefixo"].lower() == g.prefixo.lower():
            if bool(m["parte"]) != g.multiparte:
                return None
            return g
    return None


def grupos_faltando(nomes: list[str]) -> list[str]:
    """O que falta na listagem para a versao estar completa: grupos sem nenhum
    arquivo e, nos grupos em partes, buracos na sequencia 0..N (pasta ainda em
    publicacao com Estabelecimentos0 e 2 mas sem o 1)."""
    partes: dict[str, set[int]] = {}
    for n in nomes:
        g = grupo_do_arquivo(n)
        if g is None:
            continue
        m = _ZIP_RE.match(n)
        partes.setdefault(g.nome, set())
        if g.multiparte and m and m["parte"]:
            partes[g.nome].add(int(m["parte"]))
    faltando: list[str] = []
    for g in GRUPOS:
        if g.nome not in partes:
            faltando.append(g.prefixo)
        elif g.multiparte and partes[g.nome]:
            faltando += [f"{g.prefixo}{i}" for i in range(max(partes[g.nome]) + 1) if i not in partes[g.nome]]
    return faltando


# Codigos oficiais do leiaute.
SITUACOES = {1: "NULA", 2: "ATIVA", 3: "SUSPENSA", 4: "INAPTA", 8: "BAIXADA"}
PORTES = {0: "NAO INFORMADO", 1: "MICRO EMPRESA", 3: "EMPRESA DE PEQUENO PORTE", 5: "DEMAIS"}
PORTES_CURTO = {0: "Não informado", 1: "ME", 3: "EPP", 5: "Demais"}
TIPOS_SOCIO = {1: "Pessoa jurídica", 2: "Pessoa física", 3: "Estrangeiro"}
MATRIZ_FILIAL = {1: "Matriz", 2: "Filial"}
FAIXAS_ETARIAS = {
    0: "Não se aplica",
    1: "0 a 12 anos",
    2: "13 a 20 anos",
    3: "21 a 30 anos",
    4: "31 a 40 anos",
    5: "41 a 50 anos",
    6: "51 a 60 anos",
    7: "61 a 70 anos",
    8: "71 a 80 anos",
    9: "Mais de 80 anos",
}
# Qualificacoes que indicam administracao ou titularidade (tabela oficial
# Qualificacoes.zip): 05 Administrador, 10 Diretor, 16 Presidente,
# 28 Socio-Gerente, 49 Socio-Administrador, 50 Empresario, 65/66 Titular PF,
# 70/72/73/74 Administrador/Diretor/Presidente/Socio-Adm. no exterior,
# 78/79 Titular PJ. Procurador (17) NAO entra: representa, nao administra.
QUALIFICACOES_ADMINISTRACAO = (5, 10, 16, 28, 49, 50, 65, 66, 70, 72, 73, 74, 78, 79)
