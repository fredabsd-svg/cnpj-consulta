# Base local da Receita Federal, ficha completa e busca avançada

Guia de instalação, operação e referência técnica da base local de CNPJ do
CNPJ Consulta: de onde vêm os dados, como são baixados, validados e
publicados, quanto espaço ocupam, como funcionam a ficha cadastral, a busca
avançada e as exportações, e quais cuidados de LGPD o app aplica.

## 1. Visão geral

| Peça | O que faz | Onde |
|---|---|---|
| Descoberta da fonte | Encontra no portal oficial da Receita a versão mais recente e a lista de arquivos (tamanho, ETag, data) | `app/receita/fonte.py` |
| Pipeline | Baixa com retomada, verifica, extrai, valida a estrutura, importa em lotes, consolida, valida e publica | `app/receita/pipeline.py` |
| Esquema | Tabelas, tipos, colunas derivadas e ordenação física da base publicada | `app/receita/schema.py` |
| Versões | Ponteiro `ATIVA.json`, versão anterior para restaurar, limpeza de versões antigas | `app/receita/armazenamento.py` |
| Tarefas | Progresso, pausa/cancelamento, retomada, logs e auditoria (SQLite) | `app/receita/jobs.py`, `app/receita/servico.py` |
| Ficha | Ficha cadastral completa, sócio, sugestões | `app/services/ficha.py` |
| Busca avançada | Filtros, E/OU/NÃO, contadores, exportação | `app/services/busca_avancada.py`, `app/services/exportacao.py` |
| Contatos complementares | Contatos fora da Receita, sempre com fonte | `app/services/contatos.py` |
| Telas | `/busca`, `/listas`, `/socio/{id}`, `/base`, `/admin/*`, abas da empresa | `app/web/pages_base.py`, `app/web/templates/` |

## 2. Fonte oficial e descoberta dos arquivos

Os dados abertos do CNPJ ficam no **portal de arquivos da Receita Federal**
(`https://arquivos.receitafederal.gov.br/`), num compartilhamento público com
pastas mensais `AAAA-MM` dentro de `Dados/Cadastros/CNPJ`. Cada mês tem 37
arquivos ZIP (2026-10: 7,8 GB):

- `Empresas0..9.zip`, `Estabelecimentos0..9.zip`, `Socios0..9.zip` (o número de partes é lido da listagem, não é fixo);
- `Simples.zip`;
- tabelas de códigos: `Cnaes`, `Motivos`, `Municipios`, `Naturezas`, `Paises`, `Qualificacoes`.

O app **não fixa URLs de arquivos** nem usa espelhos de terceiros:

1. abre o endereço oficial configurado (`RECEITA_PORTAL_URL`) e lê do
   redirecionamento o identificador do compartilhamento público (`/s/<token>`);
2. lista as pastas pelo WebDAV público do próprio portal (o mesmo mecanismo que
   a página usa para mostrar a pasta; o token público entra como usuário, sem
   senha — não há contorno de controle de acesso);
3. confirma a pasta do CNPJ (`RECEITA_PASTA_CNPJ`); se a Receita a mover,
   procura uma pasta `CNPJ` com subpastas `AAAA-MM`;
4. escolhe a **maior pasta `AAAA-MM` completa** (uma pasta ainda em
   publicação, sem todos os grupos de arquivos, fica de fora com aviso).

Só `https://*.gov.br` é aceito como ponto de entrada. Para cada arquivo o
servidor informa tamanho, ETag e data; o ETag identifica a versão do arquivo
e é usado no `If-Range` da retomada (um download retomado nunca mistura duas
versões do mesmo arquivo).

Leiaute dos CSVs (sem cabeçalho, `;`, aspas, ISO-8859-1) conforme o documento
*Metadados / Leiaute dos Dados Abertos do CNPJ* da Receita; as colunas estão em
`app/receita/layout.py`.

## 3. Requisitos de infraestrutura

| Recurso | Mínimo | Recomendado |
|---|---|---|
| Disco livre durante a atualização | ~40 GB | **45–60 GB em SSD** |
| Disco por versão publicada | ~15 GB | ativa + anterior ≈ 30 GB |
| Memória RAM | 4 GB (com `RECEITA_DUCKDB_MEMORIA=2GB`) | 8–16 GB (`4GB` padrão) |
| CPU | 2 núcleos | 4+ núcleos |
| Rede | acesso HTTPS a `arquivos.receitafederal.gov.br` | 50 Mbit/s+ |
| Tempo | download (7–8 GB) + 30 a 90 min de importação e consolidação | |

O espaço necessário é estimado antes de começar (ZIPs restantes + maior CSV
extraído + tabelas brutas + base final) e a atualização é recusada se faltar
espaço (a menos que se marque “ignorar avisos”). Durante o processo, cada CSV é
apagado logo após ser importado e cada ZIP logo após ser extraído
(`RECEITA_MANTER_ZIPS=false`).

**Por que DuckDB e não PostgreSQL?** O app é local e de um usuário; DuckDB é
embutido (nada para instalar ou administrar), colunar e comprimido (a base
nacional cabe em ~15 GB), lê os CSVs em fluxo sem carregar tudo na memória,
derrama para disco o que passa do limite de RAM e varre dezenas de milhões de
linhas em segundos — exatamente o perfil da busca avançada (filtros
combinados sobre a tabela inteira). Consultas pontuais (CNPJ, raiz, sócio)
usam a **ordenação física** das tabelas (estatísticas mín./máx. por bloco), sem
índices em memória. O SQLite do app guarda só dados operacionais (tarefas,
auditoria, contatos complementares, buscas salvas, listas). Um servidor
PostgreSQL só se justificaria para uso multiusuário em rede.

## 4. Instalação e primeira carga

1. Instale o app normalmente (`setup.sh` / `setup.ps1`).
2. Garanta o espaço em disco da tabela acima na pasta `RECEITA_DATA_DIR`
   (padrão `./data/receita`; pode apontar para outro disco).
3. Inicie o servidor (`start.sh` / `start.bat`) e abra **Base de Dados CNPJ**
   no menu (`/base`). Entre com o token de administrador:
   - definido em `ADMIN_TOKEN` no `.env`, ou
   - gerado no primeiro uso em `data/admin_token.txt` (mostre com
     `python -m app.cli admin-token`).
4. Clique em **Verificar nova versão** (mostra tamanho e espaço necessário) e
   depois em **Baixar, importar e publicar**.

Pelo terminal (útil em servidores ou para agendar no sistema):

```bash
python -m app.cli base verificar            # versão mais recente no portal
python -m app.cli base atualizar            # tudo, em primeiro plano, com retomada
python -m app.cli base atualizar --mes 2026-09
python -m app.cli base atualizar --segundo-plano
python -m app.cli base status
python -m app.cli base retomar              # continua a última tarefa pausada/falha
python -m app.cli base cancelar
python -m app.cli base validar              # confere a base ativa
python -m app.cli base restaurar            # volta para a versão anterior
python -m app.cli base limpar               # apaga temporários de tentativas antigas
```

## 5. Pipeline de atualização

| Etapa | O que acontece | Retomada |
|---|---|---|
| Descoberta | versão e lista de arquivos no portal | — |
| Espaço em disco | estimativa x espaço livre | — |
| Download | um arquivo por vez, `Range` + `If-Range` (ETag), até 6 tentativas com espera crescente; progresso, velocidade e tempo restante | continua do byte em que parou |
| Verificação | tamanho igual ao anunciado e ZIP legível | ZIP inválido é baixado de novo uma vez |
| Extração e importação | por arquivo: extração protegida (sem caminhos maliciosos, sem ZIP-bomba, CRC conferido), validação de colunas/codificação numa amostra, importação numa **transação**, contagem de linhas rejeitadas (só o tipo do erro, nunca o conteúdo) | arquivos já importados (registrados na mesma transação) são pulados; um arquivo interrompido é refeito inteiro |
| Consolidação | tabelas tipadas, deduplicadas e ordenadas + tabelas auxiliares | cada tabela é uma etapa registrada |
| Validação | colunas obrigatórias, contagens, queda em relação à versão ativa (`RECEITA_QUEDA_MAXIMA_PCT`), órfãos (`RECEITA_ORFAOS_MAX_PCT`), CNPJs repetidos, consultas de amostra | — |
| Publicação | move para `versoes/` e troca o ponteiro `ATIVA.json` (atômico) | — |
| Limpeza | remove a área de trabalho | — |

Garantias:

- **A versão ativa nunca é tocada** pela atualização: consultas continuam
  respondendo durante todo o processo (testado com consultas concorrentes).
- A troca de versão é atômica; quem estava no meio de uma consulta termina na
  versão antiga. A **versão anterior fica no disco** e pode ser restaurada.
- **Pausar** e **Cancelar** são atendidos no próximo ponto seguro — inclusive no
  meio de uma instrução longa do DuckDB, que é interrompida e desfeita sem
  corromper o arquivo. Cancelar mantém os downloads concluídos.
- Queda do processo (energia, travamento) é detectada pela falta de sinal de
  vida (2 min) e a tarefa aparece como **interrompida**, pronta para retomar.
- A importação roda num **processo separado** (o painel acompanha pelo banco),
  então a interface nunca trava.
- Linhas rejeitadas acima de 1% num arquivo recusam o arquivo (o leiaute pode
  ter mudado); estrutura com número de colunas diferente do oficial também.

**Atualização automática:** no painel, ligue *Atualização automática* e escolha
dia e hora. Com o servidor ligado, o app verifica o portal no horário e inicia a
atualização se houver versão nova. Sem o servidor aberto, agende
`python -m app.cli base atualizar` no Agendador de Tarefas do Windows ou no cron.

## 6. Estrutura da base publicada

| Tabela | Conteúdo | Ordenação |
|---|---|---|
| `empresas` | uma linha por CNPJ básico (8 dígitos): razão social, natureza, qualificação do responsável, capital, porte, ente federativo, quantidade de sócios | `cnpj_basico` |
| `estabelecimentos` | uma linha por CNPJ completo (matriz/filial) com todos os campos oficiais + derivados (telefones normalizados e classificados, e-mail validado e domínio, CNAEs secundários em lista, endereço completo?) + cópia dos campos da empresa usados em filtros | `cnpj_completo` |
| `socios` | QSA como publicado (CPF mascarado pela Receita), nome normalizado, `socio_chave` | `cnpj_basico` |
| `socio_resumo` | um registro por sócio (`socio_chave`) e em quantas empresas aparece | `socio_chave` |
| `simples` | opção/exclusão do Simples e do MEI | `cnpj_basico` |
| `cnaes`, `motivos`, `municipios`, `naturezas`, `paises`, `qualificacoes` | tabelas oficiais de códigos | `codigo` |
| `nomes_busca` | razão social (matriz) e nome fantasia normalizados, para sugestões por prefixo | `nome_norm` |
| `municipios_uf` | municípios existentes por UF (lista dinâmica dos filtros) | `uf, nome` |
| `metadata`, `importacao_arquivos` | mês de referência, data de importação, origem, arquivos (tamanho, ETag, linhas, rejeitadas) | — |

Empresa ≠ estabelecimento: a **empresa** (raiz) tem razão social, natureza,
porte e capital; cada **estabelecimento** tem CNPJ, endereço, situação, CNAEs e
contatos próprios. A ficha mostra os dois níveis.

Deduplicação: empresas e estabelecimentos repetidos na origem são reduzidos a
um (contagem no relatório); sócios e Simples idênticos também. Órfãos
(estabelecimento sem empresa, sócio sem empresa) são contados e bloqueiam a
publicação acima do limite.

## 7. Consulta e ficha cadastral

- A página `/empresa/{cnpj}` consulta **primeiro a base local** (sem chamar
  serviços externos e sem cache). As fontes externas ficam no botão *Comparar
  com as fontes externas*; sem a base, o app usa as APIs como antes.
- A caixa de busca do início e do topo aceita **CNPJ, razão social ou nome
  fantasia**, com sugestões da base local (prefixo).
- Abas: **Resumo** (identificação, situação e motivo, situação especial,
  natureza, porte, capital, ente federativo, Simples/MEI com datas),
  **Endereço** (com país, código do município da Receita e botão de mapa),
  **Contatos** (oficiais e complementares separados, com origem, data,
  classificação, copiar/ligar/enviar e-mail), **CNAEs**, **Sócios**
  (qualificação, entrada, representante legal e sua qualificação, faixa etária,
  CPF mascarado, link para a ficha do sócio e empresas relacionadas),
  **Estabelecimentos** e **Histórico** (datas publicadas + mudanças desde a
  versão anterior da base, quando ela está no disco).
- Exportação: Excel (`/empresa/{cnpj}/ficha.xlsx`) e PDF pelo relatório de
  diligência para impressão.
- Só aparecem campos que existem na fonte. A base pública **não** traz CPF
  completo, percentual de participação, inscrição estadual ou municipal; a IE é
  consultada à parte (CCC/SEFAZ).

## 8. Busca avançada (`/busca`)

Regras de combinação: **valores do mesmo filtro são OU** (Palmas ou
Araguaína); **filtros diferentes são E**; `excluir_<filtro>` nega (NÃO); o
**construtor avançado** monta grupos E/OU com NÃO (campo `arvore` em JSON).
Toda a filtragem é feita no banco com **SQL parametrizado**; campos,
ordenações e colunas vêm de listas fechadas.

| Grupo | Filtros (nome do parâmetro) |
|---|---|
| Identificação | `q` (busca rápida), `cnpj` (completo ou início), `cnpj_basico`, `razao`, `fantasia`, `tem_fantasia`, `natureza`, `matriz_filial`, `abertura_de/ate`, `idade_min/max`, `situacao`, `data_situacao_de/ate`, `motivo` |
| Localização | `pais`, `uf`, `municipio` (código da Receita), `municipio_nome`, `bairro`, `cep` (início), `logradouro`, `endereco_completo` |
| Contatos oficiais | `contato` (`telefone_e_email`, `so_telefone`, `so_email`, `algum`, `sem_contato`), `tem_telefone`, `tem_telefone_principal/secundario`, `tipo_telefone` / `tipo_telefone_principal` (celular, fixo, indeterminado), `ddd`, `tem_email`, `email_tipo` (`dominio_proprio`, `webmail`), `email_dominio` |
| Complementares e qualidade | `comp_tem`, `comp_tipo`, `tem_site`, `comp_verificado`, `comp_desatualizado`, `comp_divergente`, `comp_verificado_desde`, `origem_contato` (`oficial`, `complementar`, `ambos`), `dados_completos`, `sem_empresa` |
| Atividade (CNAE) | `cnae_principal`, `cnae_secundario` (qualquer), `cnae_secundario_todos`, `cnae` (principal ou secundário), `cnae_grupo` / `cnae_grupo_qualquer` (prefixo: divisão, grupo, classe), `cnae_descricao`, `atividade` / `atividade_qualquer` (atalhos) |
| Sócios | `socio_nome`, `socio_qualificacao`, `socio_administrador`, `socio_entrada_de/ate`, `socio_tipo`, `socio_documento`, `socio_chave`, `socio_multiempresas` (sócio em ≥ N empresas), `compartilha_socios_com` (CNPJ), `tem_socios`, `qtd_socios_min/max` |
| Porte e perfil | `porte`, `simples`, `mei`, `capital_min/max` |

Critérios técnicos (documentados também na tela):

- **Celular/fixo** pelo plano de numeração da Anatel: número de assinante
  iniciado por 6 a 9 é celular (SMP) — com o nono dígito (9 + 8 dígitos) ou,
  como a **Receita publica na base aberta, só com 8 dígitos**; iniciado por 2 a
  5 é fixo (STFC); o resto é *indeterminado*. O app nunca acrescenta o nono
  dígito: mostra o número como publicado, com o aviso “publicado sem o nono
  dígito”. (Na base 2026-10 todos os telefones vêm com 8 dígitos.)
- **Domínio próprio provável**: o domínio do e-mail não é de provedor gratuito
  conhecido (Gmail, Hotmail, UOL...). Não confirma que seja corporativo.
- **Atividade**: sempre pelo CNAE oficial, nunca pelo nome. Os atalhos
  (`contabilidade` → classe 6920; `tecnologia` → divisão 62 e grupo 631; ...)
  estão em `app/services/cnae_palavras.py` e aparecem com os códigos na tela.
- **Mesmo sócio em várias empresas**: mesmo tipo + documento como publicado +
  nome normalizado. Para pessoa física é uma identificação **provável** (CPF
  mascarado), indicada como tal.
- **Administração**: qualificações de administrador, diretor, presidente,
  sócio-administrador, sócio-gerente, empresário e titular (lista em
  `app/receita/layout.py`).
- **Endereço completo**: logradouro, número, bairro, CEP de 8 dígitos, município e UF.
- **Simples/MEI** são informações cadastrais e não comprovam regularidade fiscal.

Interface: painel de filtros por grupo, UF → municípios carregados na hora,
filtro de texto nas listas grandes, sugestão de CNAE pela descrição, prévia do
total com *debounce*, etiquetas removíveis dos filtros ativos, ordenação,
paginação no servidor, colunas configuráveis, seleção de linhas (copiar CNPJs,
adicionar a uma lista, exportar selecionados), filtros a partir de um resultado
(clicar na UF, no município ou no CNAE), contadores de distribuição, aviso de
contato compartilhado com outras empresas (possível duplicidade) e botão
**Parar** para buscas longas (a consulta é interrompida no servidor;
`BUSCA_TIMEOUT_SEGUNDOS` limita o tempo).

**Busca salva × lista:** a busca salva guarda **critérios** e é executada de
novo a cada abertura; a lista guarda **empresas escolhidas**, com data de
inclusão e observações.

**Exportação:** CSV (Excel pt-BR) ou `.xlsx` com as colunas escolhidas; até
`EXPORTACAO_SINCRONA_MAX` linhas sai na hora, acima disso vira tarefa em
segundo plano com link de download; o teto é `EXPORTACAO_MAX_LINHAS`. As linhas
são lidas em lotes do banco (nunca tudo na memória).

## 9. Dados oficiais × complementares

| | Oficial | Complementar |
|---|---|---|
| Origem | base da Receita (declarado pela empresa) | cadastrado por um administrador |
| Fonte | Receita Federal + mês de referência | obrigatória (texto + endereço opcional) |
| Verificação | — | marcada por quem cadastrou, com data |
| Edição | nunca (só nova versão da base) | corrigir, atualizar, remover |
| Indicação na tela | selo **Receita Federal** | selo **Complementar**, verificado/não verificado, *verificação antiga*, *diverge do oficial* |

Nada é preenchido ou completado automaticamente; o app não faz raspagem de
sites nem incorpora bases de terceiros.

## 10. Segurança e LGPD

- Área administrativa com token (sessão assinada HttpOnly/SameSite=Strict,
  limite de 5 tentativas em 5 min, `Bearer` para scripts) e **trilha de
  auditoria** de todas as operações (sem dados pessoais).
- Operações destrutivas pedem confirmação digitada (`CONFIRMAR`).
- Requisições POST/DELETE de outra origem são bloqueadas; o app escuta só em
  `127.0.0.1`.
- CPFs de sócios aparecem **mascarados como publicados**; um documento de pessoa
  física sem máscara é mascarado de novo. Nomes e documentos de sócios não vão
  para pesquisas na internet, logs de processamento, histórico de buscas nem
  auditoria; o log de acesso não registra a query string.
- Exportar telefones, e-mails ou nomes de sócios exige informar a
  **finalidade**, registrada na auditoria com a quantidade de linhas.
- Contatos complementares têm retenção (`CONTATOS_RETENCAO_DIAS`) e marcação de
  desatualização (`CONTATOS_DESATUALIZADO_DIAS`).
- Finalidade e base legal sugeridas: atendimento a obrigações de diligência
  cadastral e prospecção B2B do escritório (legítimo interesse, LGPD art. 7º,
  IX), observando o art. 7º, §§ 3º e 4º: dado de acesso público deve ser tratado
  conforme a finalidade, a boa-fé e o interesse público que justificaram sua
  disponibilização. **A disponibilidade pública não autoriza qualquer uso**;
  respeite oposição e pedidos de exclusão de contatos complementares. Veja
  [`politica-de-privacidade.md`](politica-de-privacidade.md).

## 11. Testes

`pytest` cobre (sem rede, com portal simulado em `tests/receita_fake.py`):
CNPJ válido/inválido, empresas com vários estabelecimentos, sem telefone/e-mail,
sem QSA, sócios homônimos, arquivos corrompidos, não-ZIP, leiaute diferente,
versão incompleta, falta de espaço, queda de conexão com retomada por `Range`,
servidor que ignora `Range`, pausa e retomada na importação e no download,
queda no meio de um arquivo, atualização inválida que mantém a versão anterior,
restauração e poda de versões, duplicados e integridade referencial, consultas
concorrentes durante a atualização, desempenho com 300 mil estabelecimentos,
cancelamento/tempo limite de consulta, autorização administrativa, auditoria,
contatos complementares e mascaramento de dados pessoais.

```bash
.venv/bin/pytest -q
```

## 12. O que depende de credenciais ou serviços externos

| Recurso | Precisa de |
|---|---|
| Download/atualização da base | acesso HTTPS ao portal oficial da Receita (sem credencial) |
| Área administrativa | token local (`ADMIN_TOKEN` ou arquivo gerado) |
| Inscrição estadual automática | certificado A1 da empresa emissora de NF-e (já existente) |
| Sanções CEIS/CNEP | chave gratuita do Portal da Transparência (já existente) |
| Resultados da web | Tavily/Brave/SearXNG (já existente) |

Busca avançada, ficha local, listas e exportações funcionam **offline** depois
que a base está instalada.
