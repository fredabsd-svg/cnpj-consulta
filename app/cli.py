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

app.add_typer(providers_app, name="providers")
app.add_typer(consulta_app, name="consulta")


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


@app.command()
def sync_receita(
    mes: str = typer.Option(..., "--mes", help="Mes da base no formato YYYY-MM"),
    keep_zip: bool = typer.Option(False, "--keep-zip", help="Nao apagar ZIPs apos importar"),
    only_download: bool = typer.Option(False, "--only-download", help="So baixar, sem importar"),
    only_import: bool = typer.Option(False, "--only-import", help="So importar ZIPs/CSVs ja baixados"),
    data_dir: Path = typer.Option(None, "--data-dir", help="Padrao: pasta de RECEITA_LOCAL_PATH"),
) -> None:
    """Baixa e importa a base oficial da Receita Federal."""
    from app.sync.receita_federal import ReceitaFederalSync, SyncError

    target_dir = data_dir or Path(get_settings().receita_local_path).parent
    try:
        sync = ReceitaFederalSync(
            mes=mes,
            data_dir=target_dir,
            keep_zip=keep_zip,
            only_download=only_download,
            only_import=only_import,
            progress=lambda msg: console.print(f"[cyan]>[/cyan] {msg}"),
        )
        sync.run()
    except (ValueError, SyncError) as e:
        err_console.print(f"[red]Erro:[/red] {e}")
        raise typer.Exit(code=2) from e

    if not get_settings().receita_local_enabled:
        console.print(
            "[yellow]Base pronta. Para usa-la, defina RECEITA_LOCAL_ENABLED=true no .env "
            "e reinicie o servidor.[/yellow]"
        )


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
