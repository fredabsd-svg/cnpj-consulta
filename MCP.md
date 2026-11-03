# Servidor MCP: use o CNPJ Consulta pelo seu assistente de IA

O CNPJ Consulta tem um servidor **MCP (Model Context Protocol)**. Ele deixa
assistentes de IA como Claude Desktop, Claude Code, Cursor e VS Code (Copilot),
ou qualquer outro cliente MCP, usarem as funções do app: consultar e conferir
CNPJs, montar listas de prospecção, mapear sócios, exportar planilhas e
acompanhar a base local.

O servidor roda **no seu computador** e chama o app diretamente, sem precisar do
site aberto. Com a base local da Receita instalada, as consultas não saem da
máquina. A IA recebe só o resultado de cada ferramenta.

## 1. Instalar

O servidor já vem com o app (dependência `mcp`). Atualize a instalação:

```bash
# na pasta do projeto
python -m pip install -e .          # Windows: .venv\Scripts\python -m pip install -e .
```

Para testar no terminal (o servidor fica aguardando um cliente; encerre com Ctrl+C):

```bash
python -m app.mcp_server            # ou: cnpj mcp
```

## 2. Conectar o assistente

Use sempre o **Python do ambiente virtual do projeto**. O servidor ajusta
sozinho a pasta de trabalho para a raiz do projeto, onde estão o `.env` e a
pasta `data/`.

### Claude Desktop

Abra *Configurações → Desenvolvedor → Editar configuração* (arquivo
`claude_desktop_config.json`) e acrescente:

**Windows**
```json
{
  "mcpServers": {
    "cnpj-consulta": {
      "command": "C:\\Users\\SEU_USUARIO\\cnpj-consulta\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"]
    }
  }
}
```

**macOS / Linux**
```json
{
  "mcpServers": {
    "cnpj-consulta": {
      "command": "/caminho/para/cnpj-consulta/.venv/bin/python",
      "args": ["-m", "app.mcp_server"]
    }
  }
}
```

Reinicie o Claude Desktop. As ferramentas aparecem no ícone de ferramentas da conversa.

### Claude Code

```bash
claude mcp add cnpj-consulta -- /caminho/para/cnpj-consulta/.venv/bin/python -m app.mcp_server
```

### Cursor / VS Code

Em `.cursor/mcp.json` ou `.vscode/mcp.json`, use o mesmo bloco `command` /
`args` do Claude Desktop. No VS Code a chave raiz é `"servers"` em vez de
`"mcpServers"`.

### Clientes que só falam HTTP

```bash
python -m app.mcp_server --http --porta 8765     # endereço: http://127.0.0.1:8765/mcp
```

O modo HTTP só aceita `127.0.0.1`/`localhost`: o servidor não tem login e não
deve ser exposto na rede.

## 2.1 Pela rede, com chave (Tailscale)

O mesmo MCP fica **dentro do site**, no mesmo endereço e porta: `/mcp`. Todo
pedido exige a **chave MCP** (painel *Base de Dados → Acesso pela rede e MCP*,
onde também dá para gerar uma nova). Pela rede, o site em si pede o login de
administrador; no próprio PC, não.

**1. Publique o app no Tailscale (no PC onde ele roda).** Deixe `APP_HOST=127.0.0.1`
e use o proxy do próprio Tailscale, que já entrega HTTPS:

```bash
tailscale serve --bg 8000      # só os seus aparelhos (tailnet): https://SEU-PC.SUA-TAILNET.ts.net
tailscale funnel --bg 8000     # internet aberta: necessário para claude.ai, app do Claude e ChatGPT
```

O Funnel publica o endereço na internet (ele precisa ser liberado uma vez no
painel do Tailscale). Com ele ligado, qualquer pessoa alcança a tela de login e
o `/mcp`, mas sem o token de administrador e sem a chave não entra (tentativas
erradas são limitadas por IP e vão para a auditoria). Use o `serve` se as IAs
rodam em aparelhos seus; o `funnel`, se a IA roda na nuvem.

**2. Copie o endereço e a chave** no painel *Base de Dados* aberto pelo endereço
do Tailscale. Há duas formas:

- `https://SEU-PC.SUA-TAILNET.ts.net/mcp` + cabeçalho `Authorization: Bearer CHAVE`
- `https://SEU-PC.SUA-TAILNET.ts.net/mcp/k/CHAVE` (a chave vai no endereço; para
  quem só aceita uma URL). Quem tiver este endereço usa o app: não compartilhe.

**3. Configure a IA:**

| Cliente | Como |
|---|---|
| Claude Code | `claude mcp add --transport http cnpj-consulta https://SEU-PC.SUA-TAILNET.ts.net/mcp --header "Authorization: Bearer CHAVE"` |
| Cursor (`.cursor/mcp.json`) | `{"mcpServers": {"cnpj-consulta": {"url": "https://SEU-PC.SUA-TAILNET.ts.net/mcp", "headers": {"Authorization": "Bearer CHAVE"}}}}` |
| VS Code (`.vscode/mcp.json`) | `{"servers": {"cnpj-consulta": {"type": "http", "url": "https://SEU-PC.SUA-TAILNET.ts.net/mcp", "headers": {"Authorization": "Bearer CHAVE"}}}}` |
| claude.ai / app do Claude / ChatGPT | Adicione um **conector personalizado (MCP remoto)** com o endereço **com a chave** (`.../mcp/k/CHAVE`), sem autenticação. Precisa do `tailscale funnel` |
| Claude Desktop em outro computador | Conector personalizado com o endereço com a chave (precisa do Funnel), ou, só pela tailnet, `"command": "npx", "args": ["mcp-remote", "https://SEU-PC.SUA-TAILNET.ts.net/mcp", "--header", "Authorization: Bearer CHAVE"]` |

**Ferramentas que alteram dados** (atualizar a base, contatos complementares,
CPF do cofre) só rodam pela rede com `MCP_PERMITIR_ADMIN=true` no `.env` do PC.
Trocar a chave no painel derruba na hora todas as IAs que usavam a anterior.

**Com `tailscale serve`/`funnel`, ponha `CONFIAR_PROXY=true` no `.env`:** os
pedidos chegam pelo proxy do Tailscale no próprio PC, e só assim o limite de
tentativas de login conta o IP de cada pessoa (sem isso, um estranho errando a
senha pelo Funnel bloquearia o seu login por 5 minutos). Use um token de
administrador forte: o gerado pelo app (`cnpj admin-token`) já é.

**Login pelo navegador:** cada aparelho entra uma vez com o token de
administrador (`cnpj admin-token` mostra o token) e fica conectado por
`ADMIN_SESSAO_HORAS` (padrão 8 h). `APP_LOGIN_REMOTO=false` desliga essa
exigência (não recomendado com Funnel).

Se preferir ligar o app direto no IP do Tailscale (`APP_HOST=0.0.0.0`, sem
`tailscale serve`), ele também funciona, com o mesmo login e a mesma chave, mas
sem HTTPS.

## 3. Ferramentas

| Ferramenta | O que faz | Precisa de |
|---|---|---|
| `ficha_cnpj` | Ficha completa da base local: identificação, situação, endereço, contatos oficiais e complementares, CNAEs, sócios (CPF mascarado), estabelecimentos, histórico | base local |
| `consultar_cnpj` | Dados unificados com procedência, divergências entre fontes, sanções e checklist de diligência; `fontes_externas=true` compara com as APIs públicas | — |
| `sugerir_empresas` | Autocompletar por razão social, nome fantasia ou início do CNPJ | base local |
| `descrever_filtros` | Catálogo de filtros, ordenações, colunas e atalhos de atividade da busca avançada | — |
| `buscar_empresas` | Busca avançada com filtros combinados, ordenação e paginação (até 100 por página), inclusive telefones e e-mail | base local |
| `contar_empresas`, `distribuicao_resultados` | Total e distribuição por UF, situação, porte, município, CNAE… | base local |
| `listar_codigos`, `listar_municipios` | Tabelas oficiais (CNAE, naturezas, motivos, qualificações, países) e municípios por UF | base local |
| `exportar_busca` | Gera planilha xlsx/csv neste computador e devolve o caminho; qualquer coluna, sem limite de linhas (auditado) | base local |
| `buscar_socio`, `ficha_socio`, `empresas_do_socio_pj` | Empresas de um sócio, ficha do sócio, empresas em que um CNPJ é sócio | base local |
| `consultar_lote` | Quadro-resumo de vários CNPJs (até `BATCH_MAX_ITEMS`) | — |
| `verificar_sancoes` | CEIS/CNEP/CEPIM/CEAF | `PORTAL_TRANSPARENCIA_API_KEY` |
| `consultar_inscricao_estadual` | IE na SEFAZ da UF | certificado A1 |
| `pesquisar_na_internet` | Atalhos de pesquisa sobre a empresa e, se configurado, resultados da web | opcional: `WEB_SEARCH_PROVIDER` |
| `listar_buscas_salvas`, `executar_busca_salva`, `salvar_busca` | Buscas salvas (critérios) | base local |
| `listar_listas`, `ver_lista`, `criar_lista`, `adicionar_a_lista`, `editar_lista` (descrição omitida = mantém), `anotar_item_da_lista`, `remover_da_lista` (informa CNPJs inválidos e os que não estavam na lista), `remover_lista` | Listas de empresas com observações | — |
| `remover_busca_salva` | Apaga uma busca salva | — |
| `historico_de_consultas`, `listar_favoritos`, `favoritar` | Histórico de consultas e favoritos | — |
| `status_base_local`, `verificar_nova_versao` | Versão ativa, contagens, tarefa em andamento e versão mais recente no portal da Receita | — |

**Administração (desligada por padrão).** Com `MCP_PERMITIR_ADMIN=true` no
`.env` (ou no bloco `"env"` da configuração do cliente) aparecem também
`atualizar_base`, `controlar_tarefa` (pausar, retomar, cancelar),
`cadastrar_contato_complementar`, `remover_contato_complementar` e as de CPF
completo: `cpf_completo_dos_socios` (CPFs do cofre e do titular de MEI),
`buscar_por_cpf` e `cadastrar_cpf_socio` (ver "CPF completo" no README). Todas
ficam registradas na trilha de auditoria com `origem=mcp`. Os clientes MCP costumam
pedir sua confirmação antes de rodar ferramentas marcadas como de escrita.

**Recursos:** `cnpj://busca/filtros`, `cnpj://base/status` e `cnpj://empresa/{cnpj}`.

**Prompts prontos:** *Diligência cadastral* (`diligencia_cadastral`),
*Prospecção de empresas* (`prospeccao`) e *Mapa de sócios* (`mapa_de_socios`).

## 4. Exemplos de pedidos

- “Faça a diligência cadastral do CNPJ 12.345.678/0001-95.”
- “Quantos escritórios de contabilidade ativos há em Palmas e Araguaína com
  telefone e e-mail e pelo menos 2 anos? Liste os 20 primeiros.”
- “Gere uma planilha com essas empresas, com telefone e e-mail; finalidade:
  prospecção de clientes do escritório.”
- “Em que outras empresas os sócios da empresa X aparecem?”
- “A base local está atualizada? Se houver versão nova, me avise o tamanho do download.”

## 5. Regras que o servidor aplica (LGPD)

- As mesmas da interface: CPF de sócio sempre mascarado como publicado.
  Contatos oficiais e complementares vêm separados e com a origem. Atividade é
  filtrada pelo CNAE oficial.
- `exportar_busca` grava auditoria (linhas e colunas, sem os dados).
- As instruções enviadas ao assistente pedem que ele não invente contatos, não
  tente reconstituir CPFs, cite o mês de referência da base e não trate
  Simples/MEI como prova de regularidade fiscal.
- O que a IA faz com as respostas depende do provedor do assistente. Ao usar uma
  IA em nuvem, os resultados das ferramentas são enviados a ela; avalie isso
  dentro da sua política de privacidade.

## 6. Problemas comuns

| Sintoma | Solução |
|---|---|
| As ferramentas não aparecem | Confira o caminho do `python` do `.venv` na configuração e reinicie o cliente |
| “A base local da Receita é instalada pelo painel…” | Instale a base em `/base` ou com `python -m app.cli base atualizar`; enquanto isso, use `consultar_cnpj` |
| `ModuleNotFoundError: mcp` | `python -m pip install -e .` no ambiente do projeto |
| Ferramentas de administração não aparecem | Defina `MCP_PERMITIR_ADMIN=true` e reinicie o cliente |
| Projeto em outra pasta que não a do código | Defina `CNPJ_CONSULTA_DIR` com a pasta que contém `.env` e `data/` |
