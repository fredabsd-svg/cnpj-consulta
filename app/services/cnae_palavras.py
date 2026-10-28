"""Palavras-chave de atividade -> codigos oficiais da CNAE 2.3 (IBGE/CONCLA).

A busca por atividade SEMPRE usa o CNAE da Receita (principal e/ou
secundarios), nunca o nome da empresa. Estas palavras sao atalhos que o app
traduz em codigos e mostra ao usuario quais foram usados. Prefixos de 2
digitos sao divisoes, de 3 sao grupos, de 4/5 sao classes e de 7 sao
subclasses da CNAE.
"""

from __future__ import annotations

PALAVRAS_CHAVE: dict[str, dict] = {
    "contabilidade": {"rotulo": "Contabilidade e auditoria", "prefixos": ["6920"]},
    "advocacia": {"rotulo": "Advocacia e serviços jurídicos", "prefixos": ["6911"]},
    "tecnologia": {"rotulo": "Tecnologia da informação e software", "prefixos": ["62", "631"]},
    "saude": {"rotulo": "Saúde humana", "prefixos": ["86"]},
    "odontologia": {"rotulo": "Odontologia", "prefixos": ["8630504"]},
    "educacao": {"rotulo": "Educação", "prefixos": ["85"]},
    "construcao": {"rotulo": "Construção", "prefixos": ["41", "42", "43"]},
    "engenharia": {"rotulo": "Engenharia e arquitetura", "prefixos": ["711"]},
    "comercio_varejista": {"rotulo": "Comércio varejista", "prefixos": ["47"]},
    "comercio_atacadista": {"rotulo": "Comércio atacadista", "prefixos": ["46"]},
    "veiculos": {"rotulo": "Comércio e reparação de veículos", "prefixos": ["45"]},
    "farmacia": {"rotulo": "Farmácias e drogarias", "prefixos": ["47717"]},
    "alimentacao": {"rotulo": "Restaurantes e alimentação", "prefixos": ["56"]},
    "hospedagem": {"rotulo": "Hotéis e hospedagem", "prefixos": ["55"]},
    "transporte": {"rotulo": "Transporte e armazenagem", "prefixos": ["49", "50", "51", "52", "53"]},
    "industria": {"rotulo": "Indústria de transformação",
                  "prefixos": [f"{d:02d}" for d in range(10, 34)]},
    "agropecuaria": {"rotulo": "Agropecuária, pesca e florestal", "prefixos": ["01", "02", "03"]},
    "imobiliario": {"rotulo": "Atividades imobiliárias", "prefixos": ["68"]},
    "financeiro": {"rotulo": "Serviços financeiros e seguros", "prefixos": ["64", "65", "66"]},
    "consultoria": {"rotulo": "Consultoria em gestão empresarial", "prefixos": ["702"]},
    "publicidade": {"rotulo": "Publicidade e marketing", "prefixos": ["731"]},
    "beleza": {"rotulo": "Cabeleireiros e estética", "prefixos": ["96025"]},
    "academia": {"rotulo": "Academias e atividades esportivas", "prefixos": ["931"]},
    "veterinaria": {"rotulo": "Veterinária", "prefixos": ["75"]},
    "limpeza": {"rotulo": "Limpeza e conservação", "prefixos": ["812"]},
    "seguranca": {"rotulo": "Vigilância e segurança", "prefixos": ["801", "802"]},
    "eventos": {"rotulo": "Organização de eventos", "prefixos": ["8230"]},
    "telecomunicacoes": {"rotulo": "Telecomunicações", "prefixos": ["61"]},
    "energia": {"rotulo": "Energia elétrica e gás", "prefixos": ["35"]},
}


def faixas(prefixo: str) -> tuple[int, int]:
    """[inicio, fim) dos codigos de 7 digitos que comecam com o prefixo."""
    n = len(prefixo)
    base = int(prefixo) * 10 ** (7 - n)
    return base, base + 10 ** (7 - n)
