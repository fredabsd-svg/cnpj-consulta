"""Servidor MCP (Model Context Protocol) do CNPJ Consulta.

Permite que assistentes de IA (Claude Desktop, Claude Code, Cursor, VS Code e
outros clientes MCP) usem as funcoes do app: consulta e ficha de CNPJ, busca
avancada, socios, lote, sancoes, inscricao estadual, pesquisa na internet,
listas, buscas salvas, exportacao e o estado da base local.

Uso:
    python -m app.mcp_server                 # stdio (padrao dos clientes MCP)
    python -m app.mcp_server --http          # Streamable HTTP em 127.0.0.1:8765/mcp
    cnpj mcp [--http] [--porta 8765]

Chama os servicos do app diretamente (nao precisa do site rodando). As mesmas
regras da interface valem aqui: CPF de socio sempre mascarado, exportacao de
contatos exige finalidade e fica na auditoria, contatos complementares so com
fonte. Ferramentas que ALTERAM a base (atualizar, pausar, cancelar, contatos
complementares) so aparecem com MCP_PERMITIR_ADMIN=true.

Nada e escrito no stdout alem do protocolo: logs vao para o stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# O app usa caminhos relativos (.env, ./data): clientes MCP como o Claude
# Desktop nao deixam escolher a pasta de trabalho, entao a raiz do projeto e
# fixada aqui (ou em CNPJ_CONSULTA_DIR) antes de carregar a configuracao.
_RAIZ = Path(os.environ.get("CNPJ_CONSULTA_DIR") or Path(__file__).resolve().parent.parent)
if _RAIZ.is_dir() and (_RAIZ / "app").is_dir():
    os.chdir(_RAIZ)

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402

from app import __version__  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.cnpj_validator import normalize_or_none  # noqa: E402
from app.db import init_database  # noqa: E402

log = logging.getLogger("app.mcp")

LEITURA = ToolAnnotations(read_only_hint=True, open_world_hint=False)
LEITURA_EXTERNA = ToolAnnotations(read_only_hint=True, open_world_hint=True)
ESCRITA_LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
ADMIN = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

INSTRUCOES = """\
CNPJ Consulta: dados cadastrais de empresas brasileiras a partir da base
oficial da Receita Federal (dados abertos do CNPJ, baixada localmente) e de
fontes publicas.

Como usar:
- Para um CNPJ especifico use `ficha_cnpj` (base local, completa) ou
  `consultar_cnpj` (cruza fontes, com procedencia, divergencias e diligencia).
- Para encontrar empresas por criterios (UF, municipio, CNAE, porte, socios,
  contatos...) chame `descrever_filtros` uma vez e depois `buscar_empresas`.
  Valores de um mesmo filtro sao OU; filtros diferentes sao E; prefixe
  `excluir_` para NAO. Atividade economica sempre pelo CNAE, nunca pelo nome.
- Nome de empresa ou CNPJ incompleto: `sugerir_empresas`.

Regras (LGPD e procedencia):
- CPF de socio vem mascarado pela Receita (***.123.456-**); nao tente
  reconstitui-lo nem sugerir o numero completo.
- Diferencie dados oficiais (Receita) de complementares (cadastrados com fonte).
  Nao invente telefone, e-mail ou site que nao estejam nos resultados.
- Simples/MEI e informacao cadastral, nao prova regularidade fiscal.
- Exportar contatos ou nomes de socios exige informar a finalidade.
- Os dados tem data de referencia (mes da base); cite-a ao responder.
"""

mcp = MCPServer(
    "cnpj-consulta",
    title="CNPJ Consulta",
    description="Consulta de CNPJ, ficha cadastral e busca avançada sobre a base oficial da Receita Federal.",
    instructions=INSTRUCOES,
    version=__version__,
)


# ---------------------------------------------------------------- utilitarios


def _json(obj: Any) -> Any:
    """Converte resultados dos servicos (dataclasses, Pydantic, datas) em JSON."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _json(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [_json(v) for v in obj]
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def _itens(lista: list) -> dict:
    return {"total": len(lista), "itens": _json(lista)}


def _cnpj(cnpj: str) -> str:
    n = normalize_or_none(cnpj or "")
    if n is None:
        raise ToolError(f"CNPJ inválido: {cnpj!r}. Confira os 14 caracteres e os dígitos verificadores.")
    return n


def _base_ou_erro(e: Exception) -> ToolError:
    return ToolError(
        f"{e} A base local da Receita é instalada pelo painel Base de Dados CNPJ (/base) "
        "ou com `python -m app.cli base atualizar`. Sem ela, use `consultar_cnpj`."
    )


def _filtros(filtros: dict[str, Any] | None, **extra: Any):
    from app.services import busca_avancada as ba

    params: dict[str, Any] = {k: v for k, v in (filtros or {}).items() if v not in (None, "", [])}
    for k, v in extra.items():
        if v not in (None, ""):
            params[k] = v
    try:
        f = ba.Filtros.de_params({k: v if isinstance(v, list) else [v] for k, v in params.items()}
                                 if params else {})
        ba.compilar(f)
    except ba.FiltroInvalido as e:
        raise ToolError(f"Filtro inválido: {e} Veja `descrever_filtros`.") from e
    return f


async def _rodar(fn, *args, **kwargs):
    """Servicos do app sao sincronos (DuckDB/SQLite): rodam fora do laco."""
    from app.services import busca_avancada as ba
    from app.services import ficha

    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (ba.BuscaIndisponivel, ficha.BaseIndisponivel) as e:
        raise _base_ou_erro(e) from e
    except ba.FiltroInvalido as e:
        raise ToolError(f"Filtro inválido: {e}") from e
    except ba.BuscaCancelada as e:
        raise ToolError(str(e)) from e
    except RuntimeError as e:  # ex.: busca por socio sem base local
        raise ToolError(str(e)) from e


def _auditar(acao: str, detalhe: str) -> None:
    from app.receita import jobs

    jobs.auditar(acao, detalhe=f"origem=mcp {detalhe}", ip="mcp")


# ---------------------------------------------------------------- consulta


@mcp.tool(annotations=LEITURA_EXTERNA)
async def consultar_cnpj(cnpj: str, fontes_externas: bool = False, atualizar: bool = False,
                         detalhar_procedencia: bool = False) -> dict:
    """Consulta um CNPJ e devolve os dados unificados, divergencias entre fontes
    e o checklist de diligencia cadastral.

    Usa primeiro a base local da Receita (se instalada). `fontes_externas=True`
    compara com Minha Receita/BrasilAPI e demais fontes habilitadas;
    `atualizar=True` ignora o cache. `detalhar_procedencia=True` inclui a
    procedencia campo a campo (resposta maior).
    """
    from app.services.company_query import query_company_async
    from app.services.diligence import build_diligence_checklist, build_diligence_verdict
    from app.services.sanctions import check_sanctions

    n = _cnpj(cnpj)
    c = await query_company_async(n, force_refresh=atualizar, externas=fontes_externas or atualizar)
    dados = _json(c)
    if not detalhar_procedencia:
        dados.pop("campos_procedencia", None)
    if c.razao_social:
        sancoes = await check_sanctions(n)
        itens = build_diligence_checklist(c, sancoes)
        dados["diligencia"] = {"veredicto": _json(build_diligence_verdict(itens)), "itens": _json(itens)}
        dados["sancoes"] = _json(sancoes)
    else:
        dados["aviso"] = "Nenhuma fonte retornou dados para este CNPJ (pode não existir ou as fontes estarem fora do ar)."
    return dados


@mcp.tool(annotations=LEITURA)
async def ficha_cnpj(cnpj: str) -> dict:
    """Ficha cadastral completa da base local oficial: identificacao, situacao e
    motivo, natureza, porte, capital, Simples/MEI, endereco, contatos oficiais e
    complementares (com origem e data), CNAEs, socios (CPF mascarado),
    estabelecimentos da empresa e historico."""
    from app.services import contatos, ficha

    n = _cnpj(cnpj)
    dados = await _rodar(ficha.ficha_local, n)
    if dados is None:
        raise ToolError(f"CNPJ {n} não está na base local. Tente `consultar_cnpj` com fontes_externas=True.")
    dados["contatos_complementares"] = await asyncio.to_thread(contatos.listar, n)
    return _json(dados)


@mcp.tool(annotations=LEITURA)
async def sugerir_empresas(texto: str, limite: int = 10) -> dict:
    """Autocompletar: empresas cuja razao social ou nome fantasia COMECA com o
    texto, ou cujo CNPJ comeca com os digitos informados (minimo 3 letras ou 4
    digitos)."""
    from app.services import ficha

    return _itens(await _rodar(ficha.sugestoes, texto, max(1, min(limite, 20))))


# ---------------------------------------------------------------- busca avancada


@mcp.tool(annotations=LEITURA)
async def descrever_filtros() -> dict:
    """Catalogo da busca avancada: nome, tipo e opcoes de cada filtro, ordenacoes,
    colunas e atalhos de atividade (palavra-chave -> codigos CNAE). Chame antes
    de `buscar_empresas`."""
    from app.services import busca_avancada as ba
    from app.services.cnae_palavras import PALAVRAS_CHAVE

    return {
        "regras": "Valores do mesmo filtro = OU; filtros diferentes = E; prefixo `excluir_<filtro>` = NAO; "
                  "`arvore` aceita {\"op\": \"E\"|\"OU\", \"negar\": bool, \"itens\": [{\"campo\":..., \"valor\":..., \"negar\": bool}]}.",
        "exemplo": {"situacao": ["2"], "uf": ["TO"], "municipio_nome": ["PALMAS", "ARAGUAINA"],
                    "atividade": ["contabilidade"], "contato": "telefone_e_email", "idade_min": 2,
                    "tem_socios": "sim"},
        "campos": ba.descricao_campos(),
        "ordenacoes": {k: v[0] for k, v in ba.ORDENACOES.items()},
        "colunas": {k: {"rotulo": v[0], "restrita": v[2]} for k, v in ba.COLUNAS.items()},
        "colunas_padrao": ba.COLUNAS_TELA,
        "atividades": PALAVRAS_CHAVE,
    }


@mcp.tool(annotations=LEITURA)
async def buscar_empresas(filtros: dict[str, Any] | None = None, ordem: str = "razao",
                          direcao: str | None = None, pagina: int = 1, por_pagina: int = 20,
                          colunas: list[str] | None = None) -> dict:
    """Busca estabelecimentos na base local com filtros combinados (veja
    `descrever_filtros`). Ex.: {"uf": ["TO"], "situacao": ["2"],
    "atividade": ["contabilidade"], "contato": "telefone_e_email"}.
    Retorna o total, a pagina pedida (ate 100 itens) e o mes de referencia."""
    from app.services import busca_avancada as ba

    f = _filtros(filtros, ordem=ordem, direcao=direcao)
    f.pagina = max(1, int(pagina))
    f.por_pagina = max(1, min(int(por_pagina), 100))
    r = await _rodar(ba.buscar, f, colunas=colunas)
    return {
        "total": r.total, "pagina": r.pagina, "paginas": r.paginas, "por_pagina": r.por_pagina,
        "mes_referencia": r.mes_referencia, "fonte": "Receita Federal — dados abertos do CNPJ (base local)",
        "itens": _json(r.itens),
    }


@mcp.tool(annotations=LEITURA)
async def contar_empresas(filtros: dict[str, Any] | None = None) -> dict:
    """Quantos estabelecimentos atendem aos filtros (sem listar)."""
    from app.services import busca_avancada as ba

    return {"total": await _rodar(ba.contar, _filtros(filtros))}


@mcp.tool(annotations=LEITURA)
async def distribuicao_resultados(filtros: dict[str, Any] | None = None,
                                  dimensoes: list[str] | None = None, limite: int = 15) -> dict:
    """Distribuicao do resultado por dimensao: uf, situacao, porte,
    matriz_filial, tipo_telefone_1, municipio, cnae_principal."""
    from app.services import busca_avancada as ba

    dims = dimensoes or ["uf", "situacao", "porte"]
    return _json(await _rodar(ba.contadores, _filtros(filtros), dims, limite=max(1, min(limite, 50))))


@mcp.tool(annotations=LEITURA)
async def listar_codigos(tabela: str, busca: str = "") -> dict:
    """Tabelas oficiais de codigos da Receita: cnaes, naturezas, motivos,
    qualificacoes, paises. `busca` filtra por descricao ou codigo."""
    from app.services import busca_avancada as ba

    return _itens(await _rodar(ba.lista_dominio, tabela, busca, 300))


@mcp.tool(annotations=LEITURA)
async def listar_municipios(ufs: list[str]) -> dict:
    """Municipios (codigo da Receita, nome, quantidade de estabelecimentos) das UFs."""
    from app.services import busca_avancada as ba

    return _itens(await _rodar(ba.lista_municipios, ufs))


@mcp.tool(annotations=ESCRITA_LOCAL)
async def exportar_busca(filtros: dict[str, Any] | None = None, formato: str = "xlsx",
                         colunas: list[str] | None = None, finalidade: str = "") -> dict:
    """Gera planilha (xlsx ou csv) com o resultado da busca e devolve o caminho
    do arquivo neste computador. Colunas de contato/pessoais (telefones, e-mail,
    nomes de socios) exigem `finalidade` (min. 10 caracteres); a exportacao fica
    na auditoria."""
    from app.receita.armazenamento import exportacoes_dir
    from app.services import busca_avancada as ba
    from app.services import exportacao

    if formato not in ("csv", "xlsx"):
        raise ToolError("Formato deve ser csv ou xlsx.")
    try:
        cols = exportacao.validar_colunas(colunas, finalidade)
    except exportacao.ExportacaoInvalida as e:
        raise ToolError(str(e)) from e
    f = _filtros(filtros)
    total = await _rodar(ba.contar, f)
    limite = min(total, get_settings().exportacao_max_linhas)
    pasta = exportacoes_dir()
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"mcp-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{formato}"
    gravar = exportacao.gravar_xlsx if formato == "xlsx" else exportacao.gravar_csv
    n = await _rodar(gravar, f, cols, limite, destino)
    _auditar("busca.exportar", f"formato={formato} linhas={n} colunas={','.join(cols)} finalidade={finalidade.strip()[:200]}")
    return {"arquivo": str(destino.resolve()), "linhas": n, "total_encontrado": total, "colunas": cols,
            "truncado": total > limite}


# ---------------------------------------------------------------- socios


@mcp.tool(annotations=LEITURA)
async def buscar_socio(nome: str, uf: str | None = None, municipio: str | None = None,
                       limite: int = 50) -> dict:
    """Empresas em que uma pessoa ou empresa figura no quadro societario (busca
    pelo nome, sem acento, palavras em qualquer ordem). Requer a base local.
    Cada linha traz `socio_chave` para `ficha_socio`."""
    from app.services.partner_search import MAX_LIMIT, search_partners

    if len((nome or "").strip()) < 3:
        raise ToolError("Informe ao menos 3 letras do nome.")
    return _itens(await _rodar(search_partners, nome, uf=uf, municipio=municipio,
                               limit=max(1, min(limite, MAX_LIMIT))))


@mcp.tool(annotations=LEITURA)
async def ficha_socio(socio_chave: str) -> dict:
    """Ficha do socio (identificado por nome + documento como publicado) e todas
    as empresas relacionadas, com qualificacao e data de entrada. Para pessoa
    fisica, a identificacao entre empresas e PROVAVEL (CPF mascarado)."""
    from app.services import ficha

    dados = await _rodar(ficha.ficha_socio, socio_chave)
    if dados is None:
        raise ToolError("Sócio não encontrado na base local.")
    return _json(dados)


@mcp.tool(annotations=LEITURA)
async def empresas_do_socio_pj(cnpj_socio: str) -> dict:
    """Empresas em que um CNPJ (pessoa juridica) e socio."""
    from app.services.partner_search import companies_of_partner_document

    return _itens(await _rodar(companies_of_partner_document, _cnpj(cnpj_socio)))


# ---------------------------------------------------------------- lote e complementos


@mcp.tool(annotations=LEITURA_EXTERNA)
async def consultar_lote(cnpjs: list[str]) -> dict:
    """Consulta varios CNPJs (ate BATCH_MAX_ITEMS) e devolve um quadro-resumo:
    situacao, UF, porte, divergencias, veredicto da diligencia e sancoes."""
    from app.services.batch import parse_cnpj_list, run_batch, summarize

    maximo = get_settings().batch_max_items
    parsed = parse_cnpj_list("\n".join(cnpjs or []), maximo)
    if not parsed.validos:
        raise ToolError("Nenhum CNPJ válido na lista.")
    linhas = await run_batch(parsed.validos)
    return {"resumo": summarize(linhas), "invalidos": parsed.invalidos, "linhas": _json(linhas),
            "limite": maximo}


@mcp.tool(annotations=LEITURA_EXTERNA)
async def verificar_sancoes(cnpj: str) -> dict:
    """Cadastros federais de sancoes (CEIS, CNEP, CEPIM, CEAF) no Portal da
    Transparencia. Requer PORTAL_TRANSPARENCIA_API_KEY no .env."""
    from app.services.sanctions import check_sanctions, is_enabled

    if not is_enabled():
        raise ToolError("Checagem de sanções desligada: defina PORTAL_TRANSPARENCIA_API_KEY no .env.")
    return _json(await check_sanctions(_cnpj(cnpj)))


@mcp.tool(annotations=LEITURA_EXTERNA)
async def consultar_inscricao_estadual(cnpj: str, uf: str) -> dict:
    """Inscricao estadual na SEFAZ da UF (web service de consulta cadastral).
    Requer certificado A1 (CERTIFICADO_A1_PATH/SENHA); sem ele, devolve o
    endereco do portal CCC para consulta manual."""
    from app.services import inscricao_estadual, official_docs

    n = _cnpj(cnpj)
    if not inscricao_estadual.is_enabled():
        return {"consultado": False, "motivo": "Sem certificado A1 configurado.",
                "portal_ccc": official_docs.CCC_PORTAL_URL, "sintegra": official_docs.SINTEGRA_URL}
    return _json(await inscricao_estadual.consultar_ie(n, uf))


@mcp.tool(annotations=LEITURA_EXTERNA)
async def pesquisar_na_internet(cnpj: str, consulta: str | None = None) -> dict:
    """Atalhos de pesquisa sobre a EMPRESA (processos, noticias, licitacoes...) e,
    se houver provedor configurado (WEB_SEARCH_PROVIDER), os resultados da web.
    Nunca envia nomes ou documentos de socios."""
    from app.services import web_research
    from app.services.company_query import query_company_async

    c = await query_company_async(_cnpj(cnpj))
    if not c.razao_social:
        raise ToolError("Empresa não encontrada para montar a pesquisa.")
    saida: dict[str, Any] = {"atalhos": _json(web_research.build_research_links(c))}
    if web_research.is_enabled():
        saida["resultados"] = _json(await web_research.search_web(consulta or web_research.default_query(c)))
    else:
        saida["resultados"] = None
        saida["aviso"] = "Sem provedor de busca configurado; use os atalhos."
    return saida


# ---------------------------------------------------------------- buscas salvas e listas


@mcp.tool(annotations=LEITURA)
async def listar_buscas_salvas() -> dict:
    """Buscas salvas (criterios), com total da ultima execucao."""
    from app.services import listas

    return _itens(await asyncio.to_thread(listas.buscas_salvas))


@mcp.tool(annotations=LEITURA)
async def executar_busca_salva(busca_id: int, pagina: int = 1, por_pagina: int = 20) -> dict:
    """Executa de novo uma busca salva (os criterios sobre a base atual)."""
    from app.services import listas

    b = await asyncio.to_thread(listas.obter_busca, busca_id)
    if b is None:
        raise ToolError("Busca salva não encontrada.")
    filtros = dict(b["filtros"])
    ordem = filtros.pop("ordem", "razao")
    ordem = ordem[-1] if isinstance(ordem, list) else ordem
    r = await buscar_empresas(filtros, ordem=ordem, pagina=pagina, por_pagina=por_pagina)
    await asyncio.to_thread(listas.marcar_execucao, busca_id, r["total"])
    return {"busca": _json(b), **r}


@mcp.tool(annotations=ESCRITA_LOCAL)
async def salvar_busca(nome: str, filtros: dict[str, Any]) -> dict:
    """Salva os criterios de uma busca para repetir depois."""
    from app.services import listas

    _filtros(filtros)
    try:
        return _json(await asyncio.to_thread(listas.salvar_busca, nome, filtros))
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=LEITURA)
async def listar_listas() -> dict:
    """Listas de empresas (selecoes fixas com observacoes)."""
    from app.services import listas

    return _itens(await asyncio.to_thread(listas.listas))


@mcp.tool(annotations=LEITURA)
async def ver_lista(lista_id: int) -> dict:
    """Empresas de uma lista, com data de inclusao e observacoes."""
    from app.services import listas

    d = await asyncio.to_thread(listas.obter_lista, lista_id)
    if d is None:
        raise ToolError("Lista não encontrada.")
    return _json(d)


@mcp.tool(annotations=ESCRITA_LOCAL)
async def criar_lista(nome: str, descricao: str = "") -> dict:
    """Cria uma lista de empresas."""
    from app.services import listas

    try:
        return _json(await asyncio.to_thread(listas.criar_lista, nome, descricao))
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool(annotations=ESCRITA_LOCAL)
async def adicionar_a_lista(lista_id: int, cnpjs: list[str], observacao: str = "") -> dict:
    """Adiciona CNPJs a uma lista (ignora repetidos e invalidos)."""
    from app.services import listas

    itens = [{"cnpj": c, "observacao": observacao} for c in cnpjs]
    try:
        n = await asyncio.to_thread(listas.adicionar, lista_id, itens)
    except LookupError as e:
        raise ToolError("Lista não encontrada.") from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    return {"adicionados": n}


# ---------------------------------------------------------------- base local


@mcp.tool(annotations=LEITURA)
async def status_base_local() -> dict:
    """Situacao da base local: versao ativa (mes de referencia), contagens,
    disco, ultima verificacao do portal e tarefa em andamento."""
    from app.receita import servico

    e = await asyncio.to_thread(servico.estado_geral)
    job = e.get("job_ativo") or e.get("ultimo_job") or {}
    return _json({
        "status": e["status"], "ativa": e["ativa"], "verificacao": {k: v for k, v in e["verificacao"].items()
                                                                     if k != "arquivos"},
        "disco": e["disco"], "versoes": e["versoes"], "agenda": e["agenda"],
        "tarefa": {k: job.get(k) for k in ("id", "tipo", "mes", "status_efetivo", "etapa", "progresso",
                                           "mensagem", "erro", "velocidade_bps", "eta_segundos")} if job else None,
    })


@mcp.tool(annotations=LEITURA_EXTERNA)
async def verificar_nova_versao() -> dict:
    """Consulta o portal oficial da Receita e informa a versao mais recente
    publicada, o tamanho do download e se ha versao nova para instalar."""
    from app.receita import servico

    r = await asyncio.to_thread(servico.verificar_atualizacao)
    if r.get("erro"):
        raise ToolError(r["erro"])
    return _json({k: v for k, v in r.items() if k != "arquivos"} | {"qtd_arquivos": len(r.get("arquivos", []))})


# ---------------------------------------------------------------- administracao (opcional)


def _registrar_admin() -> None:
    """Ferramentas que alteram dados: so com MCP_PERMITIR_ADMIN=true."""

    @mcp.tool(annotations=ADMIN)
    async def atualizar_base(mes: str | None = None, forcar: bool = False) -> dict:
        """Inicia, em segundo plano, o download e a atualizacao da base oficial
        (AAAA-MM; padrao: a mais recente). Acompanhe com `status_base_local`."""
        from app.receita import jobs, servico

        try:
            job = await asyncio.to_thread(servico.iniciar_atualizacao, mes or None, forcar=forcar, origem="mcp")
        except jobs.JobEmAndamento as e:
            raise ToolError(str(e)) from e
        except ValueError as e:
            raise ToolError(str(e)) from e
        _auditar("base.atualizar", f"job={job['id']} mes={mes or 'mais recente'}")
        return _json(job)

    @mcp.tool(annotations=ADMIN)
    async def controlar_tarefa(job_id: int, acao: str) -> dict:
        """Pausa, retoma ou cancela uma tarefa da base (acao: pausar | retomar | cancelar)."""
        from app.receita import jobs, servico

        try:
            if acao in ("pausar", "cancelar"):
                job = await asyncio.to_thread(jobs.solicitar, job_id, acao)
            elif acao == "retomar":
                job = await asyncio.to_thread(servico.retomar, job_id)
            else:
                raise ToolError("Ação deve ser pausar, retomar ou cancelar.")
        except (LookupError, ValueError, jobs.JobEmAndamento) as e:
            raise ToolError(str(e) or "Tarefa não encontrada.") from e
        _auditar(f"base.{acao}", f"job={job_id}")
        return _json(job)

    @mcp.tool(annotations=ADMIN)
    async def cadastrar_contato_complementar(cnpj: str, tipo: str, valor: str, fonte: str,
                                             url_fonte: str | None = None, verificado: bool = False,
                                             data_verificacao: str | None = None,
                                             observacao: str | None = None) -> dict:
        """Cadastra telefone, e-mail ou site obtido FORA da Receita, com a fonte
        (obrigatoria). Use so contatos de fonte verificavel informada pelo usuario;
        nunca invente ou deduza contatos."""
        from app.services import contatos, ficha

        n = _cnpj(cnpj)
        try:
            dv = date.fromisoformat(data_verificacao) if data_verificacao else None
            try:
                f = await asyncio.to_thread(ficha.ficha_local, n)
            except ficha.BaseIndisponivel:
                f = None
            c = await asyncio.to_thread(
                contatos.salvar, n, tipo=tipo, valor=valor, fonte=fonte, url_fonte=url_fonte,
                verificado=verificado, data_verificacao=dv, observacao=observacao,
                oficiais=f["contatos_oficiais"] if f else [],
            )
        except (contatos.ContatoInvalido, ValueError) as e:
            raise ToolError(str(e)) from e
        _auditar("contato.criar", f"cnpj={n} tipo={tipo} verificado={verificado}")
        return _json(c)

    @mcp.tool(annotations=ADMIN)
    async def remover_contato_complementar(cnpj: str, contato_id: int) -> dict:
        """Remove um contato complementar (correcao ou pedido do titular)."""
        from app.services import contatos

        n = _cnpj(cnpj)
        try:
            info = await asyncio.to_thread(contatos.remover, n, contato_id)
        except LookupError as e:
            raise ToolError("Contato não encontrado.") from e
        _auditar("contato.remover", f"cnpj={n} id={contato_id} tipo={info['tipo']}")
        return {"removido": contato_id}


# ---------------------------------------------------------------- recursos e prompts


@mcp.resource("cnpj://busca/filtros", name="Filtros da busca avançada", mime_type="application/json")
async def recurso_filtros() -> dict:
    """Catalogo de filtros, colunas e atividades da busca avancada."""
    return await descrever_filtros()


@mcp.resource("cnpj://base/status", name="Situação da base local", mime_type="application/json")
async def recurso_status() -> dict:
    """Versao ativa, contagens e tarefa em andamento da base local."""
    return await status_base_local()


@mcp.resource("cnpj://empresa/{cnpj}", name="Ficha de empresa", mime_type="application/json")
async def recurso_empresa(cnpj: str) -> dict:
    """Ficha cadastral completa de um CNPJ (base local)."""
    return await ficha_cnpj(cnpj)


@mcp.prompt(title="Diligência cadastral")
def diligencia_cadastral(cnpj: str) -> str:
    """Analisa um CNPJ e produz um parecer de diligência cadastral."""
    return (
        f"Faça a diligência cadastral do CNPJ {cnpj}. Use `consultar_cnpj` (com fontes_externas=True) e "
        "`ficha_cnpj` se disponível. Informe situação cadastral e motivo, data de abertura, natureza, porte, "
        "capital, atividade principal, quadro societário (sem tentar revelar CPFs mascarados), divergências "
        "entre fontes, sanções e o veredicto do checklist. Cite o mês de referência da base e lembre que "
        "Simples/MEI não comprovam regularidade fiscal. Termine com recomendações objetivas."
    )


@mcp.prompt(title="Prospecção de empresas")
def prospeccao(perfil: str) -> str:
    """Monta uma lista de prospecção a partir de um perfil em linguagem natural."""
    return (
        f"Quero prospectar empresas com este perfil: {perfil}.\n"
        "1. Chame `descrever_filtros` e traduza o perfil em filtros (atividade SEMPRE por CNAE).\n"
        "2. Use `contar_empresas` e `distribuicao_resultados` para calibrar; mostre os filtros escolhidos.\n"
        "3. Liste as primeiras empresas com `buscar_empresas`, indicando a origem dos contatos.\n"
        "4. Se eu pedir planilha, use `exportar_busca` (contatos exigem a finalidade que eu informar).\n"
        "Não invente contatos nem trate dados públicos como autorização irrestrita de uso (LGPD)."
    )


@mcp.prompt(title="Mapa de sócios")
def mapa_de_socios(nome_ou_cnpj: str) -> str:
    """Mapeia empresas relacionadas por sócios em comum."""
    return (
        f"Mapeie as relações societárias de {nome_ou_cnpj}. Para CNPJ, use `ficha_cnpj` e, para cada sócio, "
        "`ficha_socio`; para nome, `buscar_socio`. Use também o filtro `compartilha_socios_com` em "
        "`buscar_empresas`. Explique que, para pessoa física, a ligação entre empresas é provável "
        "(mesmo nome e mesmos dígitos visíveis do CPF), não uma certeza."
    )


# ---------------------------------------------------------------- execucao


FERRAMENTAS_ADMIN = ("atualizar_base", "controlar_tarefa", "cadastrar_contato_complementar",
                     "remover_contato_complementar")
_admin_registrado = False


def admin_permitido() -> bool:
    return get_settings().mcp_permitir_admin  # variavel de ambiente ou .env


def configurar(admin: bool | None = None) -> MCPServer:
    """Prepara o banco e liga/desliga as ferramentas administrativas (idempotente)."""
    global _admin_registrado
    init_database()
    admin = admin_permitido() if admin is None else admin
    if admin and not _admin_registrado:
        _registrar_admin()
        _admin_registrado = True
    elif not admin and _admin_registrado:
        for nome in FERRAMENTAS_ADMIN:
            mcp.remove_tool(nome)
        _admin_registrado = False
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cnpj-mcp", description="Servidor MCP do CNPJ Consulta")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP em vez de stdio")
    parser.add_argument("--porta", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", help="So loopback (127.0.0.1/localhost)")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    servidor = configurar()
    if args.http:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("O servidor MCP HTTP não tem login: use apenas 127.0.0.1/localhost.")
        log.info("MCP em http://%s:%s/mcp", args.host, args.porta)
        servidor.run("streamable-http", host=args.host, port=args.porta)
    else:
        servidor.run("stdio")


if __name__ == "__main__":
    main()
