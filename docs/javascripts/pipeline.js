/* Animated pipeline diagram. ```pipeline fences hold Mermaid source; this script renders them
   with the Mermaid build Material for MkDocs uses, into the light DOM so the theme's CSS can
   animate edges and light the stages up in order. If Mermaid is unavailable it falls back to a
   plain list of stages; with prefers-reduced-motion the diagram is drawn without animation. */
(function () {
  "use strict";

  var MERMAID_URL = "https://unpkg.com/mermaid@11/dist/mermaid.min.js";
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
  var loading = null;
  var counter = 0;

  function loadMermaid() {
    if (window.mermaid && window.mermaid.render) return Promise.resolve(window.mermaid);
    if (loading) return loading;
    loading = new Promise(function (resolve, reject) {
      var waited = 0;
      var poll = window.setInterval(function () {
        waited += 100;
        if (window.mermaid && window.mermaid.render) {
          window.clearInterval(poll);
          resolve(window.mermaid);
        } else if (waited >= 1500) {
          window.clearInterval(poll);
          var script = document.createElement("script");
          script.src = MERMAID_URL;
          script.async = true;
          script.onload = function () {
            if (!window.mermaid) return reject(new Error("mermaid missing"));
            window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
            resolve(window.mermaid);
          };
          script.onerror = function () {
            reject(new Error("mermaid failed to load"));
          };
          document.head.appendChild(script);
        }
      }, 100);
    });
    loading.catch(function () {
      loading = null;
    });
    return loading;
  }

  function labels(source) {
    var seen = {};
    var out = [];
    var re = /([A-Za-z0-9_]+)\s*(?:\[\[|\[\(|\(\[|\[|\(\(|\(|\{)\s*"?([^\]\)\}"]+)"?/g;
    var m;
    while ((m = re.exec(source)) !== null) {
      if (!seen[m[1]]) {
        seen[m[1]] = true;
        out.push(m[2].replace(/<br\s*\/?>/g, " ").trim());
      }
    }
    return out;
  }

  function fallback(el, source) {
    var list = document.createElement("ol");
    list.className = "ej-pipeline__fallback";
    labels(source).forEach(function (text) {
      var item = document.createElement("li");
      item.textContent = text;
      list.appendChild(item);
    });
    el.textContent = "";
    el.appendChild(list);
    el.classList.add("is-fallback");
  }

  function animate(el) {
    var nodes = Array.prototype.slice.call(el.querySelectorAll("svg .node"));
    var placed = nodes.map(function (node) {
      var box = node.getBoundingClientRect();
      return { node: node, x: Math.round(box.left), y: Math.round(box.top) };
    });
    placed.sort(function (a, b) {
      return a.x - b.x || a.y - b.y;
    });
    var step = 0.55;
    el.style.setProperty("--ej-step", step + "s");
    el.style.setProperty("--ej-cycle", (placed.length * step + 3).toFixed(2) + "s");
    placed.forEach(function (p, i) {
      p.node.style.setProperty("--ej-i", String(i));
    });
    if (!(reduceMotion && reduceMotion.matches)) el.classList.add("is-animated");
  }

  function render(el) {
    if (el.getAttribute("data-ej-ready")) return;
    el.setAttribute("data-ej-ready", "1");
    var source = el.textContent;
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", "Pipeline: " + labels(source).join(", then "));
    loadMermaid()
      .then(function (mermaid) {
        counter += 1;
        return mermaid.render("ej-pipeline-svg-" + counter, source);
      })
      .then(function (result) {
        el.innerHTML = result.svg;
        el.classList.add("is-rendered");
        window.requestAnimationFrame(function () {
          animate(el);
        });
      })
      .catch(function () {
        fallback(el, source);
      });
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("div.ej-pipeline"), render);
  }

  if (typeof window.document$ !== "undefined" && window.document$.subscribe) {
    window.document$.subscribe(init);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
