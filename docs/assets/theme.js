/* Aplica o tema antes da primeira pintura (sem "flash"): preferencia salva ou, se nao houver, a do sistema.
 * Carregado no <head>, sem defer. Sem scripts inline: a pagina roda com CSP `script-src 'self'`. */
(function () {
    "use strict";
    var tema = null;
    try {
        tema = localStorage.getItem("cnpj-site-theme");
    } catch (e) {
        /* armazenamento bloqueado: segue o sistema */
    }
    if (tema !== "light" && tema !== "dark") {
        tema = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }
    var html = document.documentElement;
    html.setAttribute("data-theme", tema);
    html.classList.add("js");
})();
