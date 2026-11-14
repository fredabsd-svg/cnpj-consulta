/* CNPJ Consulta - chat com IA.
 * Conversa em fluxo (eventos SSE lidos com fetch), lista de conversas e
 * cadastro dos provedores. O markdown das respostas e montado com DOM
 * (textContent): nada vindo do modelo entra como HTML. */
"use strict";

(function () {
    const raiz = document.querySelector("[data-chat-app]");
    if (!raiz) return;
    const $ = (sel, r = document) => r.querySelector(sel);

    const ui = {
        lista: $("[data-chat-lista]"),
        msgs: $("[data-chat-mensagens]"),
        vazio: $("[data-chat-vazio]"),
        form: $("[data-chat-form]"),
        texto: $("[data-chat-texto]"),
        enviar: $('[data-chat="enviar"]'),
        parar: $('[data-chat="parar"]'),
        provedor: $("[data-chat-provedor]"),
        modelo: $("[data-chat-modelo]"),
        modeloOutro: $("[data-chat-modelo-outro]"),
        uso: $("[data-chat-uso]"),
        plano: $("[data-chat-plano]"),
        boasVindas: $("[data-chat-boasvindas]"),
        dialogo: $("[data-chat-dialogo]"),
        config: $("[data-chat-config-lista]"),
        exportar: $("[data-chat-exportar]"),
    };
    // turno: perguntas na tela (a exportacao de uma resposta usa esse numero, a partir de 1)
    const estado = { provedores: [], conversas: [], atual: null, controle: null, enviando: false,
        turno: 0, turnoComResposta: false };

    // ------------------------------------------------------------ utilitarios

    function el(tag, cls, texto) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (texto !== undefined && texto !== null) e.textContent = String(texto);
        return e;
    }

    function toast(msg) {
        const t = el("div", "toast", msg);
        t.setAttribute("role", "status");
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 4000);
    }

    const ICONES = {
        ok: ['<circle cx="12" cy="12" r="10"/>', "m9 12 2 2 4-4"],
        falha: ['<circle cx="12" cy="12" r="10"/>', "m15 9-6 6", "m9 9 6 6"],
        ferramenta: [null, "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"],
        lixo: [null, "M3 6h18", "M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6", "M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"],
        arquivo: [null, "M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z", "M14 2v4a2 2 0 0 0 2 2h4",
            "M16 13H8", "M16 17H8", "M10 9H8"],
        tabela: [null, "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z", "M3 9h18",
            "M3 15h18", "M9 3v18"],
    };

    function icone(nome) {
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("class", "i");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("aria-hidden", "true");
        const [circulo, ...caminhos] = ICONES[nome];
        if (circulo) {
            const c = document.createElementNS(ns, "circle");
            c.setAttribute("cx", "12"); c.setAttribute("cy", "12"); c.setAttribute("r", "10");
            svg.appendChild(c);
        }
        for (const d of caminhos) {
            const p = document.createElementNS(ns, "path");
            p.setAttribute("d", d);
            svg.appendChild(p);
        }
        return svg;
    }

    async function api(url, opcoes = {}) {
        const op = { headers: { "Content-Type": "application/json" }, ...opcoes };
        if (op.body && typeof op.body !== "string") op.body = JSON.stringify(op.body);
        const r = await fetch(url, op);
        if (r.status === 401) {
            location.href = "/admin/entrar?proximo=/chat";
            throw new Error("Sessão expirada.");
        }
        if (!r.ok) {
            let msg = "HTTP " + r.status;
            try { const j = await r.json(); msg = j.detail || msg; } catch (e) { /* corpo sem JSON */ }
            throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
        }
        return r;
    }

    function rolar() {
        ui.msgs.scrollTop = ui.msgs.scrollHeight;
    }

    // ------------------------------------------------------------ markdown seguro

    // Barra invertida e caracteres de controle ficam de fora: o navegador trata
    // "\\" como "/" e "/\\outro.site" sairia do app parecendo link interno.
    function linkSeguro(url) {
        if (/[\\\u0000-\u001f\u007f]/.test(url)) return null;
        if (/^\/(?!\/)/.test(url)) return { href: url, externo: false };
        if (/^https?:\/\/[^/]/i.test(url)) return { href: url, externo: true };
        return null;
    }

    const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(\[[^\]\n]+\]\([^)\s]+\))|(\*[^*\s\n][^*\n]*\*)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

    function inline(texto, pai) {
        let ultimo = 0;
        texto.replace(INLINE, (m, code, negrito, link, italico, url, pos) => {
            if (pos > ultimo) pai.appendChild(document.createTextNode(texto.slice(ultimo, pos)));
            if (code) pai.appendChild(el("code", null, code.slice(1, -1)));
            else if (negrito) { const b = el("strong"); inline(negrito.slice(2, -2), b); pai.appendChild(b); }
            else if (italico) { const i = el("em"); inline(italico.slice(1, -1), i); pai.appendChild(i); }
            else if (link || url) {
                let rotulo = url, alvo = url;
                if (link) { const k = link.indexOf("]("); rotulo = link.slice(1, k); alvo = link.slice(k + 2, -1); }
                const s = linkSeguro(alvo);
                if (s) {
                    const a = el("a", null, rotulo);
                    a.href = s.href;
                    if (s.externo) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
                    pai.appendChild(a);
                } else pai.appendChild(document.createTextNode(rotulo));
            }
            ultimo = pos + m.length;
            return m;
        });
        if (ultimo < texto.length) pai.appendChild(document.createTextNode(texto.slice(ultimo)));
    }

    function celulas(linha) {
        return linha.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    }

    function markdown(texto) {
        const frag = document.createDocumentFragment();
        const linhas = texto.replace(/\r\n?/g, "\n").split("\n");
        let i = 0;
        while (i < linhas.length) {
            const l = linhas[i];
            if (/^\s*```/.test(l)) {
                const corpo = [];
                i++;
                while (i < linhas.length && !/^\s*```/.test(linhas[i])) corpo.push(linhas[i++]);
                i++;
                const pre = el("pre");
                pre.appendChild(el("code", null, corpo.join("\n")));
                frag.appendChild(pre);
                continue;
            }
            if (/^\s*\|/.test(l) && i + 1 < linhas.length && /^\s*\|?\s*:?-{2,}/.test(linhas[i + 1])) {
                const wrap = el("div", "table-wrap");
                const t = el("table");
                const thead = el("thead"), tr = el("tr");
                for (const c of celulas(l)) { const th = el("th"); inline(c, th); tr.appendChild(th); }
                thead.appendChild(tr); t.appendChild(thead);
                const tbody = el("tbody");
                i += 2;
                while (i < linhas.length && /^\s*\|/.test(linhas[i])) {
                    const r = el("tr");
                    for (const c of celulas(linhas[i])) { const td = el("td"); inline(c, td); r.appendChild(td); }
                    tbody.appendChild(r);
                    i++;
                }
                t.appendChild(tbody); wrap.appendChild(t); frag.appendChild(wrap);
                continue;
            }
            const h = /^(#{1,6})\s+(.*)$/.exec(l);
            if (h) {
                const n = Math.min(4, h[1].length + 1);
                const e = el("h" + n); inline(h[2], e); frag.appendChild(e);
                i++;
                continue;
            }
            if (/^\s*(---|\*\*\*)\s*$/.test(l)) { frag.appendChild(el("hr")); i++; continue; }
            if (/^\s*>/.test(l)) {
                const q = el("blockquote"), partes = [];
                while (i < linhas.length && /^\s*>/.test(linhas[i])) partes.push(linhas[i++].replace(/^\s*>\s?/, ""));
                inline(partes.join(" "), q); frag.appendChild(q);
                continue;
            }
            const marcador = /^\s*([-*+]|\d+[.)])\s+/;
            if (marcador.test(l)) {
                const ordenada = /^\s*\d/.test(l);
                const lista = el(ordenada ? "ol" : "ul");
                while (i < linhas.length && marcador.test(linhas[i])) {
                    const li = el("li"); inline(linhas[i].replace(marcador, ""), li); lista.appendChild(li);
                    i++;
                }
                frag.appendChild(lista);
                continue;
            }
            if (!l.trim()) { i++; continue; }
            const p = el("p"), partes = [];
            while (i < linhas.length && linhas[i].trim() && !/^\s*(```|\||#{1,6}\s|>|[-*+]\s|\d+[.)]\s)/.test(linhas[i])) {
                partes.push(linhas[i++]);
            }
            if (!partes.length) partes.push(linhas[i++]);
            partes.forEach((t, k) => { if (k) p.appendChild(el("br")); inline(t, p); });
            frag.appendChild(p);
        }
        return frag;
    }

    // ------------------------------------------------------------ mensagens na tela

    function limparTela() {
        ui.msgs.replaceChildren();
        estado.turno = 0;
        estado.turnoComResposta = false;
        atualizarExportar();
    }

    function mostrarVazio() {
        limparTela();
        ui.msgs.appendChild(ui.vazio);
    }

    function bolhaUsuario(texto) {
        estado.turno += 1;
        estado.turnoComResposta = false;
        ui.msgs.appendChild(el("div", "msg msg-usuario", texto));
    }

    // Fim de um turno com resposta: atalhos para exportar so aquela resposta.
    function fecharTurno() {
        if (!estado.turno || !estado.turnoComResposta) return;
        estado.turnoComResposta = false;
        const barra = el("div", "msg-acoes");
        barra.setAttribute("role", "group");
        barra.setAttribute("aria-label", "Exportar esta resposta");
        barra.appendChild(el("span", "msg-acoes-rotulo", "Exportar resposta"));
        for (const [formato, rotulo, ic] of [["pdf", "PDF", "arquivo"], ["xlsx", "Excel", "tabela"]]) {
            const b = el("button", "msg-acao");
            b.type = "button";
            b.dataset.exportar = formato;
            b.dataset.turno = String(estado.turno);
            b.title = "Baixar esta pergunta e a resposta em " + rotulo;
            b.append(icone(ic), el("span", null, rotulo));
            barra.appendChild(b);
        }
        ui.msgs.appendChild(barra);
        atualizarExportar();
    }

    function bolhaAssistente(texto) {
        estado.turnoComResposta = true;
        const d = el("div", "msg msg-assistente prose");
        d.dataset.bruto = texto || "";
        d.appendChild(markdown(d.dataset.bruto));
        ui.msgs.appendChild(d);
        return d;
    }

    function aviso(texto) {
        ui.msgs.appendChild(el("div", "msg msg-aviso", texto));
    }

    function blocoFerramenta(nome, entrada) {
        const d = el("details", "msg msg-ferramenta");
        d.dataset.estado = "rodando";
        const s = el("summary");
        const sp = el("span", "spinner");
        sp.setAttribute("aria-hidden", "true");
        s.append(sp, el("span", null, "Ferramenta "), el("code", null, nome));
        d.appendChild(s);
        const args = typeof entrada === "string" ? entrada : JSON.stringify(entrada, null, 1);
        if (args && args !== "{}") d.appendChild(el("pre", null, "Entrada: " + args));
        ui.msgs.appendChild(d);
        return d;
    }

    function concluirFerramenta(d, ok, resumo) {
        d.dataset.estado = ok ? "ok" : "erro";
        const s = d.querySelector("summary");
        s.firstChild.replaceWith(icone(ok ? "ok" : "falha"));
        if (!ok) s.appendChild(el("span", null, " — falhou"));
        if (resumo) d.appendChild(el("pre", null, (ok ? "Resultado: " : "Erro: ") + resumo));
    }

    function renderExibicao(itens) {
        limparTela();
        for (const it of itens || []) {
            if (it.tipo === "usuario") { fecharTurno(); bolhaUsuario(it.texto); }
            else if (it.tipo === "assistente") bolhaAssistente(it.texto);
            else if (it.tipo === "aviso") aviso(it.texto);
            else if (it.tipo === "ferramenta") concluirFerramenta(blocoFerramenta(it.nome, it.entrada), it.ok, it.resumo);
        }
        fecharTurno();
        if (!ui.msgs.children.length) mostrarVazio();
        rolar();
    }

    // ------------------------------------------------------------ provedores e modelos

    function provedorAtual() {
        return estado.provedores.find((p) => p.id === ui.provedor.value);
    }

    const OUTRO = "__outro__";

    // Modelo escolhido: item da lista ou o nome digitado em "Outro modelo…".
    function modeloEscolhido() {
        return ui.modelo.value === OUTRO ? ui.modeloOutro.value.trim() : ui.modelo.value;
    }

    function definirModelo(m) {
        const p = provedorAtual();
        if (m && !Array.from(ui.modelo.options).some((o) => o.value === m)) {
            // modelo fora da lista (ex.: conversa antiga): entra como opcao propria
            const o = el("option", null, m);
            o.value = m;
            ui.modelo.insertBefore(o, ui.modelo.lastElementChild);
        }
        ui.modelo.value = m || (p && p.modelos[0]) || OUTRO;
        ui.modeloOutro.hidden = ui.modelo.value !== OUTRO;
    }

    function preencherModelos(manterValor) {
        const p = provedorAtual();
        const anterior = modeloEscolhido();
        const nomes = (p && p.nomes_modelos) || {};
        const opcoes = (p ? p.modelos : []).map((m) => {
            const o = el("option", null, nomes[m] ? nomes[m] + " — " + m : m);
            o.value = m;
            return o;
        });
        const outro = el("option", null, "Outro modelo…");
        outro.value = OUTRO;
        ui.modelo.replaceChildren(...opcoes, outro);
        definirModelo(manterValor ? anterior : (p ? p.modelo_padrao : ""));
        ui.modelo.disabled = !p;
        ui.modeloOutro.disabled = !p;
        if (!p) ui.modeloOutro.hidden = true;
        ui.plano.hidden = !(p && p.id === "chatgpt");  // "Usando o plano do ChatGPT"
    }

    function preencherProvedores() {
        const conf = estado.provedores.filter((p) => p.configurado);
        const anterior = ui.provedor.value;
        ui.provedor.replaceChildren();
        if (!conf.length) {
            const o = el("option", null, "Nenhum provedor configurado");
            o.value = ""; ui.provedor.appendChild(o);
        }
        for (const p of conf) {
            const o = el("option", null, p.nome);
            o.value = p.id;
            ui.provedor.appendChild(o);
        }
        const padrao = conf.find((p) => p.padrao) || conf[0];
        ui.provedor.value = conf.some((p) => p.id === anterior) ? anterior : (padrao ? padrao.id : "");
        preencherModelos(false);
        ui.enviar.disabled = !conf.length;
        raiz.dataset.algum = conf.length ? "sim" : "nao";
    }

    async function carregarProvedores() {
        try {
            estado.provedores = await (await api("/chat/api/provedores")).json();
        } catch (e) {
            toast("Falha ao carregar provedores: " + e.message);
            estado.provedores = [];
        }
        preencherProvedores();
        renderConfig();
    }

    async function trocarModelo() {
        if (!estado.atual || estado.enviando) return;
        try {
            const r = await api("/chat/api/conversas/" + estado.atual.id, {
                method: "PATCH", body: { provedor: ui.provedor.value, modelo: modeloEscolhido() },
            });
            estado.atual = { ...estado.atual, ...(await r.json()) };
            toast("Modelo desta conversa: " + estado.atual.modelo);
            carregarConversas();
        } catch (e) {
            toast(e.message);
            ui.provedor.value = estado.atual.provedor;
            preencherModelos(true);
            definirModelo(estado.atual.modelo);
        }
    }

    // ------------------------------------------------------------ conversas

    function renderConversas() {
        ui.lista.replaceChildren();
        if (!estado.conversas.length) {
            ui.lista.appendChild(el("li", "faint small", "Nenhuma conversa ainda."));
            return;
        }
        for (const c of estado.conversas) {
            const li = el("li");
            if (estado.atual && estado.atual.id === c.id) li.setAttribute("aria-current", "true");
            const a = el("a");
            a.href = "#c" + c.id;
            a.append(el("span", "quick-title", c.titulo), el("span", "quick-meta", c.modelo));
            a.addEventListener("click", (ev) => { ev.preventDefault(); abrirConversa(c.id); });
            const apagar = el("button", "icon-btn icon-btn-sm");
            apagar.type = "button";
            apagar.title = "Apagar conversa";
            apagar.setAttribute("aria-label", "Apagar conversa " + c.titulo);
            apagar.appendChild(icone("lixo"));
            apagar.addEventListener("click", () => apagarConversa(c));
            li.append(a, apagar);
            ui.lista.appendChild(li);
        }
    }

    async function carregarConversas() {
        try {
            estado.conversas = await (await api("/chat/api/conversas")).json();
        } catch (e) {
            estado.conversas = [];
        }
        renderConversas();
    }

    async function abrirConversa(id) {
        if (estado.enviando) return;
        try {
            const c = await (await api("/chat/api/conversas/" + id)).json();
            estado.atual = c;
            if (estado.provedores.some((p) => p.id === c.provedor && p.configurado)) {
                ui.provedor.value = c.provedor;
                preencherModelos(true);
            }
            definirModelo(c.modelo);
            ui.uso.textContent = usoTexto(c.tokens_entrada, c.tokens_saida);
            renderExibicao(c.exibicao);
            renderConversas();
            history.replaceState(null, "", "#c" + id);
        } catch (e) {
            toast(e.message);
        }
    }

    function novaConversa() {
        if (estado.enviando) return;
        estado.atual = null;
        ui.uso.textContent = "";
        mostrarVazio();
        renderConversas();
        history.replaceState(null, "", location.pathname);
        ui.texto.focus();
    }

    async function apagarConversa(c) {
        if (estado.enviando && estado.atual && estado.atual.id === c.id) {
            toast("Pare a resposta antes de apagar esta conversa.");
            return;
        }
        if (!confirm('Apagar a conversa "' + c.titulo + '"?')) return;
        try {
            await api("/chat/api/conversas/" + c.id, { method: "DELETE" });
            if (estado.atual && estado.atual.id === c.id) novaConversa();
            carregarConversas();
        } catch (e) {
            toast(e.message);
        }
    }

    function usoTexto(e, s) {
        if (!e && !s) return "";
        const f = (n) => (n || 0).toLocaleString("pt-BR");
        return "tokens: " + f(e) + " entrada · " + f(s) + " saída";
    }

    // ------------------------------------------------------------ envio e fluxo

    function ocupado(sim) {
        estado.enviando = sim;
        ui.enviar.disabled = sim || raiz.dataset.algum !== "sim";
        ui.parar.hidden = !sim;
        ui.provedor.disabled = sim;
        ui.modelo.disabled = sim;
        ui.modeloOutro.disabled = sim;
        if (sim) ui.exportar.open = false;
    }

    async function enviar(texto) {
        texto = (texto || "").trim();
        if (!texto || estado.enviando) return;
        if (!ui.provedor.value) { abrirConfig(); return; }
        ocupado(true);
        try {
            if (!estado.atual) {
                const r = await api("/chat/api/conversas", {
                    method: "POST", body: { provedor: ui.provedor.value, modelo: modeloEscolhido() },
                });
                estado.atual = { ...(await r.json()), exibicao: [] };
                history.replaceState(null, "", "#c" + estado.atual.id);
                limparTela();
            } else if (ui.msgs.contains(ui.vazio)) {
                limparTela();
            }
            ui.texto.value = "";
            ajustarAltura();
            bolhaUsuario(texto);
            rolar();
            await fluxo(estado.atual.id, texto);
        } catch (e) {
            if (e.name !== "AbortError") { aviso(e.message); toast(e.message); }
        } finally {
            estado.controle = null;
            fecharTurno();
            rolar();
            ocupado(false);
            carregarConversas();
            ui.texto.focus();
        }
    }

    async function fluxo(id, texto) {
        estado.controle = new AbortController();
        const r = await api("/chat/api/conversas/" + id + "/mensagens", {
            method: "POST", body: { texto }, signal: estado.controle.signal,
        });
        const leitor = r.body.getReader();
        const dec = new TextDecoder();
        let buffer = "";
        const ctx = { bolha: null, pensando: null, ferramentas: {}, pendente: false, sujas: new Set() };
        for (;;) {
            const { value, done } = await leitor.read();
            if (done) break;
            buffer += dec.decode(value, { stream: true });
            let fim;
            while ((fim = buffer.indexOf("\n\n")) >= 0) {
                const bloco = buffer.slice(0, fim);
                buffer = buffer.slice(fim + 2);
                for (const linha of bloco.split("\n")) {
                    if (!linha.startsWith("data:")) continue;
                    try { tratar(JSON.parse(linha.slice(5)), ctx); } catch (e) { /* evento malformado */ }
                }
            }
        }
        tirarPensando(ctx);
    }

    function tirarPensando(ctx) {
        if (ctx.pensando) { ctx.pensando.remove(); ctx.pensando = null; }
    }

    // Redesenha no proximo quadro as bolhas que mudaram (inclusive a que ficou
    // para tras quando o modelo passou a chamar uma ferramenta).
    function redesenhar(ctx, bolha) {
        ctx.sujas.add(bolha);
        if (ctx.pendente) return;
        ctx.pendente = true;
        requestAnimationFrame(() => {
            ctx.pendente = false;
            for (const b of ctx.sujas) b.replaceChildren(markdown(b.dataset.bruto));
            ctx.sujas.clear();
            rolar();
        });
    }

    function tratar(ev, ctx) {
        switch (ev.tipo) {
        case "inicio":
        case "pensando":
            if (!ctx.pensando) {
                ctx.pensando = el("div", "msg msg-pensando");
                const sp = el("span", "spinner"); sp.setAttribute("aria-hidden", "true");
                ctx.pensando.append(sp, el("span", null, "Pensando…"));
                ui.msgs.appendChild(ctx.pensando);
                rolar();
            }
            break;
        case "reiniciar":
            // O servidor refez a ida ao modelo: some o texto parcial da tentativa anterior.
            if (ctx.bolha) { ctx.sujas.delete(ctx.bolha); ctx.bolha.remove(); ctx.bolha = null; }
            break;
        case "texto":
            tirarPensando(ctx);
            if (!ctx.bolha) ctx.bolha = bolhaAssistente("");
            ctx.bolha.dataset.bruto += ev.delta;
            redesenhar(ctx, ctx.bolha);
            break;
        case "ferramenta":
            tirarPensando(ctx);
            ctx.bolha = null;
            ctx.ferramentas[ev.id] = blocoFerramenta(ev.nome, ev.entrada);
            rolar();
            break;
        case "resultado": {
            const d = ctx.ferramentas[ev.id];
            if (d) concluirFerramenta(d, ev.ok, ev.resumo);
            // proxima ida ao modelo
            tratar({ tipo: "pensando" }, ctx);
            break;
        }
        case "aviso":
            tirarPensando(ctx);
            aviso(ev.mensagem);
            rolar();
            break;
        case "erro":
            tirarPensando(ctx);
            aviso(ev.mensagem);
            toast(ev.mensagem);
            rolar();
            break;
        case "fim":
            tirarPensando(ctx);
            if (estado.atual) {
                estado.atual.tokens_entrada = (estado.atual.tokens_entrada || 0) + (ev.uso.entrada || 0);
                estado.atual.tokens_saida = (estado.atual.tokens_saida || 0) + (ev.uso.saida || 0);
                ui.uso.textContent = usoTexto(estado.atual.tokens_entrada, estado.atual.tokens_saida);
            }
            break;
        default:
            break;
        }
    }

    function ajustarAltura() {
        ui.texto.style.height = "auto";
        ui.texto.style.height = Math.min(ui.texto.scrollHeight, 240) + "px";
    }

    // ------------------------------------------------------------ exportar (PDF / Excel)

    function atualizarExportar() {
        const tem = !!estado.atual && !!ui.msgs.querySelector(".msg-acoes");
        ui.exportar.hidden = !tem;
        if (!tem) ui.exportar.open = false;
    }

    const NOME_FORMATO = { pdf: "PDF", xlsx: "Excel" };

    async function exportar(formato, turno, botao) {
        if (!estado.atual) return;
        if (estado.enviando) { toast("Aguarde a resposta terminar para exportar."); return; }
        const q = new URLSearchParams({ formato });
        if (turno) q.set("turno", turno);
        let conteudoOriginal = null;
        if (botao) {
            botao.disabled = true;
            botao.setAttribute("aria-busy", "true");
            if (botao.classList.contains("msg-acao")) {
                conteudoOriginal = Array.from(botao.childNodes);
                const sp = el("span", "spinner");
                sp.setAttribute("aria-hidden", "true");
                botao.replaceChildren(sp, el("span", null, "Gerando…"));
            }
        }
        try {
            const r = await api("/chat/api/conversas/" + estado.atual.id + "/exportar?" + q.toString());
            const blob = await r.blob();
            const m = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "");
            const nome = m ? m[1] : "consulta-ia." + formato;
            const url = URL.createObjectURL(blob);
            const a = el("a");
            a.href = url;
            a.download = nome;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 60000);
            toast(NOME_FORMATO[formato] + " pronto: " + nome);
        } catch (e) {
            toast("Não foi possível exportar: " + e.message);
        } finally {
            if (botao) {
                botao.disabled = false;
                botao.removeAttribute("aria-busy");
                if (conteudoOriginal) botao.replaceChildren(...conteudoOriginal);
            }
        }
    }

    // ------------------------------------------------------------ configuracao dos provedores

    function abrirConfig() {
        renderConfig();
        if (typeof ui.dialogo.showModal === "function") ui.dialogo.showModal();
        else ui.dialogo.setAttribute("open", "");
    }

    function campo(rotulo, input, dica) {
        const f = el("div", "field");
        const id = "cfg-" + Math.random().toString(36).slice(2, 9);
        input.id = id;
        const l = el("label", null, rotulo);
        l.htmlFor = id;
        f.append(l, input);
        if (dica) f.appendChild(el("p", "hint", dica));
        return f;
    }

    function renderConfig() {
        ui.config.replaceChildren();
        for (const p of estado.provedores) {
            const card = el("form", "chat-prov");
            const topo = el("div", "chat-prov-topo");
            const nome = el("strong", null, p.nome);
            const badge = el("span", "badge " + (p.configurado ? "badge-success" : "badge-plain"),
                p.configurado ? (p.origem === "env" ? "Configurado (.env)" : "Configurado") : "Não configurado");
            topo.append(nome, badge);
            card.appendChild(topo);

            if (p.login && p.origem !== "env") {
                // Login oficial do provedor (OAuth): abre a pagina de login dele e
                // volta com a chave da conta, sem copiar e colar.
                const entrar = el("div", "chat-prov-login");
                const conta = p.conta || null;
                if (p.id === "chatgpt" && conta && conta.conectado) {
                    entrar.appendChild(el("span", "small", "Conectado" + (conta.email ? " como " + conta.email : "") + "."));
                    const sair = el("button", "btn btn-ghost btn-sm", "Sair");
                    sair.type = "button";
                    sair.addEventListener("click", () => sairChatGPT());
                    const uso = el("a", "small", "Gerenciar uso");
                    uso.href = conta.gerenciar_uso; uso.target = "_blank"; uso.rel = "noopener noreferrer";
                    entrar.append(sair, uso);
                } else {
                    const rotulo = p.id === "chatgpt" ? "Continuar com o ChatGPT"
                        : (p.tem_chave ? "Entrar de novo com " : "Entrar com ") + p.nome.split(" (")[0];
                    const a = el("a", "btn btn-primary btn-sm", rotulo);
                    a.href = "/chat/login/" + encodeURIComponent(p.id) + "/iniciar";
                    entrar.append(a, el("span", "hint", p.id === "chatgpt"
                        ? "Usa o seu plano do ChatGPT; abre o login da OpenAI, sem chave."
                        : "Abre o login da sua conta; não precisa copiar a chave."));
                    if (p.id === "chatgpt" && conta && conta.registrado) {
                        const outra = el("a", "small", "Entrar com outra conta");
                        outra.href = "/chat/login/chatgpt/iniciar?nova_conta=1";
                        entrar.appendChild(outra);
                    }
                }
                card.appendChild(entrar);
            }

            const campos = el("div", "chat-prov-campos");
            let chave = null, url = null;
            if (p.precisa_chave || p.id === "personalizado") {
                chave = el("input", "input");
                chave.type = "password";
                chave.autocomplete = "off";
                chave.placeholder = p.tem_chave ? "chave salva " + p.chave_final + " (deixe vazio para manter)" : "cole a chave da API";
                campos.appendChild(campo("Chave da API", chave));
            }
            if (p.local || p.id === "personalizado") {
                url = el("input", "input");
                url.type = "url";
                url.value = p.configurado || p.origem ? p.base_url : "";
                url.placeholder = p.local ? p.base_url || "http://localhost:11434/v1" : "https://servidor/v1";
                campos.appendChild(campo("Endereço (compatível com OpenAI)", url));
            }
            const extras = el("input", "input");
            extras.placeholder = "ex.: modelo-a, modelo-b";
            campos.appendChild(campo("Modelos extras (opcional)", extras,
                "A lista de modelos da sua conta é carregada sozinha; acrescente aqui nomes que não aparecem nela."));
            card.appendChild(campos);

            const acoes = el("div", "chat-prov-acoes");
            const salvar = el("button", "btn btn-primary btn-sm", "Salvar");
            salvar.type = "submit";
            acoes.appendChild(salvar);
            if (p.tem_chave && p.origem !== "env") {
                const apagar = el("button", "btn btn-ghost btn-sm", "Apagar chave");
                apagar.type = "button";
                apagar.addEventListener("click", () => salvarProvedor(p, { apagar_chave: true }));
                acoes.appendChild(apagar);
            }
            if (p.onde_obter) {
                const a = el("a", "small", p.local ? "Instalar" : "Obter chave");
                a.href = p.onde_obter; a.target = "_blank"; a.rel = "noopener noreferrer";
                acoes.appendChild(a);
            }
            if (p.configurado && p.modelos.length) acoes.appendChild(el("span", "faint small", p.modelos.length + " modelo(s)"));
            card.appendChild(acoes);

            card.addEventListener("submit", (ev) => {
                ev.preventDefault();
                const dados = { modelos: extras.value };
                if (chave && chave.value.trim()) dados.api_key = chave.value.trim();
                if (url) dados.base_url = url.value.trim();
                salvarProvedor(p, dados);
            });
            ui.config.appendChild(card);
        }
    }

    async function sairChatGPT() {
        try {
            const r = await (await api("/chat/api/provedores/chatgpt/sair", { method: "POST", body: {} })).json();
            toast(r.revogacao_confirmada ? "Você saiu do ChatGPT."
                : "Login apagado deste computador, mas a OpenAI não confirmou. Desconecte o app nas configurações do ChatGPT.");
            await carregarProvedores();
        } catch (e) {
            toast("ChatGPT: " + e.message);
        }
    }

    async function salvarProvedor(p, dados) {
        try {
            await api("/chat/api/provedores/" + p.id, { method: "POST", body: { habilitado: true, ...dados } });
            toast(p.nome + ": configuração salva.");
            await carregarProvedores();
        } catch (e) {
            toast(p.nome + ": " + e.message);
        }
    }

    // ------------------------------------------------------------ eventos

    ui.form.addEventListener("submit", (ev) => { ev.preventDefault(); enviar(ui.texto.value); });
    ui.texto.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); enviar(ui.texto.value); }
    });
    ui.texto.addEventListener("input", ajustarAltura);
    ui.parar.addEventListener("click", () => { if (estado.controle) estado.controle.abort(); });
    ui.provedor.addEventListener("change", () => { preencherModelos(false); trocarModelo(); });
    ui.modelo.addEventListener("change", () => {
        ui.modeloOutro.hidden = ui.modelo.value !== OUTRO;
        if (ui.modelo.value === OUTRO) { ui.modeloOutro.focus(); return; }
        trocarModelo();
    });
    ui.modeloOutro.addEventListener("change", () => { if (modeloEscolhido()) trocarModelo(); });
    $('[data-chat="nova"]').addEventListener("click", novaConversa);
    $('[data-chat="configurar"]').addEventListener("click", abrirConfig);
    $('[data-chat="fechar"]').addEventListener("click", () => ui.dialogo.close());
    $('[data-chat="entendi"]').addEventListener("click", () => ui.boasVindas.close());
    ui.msgs.addEventListener("click", (ev) => {
        const ex = ev.target.closest("[data-exportar]");
        if (ex) { exportar(ex.dataset.exportar, Number(ex.dataset.turno) || null, ex); return; }
        const b = ev.target.closest("[data-exemplo]");
        if (b) enviar(b.textContent);
    });
    ui.exportar.addEventListener("toggle", () => {
        if (ui.exportar.open && estado.enviando) {
            ui.exportar.open = false;
            toast("Aguarde a resposta terminar para exportar.");
        }
    });
    ui.exportar.querySelectorAll("[data-exportar]").forEach((b) => b.addEventListener("click", async () => {
        await exportar(b.dataset.exportar, null, b);
        ui.exportar.open = false;
    }));
    // Menu fecha ao clicar fora ou com Esc (volta o foco para o botao).
    document.addEventListener("click", (ev) => {
        if (ui.exportar.open && !ui.exportar.contains(ev.target)) ui.exportar.open = false;
    });
    ui.exportar.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && ui.exportar.open) {
            ui.exportar.open = false;
            ui.exportar.querySelector("summary").focus();
        }
    });

    (async function iniciar() {
        const q = new URLSearchParams(location.search);
        const boasVindas = q.get("login_ok") === "chatgpt" && q.get("primeiro") === "1" && !!ui.boasVindas.showModal;
        if (boasVindas) {
            ui.boasVindas.showModal();  // so no primeiro login
        } else if (q.has("login_ok") || q.has("login_erro")) {
            toast(q.has("login_ok") ? "Login feito: " + q.get("login_ok") + " configurado." : q.get("login_erro"));
        }
        if (q.has("login_ok") || q.has("login_erro")) history.replaceState(null, "", location.pathname + location.hash);
        await carregarProvedores();
        await carregarConversas();
        const m = /^#c(\d+)$/.exec(location.hash);
        if (m) abrirConversa(Number(m[1]));
        else if (raiz.dataset.algum !== "sim" && !boasVindas) abrirConfig();
    })();
})();
