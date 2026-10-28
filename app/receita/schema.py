"""Esquema da base publicada (DuckDB) e o SQL de consolidacao.

Tabelas publicadas (versao de esquema 2):

- empresas: uma linha por CNPJ basico (8 digitos) -- a pessoa juridica.
- estabelecimentos: uma linha por CNPJ completo (14) -- matriz ou filial. Leva
  uma copia dos campos da empresa usados em filtros (razao social, natureza,
  porte, capital, quantidade de socios, Simples/MEI) para a busca avancada
  varrer uma tabela so, sem juncao de 60 milhoes de linhas a cada consulta.
- socios: QSA como publicado (CPF ja vem mascarado pela Receita: ***123456**).
  `socio_chave` agrupa o MESMO socio entre empresas: tipo + documento como
  publicado + nome normalizado. Para pessoa fisica e uma identificacao
  provavel (mesmo nome e mesmos 6 digitos visiveis do CPF), nao certeza.
- socio_resumo: um registro por socio_chave com a quantidade de empresas.
- simples: opcao pelo Simples Nacional / MEI (dado cadastral; NAO comprova
  regularidade fiscal).
- cnaes, motivos, municipios, naturezas, paises, qualificacoes: dominios.
- nomes_busca: razao social e nome fantasia normalizados, ordenados, para
  sugestoes por prefixo (as estatisticas min/max do DuckDB pulam blocos).
- municipios_uf: municipios existentes por UF (lista dinamica dos filtros).
- metadata, importacao_arquivos, estatisticas: procedencia e validacao.

Ordenacao fisica: estabelecimentos por cnpj_completo, empresas e socios por
cnpj_basico e nomes_busca por nome -- assim as consultas pontuais leem poucos
blocos sem precisar de indices em memoria.
"""

from __future__ import annotations

# Provedores de e-mail gratuitos/publicos mais comuns no Brasil. Um dominio
# FORA desta lista e apenas "dominio proprio provavel" -- nunca "corporativo
# verificado".
WEBMAIL = (
    "gmail.com", "hotmail.com", "hotmail.com.br", "outlook.com", "outlook.com.br",
    "live.com", "msn.com", "yahoo.com", "yahoo.com.br", "ymail.com", "bol.com.br",
    "uol.com.br", "terra.com.br", "ig.com.br", "globo.com", "globomail.com",
    "icloud.com", "me.com", "aol.com", "zipmail.com.br", "r7.com", "oi.com.br",
    "protonmail.com", "proton.me", "gmx.com", "mail.com", "yandex.com", "zoho.com",
    "sercomtel.com.br", "brturbo.com.br", "click21.com.br", "pop.com.br",
)

MACROS = r"""
CREATE OR REPLACE TEMP MACRO limpo(x) AS NULLIF(trim(x), '');
CREATE OR REPLACE TEMP MACRO inteiro(x) AS TRY_CAST(trim(x) AS INTEGER);
CREATE OR REPLACE TEMP MACRO data_rfb(x) AS
    TRY_CAST(TRY_STRPTIME(NULLIF(NULLIF(trim(x), ''), '00000000'), '%Y%m%d') AS DATE);
CREATE OR REPLACE TEMP MACRO norm(x) AS
    trim(regexp_replace(regexp_replace(strip_accents(upper(coalesce(x, ''))),
         '[^A-Z0-9 ]', ' ', 'g'), '\s+', ' ', 'g'));
CREATE OR REPLACE TEMP MACRO digitos(x) AS regexp_replace(coalesce(x, ''), '[^0-9]', '', 'g');
CREATE OR REPLACE TEMP MACRO tel(ddd, num) AS
    CASE WHEN length(ltrim(digitos(ddd), '0')) = 2 AND length(digitos(num)) IN (8, 9)
         THEN ltrim(digitos(ddd), '0') || digitos(num) END;
-- Plano de numeracao da Anatel: celular (SMP) = 9 + 8 digitos iniciados por 6-9;
-- fixo (STFC) = 8 digitos iniciados por 2-5. A base publica traz o numero com 8
-- digitos (sem o nono digito); o app classifica, mas nunca acrescenta o digito.
CREATE OR REPLACE TEMP MACRO tipo_tel(t) AS
    CASE WHEN t IS NULL THEN NULL
         WHEN length(t) = 11 AND substr(t, 3, 2) IN ('96', '97', '98', '99') THEN 'celular'
         WHEN length(t) = 10 AND substr(t, 3, 1) IN ('6', '7', '8', '9') THEN 'celular'
         WHEN length(t) = 10 AND substr(t, 3, 1) IN ('2', '3', '4', '5') THEN 'fixo'
         ELSE 'indeterminado' END;
CREATE OR REPLACE TEMP MACRO email_ok(x) AS
    CASE WHEN regexp_full_match(lower(trim(coalesce(x, ''))), '[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}')
         THEN lower(trim(x)) END;
"""

# Consolidacao: (nome da etapa, SQL). Cada uma roda numa transacao propria e
# fica registrada em _etapas; ao retomar, as concluidas sao puladas.
# {bruto_X} e substituido pela uniao das tabelas brutas do grupo X.


def sql_dominio(tabela: str) -> str:
    return f"""
    CREATE TABLE {tabela} AS
    SELECT codigo, any_value(descricao) AS descricao FROM (
        SELECT inteiro(codigo) AS codigo, limpo(descricao) AS descricao FROM {{bruto_{tabela}}}
    ) WHERE codigo IS NOT NULL GROUP BY codigo ORDER BY codigo
    """


SQL_SIMPLES = """
CREATE TABLE simples AS
SELECT DISTINCT ON (cnpj_basico)
    cnpj_basico,
    upper(limpo(opcao_simples)) AS opcao_simples,
    data_rfb(data_opcao_simples) AS data_opcao_simples,
    data_rfb(data_exclusao_simples) AS data_exclusao_simples,
    upper(limpo(opcao_mei)) AS opcao_mei,
    data_rfb(data_opcao_mei) AS data_opcao_mei,
    data_rfb(data_exclusao_mei) AS data_exclusao_mei
FROM {bruto_simples}
WHERE length(trim(cnpj_basico)) = 8
ORDER BY cnpj_basico
"""

SQL_SOCIOS = """
CREATE TABLE socios AS
SELECT *,
    substr(md5(coalesce(CAST(identificador_de_socio AS VARCHAR), '') || '|' ||
               coalesce(cpf_cnpj_socio, '') || '|' || nome_socio_norm), 1, 16) AS socio_chave
FROM (
    SELECT DISTINCT
        trim(cnpj_basico) AS cnpj_basico,
        CAST(inteiro(identificador_de_socio) AS TINYINT) AS identificador_de_socio,
        limpo(nome_socio) AS nome_socio,
        norm(nome_socio) AS nome_socio_norm,
        limpo(cpf_cnpj_socio) AS cpf_cnpj_socio,
        CAST(inteiro(qualificacao_socio) AS SMALLINT) AS qualificacao_socio,
        data_rfb(data_entrada_sociedade) AS data_entrada_sociedade,
        CAST(inteiro(pais) AS SMALLINT) AS pais,
        NULLIF(limpo(representante_legal), '***000000**') AS representante_legal,
        limpo(nome_representante) AS nome_representante,
        CAST(NULLIF(inteiro(qualificacao_representante_legal), 0) AS SMALLINT) AS qualificacao_representante_legal,
        CAST(inteiro(faixa_etaria) AS TINYINT) AS faixa_etaria
    FROM {bruto_socios}
    WHERE length(trim(cnpj_basico)) = 8
)
ORDER BY cnpj_basico, nome_socio_norm
"""

SQL_EMPRESAS = """
CREATE TABLE empresas AS
WITH qs AS (SELECT cnpj_basico, count(*) AS qtd_socios FROM socios GROUP BY cnpj_basico),
base AS (
    SELECT {distinct_empresas}
        trim(cnpj_basico) AS cnpj_basico,
        limpo(razao_social) AS razao_social,
        norm(razao_social) AS razao_social_norm,
        CAST(inteiro(natureza_juridica) AS SMALLINT) AS natureza_juridica,
        CAST(inteiro(qualificacao_responsavel) AS SMALLINT) AS qualificacao_responsavel,
        TRY_CAST(replace(trim(capital_social), ',', '.') AS DECIMAL(18, 2)) AS capital_social,
        CAST(inteiro(porte_empresa) AS TINYINT) AS porte_empresa,
        limpo(ente_federativo_responsavel) AS ente_federativo_responsavel
    FROM {bruto_empresas}
    WHERE length(trim(cnpj_basico)) = 8
)
SELECT base.*, coalesce(qs.qtd_socios, 0) AS qtd_socios
FROM base LEFT JOIN qs USING (cnpj_basico)
ORDER BY cnpj_basico
"""

SQL_ESTABELECIMENTOS = """
CREATE TABLE estabelecimentos AS
WITH r AS (
    SELECT {distinct_estab} *, trim(cnpj_basico) || trim(cnpj_ordem) || trim(cnpj_dv) AS cnpj_completo
    FROM {bruto_estabelecimentos}
    WHERE length(trim(cnpj_basico) || trim(cnpj_ordem) || trim(cnpj_dv)) = 14
),
t AS (
    SELECT
        trim(r.cnpj_basico) AS cnpj_basico, trim(r.cnpj_ordem) AS cnpj_ordem, trim(r.cnpj_dv) AS cnpj_dv,
        r.cnpj_completo,
        CAST(inteiro(r.identificador_matriz_filial) AS TINYINT) AS identificador_matriz_filial,
        limpo(r.nome_fantasia) AS nome_fantasia,
        NULLIF(norm(r.nome_fantasia), '') AS nome_fantasia_norm,
        CAST(inteiro(r.situacao_cadastral) AS TINYINT) AS situacao_cadastral,
        data_rfb(r.data_situacao_cadastral) AS data_situacao_cadastral,
        CAST(inteiro(r.motivo_situacao_cadastral) AS SMALLINT) AS motivo_situacao_cadastral,
        limpo(r.nome_cidade_exterior) AS nome_cidade_exterior,
        CAST(inteiro(r.pais) AS SMALLINT) AS pais,
        data_rfb(r.data_inicio_atividade) AS data_inicio_atividade,
        inteiro(r.cnae_fiscal_principal) AS cnae_fiscal_principal,
        limpo(r.cnae_fiscal_secundaria) AS cnae_fiscal_secundaria,
        list_filter(
            list_transform(string_split(coalesce(r.cnae_fiscal_secundaria, ''), ','), lambda c: TRY_CAST(trim(c) AS INTEGER)),
            lambda c: c IS NOT NULL
        ) AS cnaes_secundarios,
        limpo(r.tipo_logradouro) AS tipo_logradouro, limpo(r.logradouro) AS logradouro,
        limpo(r.numero) AS numero, limpo(r.complemento) AS complemento, limpo(r.bairro) AS bairro,
        NULLIF(digitos(r.cep), '') AS cep,
        upper(limpo(r.uf)) AS uf,
        CAST(inteiro(r.municipio) AS SMALLINT) AS municipio,
        limpo(r.ddd_telefone_1) AS ddd_telefone_1, limpo(r.numero_telefone_1) AS numero_telefone_1,
        limpo(r.ddd_telefone_2) AS ddd_telefone_2, limpo(r.numero_telefone_2) AS numero_telefone_2,
        limpo(r.ddd_fax) AS ddd_fax, limpo(r.numero_fax) AS numero_fax,
        limpo(r.correio_eletronico) AS correio_eletronico,
        limpo(r.situacao_especial) AS situacao_especial,
        data_rfb(r.data_situacao_especial) AS data_situacao_especial,
        tel(r.ddd_telefone_1, r.numero_telefone_1) AS telefone_1,
        tel(r.ddd_telefone_2, r.numero_telefone_2) AS telefone_2,
        email_ok(r.correio_eletronico) AS email
    FROM r
)
SELECT t.*,
    tipo_tel(t.telefone_1) AS tipo_telefone_1,
    tipo_tel(t.telefone_2) AS tipo_telefone_2,
    split_part(t.email, '@', 2) AS email_dominio,
    CASE WHEN t.email IS NULL THEN NULL ELSE list_contains({webmail}, split_part(t.email, '@', 2)) END AS email_webmail,
    (t.logradouro IS NOT NULL AND t.numero IS NOT NULL AND t.bairro IS NOT NULL
        AND length(coalesce(t.cep, '')) = 8 AND t.municipio IS NOT NULL AND t.uf IS NOT NULL) AS endereco_completo,
    em.razao_social, em.razao_social_norm, em.natureza_juridica, em.porte_empresa,
    em.capital_social, coalesce(em.qtd_socios, 0) AS qtd_socios,
    (em.cnpj_basico IS NOT NULL) AS tem_empresa,
    sim.opcao_simples, sim.opcao_mei
FROM t
LEFT JOIN empresas em ON em.cnpj_basico = t.cnpj_basico
LEFT JOIN simples sim ON sim.cnpj_basico = t.cnpj_basico
ORDER BY t.cnpj_completo
"""

SQL_SOCIO_RESUMO = """
CREATE TABLE socio_resumo AS
SELECT socio_chave,
    any_value(nome_socio) AS nome_socio,
    any_value(nome_socio_norm) AS nome_socio_norm,
    any_value(cpf_cnpj_socio) AS cpf_cnpj_socio,
    any_value(identificador_de_socio) AS identificador_de_socio,
    count(DISTINCT cnpj_basico) AS qtd_empresas
FROM socios
GROUP BY socio_chave
ORDER BY socio_chave
"""

SQL_NOMES_BUSCA = """
CREATE TABLE nomes_busca AS
SELECT nome_norm, cnpj_completo, tipo FROM (
    SELECT razao_social_norm AS nome_norm, cnpj_completo, 'R' AS tipo
    FROM estabelecimentos WHERE identificador_matriz_filial = 1 AND razao_social_norm <> ''
    UNION ALL
    SELECT nome_fantasia_norm, cnpj_completo, 'F'
    FROM estabelecimentos WHERE nome_fantasia_norm IS NOT NULL
)
ORDER BY nome_norm
"""

SQL_MUNICIPIOS_UF = """
CREATE TABLE municipios_uf AS
SELECT e.uf, e.municipio AS codigo, any_value(m.descricao) AS nome, count(*) AS qtd
FROM estabelecimentos e LEFT JOIN municipios m ON m.codigo = e.municipio
WHERE e.uf IS NOT NULL AND e.municipio IS NOT NULL
GROUP BY e.uf, e.municipio
ORDER BY e.uf, nome
"""

# Colunas obrigatorias na base publicada (checadas na validacao e ao abrir).
COLUNAS_OBRIGATORIAS = {
    "empresas": ("cnpj_basico", "razao_social", "razao_social_norm", "natureza_juridica", "capital_social", "porte_empresa", "qtd_socios"),
    "estabelecimentos": (
        "cnpj_completo", "cnpj_basico", "identificador_matriz_filial", "situacao_cadastral",
        "cnae_fiscal_principal", "cnaes_secundarios", "uf", "municipio", "telefone_1",
        "tipo_telefone_1", "email", "email_dominio", "endereco_completo", "razao_social_norm",
    ),
    "socios": ("cnpj_basico", "nome_socio", "nome_socio_norm", "socio_chave", "qualificacao_socio"),
    "socio_resumo": ("socio_chave", "qtd_empresas"),
    "simples": ("cnpj_basico", "opcao_simples", "opcao_mei"),
    "nomes_busca": ("nome_norm", "cnpj_completo", "tipo"),
    "municipios_uf": ("uf", "codigo", "nome"),
    "cnaes": ("codigo", "descricao"),
}
