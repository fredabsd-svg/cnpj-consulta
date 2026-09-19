"""Testes do conciliador de resultados."""

from __future__ import annotations

from datetime import UTC, datetime

from app.providers.base import ProviderResult
from app.services.reconciliation import reconcile


def _mk(provider: str, raw: dict, *, mirror: bool = False) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        raw=raw,
        http_status=200,
        url=f"https://{provider}/x",
        fetched_at=datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
        is_mirror_of_rfb=mirror,
    )


def _scalar_conflicts(conflitos: list[str]) -> list[str]:
    """Ignora conflitos de campos compostos (logradouro costuma divergir entre fontes)."""
    prefixes = ("Endereco:", "Telefones:", "CNAE principal:", "CNAEs secundarios:")
    return [c for c in conflitos if not c.startswith(prefixes)]


class TestReconcile:
    def test_primary_wins_over_mirror(self, brasilapi_payload, receitaws_payload):
        # BrasilAPI (espelho) e ReceitaWS (espelho); Minha Receita e a primaria
        results = [
            _mk("minha_receita", brasilapi_payload, mirror=False),
            _mk("brasilapi", brasilapi_payload, mirror=True),
            _mk("receitaws", receitaws_payload, mirror=True),
        ]
        c = reconcile("19131243000197", results)
        assert c.razao_social == "OPEN KNOWLEDGE BRASIL"
        assert c.cnpj == "19131243000197"
        assert c.cnpj_formatado == "19.131.243/0001-97"

    def test_no_valid_results_returns_minimal(self):
        results = [
            ProviderResult(
                provider="x", raw={}, http_status=500,
                fetched_at=datetime.now(UTC),
            )
        ]
        c = reconcile("19131243000197", results)
        assert c.cnpj == "19131243000197"
        assert c.razao_social is None
        # Fontes ainda assim sao registradas
        assert len(c.fontes) == 1

    def test_partners_are_merged(self, brasilapi_payload, receitaws_payload):
        # BrasilAPI: socios tem CPF mascarado; ReceitaWS: apenas nome+qual
        results = [
            _mk("minha_receita", brasilapi_payload),
            _mk("receitaws", receitaws_payload, mirror=True),
        ]
        c = reconcile("19131243000197", results)
        assert len(c.socios) == 1
        # Nenhum socio deve ter CPF desmascarado
        for s in c.socios:
            assert s.documento_mascarado is None or "***" in (s.documento_mascarado or "")

    def test_conflict_detection(self):
        # Duas fontes com razao social diferente
        results = [
            _mk("minha_receita", {"razao_social": "EMPRESA A LTDA", "situacao_cadastral": 2}),
            _mk("brasilapi", {"razao_social": "EMPRESA B LTDA", "situacao_cadastral": 2}, mirror=True),
        ]
        c = reconcile("19131243000197", results)
        assert any(conflito.startswith("Razao social") for conflito in c.conflitos)

    def test_same_company_in_different_formats_has_no_scalar_conflict(
        self, brasilapi_payload, receitaws_payload
    ):
        """Regressao: formato diferente ("399-9 - Associacao Privada") nao e conflito."""
        results = [
            _mk("minha_receita", brasilapi_payload),
            _mk("brasilapi", brasilapi_payload, mirror=True),
            _mk("receitaws", receitaws_payload, mirror=True),
        ]
        c = reconcile("19131243000197", results)
        assert _scalar_conflicts(c.conflitos) == []
        assert c.natureza_juridica == "Associacao Privada"
        assert c.situacao_cadastral == "ATIVA"
        assert c.porte == "DEMAIS"

    def test_capital_social_dot_decimal(self, brasilapi_payload, receitaws_payload):
        """Regressao: "150000.00" do ReceitaWS virava 15.000.000,00."""
        receitaws_payload["capital_social"] = "150000.00"
        brasilapi_payload["capital_social"] = 150000
        only_rws = reconcile("19131243000197", [_mk("receitaws", receitaws_payload, mirror=True)])
        assert only_rws.capital_social == 150000.0
        both = reconcile(
            "19131243000197",
            [_mk("minha_receita", brasilapi_payload), _mk("receitaws", receitaws_payload, mirror=True)],
        )
        assert _scalar_conflicts(both.conflitos) == []

    def test_partners_not_duplicated(self, brasilapi_payload, receitaws_payload):
        """Regressao: ReceitaWS sem data de entrada duplicava cada socio."""
        results = [
            _mk("minha_receita", brasilapi_payload),
            _mk("receitaws", receitaws_payload, mirror=True),
        ]
        c = reconcile("19131243000197", results)
        assert len(c.socios) == 1
        socio = c.socios[0]
        assert socio.documento_mascarado == "***123456**"
        assert socio.qualificacao == "Presidente"
        assert socio.fonte == "minha_receita, receitaws"

    def test_confidence_levels(self, brasilapi_payload, receitaws_payload):
        def conf(results, campo="razao_social"):
            c = reconcile("19131243000197", results)
            return {p.confianca for p in c.campos_procedencia if p.campo == campo}

        # Uma fonte so: media (antes saia "alta")
        assert conf([_mk("minha_receita", brasilapi_payload)]) == {"media"}
        # BrasilAPI repassa a Minha Receita: continua uma base so
        assert conf(
            [_mk("minha_receita", brasilapi_payload), _mk("brasilapi", brasilapi_payload, mirror=True)]
        ) == {"media"}
        # Duas bases independentes concordando: alta
        assert conf(
            [_mk("minha_receita", brasilapi_payload), _mk("receitaws", receitaws_payload, mirror=True)]
        ) == {"alta"}

    def test_divergence_is_low_confidence_and_lists_values(self):
        results = [
            _mk("minha_receita", {"razao_social": "EMPRESA A LTDA"}),
            _mk("receitaws", {"nome": "EMPRESA B LTDA"}, mirror=True),
        ]
        c = reconcile("19131243000197", results)
        assert c.razao_social == "EMPRESA A LTDA"  # primaria vence
        assert "minha_receita: EMPRESA A LTDA" in c.conflitos[0]
        assert "receitaws: EMPRESA B LTDA" in c.conflitos[0]
        assert {p.confianca for p in c.campos_procedencia if p.campo == "razao_social"} == {"baixa"}

    def test_phone_from_combined_ddd_field(self, brasilapi_payload):
        """Minha Receita/BrasilAPI mandam DDD+numero juntos em ddd_telefone_1."""
        c = reconcile("19131243000197", [_mk("minha_receita", brasilapi_payload)])
        assert [(t.ddd, t.numero) for t in c.telefones] == [("11", "23851939")]

    def test_cnae_codes_normalized(self, brasilapi_payload, receitaws_payload):
        c = reconcile("19131243000197", [_mk("receitaws", receitaws_payload, mirror=True)])
        assert c.cnae_principal.codigo == "9430800"
        c2 = reconcile("19131243000197", [_mk("minha_receita", brasilapi_payload)])
        assert c2.cnae_principal.codigo == "9430800"

    def test_receita_local_codes_are_translated(self):
        raw = {
            "razao_social": "EMPRESA TESTE LTDA",
            "situacao_cadastral": "02",
            "porte_empresa": "01",
            "capital_social": "1000,00",
            "natureza_juridica": "2062",
            "natureza_juridica_descricao": "Sociedade Empresaria Limitada",
            "identificador_matriz_filial": "1",
            "data_inicio_atividade": "20200115",
            "opcao_simples": "S",
            "opcao_mei": "N",
            "socios": [
                {"nome_socio": "JOAO DA SILVA", "identificador_de_socio": "2",
                 "qualificacao_socio": "49", "qualificacao_socio_descricao": "Socio-Administrador",
                 "data_entrada_sociedade": "20200115", "cpf_cnpj_socio": "***123456**"}
            ],
        }
        c = reconcile("19131243000197", [_mk("receita_local", raw)])
        assert c.situacao_cadastral == "ATIVA"
        assert c.porte == "MICRO EMPRESA"
        assert c.capital_social == 1000.0
        assert c.natureza_juridica == "Sociedade Empresaria Limitada"
        assert c.matriz_filial == "MATRIZ"
        assert c.opcao_simples is True and c.opcao_mei is False
        assert c.data_abertura.isoformat() == "2020-01-15"
        assert c.socios[0].qualificacao == "Socio-Administrador"
        assert c.socios[0].tipo == "PESSOA_FISICA"

    def test_source_metadata_includes_mirror_flag(self):
        results = [
            _mk("minha_receita", {"razao_social": "X"}, mirror=False),
            _mk("brasilapi", {"razao_social": "X"}, mirror=True),
        ]
        c = reconcile("19131243000197", results)
        assert len(c.fontes) == 2
        minha = next(f for f in c.fontes if f.fonte == "minha_receita")
        brasil = next(f for f in c.fontes if f.fonte == "brasilapi")
        assert minha.espelho_rfb is False
        assert brasil.espelho_rfb is True

    def test_compound_provenance_for_address_cnae_phones(self, brasilapi_payload):
        c = reconcile("19131243000197", [_mk("minha_receita", brasilapi_payload)])
        campos = {p.campo for p in c.campos_procedencia}
        assert "endereco" in campos
        assert "telefones" in campos
        assert "cnae_principal" in campos
        assert c.endereco is not None
        assert c.endereco.cep == "01311902"
        phone_prov = [p for p in c.campos_procedencia if p.campo == "telefones"]
        assert phone_prov[0].confianca == "media"

    def test_address_conflict_when_sources_diverge(self):
        a = {
            "razao_social": "EMPRESA X",
            "logradouro": "RUA UM",
            "numero": "10",
            "municipio": "SAO PAULO",
            "uf": "SP",
            "cep": "01001000",
        }
        b = {
            "nome": "EMPRESA X",
            "logradouro": "RUA DOIS",
            "numero": "10",
            "municipio": "SAO PAULO",
            "uf": "SP",
            "cep": "01001000",
        }
        c = reconcile(
            "19131243000197",
            [_mk("minha_receita", a), _mk("receitaws", b, mirror=True)],
        )
        assert any(x.startswith("Endereco:") for x in c.conflitos)
        assert {p.confianca for p in c.campos_procedencia if p.campo == "endereco"} == {"baixa"}
        # Primaria vence
        assert c.endereco is not None
        assert "UM" in (c.endereco.logradouro or "").upper()

    def test_cnae_principal_conflict(self):
        a = {"razao_social": "X", "cnae_fiscal": 6201501, "cnae_fiscal_descricao": "Dev software"}
        b = {
            "nome": "X",
            "atividade_principal": [{"code": "62.01-5-01", "text": "Outro"}],
        }
        # Mesmo codigo apos normalizacao -> sem conflito
        ok = reconcile(
            "19131243000197",
            [_mk("minha_receita", a), _mk("receitaws", b, mirror=True)],
        )
        assert not any(x.startswith("CNAE principal:") for x in ok.conflitos)
        assert ok.cnae_principal.codigo == "6201501"

        b2 = {
            "nome": "X",
            "atividade_principal": [{"code": "47.11-3-01", "text": "Comercio"}],
        }
        bad = reconcile(
            "19131243000197",
            [_mk("minha_receita", a), _mk("receitaws", b2, mirror=True)],
        )
        assert any(x.startswith("CNAE principal:") for x in bad.conflitos)

    def test_phones_agree_across_formats(self, brasilapi_payload, receitaws_payload):
        c = reconcile(
            "19131243000197",
            [
                _mk("minha_receita", brasilapi_payload),
                _mk("receitaws", receitaws_payload, mirror=True),
            ],
        )
        assert not any(x.startswith("Telefones:") for x in c.conflitos)
        phone_conf = {p.confianca for p in c.campos_procedencia if p.campo == "telefones"}
        assert phone_conf == {"alta"}
