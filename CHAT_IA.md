# Chat com IA

A página **Chat com IA** (`/chat`) permite pedir em linguagem natural tudo o que o
sistema faz: consultar e comparar CNPJs, fazer a diligência cadastral, montar
listas de prospecção, mapear sócios, exportar planilhas, cruzar dados com SQL,
cuidar de listas, buscas salvas e favoritos, e acompanhar ou atualizar a base
local da Receita.

A página só abre para o **administrador** (mesmo login de `/base`), porque o
modelo pode alterar dados do app.

## 1. Escolher e configurar um provedor

Clique em **Configurar provedores** na página do chat e cole a chave de API (ou o
endereço, para modelos locais). Se preferir, coloque no `.env` e reinicie o app:
os valores do `.env` têm prioridade sobre os cadastrados na tela.

| Provedor | Variável no `.env` | Modelos |
|---|---|---|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | Claude Opus 5.5 (padrão), Sonnet 5.5, Haiku 5.5, Fable 5.1 |
| OpenAI | `OPENAI_API_KEY` | lista carregada da sua conta |
| Google Gemini | `GEMINI_API_KEY` | lista carregada da sua conta |
| OpenRouter | `OPENROUTER_API_KEY` | centenas de modelos de vários fabricantes com uma chave só |
| DeepSeek | `DEEPSEEK_API_KEY` | lista carregada da sua conta |
| Groq | `GROQ_API_KEY` | lista carregada da sua conta |
| Mistral | `MISTRAL_API_KEY` | lista carregada da sua conta |
| xAI (Grok) | `XAI_API_KEY` | lista carregada da sua conta |
| Ollama (local) | `OLLAMA_URL=http://localhost:11434/v1` | modelos instalados no Ollama |
| LM Studio (local) | `LMSTUDIO_URL=http://localhost:1234/v1` | modelo carregado no LM Studio |
| Personalizado | `CHAT_CUSTOM_URL`, `CHAT_CUSTOM_API_KEY`, `CHAT_CUSTOM_MODELOS` | qualquer servidor compatível com a API da OpenAI (vLLM, LiteLLM, Azure OpenAI…). Parâmetros do endereço são mantidos (ex.: Azure: `https://RECURSO.openai.azure.com/openai/deployments/MODELO?api-version=AAAA-MM-DD`) |

- A lista de modelos dos provedores compatíveis com OpenAI vem do próprio
  provedor (`/models`) e é atualizada a cada 10 minutos. O campo de modelo também
  aceita digitar qualquer nome; em **Modelos extras** você fixa nomes que não
  aparecem na lista.
- O modelo pode ser trocado no meio da conversa dentro do mesmo tipo (entre os
  Claude, ou entre os compatíveis com OpenAI). Para passar de um tipo ao outro,
  comece uma nova conversa.
- `CHAT_PROVEDOR_PADRAO` e `CHAT_MODELO_PADRAO` definem o que vem selecionado.
- **Escolha de modelo:** o modelo precisa saber usar ferramentas (*tool calling*).
  Os grandes modelos dos provedores em nuvem sabem; entre os locais, use modelos
  com suporte a ferramentas (o Ollama marca isso como *tools*) e de bom tamanho.
  Modelos pequenos erram mais os parâmetros das buscas.

### Modelos Claude

O chat usa o SDK oficial da Anthropic com raciocínio adaptativo, resposta em
fluxo e cache de prompt (as ferramentas e as instruções são reaproveitadas entre
as chamadas, o que reduz custo). `CHAT_ESFORCO` (`low`, `medium`, `high`,
`xhigh`, `max`; padrão `medium`) troca profundidade de raciocínio por custo e
tempo. Se o filtro de segurança de um modelo recusar um pedido legítimo, a API
refaz o pedido automaticamente com o modelo de reserva recomendado pela
Anthropic (`fallbacks: "default"`; não existe para o Haiku).

## 2. O que o modelo consegue fazer

O chat usa **as mesmas ferramentas do [servidor MCP](MCP.md)** (uma camada só para
os dois), inclusive as administrativas (entre elas as de CPF completo do cofre,
que a IA só usa quando você pede: `cpf_completo_dos_socios`, `buscar_por_cpf`,
`cadastrar_cpf_socio`), mais três ferramentas próprias:

| Ferramenta | O que faz |
|---|---|
| `descrever_bancos` | Tabelas e colunas da base da Receita e do banco do app |
| `consultar_sql_base` | Um `SELECT` na base oficial (DuckDB), para rankings, agregações e cruzamentos que as ferramentas prontas não cobrem |
| `consultar_sql_app` | Um `SELECT` no banco do app: listas, buscas salvas, histórico, favoritos, contatos complementares, tarefas da base, auditoria e conversas |

Exportações feitas pelo chat aparecem como link de download na resposta
(`/chat/arquivos/...`, só para o administrador).

Exemplos:

- “Faça a diligência cadastral do CNPJ 12.345.678/0001-95.”
- “Quais os 10 CNAEs com mais empresas abertas em 2026 em Goiás?”
- “Monte uma lista ‘Construtoras Palmas’ com as construtoras ativas de Palmas abertas há mais de 2 anos.”
- “Gere uma planilha dessas empresas com telefone e e-mail; finalidade: prospecção de clientes do escritório.”
- “Em que outras empresas aparecem os sócios da empresa X?”
- “A base está atualizada? Se houver versão nova, atualize.” (o modelo confirma antes de iniciar)

## 3. Segurança e LGPD

- **Para onde vão os dados.** As mensagens e os resultados das ferramentas
  (inclusive dados de empresas e nomes de sócios) são enviados ao provedor
  escolhido, conforme a política de privacidade dele. Com Ollama ou LM Studio
  nada sai do computador. Avalie isso dentro da política de privacidade do
  escritório.
- **Chaves.** Ficam no `.env` ou no banco do app (`data/app.db`), nunca voltam
  ao navegador (a tela mostra só os 4 últimos caracteres) e a tabela que as
  guarda é bloqueada para as consultas SQL do chat. Proteja a pasta `data/`.
- **SQL somente-leitura.** Na base da Receita: conexão `read_only`, uma única
  instrução `SELECT`, sem acesso a arquivos, rede ou extensões e com tempo
  limite (`CHAT_SQL_TIMEOUT_SEGUNDOS`) e teto de linhas (`CHAT_SQL_MAX_LINHAS`).
  No banco do app: arquivo aberto em modo somente-leitura e autorizador do SQLite
  que só permite leitura.
- **Alterações** (listas, favoritos, contatos complementares, atualizar/pausar a
  base) só por ferramentas do app, que validam os dados e gravam auditoria com
  `origem=chat`. Cada mensagem enviada também é auditada (`chat.mensagem`, com
  conversa, provedor e modelo; sem o conteúdo). O modelo é instruído a confirmar
  antes de ações que alteram dados.
- **Mesmas regras da interface:** CPF de sócio sempre mascarado, contatos
  oficiais separados dos complementares, nada de contatos inventados,
  Simples/MEI não é prova de regularidade fiscal, exportação de contatos exige
  finalidade.
- **Limites:** `CHAT_MAX_RODADAS` (idas ao modelo com ferramentas por mensagem,
  padrão 25; cada ida pode pedir várias ferramentas em paralelo; ao esgotar, o
  modelo responde mais uma vez, sem ferramentas, com o que já reuniu) e `CHAT_RESULTADO_MAX_CHARS` (tamanho máximo de um resultado enviado ao
  modelo) evitam laços e custos inesperados. Para desligar o chat:
  `CHAT_HABILITADO=false`.

## 4. Problemas comuns

| Sintoma | Solução |
|---|---|
| “Nenhum provedor configurado” | Cadastre uma chave em **Configurar provedores** ou no `.env` (e reinicie) |
| “Chave … inválida” | Confira a chave e se a conta tem crédito/permissão para o modelo |
| “Modelo não encontrado” | Escolha um modelo da lista ou confira o nome digitado |
| Lista de modelos vazia | O provedor não respondeu a `/models`; digite o nome do modelo ou use **Modelos extras** |
| Ollama/LM Studio não conecta | Deixe o programa aberto e confira o endereço (termina em `/v1`) |
| Modelo local responde sem usar as ferramentas | Use um modelo com suporte a ferramentas e de maior tamanho |
| “A base local da Receita é instalada pelo painel…” | Instale a base em `/base`; sem ela o chat ainda consulta pelas APIs públicas |
