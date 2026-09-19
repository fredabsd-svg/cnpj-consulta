# Como contribuir

Obrigado pelo interesse em melhorar o CNPJ Consulta! Este guia mostra como preparar o ambiente e o que esperamos de uma contribuição.

## Preparando o ambiente

Requisitos: Python 3.12+ e Git.

```bash
git clone https://github.com/fredabsd-svg/cnpj-consulta.git
cd cnpj-consulta
./setup.sh          # Windows: .\setup.ps1
```

O `setup` cria o `.venv` e instala o projeto com as dependências de desenvolvimento (`pip install -e ".[dev]"`). Para subir o servidor: `./start.sh` (Windows: `.\start.ps1`).

## Antes de abrir um pull request

```bash
pytest          # todos os testes precisam passar
ruff check .    # sem avisos de lint
```

O CI roda os dois em várias versões do Python. O `mypy` está configurado no `pyproject.toml`, mas ainda aponta alguns erros de tipagem que já existiam; corrigi-los é uma ótima primeira contribuição, mas o `mypy` não bloqueia o CI.

Ao alterar comportamento, inclua ou ajuste testes em `tests/`. Os testes usam dados fictícios e mocks das fontes (`respx`): **nenhum teste deve fazer requisições reais**.

## Convenções

- Estilo: `ruff` com linha de até 100 caracteres (veja `[tool.ruff]` no `pyproject.toml`).
- Textos da interface, mensagens e comentários em português (pt-BR).
- Commits pequenos, com mensagem que explique o **porquê** da mudança.

## Privacidade: o que não pode entrar no repositório

O projeto lida com dados de empresas, e a política é a de nunca expor pessoas:

- Não commite o `.env`, o banco `data/app.db`, o cache nem a base da Receita Federal (já estão no `.gitignore`).
- Use **dados fictícios** para pessoas (nomes de sócios, CPFs mascarados, e-mails, telefones) em testes, fixtures, documentação e capturas de tela. CNPJs de pessoas jurídicas são públicos, mas evite associar um CNPJ real a dados inventados.
- Nunca inclua funcionalidades que tentem descobrir ou desmascarar CPF, ou que busquem dados pessoais. Veja a [política de privacidade](politica-de-privacidade.md).

## Adicionando uma nova fonte de dados

O passo a passo, com um exemplo de adaptador, está na seção 8.4 do [relatório técnico](RELATORIO_FINAL.md). Em resumo: crie o adaptador em `app/providers/`, adicione as configurações em `app/config.py` e no `.env.example`, registre a classe em `app/providers/registry.py`, escreva testes com um payload de exemplo em `tests/fixtures/` e declare se a fonte é primária ou espelho da Receita (isso afeta a confiança calculada na conciliação).

## Landing page e identidade visual

A landing page fica em `docs/` (GitHub Pages) e é HTML, CSS e JS puros, sem dependências e sem requisições a terceiros. Para vê-la localmente:

```bash
python -m http.server 8090 --directory docs
```

Depois abra <http://127.0.0.1:8090>. Mantenha a página compatível com a CSP definida no `<meta>` do `index.html` (sem `style=""` nem scripts inline).

## Reportando problemas

Use os [formulários de issue](https://github.com/fredabsd-svg/cnpj-consulta/issues/new/choose). **Não cole dados pessoais nem o conteúdo do seu `.env`.**

## Licença

Ao contribuir, você concorda que sua contribuição será licenciada sob a [licença MIT](LICENSE) do projeto.
