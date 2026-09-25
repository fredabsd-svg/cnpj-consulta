"""Documentos e consultas nos sites oficiais (Receita Federal e SEFAZ).

O app NAO emite nem reproduz documento oficial: ele abre o proprio site do
orgao, com o CNPJ ja preenchido quando o site aceita, e o usuario resolve o
captcha ou faz login la. Nada aqui contorna captcha ou autenticacao.

- Cartao CNPJ (Comprovante de Inscricao e de Situacao Cadastral): o app da
  Receita le `?cnpj=` e preenche o campo; exige hCaptcha. Pode ser exibido
  dentro do app (iframe), pois o site nao proibe o enquadramento.
- CCC (Cadastro Centralizado de Contribuintes, SVRS): consulta de inscricao
  estadual em todas as UFs; exige login gov.br e proibe iframe, entao abre em
  nova janela.
"""

from __future__ import annotations

from urllib.parse import quote

RECEITA_ORIGIN = "https://solucoes.receita.fazenda.gov.br"
_COMPROVANTE = RECEITA_ORIGIN + "/Servicos/cnpjreva/Cnpjreva_Solicitacao.asp?cnpj={cnpj}"
CCC_PORTAL_URL = "https://dfe-portal.svrs.rs.gov.br/NFE/CCC"
SINTEGRA_URL = "https://www.sintegra.gov.br/"


def comprovante_url(cnpj: str) -> str:
    """Pagina oficial do Cartao CNPJ com o numero (14 caracteres) preenchido."""
    return _COMPROVANTE.format(cnpj=quote(cnpj, safe=""))
