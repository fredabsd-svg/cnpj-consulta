"""Schemas Pydantic para empresa, socios e procedencia."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Address(BaseModel):
    """Endereco comercial."""

    logradouro: str | None = None
    numero: str | None = None
    complemento: str | None = None
    bairro: str | None = None
    municipio: str | None = None
    uf: str | None = None
    cep: str | None = None


class Phone(BaseModel):
    """Telefone."""

    ddd: str | None = None
    numero: str | None = None
    tipo: str | None = None  # fixo, celular, fax


class CNAE(BaseModel):
    """CNAE (Classificacao Nacional de Atividades Economicas)."""

    codigo: str
    descricao: str | None = None


class PartnerPublic(BaseModel):
    """Socio ou administrador -- apenas dados publicos."""

    nome: str | None = None
    tipo: str | None = None  # PESSOA_FISICA, PESSOA_JURIDICA, ESTRANGEIRO
    qualificacao: str | None = None
    data_entrada: date | None = None
    documento_mascarado: str | None = None  # CPF/CNPJ ja mascarado pela fonte
    fonte: str  # fontes que informaram o socio, separadas por virgula
    updated_at: datetime | None = None


class FieldProvenance(BaseModel):
    """Proveniencia de um campo especifico."""

    campo: str
    valor: str | None = None
    fonte: str
    consulta_id: str | None = None
    data_consulta: datetime | None = None
    data_atualizacao_fonte: datetime | None = None
    confianca: Literal["alta", "media", "baixa"] = "media"
    observacao: str | None = None


class SourceEntry(BaseModel):
    """Entrada da tabela de procedencia."""

    fonte: str
    url: str | None = None
    status: int
    data_consulta: datetime
    data_atualizacao: datetime | None = None
    campos_fornecidos: list[str] = Field(default_factory=list)
    erro: str | None = None
    espelho_rfb: bool = False


class CompanyUnified(BaseModel):
    """Resposta unificada para um CNPJ."""

    model_config = ConfigDict(extra="forbid")

    cnpj: str
    cnpj_formatado: str
    razao_social: str | None = None
    nome_fantasia: str | None = None
    situacao_cadastral: str | None = None
    data_situacao_cadastral: date | None = None
    motivo_situacao: str | None = None
    data_abertura: date | None = None
    natureza_juridica: str | None = None
    porte: str | None = None
    capital_social: float | None = None
    endereco: Address | None = None
    telefones: list[Phone] = Field(default_factory=list)
    email: str | None = None
    cnae_principal: CNAE | None = None
    cnaes_secundarios: list[CNAE] = Field(default_factory=list)
    opcao_simples: bool | None = None
    opcao_mei: bool | None = None
    matriz_filial: Literal["MATRIZ", "FILIAL"] | None = None
    filiais: list[str] = Field(default_factory=list)
    situacao_especial: str | None = None
    data_ultima_atualizacao: datetime | None = None
    socios: list[PartnerPublic] = Field(default_factory=list)

    # Procedencia
    fontes: list[SourceEntry] = Field(default_factory=list)
    campos_procedencia: list[FieldProvenance] = Field(default_factory=list)
    conflitos: list[str] = Field(default_factory=list)
    origem_cache: bool = False  # True quando a resposta veio do cache local
