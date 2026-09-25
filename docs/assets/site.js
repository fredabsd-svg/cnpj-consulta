/* CNPJ Consulta - landing page.
 * Melhorias progressivas: a pagina inteira le e navega sem este arquivo.
 * Tudo roda no navegador; nenhuma requisicao e feita. Sem scripts inline (CSP `script-src 'self'`). */
"use strict";

/*<validator>*/
// Mesmo algoritmo de app/core/cnpj_validator.py: cada caractere vale ord(c) - 48
// (digitos 0-9 valem 0-9; letras A-Z valem 17-42), modulo 11 com os pesos tradicionais.
const PESOS_DV1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2];
const PESOS_DV2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2];

function limpar(valor) {
    // remove tudo que nao e A-Z/0-9 ANTES de por em maiuscula (mesma ordem do Python)
    return String(valor == null ? "" : valor).replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

function calcularDv(chars, pesos) {
    let total = 0;
    for (let i = 0; i < pesos.length; i++) total += (chars.charCodeAt(i) - 48) * pesos[i];
    const resto = total % 11;
    return resto < 2 ? 0 : 11 - resto;
}

function mascarar(valor) {
    const v = limpar(valor).slice(0, 14);
    const partes = [v.slice(0, 2), v.slice(2, 5), v.slice(5, 8), v.slice(8, 12), v.slice(12, 14)];
    let saida = partes[0];
    if (partes[1]) saida += "." + partes[1];
    if (partes[2]) saida += "." + partes[2];
    if (partes[3]) saida += "/" + partes[3];
    if (partes[4]) saida += "-" + partes[4];
    return saida;
}

function analisar(valor) {
    const s = limpar(valor);
    if (s.length === 0) return { estado: "idle", s };
    if (s.length < 14) return { estado: "typing", s };
    if (s.length > 14) return { estado: "bad", motivo: "tamanho", s };
    if (!/^[A-Z0-9]{12}[0-9]{2}$/.test(s)) return { estado: "bad", motivo: "formato", s };
    if (new Set(s).size === 1) return { estado: "bad", motivo: "repetido", s };
    const base = s.slice(0, 12);
    const dv1 = calcularDv(base, PESOS_DV1);
    const dv2 = calcularDv(base + String(dv1), PESOS_DV2);
    const esperado = String(dv1) + String(dv2);
    if (s.slice(12) !== esperado) return { estado: "bad", motivo: "dv", s, esperado };
    return { estado: "ok", s, esperado, alfanumerico: !/^[0-9]+$/.test(s) };
}
/*</validator>*/

(function () {
    const $ = (sel, raiz = document) => raiz.querySelector(sel);
    const $$ = (sel, raiz = document) => Array.from(raiz.querySelectorAll(sel));
    const semMovimento = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // ------------------------------------------------------------------ tema
    function initTema() {
        const html = document.documentElement;
        const botoes = $$("[data-theme-toggle]");
        const rotular = () => {
            const claro = html.dataset.theme === "light";
            botoes.forEach((b) => b.setAttribute("aria-label", claro ? "Mudar para o tema escuro" : "Mudar para o tema claro"));
        };
        rotular();
        botoes.forEach((b) =>
            b.addEventListener("click", () => {
                html.dataset.theme = html.dataset.theme === "light" ? "dark" : "light";
                try {
                    localStorage.setItem("cnpj-site-theme", html.dataset.theme);
                } catch (e) {
                    /* sem armazenamento: vale ate recarregar */
                }
                rotular();
            }),
        );
    }

    // ------------------------------------------------------------ cabecalho
    function initCabecalho() {
        const header = $(".site-header");
        if (header) {
            const aoRolar = () => header.classList.toggle("scrolled", window.scrollY > 8);
            window.addEventListener("scroll", aoRolar, { passive: true });
            aoRolar();
        }
        const menu = $(".menu");
        if (menu) {
            const fechar = () => menu.removeAttribute("open");
            $$(".menu-panel a", menu).forEach((a) => a.addEventListener("click", fechar));
            document.addEventListener("keydown", (ev) => {
                if (ev.key === "Escape" && menu.hasAttribute("open")) {
                    fechar();
                    $("summary", menu).focus();
                }
            });
            document.addEventListener("click", (ev) => {
                if (menu.hasAttribute("open") && !menu.contains(ev.target)) fechar();
            });
        }
        // link da secao visivel no menu
        const links = $$('.nav a[href^="#"]');
        if (links.length && "IntersectionObserver" in window) {
            const porId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
            const io = new IntersectionObserver(
                (entradas) => {
                    entradas.forEach((e) => {
                        if (!e.isIntersecting) return;
                        links.forEach((a) => a.removeAttribute("aria-current"));
                        const alvo = porId.get(e.target.id);
                        if (alvo) alvo.setAttribute("aria-current", "true");
                    });
                },
                { rootMargin: "-40% 0px -55% 0px" },
            );
            porId.forEach((_, id) => {
                const sec = document.getElementById(id);
                if (sec) io.observe(sec);
            });
        }
    }

    // ------------------------------------------------- abas (padrao WAI-ARIA)
    function initAbas() {
        $$("[data-tabs]").forEach((raiz) => {
            const abas = $$('[role="tab"]', raiz);
            const paineis = abas.map((a) => document.getElementById(a.getAttribute("aria-controls")));
            if (!abas.length || paineis.some((p) => !p)) return;
            const selecionar = (i, foco) => {
                abas.forEach((a, k) => {
                    const ativa = k === i;
                    a.setAttribute("aria-selected", String(ativa));
                    a.tabIndex = ativa ? 0 : -1;
                    paineis[k].hidden = !ativa;
                });
                if (foco) abas[i].focus();
                // controle segmentado rolavel (celular): centraliza a aba ativa sem rolar a pagina
                const seg = abas[i].parentElement;
                if (seg && seg.scrollWidth > seg.clientWidth) {
                    seg.scrollLeft = abas[i].offsetLeft - (seg.clientWidth - abas[i].offsetWidth) / 2;
                }
            };
            abas.forEach((aba, i) => {
                aba.addEventListener("click", () => selecionar(i, false));
                // lista vertical (aria-orientation="vertical"): setas para cima/baixo; horizontal: esquerda/direita
                const vertical = aba.closest('[role="tablist"]')?.getAttribute("aria-orientation") === "vertical";
                const teclas = vertical
                    ? { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: abas.length - 1 }
                    : { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: abas.length - 1 };
                aba.addEventListener("keydown", (ev) => {
                    const destino = teclas[ev.key];
                    if (destino === undefined) return;
                    ev.preventDefault();
                    selecionar((destino + abas.length) % abas.length, true);
                });
            });
            let inicial = abas.findIndex((a) => a.getAttribute("aria-selected") === "true");
            const abrirEm = (id) => {
                const idx = abas.findIndex((a) => a.id === id);
                if (idx >= 0) inicial = idx;
            };
            // no tema claro, a demonstracao abre na captura do tema claro
            if (raiz.dataset.lightTab && document.documentElement.dataset.theme === "light") abrirEm(raiz.dataset.lightTab);
            // em celular, a demonstracao abre na captura do celular (a de desktop fica minuscula)
            if (raiz.dataset.mobileTab && window.matchMedia("(max-width: 640px)").matches) abrirEm(raiz.dataset.mobileTab);
            selecionar(inicial >= 0 ? inicial : 0, false);
        });
    }

    // ------------------------------------------------------------- copiar
    async function copiarTexto(texto) {
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(texto);
                return;
            } catch (e) {
                /* permissao negada (ex.: dentro de iframe): tenta o metodo classico abaixo */
            }
        }
        await new Promise((resolve, reject) => {
            const area = document.createElement("textarea");
            area.value = texto;
            area.setAttribute("readonly", "");
            area.className = "visually-hidden";
            document.body.appendChild(area);
            area.select();
            area.setSelectionRange(0, texto.length); // iOS
            try {
                document.execCommand("copy") ? resolve() : reject(new Error("copy"));
            } catch (e) {
                reject(e);
            } finally {
                area.remove();
            }
        });
    }

    function initCopiar() {
        const aviso = $("#aviso-copia");
        $$("[data-copy]").forEach((btn) => {
            const rotulo = $(".copy-label", btn);
            btn.addEventListener("click", async () => {
                const alvo = document.getElementById(btn.dataset.copy);
                if (!alvo) return;
                const clone = alvo.cloneNode(true);
                // prompts ("$ ") e linhas de saida nao entram no que vai para a area de transferencia
                $$(".ps, .out", clone).forEach((n) => n.remove());
                const texto = clone.textContent.replace(/^\s*\n/, "").replace(/\s+$/, "") + "\n";
                try {
                    await copiarTexto(texto);
                    btn.dataset.copied = "true";
                    if (rotulo) rotulo.textContent = "Copiado!";
                    if (aviso) aviso.textContent = "Comando copiado para a área de transferência.";
                } catch (e) {
                    if (rotulo) rotulo.textContent = "Selecione e copie";
                }
                setTimeout(() => {
                    delete btn.dataset.copied;
                    if (rotulo) rotulo.textContent = "Copiar";
                    if (aviso) aviso.textContent = "";
                }, 2200);
            });
        });
    }

    // ------------------------------------------------------ validador (demo)
    function initValidador() {
        const input = $("#cnpj-input");
        const cartao = $("#cartao");
        if (!input || !cartao) return;
        const numero = $("#cartao-numero");
        const selo = $("#cartao-selo");
        const formato = $("#cartao-formato");
        const dvEl = $("#cartao-dv");
        const resultado = $("#resultado");
        const titulo = $("#resultado-titulo");
        const detalhe = $("#resultado-detalhe");
        const MODELO = "NN.NNN.NNN/NNNN-NN";

        const desenharNumero = (s) => {
            const frag = document.createDocumentFragment();
            let k = 0;
            for (const ch of MODELO) {
                const span = document.createElement("span");
                if (ch === "N") {
                    if (k < s.length) span.textContent = s[k++];
                    else {
                        span.textContent = "0";
                        span.className = "ph";
                    }
                } else {
                    span.textContent = ch;
                    span.className = "sep";
                }
                frag.appendChild(span);
            }
            numero.replaceChildren(frag);
        };

        const MOTIVOS = {
            tamanho: ["Comprimento inválido", "O CNPJ tem exatamente 14 caracteres (12 da raiz e da ordem + 2 dígitos verificadores)."],
            formato: ["Formato inválido", "Os dois últimos caracteres (dígitos verificadores) precisam ser números; letras só valem nas 12 primeiras posições."],
            repetido: ["CNPJ inválido", "Sequências de um único caractere repetido (como 00.000.000/0000-00) são rejeitadas."],
        };

        const atualizar = () => {
            const r = analisar(input.value);
            const s = r.s.slice(0, 14);
            desenharNumero(s);
            cartao.dataset.estado = r.estado === "ok" ? "ok" : r.estado === "bad" ? "bad" : r.estado;
            resultado.dataset.estado = r.estado;
            input.setAttribute("aria-invalid", r.estado === "bad" ? "true" : "false");

            selo.textContent = { idle: "Aguardando", typing: "Digitando…", ok: "Válido", bad: "Inválido" }[r.estado];
            formato.textContent = s.length === 0 ? "—" : /^[0-9]+$/.test(s) ? "Numérico" : "Alfanumérico";
            dvEl.textContent = r.estado === "ok" ? r.esperado + " ✓" : r.motivo === "dv" ? "esperado " + r.esperado : "—";

            if (r.estado === "idle") {
                titulo.textContent = "Digite ou escolha um exemplo";
                detalhe.textContent = "Aceita com ou sem pontuação, numérico ou alfanumérico.";
            } else if (r.estado === "typing") {
                titulo.textContent = "Continue digitando…";
                detalhe.textContent = s.length + " de 14 caracteres.";
            } else if (r.estado === "ok") {
                titulo.textContent = "Dígitos verificadores conferem";
                detalhe.textContent =
                    "Formato " + (r.alfanumerico ? "alfanumérico (o novo)" : "numérico") + ", DV " + r.esperado +
                    ". Isso prova que o número é bem formado, não que a empresa existe: para isso o app consulta as fontes.";
            } else if (r.motivo === "dv") {
                titulo.textContent = "Dígitos verificadores não conferem";
                detalhe.textContent = "Esperado " + r.esperado + ", informado " + r.s.slice(12) + ". Confira se digitou algum caractere errado.";
            } else {
                titulo.textContent = MOTIVOS[r.motivo][0];
                detalhe.textContent = MOTIVOS[r.motivo][1];
            }
        };

        input.addEventListener("input", () => {
            const noFim = input.selectionStart === input.value.length;
            input.value = mascarar(input.value);
            if (noFim) input.setSelectionRange(input.value.length, input.value.length);
            atualizar();
        });
        $$("[data-fill]").forEach((b) =>
            b.addEventListener("click", () => {
                input.value = mascarar(b.dataset.fill);
                atualizar();
                input.focus();
            }),
        );
        input.value = mascarar(input.value);
        atualizar();
    }

    // ------------------------------------------------- revelar ao rolar
    function initRevelar() {
        const itens = $$(".reveal");
        if (!itens.length) return;
        if (semMovimento || !("IntersectionObserver" in window)) {
            itens.forEach((el) => el.classList.add("in"));
            return;
        }
        const io = new IntersectionObserver(
            (entradas) =>
                entradas.forEach((e) => {
                    if (!e.isIntersecting) return;
                    e.target.classList.add("in");
                    io.unobserve(e.target);
                }),
            { rootMargin: "0px 0px -8% 0px", threshold: 0.06 },
        );
        itens.forEach((el) => io.observe(el));
    }

    document.addEventListener("DOMContentLoaded", () => {
        initTema();
        initCabecalho();
        initAbas();
        initCopiar();
        initValidador();
        initRevelar();
    });
})();
