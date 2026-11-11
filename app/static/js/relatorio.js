(function () {
  var btn = document.getElementById("btn-imprimir") || document.querySelector("[data-print]");
  if (btn) {
    btn.addEventListener("click", function () {
      window.print();
    });
  }

  // Capa personalizada (escritorio, responsavel): fica guardada neste navegador
  // e volta sozinha no proximo relatorio. Destinatario e referencia mudam a cada caso.
  var form = document.querySelector("[data-personalizar]");
  if (!form) return;
  var CHAVE = "cnpj-consulta:relatorio-capa";
  var fixos = ["escritorio", "responsavel"];
  var guardado = {};
  try {
    guardado = JSON.parse(window.localStorage.getItem(CHAVE) || "{}") || {};
  } catch (e) {
    guardado = {};
  }
  form.addEventListener("submit", function () {
    var novo = {};
    fixos.forEach(function (nome) {
      novo[nome] = (form.elements[nome].value || "").trim();
    });
    try {
      window.localStorage.setItem(CHAVE, JSON.stringify(novo));
    } catch (e) {
      /* navegador sem armazenamento: so nao lembra */
    }
  });
  var params = new URLSearchParams(window.location.search);
  var semCapa = fixos.every(function (nome) {
    return !params.has(nome);
  });
  var temGuardado = fixos.some(function (nome) {
    return guardado[nome];
  });
  if (semCapa && temGuardado) {
    fixos.forEach(function (nome) {
      if (guardado[nome]) params.set(nome, guardado[nome]);
    });
    window.location.replace(window.location.pathname + "?" + params.toString());
  }
})();
