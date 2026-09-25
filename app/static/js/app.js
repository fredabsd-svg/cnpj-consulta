/* CNPJ Consulta - melhorias progressivas.
 * Todas as paginas funcionam sem este arquivo; aqui ficam mascara, estados de
 * carregamento, abas acessiveis e acoes (copiar, favoritar, apagar historico).
 * Sem scripts inline: a CSP do servidor so permite scripts de 'self'. */
"use strict";

document.documentElement.classList.add("js");

// Tema: aplicado ja no <head> (antes da pintura) para nao piscar.
// Preferencia salva pelo usuario; sem ela, segue o tema do sistema.
(function () {
    let tema = null;
    try {
        tema = localStorage.getItem("cnpj.tema");
    } catch (e) {
        /* armazenamento bloqueado: segue o sistema */
    }
    if (tema !== "claro" && tema !== "escuro") {
        tema = window.matchMedia("(prefers-color-scheme: light)").matches ? "claro" : "escuro";
    }
    document.documentElement.dataset.tema = tema;
})();

(function () {
    function toast(message) {
        const el = document.createElement("div");
        el.className = "toast";
        el.setAttribute("role", "status");
        el.textContent = message;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 2500);
    }

    // Mascara do CNPJ (aceita o formato alfanumerico: letras nas 12 primeiras posicoes)
    function maskCnpj(value) {
        const v = value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 14);
        const parts = [v.slice(0, 2), v.slice(2, 5), v.slice(5, 8), v.slice(8, 12), v.slice(12, 14)];
        let out = parts[0];
        if (parts[1]) out += "." + parts[1];
        if (parts[2]) out += "." + parts[2];
        if (parts[3]) out += "/" + parts[3];
        if (parts[4]) out += "-" + parts[4];
        return out;
    }

    function initMask() {
        document.querySelectorAll("[data-cnpj-mask]").forEach((input) => {
            input.addEventListener("input", () => {
                const atEnd = input.selectionStart === input.value.length;
                input.value = maskCnpj(input.value);
                if (atEnd) input.setSelectionRange(input.value.length, input.value.length);
            });
            input.addEventListener("paste", () => setTimeout(() => (input.value = maskCnpj(input.value))));
        });
    }

    // Estado "carregando" em formularios e links demorados
    function initLoading() {
        document.querySelectorAll("form[data-loading]").forEach((form) => {
            form.addEventListener("submit", () => {
                form.setAttribute("aria-busy", "true");
                const btn = form.querySelector("button[type=submit]");
                if (btn) {
                    btn.setAttribute("aria-busy", "true");
                    btn.dataset.html = btn.innerHTML;
                    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + form.dataset.loading;
                }
            });
        });
        document.querySelectorAll("a[data-loading]").forEach((a) => {
            a.addEventListener("click", (ev) => {
                // Ctrl/Cmd+clique abre em outra aba: esta pagina nao fica "carregando"
                if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button !== 0) return;
                a.setAttribute("aria-busy", "true");
                a.dataset.html = a.innerHTML;
                a.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + a.dataset.loading;
            });
        });
        // Ao voltar pelo historico do navegador, restaura botoes e links
        window.addEventListener("pageshow", (ev) => {
            if (!ev.persisted) return;
            document.querySelectorAll("[aria-busy=true]").forEach((el) => {
                el.removeAttribute("aria-busy");
                if (el.dataset.html) el.innerHTML = el.dataset.html;
                else if (el.dataset.label) el.textContent = el.dataset.label;
            });
        });
    }

    // Abas acessiveis (padrao WAI-ARIA tabs) com sincronia do #hash
    function initTabs() {
        const list = document.querySelector("[role=tablist]");
        if (!list) return;
        const tabs = Array.from(list.querySelectorAll("[role=tab]"));
        const panels = tabs.map((t) => document.getElementById(t.getAttribute("aria-controls")));

        function select(index, focus, updateUrl = true) {
            tabs.forEach((t, i) => {
                const on = i === index;
                t.setAttribute("aria-selected", String(on));
                t.tabIndex = on ? 0 : -1;
                panels[i].hidden = !on;
            });
            if (focus) tabs[index].focus();
            // So grava o #hash quando o usuario troca de aba; gravar no carregamento
            // fazia o navegador rolar ate a aba ao recarregar a pagina.
            if (updateUrl) history.replaceState(null, "", index === 0 ? location.pathname + location.search : "#" + panels[index].id);
        }

        tabs.forEach((tab, i) => {
            tab.addEventListener("click", (ev) => {
                ev.preventDefault();
                select(i, false);
            });
            tab.addEventListener("keydown", (ev) => {
                const map = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 };
                if (!(ev.key in map)) return;
                ev.preventDefault();
                select((map[ev.key] + tabs.length) % tabs.length, true);
            });
        });
        document.querySelectorAll("[data-open-tab]").forEach((link) => {
            link.addEventListener("click", (ev) => {
                const idx = panels.findIndex((p) => p.id === link.dataset.openTab);
                if (idx >= 0) {
                    ev.preventDefault();
                    select(idx, true);
                }
            });
        });
        function syncWithHash() {
            const fromHash = panels.findIndex((p) => "#" + p.id === location.hash);
            select(fromHash >= 0 ? fromHash : 0, false, false);
        }
        // Links diretos (#fontes) e os botoes Voltar/Avancar do navegador
        window.addEventListener("hashchange", syncWithHash);
        syncWithHash();
    }

    function initPrint() {
        document.querySelectorAll("[data-print]").forEach((btn) => {
            btn.addEventListener("click", () => window.print());
        });
    }

    function initCopy() {
        document.querySelectorAll("[data-copy]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                try {
                    await navigator.clipboard.writeText(btn.dataset.copy);
                    toast("Copiado: " + btn.dataset.copy);
                } catch (e) {
                    toast("Não foi possível copiar automaticamente.");
                }
            });
        });
    }

    async function send(method, url) {
        const resp = await fetch(url, { method, headers: { Accept: "application/json" } });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
    }

    function initFavorite() {
        document.querySelectorAll("[data-favorite-url]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const on = btn.getAttribute("aria-pressed") === "true";
                const url = btn.dataset.favoriteUrl + (on ? "" : "?label=" + encodeURIComponent(btn.dataset.label || ""));
                btn.disabled = true;
                try {
                    await send(on ? "DELETE" : "POST", url);
                    btn.setAttribute("aria-pressed", String(!on));
                    btn.querySelector("[data-favorite-text]").textContent = on ? "Favoritar" : "Favorito";
                    toast(on ? "Removido dos favoritos" : "Adicionado aos favoritos");
                } catch (e) {
                    toast("Falha ao salvar favorito (" + e.message + ")");
                } finally {
                    btn.disabled = false;
                }
            });
        });
    }

    function initDeleteActions() {
        document.querySelectorAll("[data-delete-url]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
                btn.disabled = true;
                try {
                    await send("DELETE", btn.dataset.deleteUrl);
                    location.reload();
                } catch (e) {
                    btn.disabled = false;
                    toast("Falha ao apagar (" + e.message + ")");
                }
            });
        });
    }

    function salvar(chave, valor) {
        try {
            localStorage.setItem(chave, valor);
        } catch (e) {
            /* sem armazenamento: vale so ate recarregar */
        }
    }

    // Casca: alternar tema, recolher a barra lateral e menu do celular
    function initShell() {
        const html = document.documentElement;
        document.querySelectorAll('[data-acao="tema"]').forEach((btn) => {
            btn.addEventListener("click", () => {
                html.dataset.tema = html.dataset.tema === "claro" ? "escuro" : "claro";
                salvar("cnpj.tema", html.dataset.tema);
            });
        });

        const app = document.getElementById("app");
        const recolher = document.querySelector('[data-acao="recolher"]');
        if (app && recolher) {
            let recolhida = false;
            try {
                recolhida = localStorage.getItem("cnpj.sidebar") === "recolhida";
            } catch (e) {
                /* padrao: aberta */
            }
            const aplicar = () => {
                app.dataset.recolhida = String(recolhida);
                recolher.setAttribute("aria-expanded", String(!recolhida));
                recolher.title = recolhida ? "Expandir menu" : "Recolher menu";
            };
            aplicar();
            recolher.addEventListener("click", () => {
                recolhida = !recolhida;
                aplicar();
                salvar("cnpj.sidebar", recolhida ? "recolhida" : "aberta");
            });
        }

        const menu = document.querySelector('[data-acao="menu"]');
        const nav = document.getElementById("menu-mobile");
        if (menu && nav) {
            const alternar = (aberto) => {
                nav.dataset.aberto = String(aberto);
                menu.setAttribute("aria-expanded", String(aberto));
                menu.setAttribute("aria-label", aberto ? "Fechar menu" : "Abrir menu");
            };
            menu.addEventListener("click", () => alternar(nav.dataset.aberto !== "true"));
            document.addEventListener("keydown", (ev) => {
                if (ev.key === "Escape" && nav.dataset.aberto === "true") {
                    alternar(false);
                    menu.focus();
                }
            });
        }
    }

    // Atalho "/": foca a busca de CNPJ (como em apps SaaS), exceto ao digitar
    function initShortcut() {
        document.addEventListener("keydown", (ev) => {
            if (ev.key !== "/" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
            const t = ev.target;
            if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
            const alvo = document.querySelector("[data-atalho-busca]");
            if (!alvo) return;
            ev.preventDefault();
            alvo.focus();
            alvo.select();
        });
    }

    // Pesquisa na internet sem recarregar a pagina (sem JS, o form abre /empresa/{cnpj}/internet).
    // Resultados montados com textContent: nada vindo do provedor vira HTML.
    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text) e.textContent = text;
        return e;
    }

    function renderWeb(box, data) {
        box.replaceChildren();
        if (data.erro) {
            const alerta = el("div", "alert alert-warning section");
            alerta.setAttribute("role", "alert");
            const wrap = el("div");
            wrap.appendChild(el("p", "", data.erro));
            alerta.appendChild(wrap);
            box.appendChild(alerta);
            return;
        }
        const itens = data.resultados || [];
        if (!itens.length) {
            box.appendChild(el("p", "empty-state", "Nenhum resultado para " + data.consulta + ". Tente sem aspas ou com o nome fantasia."));
            return;
        }
        const lista = el("ol", "web-results");
        itens.forEach((r) => {
            if (!/^https?:\/\//i.test(r.url)) return;
            const li = el("li");
            const a = el("a", "", r.titulo);
            a.href = r.url;
            a.target = "_blank";
            a.rel = "noopener noreferrer nofollow";
            li.append(a, el("span", "dominio", r.dominio));
            if (r.trecho) li.appendChild(el("p", "", r.trecho));
            lista.appendChild(li);
        });
        const extra = [data.do_cache ? "do cache" : "", data.atribuicao || ""].filter(Boolean).join(" · ");
        box.append(lista, el("p", "web-status", itens.length + " resultado(s) para " + data.consulta + (extra ? " · " + extra : "")));
    }

    function initWebSearch() {
        document.querySelectorAll("form[data-web-search]").forEach((form) => {
            const box = form.parentElement.querySelector("[data-web-results]");
            const btn = form.querySelector("button[type=submit]");
            if (!box || !btn) return;
            form.addEventListener("submit", async (ev) => {
                ev.preventDefault();
                const q = form.querySelector("input[name=q]").value.trim();
                const html = btn.innerHTML;
                btn.disabled = true;
                btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Pesquisando…';
                box.replaceChildren(el("p", "web-status", "Pesquisando na web…"));
                try {
                    const resp = await fetch(form.dataset.webSearch + "?q=" + encodeURIComponent(q), { headers: { Accept: "application/json" } });
                    if (resp.status === 429) throw new Error("limite de consultas por minuto atingido; aguarde um pouco");
                    if (!resp.ok) throw new Error("HTTP " + resp.status);
                    renderWeb(box, await resp.json());
                } catch (e) {
                    renderWeb(box, { erro: "Não foi possível pesquisar (" + e.message + ")." });
                } finally {
                    btn.disabled = false;
                    btn.innerHTML = html;
                }
            });
        });
    }

    // Lote: conta os CNPJs colados (estimativa por tamanho; a validacao real e no servidor)
    function initBatchCounter() {
        const input = document.querySelector("[data-batch-input]");
        const out = document.querySelector("[data-batch-counter]");
        if (!input || !out) return;
        const max = Number(input.dataset.max || 30);
        const atualizar = () => {
            const vistos = new Set();
            input.value.split(/[\s,;|]+/).forEach((t) => {
                const v = t.toUpperCase().replace(/[^A-Z0-9]/g, "");
                if (v.length === 14) vistos.add(v);
            });
            const n = vistos.size;
            out.textContent = "";
            if (!n) return;
            const forte = el("strong", "", String(n));
            out.append(forte, document.createTextNode(n === 1 ? " CNPJ detectado" : " CNPJs detectados"));
            if (n > max) out.appendChild(document.createTextNode(" · só os " + max + " primeiros serão consultados"));
        };
        input.addEventListener("input", atualizar);
        atualizar();
    }

    document.addEventListener("DOMContentLoaded", () => {
        initShortcut();
        initWebSearch();
        initBatchCounter();
        initShell();
        initMask();
        initLoading();
        initTabs();
        initCopy();
        initPrint();
        initFavorite();
        initDeleteActions();
    });
})();
