"""CLI principal (Typer)."""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console

from app import __version__
from app.config import get_settings

app = typer.Typer(
    name="cnpj",
    help="CNPJ Consulta - CLI",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

# Sub-comandos
providers_app = typer.Typer(help="Gerenciar provedores", no_args_is_help=True)
consulta_app = typer.Typer(help="Consultas pre-definidas", no_args_is_help=True)
base_app = typer.Typer(help="Base local da Receita Federal (dados abertos do CNPJ)", no_args_is_help=True)

app.add_typer(providers_app, name="providers")
app.add_typer(consulta_app, name="consulta")
app.add_typer(base_app, name="base")


@app.callback()
def main_callback(
    version: bool = typer.Option(False, "--version", help="Mostra a versao"),
) -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if version:
        console.print(f"cnpj-consulta {__version__}")
        raise typer.Exit()


@app.command()
def init_db() -> None:
    """Inicializa o banco SQLite principal."""
    from app.db import init_database

    init_database()
    console.print("[green]Banco inicializado em[/green]", get_settings().database_url)


@app.command()
def consultar(
    cnpj: str = typer.Argument(..., help="CNPJ com ou sem mascara"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Saida em JSON"),
    atualizar: bool = typer.Option(False, "--atualizar", help="Ignora o cache e consulta as fontes"),
) -> None:
    """Consulta um CNPJ em todos os provedores habilitados."""
    from app.db import init_database
    from app.services.company_query import query_company

    init_database()
    try:
        result = query_company(cnpj, force_refresh=atualizar)
    except ValueError as e:
        err_console.print(f"[red]Erro:[/red] CNPJ invalido: {cnpj}")
        raise typer.Exit(code=2) from e

    if json_output:
        console.print_json(result.model_dump_json(indent=2))
    else:
        from app.cli_format import format_company_result

        format_company_result(result, console)


@app.command()
def socio(
    nome: str = typer.Argument(..., help="Nome completo ou parcial"),
    uf: str = typer.Option(None, "--uf", help="UF da sede (sigla)"),
    municipio: str = typer.Option(None, "--municipio", help="Municipio da sede"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Maximo de resultados"),
) -> None:
    """Busca empresas por nome de socio (requer base local)."""
    from app.services.partner_search import search_partners

    try:
        results = search_partners(nome, uf=uf, municipio=municipio, limit=limit)
    except RuntimeError as e:
        err_console.print(f"[red]Erro:[/red] {e}")
        raise typer.Exit(code=2) from e

    from app.cli_format import format_partner_results

    format_partner_results(results, console)


@app.command()
def lote(
    arquivo: Path = typer.Argument(..., help="Arquivo texto/CSV com CNPJs (um por linha ou separados)"),
    saida: Path = typer.Option(None, "--csv", help="Grava o resultado em CSV (Excel pt-BR)"),
) -> None:
    """Consulta varios CNPJs de uma vez (mesma regra da pagina /lote)."""
    import asyncio

    from rich.markup import escape

    from app.db import init_database
    from app.providers.registry import get_registry
    from app.services.batch import batch_csv, parse_cnpj_list, run_batch, summarize

    try:
        dados = arquivo.read_bytes()
    except OSError as e:
        err_console.print(f"[red]Erro:[/red] nao foi possivel ler {escape(str(arquivo))}: {escape(str(e))}")
        raise typer.Exit(code=2) from e
    try:
        texto = dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = dados.decode("cp1252", errors="replace")  # CSV "ANSI" salvo pelo Excel pt-BR
    parsed = parse_cnpj_list(texto, max_items=10_000)  # CLI local: sem teto da web
    if parsed.invalidos:
        err_console.print(f"[yellow]Ignorados (invalidos):[/yellow] {escape(', '.join(parsed.invalidos))}")
    if not parsed.validos:
        err_console.print("[red]Erro:[/red] nenhum CNPJ valido no arquivo.")
        raise typer.Exit(code=2)
    init_database()

    async def _run():
        try:
            return await run_batch(parsed.validos)
        finally:
            await get_registry().aclose()

    rows = asyncio.run(_run())
    if saida:  # grava antes de imprimir: o arquivo nao depende da exibicao
        saida.write_bytes(batch_csv(rows, excel=True))
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    for col in ("CNPJ", "Razao social", "Situacao", "UF", "Diverg.", "Veredicto"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.cnpj_formatado,
            escape(r.razao_social or r.mensagem or "-"),
            escape(r.situacao or "-"),
            escape(r.uf or "-"),
            str(r.conflitos),
            r.veredicto or "-",
        )
    console.print(table)
    resumo = summarize(rows)
    console.print(
        f"{resumo['total']} consultados · {resumo['ativas']} ativas · "
        f"{resumo['irregulares']} irregulares · {resumo['com_conflito']} com divergencia"
    )
    pendentes = resumo["pendentes"]
    if pendentes:
        console.print(
            f"[yellow]{pendentes} CNPJ(s) sem resposta (limite ou fonte fora do ar).[/yellow] "
            "Rode de novo em 1 minuto: os ja consultados vem do cache."
        )
    if saida:
        console.print(f"[green]CSV gravado em[/green] {escape(str(saida))}")


@providers_app.command("list")
def providers_list() -> None:
    """Lista provedores habilitados."""
    from app.providers.registry import get_registry

    reg = get_registry()
    console.print("[bold]Provedores habilitados:[/bold]")
    for p in reg.all():
        tipo = "espelho" if p.is_mirror_of_rfb else "primaria"
        console.print(f"  - {p.name} ({tipo}) | {p.base_url} | limite: {p.rate_limit_per_minute}/min")


@consulta_app.command("historico")
def consulta_historico(limit: int = typer.Option(20, "--limit", "-n")) -> None:
    """Mostra as ultimas consultas realizadas."""
    from app.services.history_service import recent_queries

    rows = recent_queries(limit=limit)
    if not rows:
        console.print("Nenhuma consulta registrada.")
        return
    for r in rows:
        console.print(f"  {r['queried_at']}  {r['cnpj']}  {r.get('razao_social') or '-'}")


def _gb(n: int | float | None) -> str:
    return f"{(n or 0) / 1024**3:.2f} GB"


@base_app.command("status")
def base_status() -> None:
    """Mostra a versao ativa, contagens, disco e a ultima tarefa."""
    from app.db import init_database
    from app.receita.servico import estado_geral

    init_database()
    e = estado_geral()
    console.print(f"[bold]Situacao:[/bold] {e['status']}")
    a = e["ativa"]
    if a:
        console.print(f"Versao ativa: {a['mes'] or '?'} ({a['arquivo']}, {_gb(a['tamanho'])})")
        for t, n in a["contagens"].items():
            console.print(f"  {t}: {n:,}")
    v = e["verificacao"]
    if v.get("mes_remoto"):
        console.print(f"Mais recente no portal: {v['mes_remoto']} (verificado em {v['verificado_em'][:16]})")
    console.print(f"Disco: {_gb(e['disco']['usado'])} usados, {_gb(e['disco']['livre'])} livres em {e['disco']['pasta']}")
    j = e["ultimo_job"]
    if j:
        console.print(f"Ultima tarefa #{j['id']} ({j['tipo']} {j['mes'] or ''}): {j['status_efetivo']} - {j['mensagem'] or ''}")


@base_app.command("verificar")
def base_verificar() -> None:
    """Consulta o portal oficial e informa se ha versao nova."""
    from app.db import init_database
    from app.receita.servico import verificar_atualizacao

    init_database()
    r = verificar_atualizacao()
    if r.get("erro"):
        err_console.print(f"[red]Erro:[/red] {r['erro']}")
        raise typer.Exit(code=2)
    console.print(f"Versao mais recente: [bold]{r['mes_remoto']}[/bold] ({len(r['arquivos'])} arquivos, {_gb(r['tamanho_total'])})")
    console.print(f"Base local: {r['mes_local'] or 'nao instalada'}")
    console.print("[yellow]Ha versao nova.[/yellow]" if r["nova_versao"] else "[green]Base em dia.[/green]")


@base_app.command("atualizar")
def base_atualizar(
    mes: str = typer.Option(None, "--mes", help="AAAA-MM; padrao: a mais recente do portal"),
    forcar: bool = typer.Option(False, "--forcar", help="Ignora avisos de espaco e de queda de contagem"),
    segundo_plano: bool = typer.Option(False, "--segundo-plano", help="Executa num processo separado"),
) -> None:
    """Baixa, valida, importa e publica a base oficial (retoma se interrompida)."""
    from app.db import init_database
    from app.receita import jobs
    from app.receita.servico import executar_job, iniciar_atualizacao

    init_database()
    try:
        job = iniciar_atualizacao(mes, forcar=forcar, origem="cli", em_processo=segundo_plano)
    except jobs.JobEmAndamento as e:
        err_console.print(f"[red]Erro:[/red] {e} Use `cnpj base retomar` ou `cnpj base cancelar`.")
        raise typer.Exit(code=2) from e
    jobs.auditar("base.atualizar", detalhe=f"cli job={job['id']} mes={mes or 'mais recente'}")
    if segundo_plano:
        console.print(f"Tarefa #{job['id']} iniciada em segundo plano. Acompanhe com `cnpj base status`.")
        return
    _executar_com_saida(job["id"], executar_job)


def _executar_com_saida(job_id: int, executar) -> None:
    import logging as _logging

    _logging.getLogger("app.receita").setLevel(_logging.INFO)
    status = executar(job_id)
    from app.receita import jobs

    j = jobs.obter(job_id) or {}
    cor = {"concluido": "green", "pausado": "yellow", "cancelado": "yellow"}.get(status, "red")
    console.print(f"[{cor}]Tarefa #{job_id}: {status}[/{cor}] {j.get('mensagem') or ''}")
    if j.get("erro"):
        err_console.print(j["erro"])
    if status != "concluido":
        raise typer.Exit(code=0 if status in ("pausado", "cancelado") else 1)


@base_app.command("executar", hidden=True)
def base_executar(job_id: int = typer.Argument(...)) -> None:
    """Processo de trabalho (usado pelo painel)."""
    from app.receita.servico import executar_job

    executar_job(job_id)


@base_app.command("retomar")
def base_retomar(job_id: int = typer.Argument(None, help="Padrao: a ultima tarefa")) -> None:
    """Retoma uma atualizacao pausada, interrompida ou com falha."""
    from app.db import init_database
    from app.receita import jobs
    from app.receita.servico import executar_job, retomar

    init_database()
    job_id = job_id or (jobs.ultimo_job("atualizar") or {}).get("id")
    if not job_id:
        err_console.print("Nenhuma tarefa para retomar.")
        raise typer.Exit(code=2)
    try:
        retomar(job_id, em_processo=False)
    except (ValueError, LookupError, jobs.JobEmAndamento) as e:
        err_console.print(f"[red]Erro:[/red] {e}")
        raise typer.Exit(code=2) from e
    _executar_com_saida(job_id, executar_job)


@base_app.command("cancelar")
def base_cancelar(job_id: int = typer.Argument(None)) -> None:
    """Pede o cancelamento da tarefa em andamento (a base ativa nao muda)."""
    from app.db import init_database
    from app.receita import jobs

    init_database()
    job_id = job_id or (jobs.job_ativo() or jobs.ultimo_job() or {}).get("id")
    if not job_id:
        err_console.print("Nenhuma tarefa.")
        raise typer.Exit(code=2)
    try:
        jobs.solicitar(job_id, "cancelar")
    except (ValueError, LookupError) as e:
        err_console.print(f"[red]Erro:[/red] {e}")
        raise typer.Exit(code=2) from e
    console.print(f"Cancelamento solicitado para a tarefa #{job_id}.")


@base_app.command("validar")
def base_validar() -> None:
    """Confere a base ativa (esquema, contagens, orfaos, consultas de amostra)."""
    from app.db import init_database
    from app.receita.servico import executar_job, iniciar_validacao

    init_database()
    job = iniciar_validacao(em_processo=False)
    _executar_com_saida(job["id"], executar_job)


@base_app.command("restaurar")
def base_restaurar() -> None:
    """Volta para a versao anterior da base."""
    from app.receita import armazenamento, jobs

    try:
        nome = armazenamento.restaurar_anterior()
    except FileNotFoundError as e:
        err_console.print(f"[red]Erro:[/red] {e}")
        raise typer.Exit(code=2) from e
    jobs.auditar("base.restaurar", detalhe=f"cli arquivo={nome}")
    console.print(f"[green]Versao ativa:[/green] {nome}")


@base_app.command("limpar")
def base_limpar() -> None:
    """Apaga arquivos temporarios de atualizacoes interrompidas."""
    from app.db import init_database
    from app.receita import jobs
    from app.receita.servico import limpar_trabalho

    init_database()
    try:
        liberado = limpar_trabalho()
    except jobs.JobEmAndamento as e:
        err_console.print(f"[red]Erro:[/red] {e}")
        raise typer.Exit(code=2) from e
    console.print(f"{_gb(liberado)} liberados.")


@app.command(hidden=True)
def sync_receita(
    mes: str = typer.Option(None, "--mes", help="AAAA-MM (padrao: mais recente)"),
) -> None:
    """[Obsoleto] Use `cnpj base atualizar`."""
    console.print("[yellow]`sync-receita` foi substituido por `cnpj base atualizar`.[/yellow]")
    base_atualizar(mes=mes, forcar=False, segundo_plano=False)


@app.command()
def mcp(
    http: bool = typer.Option(False, "--http", help="Streamable HTTP em vez de stdio"),
    porta: int = typer.Option(8765, "--porta", help="Porta do modo HTTP"),
) -> None:
    """Servidor MCP para assistentes de IA (Claude, Cursor...). Ver MCP.md."""
    from app.mcp_server import main as mcp_main

    mcp_main(["--http", "--porta", str(porta)] if http else [])


@app.command()
def admin_token() -> None:
    """Mostra o token de acesso a area administrativa."""
    from app.services.admin_auth import token_admin

    console.print(token_admin())


@app.command()
def limpar_historico() -> None:
    """Apaga o historico local de consultas."""
    from app.services.history_service import clear_history

    n = clear_history()
    console.print(f"[green]{n} entradas removidas.[/green]")


@app.command()
def limpar_cache() -> None:
    """Apaga o cache local de respostas."""
    from app.services.cache import clear_cache

    n = clear_cache()
    console.print(f"[green]{n} entradas removidas do cache.[/green]")


if __name__ == "__main__":
    app()
