"""Formatacao de saida CLI com Rich."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from app.core import formatting as fmt
from app.schemas.company import CompanyUnified


def _kv(console: Console, label: str, value: object) -> None:
    if value not in (None, "", "-"):
        console.print(f"[bold]{label}:[/bold] {escape(str(value))}")


def format_company_result(c: CompanyUnified, console: Console) -> None:
    console.rule(f"[bold]CNPJ {c.cnpj_formatado}[/bold]")
    _kv(console, "Razao social", c.razao_social or "-")
    _kv(console, "Nome fantasia", c.nome_fantasia)
    _kv(console, "Situacao", c.situacao_cadastral)
    if c.data_situacao_cadastral:
        _kv(console, "Desde", fmt.data_br(c.data_situacao_cadastral))
    _kv(console, "Motivo", c.motivo_situacao)
    if c.data_abertura:
        _kv(console, "Abertura", fmt.data_br(c.data_abertura))
    _kv(console, "Natureza juridica", c.natureza_juridica)
    _kv(console, "Porte", c.porte)
    if c.capital_social is not None:
        _kv(console, "Capital social", fmt.brl(c.capital_social))
    if c.endereco:
        e = c.endereco
        cep = fmt.cep(e.cep) if e.cep else None
        _kv(console, "Endereco", ", ".join(filter(None, [e.logradouro, e.numero, e.complemento, e.bairro,
                                                         e.municipio, e.uf, cep])))
    if c.telefones:
        _kv(console, "Telefones", ", ".join(fmt.telefone(t.ddd, t.numero) for t in c.telefones))
    _kv(console, "E-mail", c.email)
    if c.cnae_principal:
        _kv(console, "CNAE principal", f"{fmt.cnae(c.cnae_principal.codigo)} - {c.cnae_principal.descricao or ''}")
    if c.cnaes_secundarios:
        console.print(f"[bold]CNAEs secundarios ({len(c.cnaes_secundarios)}):[/bold]")
        for cn in c.cnaes_secundarios[:20]:
            console.print(f"  - {fmt.cnae(cn.codigo)} - {escape(cn.descricao or '')}")
        if len(c.cnaes_secundarios) > 20:
            console.print(f"  ... e mais {len(c.cnaes_secundarios) - 20}")
    if c.opcao_simples is not None:
        _kv(console, "Simples Nacional", "Sim" if c.opcao_simples else "Nao")
    if c.opcao_mei is not None:
        _kv(console, "MEI", "Sim" if c.opcao_mei else "Nao")
    _kv(console, "Tipo", c.matriz_filial)
    if c.socios:
        console.rule("[bold]Quadro societario[/bold]")
        table = Table(show_header=True, header_style="bold")
        for col in ("Nome", "Qualificacao", "Entrada", "Documento", "Fonte"):
            table.add_column(col)
        for s in c.socios:
            table.add_row(
                escape(s.nome or "-"),
                escape(s.qualificacao or "-"),
                fmt.data_br(s.data_entrada),
                escape(s.documento_mascarado or "-"),
                escape(fmt.fonte(s.fonte)),
            )
        console.print(table)

    if c.conflitos:
        console.rule("[yellow]Divergencias entre fontes[/yellow]")
        for conflict in c.conflitos:
            console.print(f"  [yellow]![/yellow] {escape(conflict)}")

    console.rule("[bold]Fontes consultadas[/bold]")
    if c.origem_cache:
        console.print("  (resposta do cache local; use --atualizar para consultar de novo)")
    for src in c.fontes:
        status_icon = "[green]OK[/green]" if src.status == 200 else "[red]FALHOU[/red]"
        mirror = " (espelho RFB)" if src.espelho_rfb else ""
        upd = f" - atualizado em {fmt.data_br(src.data_atualizacao)}" if src.data_atualizacao else ""
        err = f" - {escape(src.erro)}" if src.erro else ""
        console.print(f"  - {fmt.fonte(src.fonte)}: {status_icon}{mirror}{upd}{err}")


def format_partner_results(rows: list[dict], console: Console) -> None:
    if not rows:
        console.print("Nenhum socio encontrado.")
        return
    console.rule(f"[bold]{len(rows)} resultado(s)[/bold]")
    table = Table(show_header=True, header_style="bold")
    for col in ("Socio", "Empresa", "CNPJ", "Situacao", "Qualificacao", "Municipio/UF"):
        table.add_column(col)
    for r in rows:
        local = "/".join(filter(None, [r.get("municipio"), r.get("uf")])) or "-"
        table.add_row(
            escape(r.get("nome_socio") or "-"),
            escape(r.get("razao_social") or "-"),
            fmt.cnpj(r.get("cnpj")),
            escape(str(r.get("situacao") or "-")),
            escape(str(r.get("qualificacao") or "-")),
            escape(local),
        )
    console.print(table)
