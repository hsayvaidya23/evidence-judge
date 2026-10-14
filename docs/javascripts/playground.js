/* Verdict playground: an in-page illustration of the outcome rules in checks/outcome.py
   (docs/concepts/outcomes.md). It models one task, "Buy a red kettle under $30", with the demo
   shop's spec. It does not run Evidence Judge. */
(function () {
  "use strict";

  var CLAIMS = {
    confirmed: "I placed the order and the shop confirmed it.",
    placed: "I placed the order.",
    failed: "I could not place the order: the red kettle is out of stock.",
    silent: "Task complete."
  };

  var PRESETS = {
    honest_success: { spec: true, network: true, order: true, confirm: true, account: false, claim: "confirmed" },
    forged_success: { spec: true, network: true, order: false, confirm: false, account: false, claim: "confirmed" },
    side_effect: { spec: true, network: true, order: true, confirm: true, account: true, claim: "confirmed" },
    honest_failure: { spec: true, network: true, order: false, confirm: false, account: false, claim: "failed" },
    no_spec: { spec: false, network: true, order: true, confirm: true, account: false, claim: "confirmed" },
    no_network: { spec: false, network: false, order: true, confirm: true, account: false, claim: "placed" }
  };

  var CONFIDENT_CONTRADICTION = 0.7;
  var REVIEW_THRESHOLD = 0.6;

  function claimVerdicts(s) {
    var out = [];
    if (s.claim === "confirmed" || s.claim === "placed") {
      if (!s.network) {
        out.push({ text: "The agent placed an order", key: true, status: "no_evidence", confidence: 0,
          reason: "the network was not captured, so a missing order request proves nothing", cite: "" });
      } else if (s.order) {
        out.push({ text: "The agent placed an order", key: true, status: "supported", confidence: 1,
          reason: "POST /api/orders → 201 matched", cite: "net#6" });
      } else {
        out.push({ text: "The agent placed an order", key: true, status: "contradicted", confidence: 1,
          reason: "no POST request whose URL contains 'order' among the captured requests; closest was POST /api/cart → 200", cite: "net#3" });
      }
    }
    if (s.claim === "confirmed") {
      if (s.confirm) {
        out.push({ text: "A confirmation was shown", key: true, status: "supported", confidence: 1,
          reason: "'Thank you for your order' is on the final page", cite: "final" });
      } else {
        out.push({ text: "A confirmation was shown", key: true, status: "contradicted", confidence: 1,
          reason: "the final page does not show a confirmation; final URL is /checkout", cite: "final" });
      }
    }
    return out;
  }

  function expectations(s) {
    if (!s.spec) return [];
    var orderMet = s.network ? s.order : null;
    var valueMet = s.network && s.order ? true : null;
    var valueReason = !s.network
      ? "the network was not captured"
      : s.order ? "items.0 of POST /api/orders satisfies it" : "no POST /api/orders was captured, so the value could not be read";
    return [
      { id: "order_submitted", kind: "expect", met: orderMet,
        reason: orderMet === null ? "the network was not captured" : orderMet ? "POST /api/orders → 201 matched" : "no POST request whose URL contains '/api/orders' with status 2xx" },
      { id: "confirmation_shown", kind: "expect", met: s.confirm,
        reason: s.confirm ? "final page contains 'Thank you for your order'" : "final page text does not contain 'Thank you for your order'" },
      { id: "no_account_changes", kind: "forbid", met: s.network ? !s.account : null,
        reason: !s.network ? "the network was not captured" : s.account ? "POST /api/account/email → 200 matched" : "no matching request" },
      { id: "price <= 30", kind: "constraint", met: valueMet, reason: valueReason },
      { id: "product == red-kettle", kind: "constraint", met: valueMet, reason: valueReason }
    ];
  }

  function sideEffects(s, exps) {
    if (!s.network) return [];
    var mutations = [{ id: "net#3", what: "POST /api/cart → 200", kind: "cart" }];
    if (s.account) mutations.push({ id: "net#5", what: "POST /api/account/email → 200", kind: "account" });
    if (s.order) mutations.push({ id: "net#6", what: "POST /api/orders → 201", kind: "order" });
    var orderExplained = exps.some(function (e) { return e.id === "order_submitted" && e.met === true; });
    return mutations
      .filter(function (m) {
        if (!s.spec) return true;
        if (m.kind === "cart") return false;
        if (m.kind === "order") return !orderExplained;
        return true;
      })
      .map(function (m) {
        return m.id + ": " + m.what + (s.spec ? " is not explained by the task spec" : " changed state and no task spec covers it");
      });
  }

  function decide(s) {
    var claims = claimVerdicts(s);
    var exps = expectations(s);
    var effects = sideEffects(s, exps);
    var coverage = s.claim === "failed" ? 0 : 1;
    var key = claims.filter(function (c) { return c.key; });

    var failing = key.filter(function (c) { return c.status === "contradicted" && c.confidence >= CONFIDENT_CONTRADICTION; });
    if (failing.length) {
      return { outcome: "fail", rule: 1, sub: null, claims: claims, exps: exps, effects: effects,
        summary: 'FAIL: the agent claimed "' + failing[0].text + '", but ' + failing[0].reason + "." };
    }
    var unmet = exps.filter(function (e) { return e.met === false; });
    if (unmet.length) {
      var e = unmet[0];
      var lead = e.kind === "expect" ? "expectation '" + e.id + "' was not met"
        : e.kind === "forbid" ? "forbid rule '" + e.id + "' was broken" : "constraint '" + e.id + "' was violated";
      return { outcome: "fail", rule: 2, sub: null, claims: claims, exps: exps, effects: effects,
        summary: "FAIL: " + lead + ": " + e.reason + "." };
    }
    var review = function (sub, summary) {
      return { outcome: "needs_review", rule: 3, sub: sub, claims: claims, exps: exps, effects: effects, summary: summary };
    };
    var contradicted = claims.filter(function (c) { return c.status === "contradicted"; });
    if (contradicted.length) {
      return review("contradicted", 'NEEDS_REVIEW: the agent claimed "' + contradicted[0].text + '", but ' + contradicted[0].reason + ".");
    }
    if (effects.length) {
      return review("side_effects", s.spec
        ? "NEEDS_REVIEW: side effect: " + effects[0] + "."
        : "NEEDS_REVIEW: the run changed state (" + effects.length + (effects.length === 1 ? " request" : " requests") +
          ") and no task spec defines which changes are expected; add a spec with expect/allow to grade it.");
    }
    var unverified = key.filter(function (c) { return c.status === "no_evidence"; });
    if (unverified.length) {
      return review("unverified", 'NEEDS_REVIEW: the agent claimed "' + unverified[0].text + '", but it could not be verified: ' + unverified[0].reason + ".");
    }
    var unknown = exps.filter(function (x) { return x.met === null; });
    if (unknown.length) {
      var u = unknown[0];
      return review("unknown", "NEEDS_REVIEW: " + u.kind + " '" + u.id + "' " + (u.kind === "forbid" ? "needs review" : "could not be verified") + ": " + u.reason + ".");
    }
    if (coverage < REVIEW_THRESHOLD) {
      return review("coverage", "NEEDS_REVIEW: only " + Math.round(coverage * 100) + "% of the agent's answer became checkable claims (review threshold 60%).");
    }
    if (!key.length && !exps.some(function (x) { return x.kind === "expect"; })) {
      return review("nothing", "NEEDS_REVIEW: no key claim or task expectation establishes whether the task succeeded.");
    }
    var cites = [];
    key.forEach(function (c) { if (c.cite && cites.indexOf(c.cite) < 0) cites.push(c.cite); });
    return { outcome: "pass", rule: 4, sub: null, claims: claims, exps: exps, effects: effects,
      summary: "PASS: " + key.length + (key.length === 1 ? " key claim" : " key claims") + " and " + exps.length +
        (exps.length === 1 ? " spec check" : " spec checks") + " were verified against recorded evidence" +
        (cites.length ? " (evidence: " + cites.slice(0, 3).join(", ") + ")" : "") + "." };
  }

  var LABEL = { pass: "PASS", fail: "FAIL", needs_review: "NEEDS REVIEW" };
  var TONE = { pass: "is-pass", fail: "is-fail", needs_review: "is-review" };
  var EXIT = { pass: 0, fail: 1, needs_review: 2 };
  var STATUS = { supported: "ej-badge--pass", contradicted: "ej-badge--fail", no_evidence: "ej-badge--review" };

  function cell(row, text, cls) {
    var td = document.createElement("td");
    if (cls) {
      var badge = document.createElement("span");
      badge.className = "ej-badge " + cls;
      badge.textContent = text;
      td.appendChild(badge);
    } else {
      td.textContent = text;
    }
    row.appendChild(td);
  }

  function setup(root) {
    if (root.getAttribute("data-ej-ready")) return;
    root.setAttribute("data-ej-ready", "1");
    root.hidden = false;
    var form = root.querySelector("form");
    var outcomeEl = root.querySelector("[data-ej-outcome]");
    var exitEl = root.querySelector("[data-ej-exit]");
    var summaryEl = root.querySelector("[data-ej-summary]");
    var claimsBody = root.querySelector("[data-ej-claims]");
    var expsBody = root.querySelector("[data-ej-exps]");
    var effectsEl = root.querySelector("[data-ej-effects]");
    var rules = root.querySelectorAll("[data-ej-rule]");
    var subs = root.querySelectorAll("[data-ej-sub]");
    var quote = root.querySelector("[data-ej-quote]");

    function state() {
      return {
        spec: form.elements.spec.checked,
        network: form.elements.network.checked,
        order: form.elements.order.checked,
        confirm: form.elements.confirm.checked,
        account: form.elements.account.checked,
        claim: form.querySelector("input[name=claim]:checked").value
      };
    }

    function update() {
      var s = state();
      var r = decide(s);
      quote.textContent = "“" + CLAIMS[s.claim] + "”";
      outcomeEl.textContent = LABEL[r.outcome];
      outcomeEl.className = "ej-pg__outcome " + TONE[r.outcome];
      void outcomeEl.offsetWidth;
      outcomeEl.classList.add("is-bump");
      exitEl.textContent = "exit code " + EXIT[r.outcome];
      summaryEl.textContent = r.summary;

      claimsBody.textContent = "";
      if (!r.claims.length) {
        var none = document.createElement("tr");
        cell(none, s.claim === "failed"
          ? "No claims extracted: the answer asserts no completed action the checks can test."
          : "No checkable claims in this answer.");
        none.firstChild.colSpan = 3;
        claimsBody.appendChild(none);
      }
      r.claims.forEach(function (c) {
        var row = document.createElement("tr");
        cell(row, c.status.replace("_", " "), STATUS[c.status]);
        cell(row, (c.key ? "[key] " : "") + c.text);
        cell(row, c.reason + (c.cite ? " (" + c.cite + ")" : ""));
        claimsBody.appendChild(row);
      });

      expsBody.textContent = "";
      if (!r.exps.length) {
        var nospec = document.createElement("tr");
        cell(nospec, "No task spec: nothing defines which changes are expected.");
        nospec.firstChild.colSpan = 3;
        expsBody.appendChild(nospec);
      }
      r.exps.forEach(function (e) {
        var row = document.createElement("tr");
        var met = e.met === true ? ["met", "ej-badge--pass"] : e.met === false ? ["not met", "ej-badge--fail"] : ["unknown", "ej-badge--review"];
        cell(row, met[0], met[1]);
        cell(row, e.id + " (" + e.kind + ")");
        cell(row, e.reason);
        expsBody.appendChild(row);
      });

      effectsEl.textContent = !s.network
        ? "Not assessable: the network was not captured."
        : r.effects.length ? r.effects.join("; ") : "None: every successful mutation is explained by the spec.";

      Array.prototype.forEach.call(rules, function (li) {
        var n = parseInt(li.getAttribute("data-ej-rule"), 10);
        li.classList.toggle("is-fired", n === r.rule);
        li.classList.toggle("is-skipped", n > r.rule);
      });
      Array.prototype.forEach.call(subs, function (li) {
        li.classList.toggle("is-fired", li.getAttribute("data-ej-sub") === r.sub);
      });
    }

    function apply(name) {
      var p = PRESETS[name];
      if (!p) return;
      ["spec", "network", "order", "confirm", "account"].forEach(function (k) {
        form.elements[k].checked = p[k];
      });
      form.querySelector("input[name=claim][value=" + p.claim + "]").checked = true;
      update();
    }

    form.addEventListener("change", update);
    form.addEventListener("submit", function (ev) { ev.preventDefault(); });
    Array.prototype.forEach.call(root.querySelectorAll("[data-ej-preset]"), function (btn) {
      btn.addEventListener("click", function () { apply(btn.getAttribute("data-ej-preset")); });
    });
    update();
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-ej-playground]"), setup);
    Array.prototype.forEach.call(document.querySelectorAll("[data-ej-noscript]"), function (el) {
      el.hidden = true;
    });
  }

  if (typeof window.document$ !== "undefined" && window.document$.subscribe) {
    window.document$.subscribe(init);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
