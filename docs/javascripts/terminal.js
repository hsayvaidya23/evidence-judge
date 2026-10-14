/* Typing terminal: replays recorded CLI output line by line. Without JS, or with
   prefers-reduced-motion, the full output is shown as static text. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");

  function wait(ms, run) {
    return new Promise(function (resolve, reject) {
      window.setTimeout(function () {
        if (run.cancelled) reject(new Error("cancelled"));
        else resolve();
      }, ms);
    });
  }

  function setup(root) {
    if (root.getAttribute("data-ej-ready")) return;
    root.setAttribute("data-ej-ready", "1");

    var lines = Array.prototype.slice.call(root.querySelectorAll(".ej-line"));
    var replay = root.querySelector(".ej-terminal__replay");
    var caret = document.createElement("span");
    caret.className = "ej-caret";
    caret.setAttribute("aria-hidden", "true");
    var commands = lines.map(function (line) {
      var cmd = line.querySelector(".t-cmd");
      return cmd ? cmd.textContent : null;
    });
    var current = null;

    function showAll() {
      lines.forEach(function (line, i) {
        line.classList.remove("is-hidden");
        var cmd = line.querySelector(".t-cmd");
        if (cmd && commands[i] !== null) cmd.textContent = commands[i];
      });
      if (caret.parentNode) caret.parentNode.removeChild(caret);
    }

    function play() {
      if (current) current.cancelled = true;
      var run = { cancelled: false };
      current = run;
      lines.forEach(function (line) {
        line.classList.add("is-hidden");
      });
      root.setAttribute("aria-busy", "true");

      var chain = wait(250, run);
      lines.forEach(function (line, i) {
        chain = chain.then(function () {
          line.classList.remove("is-hidden");
          var cmd = line.querySelector(".t-cmd");
          if (!cmd || commands[i] === null) {
            line.appendChild(caret);
            return wait(parseInt(line.getAttribute("data-delay") || "70", 10), run);
          }
          var text = commands[i];
          cmd.textContent = "";
          cmd.parentNode.appendChild(caret);
          var typed = Promise.resolve();
          for (var c = 0; c < text.length; c++) {
            (function (n) {
              typed = typed.then(function () {
                cmd.textContent = text.slice(0, n + 1);
                return wait(text.charAt(n) === " " ? 30 : 14 + Math.random() * 22, run);
              });
            })(c);
          }
          return typed.then(function () {
            return wait(450, run);
          });
        });
      });
      chain
        .then(function () {
          root.removeAttribute("aria-busy");
          lines[lines.length - 1].appendChild(caret);
        })
        .catch(function () {});
    }

    if (replay) {
      replay.hidden = false;
      replay.addEventListener("click", function () {
        if (reduceMotion && reduceMotion.matches) showAll();
        else play();
      });
    }

    if ((reduceMotion && reduceMotion.matches) || !("IntersectionObserver" in window)) {
      showAll();
      return;
    }

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            observer.disconnect();
            play();
          }
        });
      },
      { threshold: 0.35 }
    );
    observer.observe(root);
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-ej-terminal]"), setup);
  }

  if (typeof window.document$ !== "undefined" && window.document$.subscribe) {
    window.document$.subscribe(init);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
