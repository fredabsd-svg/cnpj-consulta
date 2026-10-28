/* CNPJ Consulta - base local, busca avancada e painel administrativo.
 * Melhoria progressiva: todas as telas funcionam sem este arquivo (formularios
 * GET/POST comuns). Aqui ficam sugestoes, contagem previa com debounce,
 * municipios por UF, construtor E/OU/NAO, selecao de linhas, colunas,
 * contadores e o acompanhamento ao vivo das atualizacoes da base.
 * Todo texto vindo do servidor entra via textContent (nunca innerHTML). */
"use strict";

(function () {
    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = String(text);
        return e;
    }

    function toast(msg) {
        const t = el("div", "toast", msg);
        t.setAttribute("role", "status");
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 3500);
    }

    function debounce(fn, ms) {
        let id;
        return (...args) => {
            clearTimeout(id);
            id = setTimeout(() => fn(...args), ms);
        };
    }

    function fmtNum(n) {
        return Number(n).toLocaleString("pt-BR");
    }

    function fmtCnpj(c) {
        return c.length === 14 ? `${c.slice(0, 2)}.${c.slice(2, 5)}.${c.slice(5, 8)}/${c.slice(8, 12)}-${c.slice(12)}` : c;
    }

    function fmtBytes(n) {
        if (!n) return "0";
        if (n >= 1073741824) return (n / 1073741824).toFixed(2).replace(".", ",") + " GB";
        if (n >= 1048576) return (n / 1048576).toFixed(1).replace(".", ",") + " MB";
        return Math.round(n / 1024) + " KB";
    }

    async function api(method, url, body) {
        const opts = { method, headers: { Accept: "application/json" } };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        const r = await fetch(url, opts);
        let data = null;
        try {
            data = await r.json();
        } catch (e) {
            data = null;
        }
        if (!r.ok) throw new Error((data && data.detail) || "HTTP " + r.status);
        return data;
    }

    // ------------------------------------------------------------ sugestoes
    function initSugestoes() {
        $$("[data-sugestoes]").forEach((input) => {
            const lista = document.getElementById(input.getAttribute("aria-controls"));
            if (!lista) return;
            let controle = null;
            let itens = [];
            let ativo = -1;

            const fechar = () => {
                lista.hidden = true;
                input.setAttribute("aria-expanded", "false");
                ativo = -1;
            };
            const marcar = (i) => {
                ativo = i;
                $$("li", lista).forEach((li, j) => li.setAttribute("aria-selected", String(j === i)));
            };
            const abrir = (cnpj) => (location.href = "/empresa/" + cnpj);

            const buscar = debounce(async () => {
                const q = input.value.trim();
                if (controle) controle.abort();
                if (q.replace(/\W/g, "").length < 3) return fechar();
                controle = new AbortController();
                try {
                    const r = await fetch("/api/base/sugestoes?q=" + encodeURIComponent(q), { signal: controle.signal });
                    if (!r.ok) return fechar();
                    itens = await r.json();
                } catch (e) {
                    return;
                }
                lista.replaceChildren();
                itens.forEach((it, i) => {
                    const li = el("li");
                    li.id = lista.id + "-" + i;
                    li.setAttribute("role", "option");
                    li.append(el("span", "sug-nome", it.razao_social || it.nome_fantasia || fmtCnpj(it.cnpj)));
                    const meta = [fmtCnpj(it.cnpj), it.nome_fantasia, it.matriz_filial, [it.municipio, it.uf].filter(Boolean).join("/"), it.situacao]
                        .filter(Boolean)
                        .join(" · ");
                    li.append(el("span", "sug-meta", meta));
                    li.addEventListener("mousedown", (ev) => {
                        ev.preventDefault();
                        abrir(it.cnpj);
                    });
                    lista.appendChild(li);
                });
                if (!/^\d/.test(q)) {
                    const mais = el("li");
                    mais.setAttribute("role", "option");
                    mais.append(el("span", "sug-mais", "Buscar “" + q + "” na busca avançada →"));
                    mais.addEventListener("mousedown", (ev) => {
                        ev.preventDefault();
                        location.href = "/busca?q=" + encodeURIComponent(q);
                    });
                    lista.appendChild(mais);
                }
                const tem = lista.children.length > 0;
                lista.hidden = !tem;
                input.setAttribute("aria-expanded", String(tem));
                marcar(-1);
            }, 250);

            input.addEventListener("input", buscar);
            input.addEventListener("blur", () => setTimeout(fechar, 150));
            input.addEventListener("keydown", (ev) => {
                const n = lista.children.length;
                if (lista.hidden || !n) return;
                if (ev.key === "ArrowDown") {
                    ev.preventDefault();
                    marcar((ativo + 1) % n);
                } else if (ev.key === "ArrowUp") {
                    ev.preventDefault();
                    marcar((ativo - 1 + n) % n);
                } else if (ev.key === "Enter" && ativo >= 0) {
                    ev.preventDefault();
                    lista.children[ativo].dispatchEvent(new MouseEvent("mousedown"));
                } else if (ev.key === "Escape") {
                    fechar();
                }
                if (ativo >= 0) input.setAttribute("aria-activedescendant", lista.children[ativo].id || "");
            });
        });
    }

    // ------------------------------------------------------------ confirmacoes
    function initConfirmacoes() {
        $$("form[data-confirmar]").forEach((form) => {
            if (form.dataset.api) return; // tratado em initApiForms
            form.addEventListener("submit", (ev) => {
                if (!window.confirm(form.dataset.confirmar)) ev.preventDefault();
            });
        });
    }

    // ------------------------------------------------------------ busca avancada
    function qsDoForm(form) {
        const p = new URLSearchParams();
        new FormData(form).forEach((v, k) => {
            if (String(v).trim() !== "") p.append(k, v);
        });
        const q = document.getElementById("f-q");
        if (q && q.value.trim() && !p.has("q")) p.append("q", q.value.trim());
        p.delete("pagina");
        return p;
    }

    function initBusca() {
        const form = $("form[data-busca]");
        if (!form) return;

        // Municipios conforme as UFs marcadas
        const selMun = $("[data-municipios]", form);
        const carregarMunicipios = async () => {
            if (!selMun) return;
            const ufs = $$("[data-uf]:checked", form).map((c) => c.value);
            const selecionados = new Set(Array.from(selMun.selectedOptions).map((o) => o.value));
            if (!ufs.length) {
                Array.from(selMun.options).forEach((o) => {
                    if (!o.selected) o.remove();
                });
                return;
            }
            try {
                const dados = await api("GET", "/api/base/municipios?" + ufs.map((u) => "uf=" + u).join("&"));
                selMun.replaceChildren();
                dados.forEach((m) => {
                    const o = el("option", "", m.nome + "/" + m.uf);
                    o.value = m.codigo;
                    o.selected = selecionados.has(String(m.codigo));
                    selMun.appendChild(o);
                });
            } catch (e) {
                toast("Não foi possível carregar os municípios (" + e.message + ").");
            }
        };
        $$("[data-uf]", form).forEach((c) => c.addEventListener("change", carregarMunicipios));

        // Filtro de texto para listas grandes (select multiplo)
        $$("select[data-multi]", form).forEach((sel) => {
            if (sel.options.length < 8) return;
            const busca = el("input", "input input-sm multi-filtro");
            busca.type = "search";
            busca.placeholder = "Filtrar opções…";
            busca.setAttribute("aria-label", "Filtrar opções de " + (sel.labels[0] ? sel.labels[0].textContent : "lista"));
            sel.before(busca);
            busca.addEventListener("input", () => {
                const t = busca.value.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
                Array.from(sel.options).forEach((o) => {
                    const txt = o.textContent.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
                    o.hidden = !!t && !txt.includes(t) && !o.selected;
                });
            });
        });

        // CNAE: sugestoes de codigos pela descricao (datalist)
        const dl = document.getElementById("lista-cnaes");
        if (dl) {
            let ctrl = null;
            const sugerir = debounce(async (input) => {
                const termo = input.value.split(",").pop().trim();
                if (termo.length < 3 || /^\d+$/.test(termo)) return;
                if (ctrl) ctrl.abort();
                ctrl = new AbortController();
                try {
                    const r = await fetch("/api/base/dominio/cnaes?q=" + encodeURIComponent(termo), { signal: ctrl.signal });
                    const itens = r.ok ? await r.json() : [];
                    dl.replaceChildren();
                    const prefixo = input.value.includes(",") ? input.value.slice(0, input.value.lastIndexOf(",") + 1) + " " : "";
                    itens.slice(0, 30).forEach((c) => {
                        const o = el("option", "", c.descricao);
                        o.value = prefixo + String(c.codigo).padStart(7, "0");
                        dl.appendChild(o);
                    });
                } catch (e) {
                    /* digitacao seguinte cancela */
                }
            }, 300);
            $$("[data-cnae-busca]", form).forEach((input) => {
                input.setAttribute("list", "lista-cnaes");
                input.addEventListener("input", () => sugerir(input));
            });
        }

        // Contagem previa (debounce + cancelamento da anterior)
        const previa = $("[data-previa]", form);
        let ctrlPrevia = null;
        const atualizarPrevia = debounce(async () => {
            if (!previa) return;
            const p = qsDoForm(form);
            p.delete("ordem");
            if ([...p.keys()].length === 0) {
                previa.textContent = "";
                return;
            }
            if (ctrlPrevia) ctrlPrevia.abort();
            ctrlPrevia = new AbortController();
            previa.textContent = "Contando…";
            try {
                const r = await fetch("/api/busca/contar?" + p.toString(), { signal: ctrlPrevia.signal });
                const d = await r.json();
                previa.textContent = r.ok ? fmtNum(d.total) + " estabelecimento(s) com estes filtros" : d.detail || "Filtro inválido";
            } catch (e) {
                if (e.name !== "AbortError") previa.textContent = "";
            }
        }, 700);
        form.addEventListener("input", atualizarPrevia);
        form.addEventListener("change", atualizarPrevia);

        // Buscas longas: botao para parar (interrompe a consulta no servidor)
        form.addEventListener("submit", () => {
            const caixa = el("div", "cancelar-busca");
            caixa.setAttribute("role", "status");
            caixa.append(el("span", "spinner"), el("span", "", "Buscando na base local…"));
            const parar = el("button", "btn btn-sm", "Parar");
            parar.type = "button";
            parar.addEventListener("click", () => {
                window.stop();
                caixa.remove();
                toast("Busca interrompida.");
            });
            caixa.append(parar);
            setTimeout(() => document.body.appendChild(caixa), 400);
        });

        initConstrutor(form);
    }

    // Construtor E/OU/NAO: grava JSON no campo "arvore"
    async function initConstrutor(form) {
        const box = $("[data-construtor]", form);
        const campoJson = document.getElementById("f-arvore");
        if (!box || !campoJson) return;
        let catalogo;
        try {
            catalogo = await api("GET", "/api/busca/campos");
        } catch (e) {
            return;
        }
        const campos = catalogo.campos;
        let arvore = null;
        try {
            arvore = campoJson.value.trim() ? JSON.parse(campoJson.value) : null;
        } catch (e) {
            arvore = null;
        }
        if (!arvore || !Array.isArray(arvore.itens)) arvore = { op: "E", itens: [] };
        // Formato do construtor: raiz E com grupos; cada grupo tem op E/OU e folhas
        arvore.itens = arvore.itens.map((g) => (g.itens ? g : { op: "OU", itens: [g] }));

        const salvar = () => {
            const util = {
                op: "E",
                itens: arvore.itens
                    .map((g) => ({ op: g.op, negar: !!g.negar, itens: g.itens.filter((f) => f.campo && String(f.valor ?? "").trim() !== "") }))
                    .filter((g) => g.itens.length),
            };
            campoJson.value = util.itens.length ? JSON.stringify(util) : "";
            campoJson.dispatchEvent(new Event("input", { bubbles: true }));
        };

        const render = () => {
            box.replaceChildren();
            arvore.itens.forEach((g, gi) => {
                const grupo = el("div", "construtor-grupo");
                const cab = el("div", "construtor-cabecalho");
                const op = el("select", "select select-sm");
                [["OU", "Qualquer condição (OU)"], ["E", "Todas as condições (E)"]].forEach(([v, r]) => {
                    const o = el("option", "", r);
                    o.value = v;
                    o.selected = g.op === v;
                    op.appendChild(o);
                });
                op.setAttribute("aria-label", "Combinação do grupo " + (gi + 1));
                op.addEventListener("change", () => {
                    g.op = op.value;
                    salvar();
                });
                const neg = el("label", "check");
                const negIn = el("input");
                negIn.type = "checkbox";
                negIn.checked = !!g.negar;
                negIn.addEventListener("change", () => {
                    g.negar = negIn.checked;
                    salvar();
                });
                neg.append(negIn, document.createTextNode(" NÃO (excluir grupo)"));
                const rem = el("button", "btn btn-sm btn-ghost", "Remover grupo");
                rem.type = "button";
                rem.addEventListener("click", () => {
                    arvore.itens.splice(gi, 1);
                    salvar();
                    render();
                });
                cab.append(el("strong", "", "Grupo " + (gi + 1)), op, neg, rem);
                grupo.appendChild(cab);

                g.itens.forEach((f, fi) => {
                    const linha = el("div", "construtor-linha");
                    const sel = el("select", "select select-sm");
                    sel.setAttribute("aria-label", "Campo");
                    campos.forEach((c) => {
                        const o = el("option", "", c.grupo_rotulo + " · " + c.rotulo);
                        o.value = c.nome;
                        o.selected = f.campo === c.nome;
                        sel.appendChild(o);
                    });
                    const valor = el("input", "input input-sm");
                    valor.setAttribute("aria-label", "Valor");
                    valor.value = Array.isArray(f.valor) ? f.valor.join(", ") : f.valor ?? "";
                    const info = () => campos.find((c) => c.nome === sel.value);
                    const dica = () => {
                        const c = info();
                        valor.placeholder = c && c.opcoes.length ? c.opcoes.map((o) => o.valor + "=" + o.rotulo).slice(0, 4).join("; ") : c && c.tipo === "data" ? "AAAA-MM-DD" : "valor (vários: separe por vírgula)";
                    };
                    dica();
                    const aplicar = () => {
                        const c = info();
                        f.campo = sel.value;
                        const partes = valor.value.split(",").map((x) => x.trim()).filter(Boolean);
                        f.valor = c && c.tipo === "multi" ? partes : valor.value.trim();
                        salvar();
                    };
                    sel.addEventListener("change", () => {
                        dica();
                        aplicar();
                    });
                    valor.addEventListener("input", aplicar);
                    const nl = el("label", "check");
                    const nIn = el("input");
                    nIn.type = "checkbox";
                    nIn.checked = !!f.negar;
                    nIn.addEventListener("change", () => {
                        f.negar = nIn.checked;
                        salvar();
                    });
                    nl.append(nIn, document.createTextNode(" NÃO"));
                    const x = el("button", "btn btn-sm btn-ghost", "×");
                    x.type = "button";
                    x.setAttribute("aria-label", "Remover condição");
                    x.addEventListener("click", () => {
                        g.itens.splice(fi, 1);
                        salvar();
                        render();
                    });
                    linha.append(sel, valor, nl, x);
                    grupo.appendChild(linha);
                });
                const add = el("button", "btn btn-sm", "+ Condição");
                add.type = "button";
                add.addEventListener("click", () => {
                    g.itens.push({ campo: "uf", valor: "" });
                    render();
                });
                grupo.appendChild(add);
                box.appendChild(grupo);
            });
            const novo = el("button", "btn btn-sm", "+ Grupo de condições");
            novo.type = "button";
            novo.addEventListener("click", () => {
                arvore.itens.push({ op: "OU", itens: [{ campo: "uf", valor: "" }] });
                render();
            });
            box.appendChild(novo);
            box.appendChild(el("p", "hint", "Os grupos são combinados com E entre si e com os filtros do painel. Ex.: grupo OU com “Município = Palmas” e “Município = Araguaína”."));
        };
        render();
    }

    // Ordenacao e itens por pagina: trocam o parametro e recarregam
    function initTrocaParam() {
        $$("[data-troca-param]").forEach((sel) => {
            sel.addEventListener("change", () => {
                const u = new URL(location.href);
                u.searchParams.set(sel.dataset.trocaParam, sel.value);
                u.searchParams.delete("pagina");
                if (sel.dataset.trocaParam === "ordem") u.searchParams.delete("direcao");
                location.href = u.toString();
            });
        });
    }

    // Selecao de linhas, copiar, adicionar a lista, exportar selecionados
    function initSelecao() {
        const tabela = $("[data-resultados]");
        const barra = $("[data-selecao-barra]");
        if (!tabela || !barra) return;
        const caixas = $$("[data-sel]", tabela);
        const todos = $("[data-sel-todos]", tabela);
        const total = $("[data-selecao-total]", barra);
        const marcados = () => caixas.filter((c) => c.checked).map((c) => c.value);
        const atualizar = () => {
            const n = marcados().length;
            barra.hidden = n === 0;
            total.textContent = String(n);
            caixas.forEach((c) => c.closest("tr").classList.toggle("selecionada", c.checked));
            if (todos) todos.checked = n === caixas.length && n > 0;
        };
        caixas.forEach((c) => c.addEventListener("change", atualizar));
        if (todos)
            todos.addEventListener("change", () => {
                caixas.forEach((c) => (c.checked = todos.checked));
                atualizar();
            });
        const copiar = $("[data-copiar-selecionados]", barra);
        if (copiar)
            copiar.addEventListener("click", async () => {
                const txt = marcados().map(fmtCnpj).join("\n");
                try {
                    await navigator.clipboard.writeText(txt);
                    toast(marcados().length + " CNPJ(s) copiados");
                } catch (e) {
                    toast("Não foi possível copiar.");
                }
            });
        const add = $("[data-adicionar-lista]", barra);
        if (add)
            add.addEventListener("click", async () => {
                const id = $("[data-lista-destino]", barra).value;
                const itens = marcados().map((cnpj) => {
                    const tr = tabela.querySelector('tr[data-cnpj="' + cnpj + '"]');
                    const nome = tr ? tr.querySelector(".cell-title") : null;
                    return { cnpj, razao_social: nome ? nome.textContent.trim() : null };
                });
                try {
                    const r = await api("POST", "/api/listas/" + id + "/itens", { itens });
                    toast(r.adicionados + " empresa(s) adicionada(s) à lista");
                } catch (e) {
                    toast("Falha ao adicionar: " + e.message);
                }
            });
        const exp = $("[data-exportar-selecionados]", barra);
        if (exp)
            exp.addEventListener("click", () => {
                const form = $("form[data-exportar]");
                const p = form ? new URLSearchParams(new FormData(form)) : new URLSearchParams();
                p.set("selecionados", marcados().join(","));
                exportar("/api/busca/exportar." + exp.dataset.exportarSelecionados, p);
            });
    }

    // Colunas visiveis (preferencia salva neste navegador)
    function initColunas() {
        const tabela = $("[data-resultados]");
        if (!tabela) return;
        let ocultas = [];
        try {
            ocultas = JSON.parse(localStorage.getItem("cnpj.colunas.ocultas") || "[]");
        } catch (e) {
            ocultas = [];
        }
        const aplicar = () => $$("[data-col]", tabela).forEach((c) => (c.hidden = ocultas.includes(c.dataset.col)));
        $$("[data-col-toggle]").forEach((chk) => {
            chk.checked = !ocultas.includes(chk.dataset.colToggle);
            chk.addEventListener("change", () => {
                ocultas = ocultas.filter((x) => x !== chk.dataset.colToggle);
                if (!chk.checked) ocultas.push(chk.dataset.colToggle);
                try {
                    localStorage.setItem("cnpj.colunas.ocultas", JSON.stringify(ocultas));
                } catch (e) {
                    /* sem armazenamento */
                }
                aplicar();
            });
        });
        aplicar();
    }

    // Exportacao: pequena = download direto; grande = segundo plano com aviso
    async function exportar(url, params) {
        const restritas = $$("form[data-exportar] input[data-restrita]:checked");
        const fin = params.get("finalidade") || "";
        if (restritas.length && fin.trim().length < 10) {
            toast("Informe a finalidade (mín. 10 caracteres) para exportar colunas de contato ou pessoais.");
            const f = document.getElementById("f-finalidade");
            if (f) f.focus();
            return;
        }
        try {
            const r = await fetch(url + "?" + params.toString());
            const tipo = r.headers.get("content-type") || "";
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                throw new Error(d.detail || "HTTP " + r.status);
            }
            if (tipo.includes("application/json")) {
                const d = await r.json();
                toast(d.mensagem || "Exportação em segundo plano iniciada.");
                acompanharExportacao(d.exportacao.id);
                return;
            }
            const blob = await r.blob();
            const a = el("a");
            a.href = URL.createObjectURL(blob);
            a.download = url.endsWith(".xlsx") ? "busca-cnpj.xlsx" : "busca-cnpj.csv";
            document.body.appendChild(a);
            a.click();
            setTimeout(() => {
                URL.revokeObjectURL(a.href);
                a.remove();
            }, 1000);
        } catch (e) {
            toast("Falha na exportação: " + e.message);
        }
    }

    function acompanharExportacao(id) {
        const tick = async () => {
            try {
                const d = await api("GET", "/api/exportacoes/" + id);
                if (d.status === "concluido") {
                    toast("Exportação pronta: " + fmtNum(d.feitas) + " linhas. Baixando…");
                    location.href = "/api/exportacoes/" + id + "/arquivo";
                    return;
                }
                if (d.status === "falhou") {
                    toast("A exportação falhou: " + (d.erro || ""));
                    return;
                }
                setTimeout(tick, 3000);
            } catch (e) {
                setTimeout(tick, 5000);
            }
        };
        setTimeout(tick, 2000);
    }

    function initExportar() {
        const form = $("form[data-exportar]");
        if (!form) return;
        form.addEventListener("submit", (ev) => {
            ev.preventDefault();
            const btn = ev.submitter;
            const url = (btn && btn.getAttribute("formaction")) || form.getAttribute("action");
            exportar(url, new URLSearchParams(new FormData(form)));
        });
    }

    // Contadores (distribuicao do resultado) e possiveis duplicidades
    async function initContadores() {
        const box = $("[data-contadores]");
        if (!box) return;
        const qs = box.dataset.qs || "";
        const dims = [
            ["uf", "UF", "uf"],
            ["situacao", "Situação", "situacao"],
            ["porte", "Porte", "porte"],
            ["municipio", "Município", "municipio"],
            ["cnae_principal", "CNAE principal", "cnae_principal"],
            ["tipo_telefone_1", "Tipo do telefone principal", "tipo_telefone_principal"],
        ];
        try {
            const d = await api("GET", "/api/busca/contadores?" + qs + "&" + dims.map((x) => "dimensao=" + x[0]).join("&"));
            const vazio = $("[data-contadores-vazio]", box);
            if (vazio) vazio.remove();
            const grade = el("div", "contadores");
            dims.forEach(([dim, rotulo, param]) => {
                const itens = d[dim] || [];
                if (!itens.length) return;
                const bloco = el("div", "contador");
                bloco.appendChild(el("h3", "", rotulo));
                const ol = el("ol");
                const max = Math.max(...itens.map((i) => i.total));
                itens.slice(0, 8).forEach((i) => {
                    const li = el("li");
                    if (i.valor !== null && i.valor !== undefined) {
                        const a = el("a", "", i.rotulo);
                        const p = new URLSearchParams(qs);
                        p.append(param, i.valor);
                        a.href = "/busca?" + p.toString();
                        a.title = "Adicionar este filtro";
                        li.appendChild(a);
                    } else li.appendChild(el("span", "faint", i.rotulo));
                    li.appendChild(el("span", "mono", fmtNum(i.total)));
                    ol.appendChild(li);
                    const m = el("meter");
                    m.min = 0;
                    m.max = max;
                    m.value = i.total;
                    ol.appendChild(m);
                });
                bloco.appendChild(ol);
                grade.appendChild(bloco);
            });
            box.appendChild(grade);
        } catch (e) {
            const vazio = $("[data-contadores-vazio]", box);
            if (vazio) vazio.textContent = "Contadores indisponíveis (" + e.message + ").";
        }

        const tabela = $("[data-resultados]");
        if (!tabela) return;
        const cnpjs = $$("tr[data-cnpj]", tabela).map((tr) => tr.dataset.cnpj);
        try {
            const dup = await api("POST", "/api/busca/duplicidades", { cnpjs });
            Object.entries(dup).forEach(([cnpj, info]) => {
                const alvo = tabela.querySelector('tr[data-cnpj="' + cnpj + '"] [data-dup]');
                if (!alvo) return;
                const partes = [];
                if (info.telefone_compartilhado) partes.push("telefone usado por mais " + info.telefone_compartilhado + " empresa(s)");
                if (info.email_compartilhado) partes.push("e-mail usado por mais " + info.email_compartilhado + " empresa(s)");
                const b = el("span", "badge badge-warning", "Contato compartilhado");
                b.title = partes.join("; ") + ". Pode ser duplicidade ou o contato do escritório de contabilidade.";
                alvo.appendChild(b);
            });
        } catch (e) {
            /* opcional */
        }
    }

    // ------------------------------------------------------------ painel da base
    function initApiForms() {
        $$("form[data-api]").forEach((form) => {
            form.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                let body = {};
                if (form.dataset.confirmar) {
                    if (form.dataset.confirmarTexto) {
                        const r = window.prompt(form.dataset.confirmar + "\n\nPara confirmar, digite " + form.dataset.confirmarTexto + ":");
                        if (r === null) return;
                        body.confirmar = r.trim();
                    } else if (!window.confirm(form.dataset.confirmar)) return;
                }
                if (form.dataset.apiJson) {
                    const mapa = JSON.parse(form.dataset.apiJson);
                    const fd = new FormData(form);
                    Object.entries(mapa).forEach(([chave, campo]) => {
                        const v = fd.get(campo);
                        body[chave] = v === "sim" ? true : v === null ? (chave === "forcar" ? false : null) : v || null;
                    });
                }
                const btn = form.querySelector("button[type=submit]");
                if (btn) btn.disabled = true;
                try {
                    await api("POST", form.dataset.api, body);
                    location.reload();
                } catch (e) {
                    toast(e.message);
                    if (btn) btn.disabled = false;
                }
            });
        });
    }

    function initPainelBase() {
        const card = $("[data-job]");
        if (!card) return;
        const id = card.dataset.id;
        const rodando = () => ["pendente", "executando"].includes(card.dataset.status);
        if (!rodando()) return;
        const set = (sel, txt) => {
            const n = $(sel, card);
            if (n) n.textContent = txt;
        };
        const tick = async () => {
            let j;
            try {
                j = await api("GET", "/api/admin/base/job/" + id + "?logs=40");
            } catch (e) {
                setTimeout(tick, 5000);
                return;
            }
            const pct = Math.round(j.progresso || 0);
            set("[data-job-pct]", pct + "%");
            const barra = $("[data-job-barra]", card);
            if (barra) barra.value = j.progresso || 0;
            set("[data-job-msg]", j.mensagem || "");
            set("[data-job-arquivo]", j.arquivo_atual || "—");
            set("[data-job-pct-etapa]", Math.round(j.progresso_etapa || 0) + "%");
            set("[data-job-bytes]", fmtBytes(j.bytes_feitos) + " de " + fmtBytes(j.bytes_total));
            set("[data-job-vel]", j.velocidade_bps ? fmtBytes(j.velocidade_bps) + "/s" : "—");
            set("[data-job-eta]", j.eta_segundos ? Math.ceil(j.eta_segundos / 60) + " min" : "—");
            set("[data-job-registros]", fmtNum(j.registros || 0));
            let passou = false;
            $$("[data-etapa]", card).forEach((li) => {
                const atual = li.dataset.etapa === j.etapa;
                li.classList.toggle("atual", atual);
                li.classList.toggle("feita", !passou && !atual && !!j.etapa);
                if (atual) passou = true;
            });
            const logs = $("[data-job-logs]", card);
            if (logs && j.logs) {
                logs.replaceChildren();
                j.logs.forEach((l) => {
                    const li = el("li", "log-" + l.nivel);
                    li.append(el("span", "mono faint", (l.em || "").replace("T", " ").slice(0, 19) + " "), document.createTextNode(l.mensagem));
                    logs.appendChild(li);
                });
                logs.scrollTop = logs.scrollHeight;
            }
            card.dataset.status = j.status_efetivo;
            if (rodando()) setTimeout(tick, 2000);
            else location.reload();
        };
        setTimeout(tick, 1500);
    }

    document.addEventListener("DOMContentLoaded", () => {
        initSugestoes();
        initConfirmacoes();
        initBusca();
        initTrocaParam();
        initSelecao();
        initColunas();
        initExportar();
        initContadores();
        initApiForms();
        initPainelBase();
    });
})();
