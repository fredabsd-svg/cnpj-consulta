# Relatório Final — CNPJ Consulta

> Entregável 13 do escopo original. Documenta o que o app entrega, o que
> cada fonte fornece, limitações conhecidas e como evoluir.

## 1. Visão geral

Aplicativo **local** de consulta de CNPJ com:

- 5 fontes de dados (4 online + 1 base oficial local);
- interface web em português (HTMX + Jinja2, sem Node.js);
- API REST documentada (OpenAPI em `/docs`);
- CLI para automação;
- busca reversa por sócio (somente com base local);
- cache SQLite, histórico de consultas, exportação CSV/JSON;
- LGPD/LAI: dados pessoais minimizados, CPF sempre mascarado, aviso de
  uso responsável, sem armazenamento além do necessário.

Stack: **Python 3.12+ · FastAPI · SQLAlchemy 2 · SQLite · DuckDB ·
HTTPX · Pydantic v2 · Jinja2 · HTMX · Typer/Rich**.

## 2. Fontes utilizadas

| Fonte | Endpoint | Categoria |
|---|---|---|
| **Minha Receita** | `https://minhareceita.org/{cnpj}` | primária (RFB indireta) |
| **BrasilAPI** | `https://brasilapi.com.br/api/cnpj/v1/{cnpj}` | proxy da Minha Receita |
| **Receita Federal local** | `python -m app.cli sync-receita --mes YYYY-MM` | primária (oficial) |
| **ReceitaWS** | `https://www.receitaws.com.br/v1/cnpj/{cnpj}` | espelho agregado |
| **CNPJ.ws** | `https://publica.cnpj.ws/cnpj/{cnpj}` | espelho (RFB + SEFAZ + Sintegra) |

### 2.1. Funções que funcionam **sem API key**

Todas elas. Nenhuma fonte usada exige chave. As flags em `.env` (`*_API_KEY`)
existem apenas para que o usuário possa adicionar provedores pagos no futuro
— hoje estão reservadas e ignoradas.

### 2.2. Funções que dependem **apenas da base local** (offline)

- **Busca reversa por sócio** (`/api/partners/search?q=…`) — exige
  `RECEITA_LOCAL_ENABLED=true` e base sincronizada.
- **Filiais** e **empresas em que um CNPJ é sócio** (`/api/partners/{cnpj}/companies`).
- **Consulta por CNPJ offline**: com a base local habilitada ela é
  consultada junto com as demais fontes (e tem prioridade); se as APIs
  estiverem fora, os dados vêm só dela.

## 3. Limites de cada fonte

| Fonte | Rate limit configurável | Padrão | Observações |
|---|---|---|---|
| Minha Receita | `RATE_LIMIT_MINHA_RECEITA` | 30/min | API pública, sem SLA formal. Pode ter instabilidade. |
| BrasilAPI | `RATE_LIMIT_BRASILAPI` | 60/min | Proxy da Minha Receita; respostas costumam ser idênticas. |
| ReceitaWS | `RATE_LIMIT_RECEITAWS` | 3/min | Plano gratuito: 3 req/min. Responde 429 se exceder. |
| CNPJ.ws | `RATE_LIMIT_CNPJWS` | 3/min | Plano gratuito limitado. |
| Receita Federal local | — | sem limite | ~40 GB livres durante a importação. |

Todas as fontes HTTP implementam:

- **Limite local por fonte** (janela deslizante de 60 s): no limite, a
  fonte é pulada imediatamente e aparece como "limite atingido" — a
  consulta não espera por ela.
- **429 do servidor**: não é repetido (só gastaria a cota); a fonte fica
  em pausa pelo `Retry-After` (ou 60 s).
- **Retry exponencial** só para `5xx` e erro de rede
  (`REQUEST_MAX_RETRIES=2`, base `REQUEST_BACKOFF_SECONDS=1.0`).
- **Timeout** por requisição (`REQUEST_TIMEOUT_SECONDS`, padrão 10 s) e
  teto por consulta (`REQUEST_TOTAL_TIMEOUT_SECONDS`, padrão 20 s).

## 4. Campos fornecidos por cada fonte

| Campo | Minha Receita | BrasilAPI | ReceitaWS | CNPJ.ws | Receita local |
|---|---|---|---|---|---|
| `cnpj` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `razao_social` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `nome_fantasia` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `situacao_cadastral` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `data_situacao_cadastral` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `data_abertura` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `natureza_juridica` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `porte` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `capital_social` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `endereco` (logradouro/num/bairro/UF/município/CEP) | ✓ | ✓ | ✓ | ✓ | ✓ |
| `telefones` (DDD + número) | ✓ | ✓ | ✓ | ✓ | ✓ |
| `email` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cnae_principal` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cnaes_secundarios` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `opcao_simples` / `opcao_mei` | ✓ | ✓ | ✓ | ✓ | ✓ (arquivo Simples) |
| `matriz_filial` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `filiais` (CNPJs) | ✗ | ✗ | ✗ | ✗ | ✓ |
| `socios` (QSA) | ✓ | ✓ | só nome+qual | ✓ (mascarado) | ✓ (mascarado) |
| `data_atualizacao_fonte` | parcial | parcial | ✓ | parcial | ✓ (sync) |

> ReceitaWS **não retorna CPF** de sócios (apenas nome e qualificação).
> ReceitaWS e CNPJ.ws retornam o **QSA com CPF já mascarado**.
> A base local (Receita Federal) também só tem CPF mascarado (CTN art. 198).

## 5. Fontes que são espelhos da Receita Federal

Marcadas em `is_mirror_of_rfb=True`:

| Fonte | Por quê |
|---|---|
| **BrasilAPI** | A própria documentação declara ser um proxy da API Minha Receita, que por sua vez é uma reformulação da base pública da RFB. |
| **ReceitaWS** | Agrega "fontes públicas" via scraping. Não publica os termos da Cláusula Oitava (proíbe revenda via API pública), mas os dados são derivados da RFB. |
| **CNPJ.ws** | Consolida RFB + SEFAZ + Sintegra em um único payload. |

Marcadas em `is_mirror_of_rfb=False`:

| Fonte | Por quê |
|---|---|
| **Minha Receita** | Fonte primária recomendada (sem restrições comerciais, MIT, sem chave). |
| **Receita Federal local** | A própria base oficial da RFB, sincronizada via download público mensal. |

A tabela de proveniência (`campos_procedencia` no JSON) sempre indica
qual fonte forneceu cada campo e marca `espelho_rfb=True` quando
aplicável. O conciliador **prioriza fontes primárias** quando há
conflito entre espelho e primária.

## 6. Como cada fonte é exposta na UI / API

- **API**: `GET /api/companies/{cnpj}` retorna `CompanyUnified` com
  `fontes[]` (tabela de procedência completa), `campos_procedencia[]`
  (campo a campo) e `conflitos[]` (lista de divergências detectadas).
- **API auxiliares**:
  - `GET /api/companies/{cnpj}/sources` — apenas procedência.
  - `GET /api/companies/{cnpj}/partners` — apenas QSA.
  - `GET /api/companies/{cnpj}/export.json` — download JSON.
  - `GET /api/companies/{cnpj}/export.csv` — download CSV
    (`?flatten=true` para 1 linha; padrão 2 colunas campo/valor).
- **Web**: `/empresa/{cnpj}` exibe um resumo e 5 abas (Dados cadastrais,
  Sócios, Atividades, Estabelecimentos, Fontes com procedência campo a
  campo), além de favoritar, atualizar, exportar e imprimir.
- **CLI**: `python -m app.cli consultar 19131243000197 --json`.

## 7. Limitações conhecidas

1. **Defasagem**: a base da RFB é mensal. Meses sem sync têm dados
   antigos — a sincronização local mitiga isso quando habilitada.
2. **CPF sempre mascarado**: por força da LGPD + CTN art. 198, nenhuma
   fonte confiável fornece CPF completo; o app nunca tenta inferir ou
   desmascarar. `documento_mascarado` sempre exibe `***NNNNNN**`.
3. **ReceitaWS — restrição comercial**: a Cláusula Oitava dos termos
   proíbe revenda via API pública. Use localmente ou contrate o plano
   comercial antes de redistribuir. O app exibe um aviso no README.
4. **CNPJ.ws e ReceitaWS — instabilidade**: o plano gratuito pode
   recusar requisições com captcha ou erro 429. O app tenta 3 vezes com
   backoff, mas em horário de pico pode falhar.
5. **Busca por sócio**: requer base local (10 GB). Sem base, o app não
   oferece alternativa online porque nenhuma API pública gratuita expõe
   busca por nome.
6. **SSRF / proxy**: o cliente HTTP tem `follow_redirects=True`. Em
   deployments em redes que precisam bloquear SSRF, ajuste
   `httpx.AsyncClient(..., follow_redirects=False)` em
   `app/providers/base.py`.
7. **Sem autenticação**: o app assume uso local single-user. Há CSP
   estrita, bloqueio de POST/DELETE de outra origem (CSRF) e cabeçalhos
   anti-clickjacking, mas para expor em rede adicione autenticação antes
   (ex.: reverse proxy com basic auth).
8. **Logs podem conter CNPJ**: os logs guardam o CNPJ consultado, mas
   nunca o CPF de sócios ou dados sensíveis. Use `LOG_LEVEL=WARNING`
   se precisar reduzir.
9. **Endereço da Receita**: a RFB já mudou o endereço dos dados abertos
   mais de uma vez. O padrão é
   `https://arquivos.receitafederal.gov.br/dados/cnpj/dados_abertos_cnpj/{AAAA-MM}/`;
   se o `sync-receita` responder 404, ajuste `RECEITA_BASE_URL`.

## 8. Como atualizar ou substituir uma fonte

### 8.1. Atualizar a base local (Receita Federal)

```bash
python -m app.cli sync-receita --mes 2026-08
```

Opções:

- `--keep-zip` — não apaga os ZIPs após extrair.
- `--only-download` — só baixa, sem importar.
- `--only-import` — só importa (assume arquivos já extraídos).

A base substitui a anterior (`DuckDB` é sobrescrito). Use `--data-dir`
para trocar o diretório padrão `./data/receita`.

### 8.2. Trocar a URL base de uma fonte

Em `.env`:

```env
BRASILAPI_BASE_URL=https://brasilapi.com.br/api/cnpj/v1
MINHA_RECEITA_BASE_URL=https://minhareceita.org
RECEITAWS_BASE_URL=https://www.receitaws.com.br/v1
CNPJWS_BASE_URL=https://publica.cnpj.ws
```

Reinicie o servidor. Sem alteração de código.

### 8.3. Habilitar / desabilitar uma fonte

Em `.env`:

```env
BRASILAPI_ENABLED=true
MINHA_RECEITA_ENABLED=true
RECEITAWS_ENABLED=false      # desliga ReceitaWS
CNPJWS_ENABLED=false          # desliga CNPJ.ws
RECEITA_LOCAL_ENABLED=true    # liga base local
```

O registry monta automaticamente apenas os habilitados. O endpoint
`GET /api/providers/status` reflete o estado atual.

### 8.4. Adicionar uma nova fonte

Crie um adapter em `app/providers/<nome>.py`:

```python
from app.providers.base import Provider, ProviderResult


class MinhaNovaFonteProvider(Provider):
    name = "minha_nova_fonte"
    is_mirror_of_rfb = False  # ou True, conforme o caso

    @property
    def enabled(self) -> bool:
        return bool(self.settings.minha_nova_fonte_enabled)

    @property
    def base_url(self) -> str:
        return self.settings.minha_nova_fonte_base_url.rstrip("/")

    def _build_url(self, cnpj: str) -> str:
        return f"{self.base_url}/cnpj/{cnpj}"

    async def fetch_company(self, cnpj: str) -> ProviderResult:
        url = self._build_url(cnpj)
        status, body = await self._request_json(url)
        # ... normalizar ...
        return ProviderResult(
            provider=self.name,
            raw=body,
            http_status=status,
            url=url,
            is_mirror_of_rfb=self.is_mirror_of_rfb,
        )
```

Depois:

1. Adicione as flags em `app/config.py` (`Settings.minha_nova_fonte_*`).
2. Atualize `.env.example` com as novas chaves.
3. Registre a classe em `app/providers/registry.py`.
4. Adicione um teste em `tests/test_<nome>_provider.py` com payload real
   (use fixtures em `tests/fixtures/`).
5. Atualize a tabela de procedência no JSON de resposta.

### 8.5. Substituir o conciliador

`app/services/reconciliation.py` é o único ponto de merge. A ordem de
prioridade está em `_PRIORITY` e os grupos de fontes não independentes
em `_SOURCE_GROUP`; cada fonte tem um extrator em `_scalar_fields()`.
Toda a UI/API consome o `CompanyUnified` final, sem acoplamento ao
conciliador.

## 9. Entregáveis x status

| # | Entregável | Status |
|---|---|---|
| 1 | Validação matemática de CNPJ | ✓ |
| 2 | Múltiplas fontes gratuitas em paralelo | ✓ |
| 3 | Retry, fallback, rate limit | ✓ |
| 4 | Reconciliação com proveniência | ✓ |
| 5 | Cache e histórico | ✓ |
| 6 | Sincronização da base local | ✓ |
| 7 | API REST + OpenAPI | ✓ |
| 8 | Interface web (Jinja2 + JS próprio, sem CDN nem Node.js) | ✓ |
| 9 | CLI | ✓ |
| 10 | LGPD (máscara, minimização, logs) | ✓ |
| 11 | Política de privacidade | ✓ |
| 12 | Testes automatizados (`pytest`) | ✓ |
| 13 | **Este relatório** | ✓ |

## 10. Como rodar localmente

```bash
git clone <repo> cnpj-consulta
cd cnpj-consulta
./setup.sh        # ou .\setup.ps1 no Windows
./start.sh        # ou .\start.ps1 no Windows
```

Acesse `http://127.0.0.1:8000`.

Para a base local:

```bash
python -m app.cli init-db
python -m app.cli sync-receita --mes 2025-09
```

## 11. Aviso legal

Os dados consultados são públicos (Lei de Acesso à Informação
12.527/2011, art. 8º). Este app **não inclui, vende ou revende**
informações pessoais. O usuário é responsável pelo uso e pelas
finalidades (LGPD art. 7º). Veja `politica-de-privacidade.md`.
