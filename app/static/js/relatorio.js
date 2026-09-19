(function () {
  var btn = document.getElementById("btn-imprimir") || document.querySelector("[data-print]");
  if (btn) {
    btn.addEventListener("click", function () {
      window.print();
    });
  }
})();
