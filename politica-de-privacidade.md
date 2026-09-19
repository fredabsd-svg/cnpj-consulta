# Politica de Privacidade

Aplicativo: **CNPJ Consulta**
Ultima atualizacao: 2026-09-18

## 1. Dados que consultamos

Este aplicativo consulta exclusivamente **dados publicos** disponibilizados por:

- Receita Federal do Brasil (base oficial em https://dados-abertos.rfb.gov.br/CNPJ/)
- BrasilAPI (https://brasilapi.com.br) - proxy da Minha Receita
- Minha Receita (https://minhareceita.org)
- ReceitaWS (https://www.receitaws.com.br)
- CNPJ.ws (https://publica.cnpj.ws)

Esses dados sao publicos por forca da **Lei de Acesso a Informacao (Lei 12.527/2011, art. 8&ordm;)**, e cada provedor pode ter licenca/termos proprios (consulte as paginas oficiais).

## 2. Dados pessoais - o que NAO fazemos

Em respeito a **LGPD (Lei 13.709/2018)** e ao **CTN (Lei 5.172/1966, art. 198)**, este aplicativo **NUNCA**:

- Tenta descobrir, inferir ou desmascarar CPF de socios.
- Busca telefones pessoais, WhatsApp ou enderecos residenciais.
- Busca datas de nascimento ou outros dados privados.
- Busca e-mails pessoais.
- Cruza dados para construir perfis.
- Compartilha dados com terceiros.
- Usa dados para spam, assedio ou prospeccao abusiva.

## 3. Dados pessoais - o que exibimos

Quando as fontes publicas fornecem, exibimos:

- **Nome** do socio/administrador (publico no QSA do CNPJ).
- **CPF/CNPJ do socio**: sempre que vier da fonte, vem **mascarado** (ex.: `***123456**`). Este aplicativo nao tenta reverter a mascara.
- **Qualificacao** (Presidente, Socio-Administrador, etc.).
- **Data de entrada** na sociedade.
- **Representante legal** (quando informado pela fonte).
- **Faixa etaria** agregada (ex.: "Entre 41 a 50 anos") - nunca a idade exata.
- **Telefones e e-mails comerciais** da empresa (nao pessoais).

## 4. Dados armazenados localmente

Em respeito ao principio da **minimizacao** (LGPD art. 6&ordm;, VII), o aplicativo armazena **apenas no seu computador**:

| Dado | Finalidade | Retencao |
|---|---|---|
| Historico de CNPJs consultados | Permitir voltar a consultas recentes | Ate apagamento manual |
| Cache de respostas | Evitar refazer consultas identicas | `CACHE_TTL_SECONDS` (padrao 24h) |
| Favoritos | Marcadores pessoais | Ate remocao manual |

Esses dados **NAO sao enviados a nenhum servidor externo**. Voce pode apaga-los a qualquer momento pela pagina `/historico` ou via CLI:

```bash
python -m app.cli limpar-historico
```

## 5. Base legal de tratamento

- **LGPD** - Lei 13.709/2018, especialmente:
  - Art. 6&ordm; (principios: finalidade, necessidade, adequacao, transparencia)
  - Art. 7&ordm; (hipoteses de tratamento - execucao de politicas publicas, interesse publico)
  - Art. 18 (direitos do titular)
- **LAI** - Lei 12.527/2011, art. 8&ordm; (dados publicos)
- **CTN** - Lei 5.172/1966, art. 198 (sigilo de CPF de socios)

## 6. Direitos do titular (LGPD art. 18)

Se voce e titular de dados exibidos por este aplicativo e deseja correcao ou remocao, voce pode:

1. Solicitar diretamente a fonte (Receita Federal, Minha Receita etc.).
2. Entrar em contato pelo e-mail abaixo para orientacao.

**E-mail de contato:** `contato@example.com`

Como os dados sao publicos e fornecidos por fontes oficiais, a fonte primaria (Receita Federal) tem o prazo legal de atualizacao.

## 7. Limitacao de responsabilidade

- Os dados podem estar desatualizados (as fontes atualizam mensalmente ou sob demanda).
- Este software NAO substitui a consulta oficial em https://www.gov.br/receitafederal/.
- Cada fonte tem sua propria politica de precisao e disponibilidade.
- O uso deste software e de **inteira responsabilidade do usuario**.

## 8. Restricoes de uso

Este software NAO deve ser usado para:

- Spam, assedio ou prospeccao abusiva.
- Discriminacao ou violacao de direitos humanos.
- Re-identificacao de pessoas a partir de dados publicos.
- Comercializacao ilegal de dados (respeitar termos de cada fonte).
- Qualquer finalidade ilicita.

## 9. Encarregado de protecao de dados (DPO)

Para questoes sobre tratamento de dados pessoais neste aplicativo:

- **E-mail:** `dpo@example.com`
- **Prazo de resposta:** ate 15 dias uteis.

## 10. Alteracoes nesta politica

Esta politica pode ser atualizada a qualquer momento. A data da ultima atualizacao esta indicada no topo. Alteracoes substanciais serao comunicadas via release notes do projeto.

## 11. Codigo aberto

Este software e distribuido sob **licenca MIT**. O codigo-fonte pode ser auditado por qualquer pessoa.

---

**Aviso final:** Este aplicativo foi desenvolvido para **uso etico e legal** de dados publicos. O respeito a LGPD e ao CTN nao e opcional.
