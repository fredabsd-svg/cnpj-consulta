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
        modelos: $("[data-chat-modelos]"),
        uso: $("[data-chat-uso]"),
        dialogo: $("[data-chat-dialogo]"),
        config: $("[data-chat-config-lista]"),
    };
    const estado = { provedores: [], conversas: [], atual: null, controle: null, enviando: false };

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

    function linkSeguro(url) {
        if (/^\/(?!\/)/.test(url)) return { href: url, externo: false };
        if (/^https?:\/\//i.test(url)) return { href: url, externo: true };
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
    }

    function mostrarVazio() {
        ui.msgs.replaceChildren(ui.vazio);
    }

    function bolhaUsuario(texto) {
        ui.msgs.appendChild(el("div", "msg msg-usuario", texto));
    }

    function bolhaAssistente(texto) {
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
            if (it.tipo === "usuario") bolhaUsuario(it.texto);
            else if (it.tipo === "assistente") bolhaAssistente(it.texto);
            else if (it.tipo === "aviso") aviso(it.texto);
            else if (it.tipo === "ferramenta") concluirFerramenta(blocoFerramenta(it.nome, it.entrada), it.ok, it.resumo);
        }
        if (!ui.msgs.children.length) mostrarVazio();
        rolar();
    }

    // ------------------------------------------------------------ provedores e modelos

    function provedorAtual() {
        return estado.provedores.find((p) => p.id === ui.provedor.value);
    }

    function preencherModelos(manterValor) {
        const p = provedorAtual();
        ui.modelos.replaceChildren(...(p ? p.modelos : []).map((m) => { const o = el("option"); o.value = m; return o; }));
        if (!manterValor) ui.modelo.value = p ? (p.modelo_padrao || "") : "";
        ui.modelo.disabled = !p;
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
                method: "PATCH", body: { provedor: ui.provedor.value, modelo: ui.modelo.value.trim() },
            });
            estado.atual = { ...estado.atual, ...(await r.json()) };
            toast("Modelo desta conversa: " + estado.atual.modelo);
            carregarConversas();
        } catch (e) {
            toast(e.message);
            ui.provedor.value = estado.atual.provedor;
            preencherModelos(true);
            ui.modelo.value = estado.atual.modelo;
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
            ui.modelo.value = c.modelo;
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
    }

    async function enviar(texto) {
        texto = (texto || "").trim();
        if (!texto || estado.enviando) return;
        if (!ui.provedor.value) { abrirConfig(); return; }
        ocupado(true);
        try {
            if (!estado.atual) {
                const r = await api("/chat/api/conversas", {
                    method: "POST", body: { provedor: ui.provedor.value, modelo: ui.modelo.value.trim() },
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
                p.tipo === "openai" ? "A lista do provedor é carregada sozinha; acrescente aqui modelos que não aparecem." : "Para usar outro modelo Claude além dos sugeridos."));
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
    ui.modelo.addEventListener("change", trocarModelo);
    $('[data-chat="nova"]').addEventListener("click", novaConversa);
    $('[data-chat="configurar"]').addEventListener("click", abrirConfig);
    $('[data-chat="fechar"]').addEventListener("click", () => ui.dialogo.close());
    ui.msgs.addEventListener("click", (ev) => {
        const b = ev.target.closest("[data-exemplo]");
        if (b) enviar(b.textContent);
    });

    (async function iniciar() {
        await carregarProvedores();
        await carregarConversas();
        const m = /^#c(\d+)$/.exec(location.hash);
        if (m) abrirConversa(Number(m[1]));
        else if (raiz.dataset.algum !== "sim") abrirConfig();
    })();
})();
