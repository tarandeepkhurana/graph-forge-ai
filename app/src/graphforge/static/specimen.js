/* Draws the specimen: a page of a document, and the map made from it.
 *
 * The wires are measured, not drawn to fixed coordinates. Each leader starts
 * level with its highlighted sentence and ends at the idea it became, so the
 * figure stays true at any width and whatever the page text wraps to -- which
 * matters, because "tied to the sentence it came from" is the claim the whole
 * figure exists to make.
 *
 * It plays once, when it first comes into view: a highlighter marks each
 * phrase, a line carries it to the map, then the two relationships draw in,
 * the "read first" arrow last. After that it only answers the pointer.
 */
(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var ORDER = ["batch", "online", "incremental"];
  // Room between two ideas for the relationship drawn between them.
  var MIN_GAP = 74;

  var reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  Array.prototype.forEach.call(document.querySelectorAll("[data-specimen]"), function (fig, index) {
    try {
      setup(fig, "sp" + index);
    } catch (err) {
      // A figure that failed to draw must not stay hidden behind its own
      // entrance animation.
      fig.classList.remove("is-armed");
    }
  });

  function setup(fig, prefix) {
    var svg = fig.querySelector(".wires");
    var map = fig.querySelector(".map");
    var sheet = fig.querySelector(".sheet");
    var timers = [];
    var started = false;
    var finished = reduceMotion;
    var parts = {};

    if (!reduceMotion) fig.classList.add("is-armed");

    function narrow() {
      return window.matchMedia("(max-width: 760px)").matches;
    }

    function box(el) {
      var r = el.getBoundingClientRect();
      var f = fig.getBoundingClientRect();
      return { x: r.left - f.left, y: r.top - f.top, w: r.width, h: r.height };
    }

    function nodeFor(key) { return map.querySelector('.node[data-node="' + key + '"]'); }
    function markFor(key) { return fig.querySelector('.hl[data-for="' + key + '"]'); }

    // The sign-in page hides its side panel on narrow screens, and a hidden
    // figure has nothing to measure.
    function hidden() {
      return fig.getClientRects().length === 0;
    }

    /* Line each idea up with its sentence, keeping room between them. */
    function place() {
      if (hidden()) return;
      var nodes = ORDER.map(nodeFor);
      if (narrow()) {
        map.classList.remove("is-placed");
        map.style.height = "";
        nodes.forEach(function (n) { n.style.top = ""; });
        return;
      }
      map.classList.add("is-placed");
      var mapTop = box(map).y;
      var floor = 0;
      var last = 0;
      nodes.forEach(function (node, i) {
        var line = markFor(ORDER[i]).getClientRects()[0];
        var f = fig.getBoundingClientRect();
        var centre = line.top - f.top + line.height / 2 - mapTop;
        var top = Math.max(centre - node.offsetHeight / 2, floor);
        node.style.top = Math.round(top) + "px";
        floor = top + node.offsetHeight + MIN_GAP;
        last = top + node.offsetHeight;
      });
      map.style.height = Math.ceil(last) + "px";
    }

    function el(name, attrs) {
      var node = document.createElementNS(SVG, name);
      Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
      return node;
    }

    function colour(name, fallback) {
      var v = getComputedStyle(fig).getPropertyValue(name).trim();
      return v || fallback;
    }

    function dotOf(key) {
      var d = box(nodeFor(key).querySelector(".dot"));
      return { x: d.x + d.w / 2, y: d.y + d.h / 2, r: d.w / 2, left: d.x };
    }

    /* Rebuild every wire from where things are now. */
    function draw() {
      if (hidden()) return;
      var f = fig.getBoundingClientRect();
      svg.setAttribute("viewBox", "0 0 " + f.width + " " + f.height);
      while (svg.firstChild) svg.removeChild(svg.firstChild);

      var firm = colour("--firm", "#3987e5");
      var inked = colour("--inked", "#d95926");

      var defs = el("defs", {});
      var arrow = el("marker", {
        id: prefix + "-arrow", viewBox: "0 0 12 12", refX: "10.5", refY: "6",
        markerWidth: "12", markerHeight: "12", markerUnits: "userSpaceOnUse", orient: "auto",
      });
      arrow.appendChild(el("path", { d: "M1,1 L11,6 L1,11 Z", fill: firm }));
      // The barred head the app uses for "read first": the arrow stops at a wall.
      var barred = el("marker", {
        id: prefix + "-barred", viewBox: "0 0 20 18", refX: "18.5", refY: "9",
        markerWidth: "20", markerHeight: "18", markerUnits: "userSpaceOnUse", orient: "auto",
      });
      barred.appendChild(el("path", { d: "M1,2 L13,9 L1,16 Z", fill: inked }));
      barred.appendChild(el("rect", { x: "15.5", y: "0.5", width: "3", height: "17", fill: inked }));
      defs.appendChild(arrow);
      defs.appendChild(barred);
      svg.appendChild(defs);

      parts = { leaders: {}, pins: {} };

      // Leaders: from the page's margin, level with the sentence, to the idea.
      if (!narrow()) {
        var sheetBox = box(sheet);
        ORDER.forEach(function (key) {
          var line = markFor(key).getClientRects()[0];
          var y1 = line.top - f.top + line.height / 2;
          var x1 = sheetBox.x + sheetBox.w - 15;
          var dot = dotOf(key);
          var x2 = dot.left - 10;
          var y2 = dot.y;
          var dx = x2 - x1;
          var path = el("path", {
            class: "wire leader", "data-key": key, pathLength: "1",
            d: "M" + x1 + "," + y1 + " C" + (x1 + dx * 0.55) + "," + y1 + " " +
               (x2 - dx * 0.45) + "," + y2 + " " + x2 + "," + y2,
          });
          var pin = el("circle", { class: "pin", "data-key": key, cx: x1, cy: y1, r: "3.5" });
          svg.appendChild(path);
          svg.appendChild(pin);
          parts.leaders[key] = path;
          parts.pins[key] = pin;
        });
      }

      // The two relationships, as the app draws them.
      var b = dotOf("batch");
      var o = dotOf("online");
      var i = dotOf("incremental");

      parts.link = wire(
        "link", o.x, b.y + b.r + 5, o.y - o.r - 8, "#" + prefix + "-arrow", "contrasts with"
      );
      parts.read = wire(
        "read", o.x, i.y - i.r - 5, o.y + o.r + 6, "#" + prefix + "-barred", "read first"
      );

      if (finished) showAll(false);
    }

    function wire(kind, x, yFrom, yTo, marker, text) {
      var path = el("path", {
        class: "wire " + kind, pathLength: "1", d: "M" + x + "," + yFrom + " L" + x + "," + yTo,
      });
      var label = el("text", {
        class: "wire-label " + kind, x: x + 18, y: (yFrom + yTo) / 2 + 4.5,
      });
      label.textContent = text;
      svg.appendChild(path);
      svg.appendChild(label);
      return { path: path, label: label, marker: "url(" + marker + ")" };
    }

    function on(node) { if (node) node.classList.add("on"); }

    function drawWire(w) {
      on(w.path);
      later(460, function () {
        w.path.setAttribute("marker-end", w.marker);
        on(w.label);
      });
    }

    function later(ms, fn) { timers.push(setTimeout(fn, ms)); }

    /* Put the figure in its finished state, with or without transitions. */
    function showAll(animate) {
      fig.classList.toggle("is-still", !animate);
      ORDER.forEach(function (key) {
        on(markFor(key));
        on(nodeFor(key));
        on(parts.leaders[key]);
        on(parts.pins[key]);
      });
      [parts.link, parts.read].forEach(function (w) {
        on(w.path);
        on(w.label);
        w.path.setAttribute("marker-end", w.marker);
      });
    }

    function play() {
      if (started) return;
      // Coming into view can beat the debounced resize redraw, so make sure
      // there is something to animate.
      if (!parts.link) { place(); draw(); }
      if (!parts.link) return;
      started = true;
      fig.classList.remove("is-still");

      ORDER.forEach(function (key, n) {
        var t = 300 + n * 760;
        later(t, function () { on(markFor(key)); });
        later(t + 340, function () { on(parts.leaders[key]); on(parts.pins[key]); });
        later(t + 700, function () { on(nodeFor(key)); });
      });
      var edges = 300 + ORDER.length * 760 + 260;
      later(edges, function () { drawWire(parts.link); });
      later(edges + 700, function () { drawWire(parts.read); });
      later(edges + 1400, function () {
        finished = true;
        timers = [];
      });
    }

    /* A resize mid-drawing jumps to the end rather than redrawing half a
       sequence against new coordinates. */
    var pending = null;
    window.addEventListener("resize", function () {
      clearTimeout(pending);
      pending = setTimeout(function () {
        if (started && !finished) {
          timers.forEach(clearTimeout);
          timers = [];
          finished = true;
        }
        place();
        draw();
      }, 120);
    });

    // Pointing at an idea finds its sentence, and a sentence finds its idea.
    function focus(key) {
      if (!finished) return;
      fig.classList.add("is-focused");
      fig.querySelectorAll(".lit").forEach(function (n) { n.classList.remove("lit"); });
      [nodeFor(key), markFor(key), parts.leaders[key], parts.pins[key]].forEach(function (n) {
        if (n) n.classList.add("lit");
      });
    }
    function unfocus() {
      fig.classList.remove("is-focused");
      fig.querySelectorAll(".lit").forEach(function (n) { n.classList.remove("lit"); });
    }
    ORDER.forEach(function (key) {
      [nodeFor(key), markFor(key)].forEach(function (target) {
        target.addEventListener("mouseenter", function () { focus(key); });
        target.addEventListener("mouseleave", unfocus);
      });
      nodeFor(key).addEventListener("focus", function () { focus(key); });
      nodeFor(key).addEventListener("blur", unfocus);
    });

    function inView() {
      var r = fig.getBoundingClientRect();
      return r.width > 0 && r.top < window.innerHeight && r.bottom > 0;
    }

    /* Start from whichever arrives first: the fonts, or a short timeout.
     *
     * Waiting on `document.fonts.ready` alone left the figure hidden for good
     * whenever that promise was slow or never settled -- the entrance hides
     * everything until it plays, so a start that never comes is a blank map.
     * Measuring with a fallback font and re-measuring once the real one lands
     * is better than showing nothing.
     */
    var kicked = false;
    function kickoff() {
      if (kicked) return;
      kicked = true;
      place();
      draw();
      if (finished) return;

      if ("IntersectionObserver" in window) {
        var watcher = new IntersectionObserver(function (entries) {
          if (entries.some(function (e) { return e.isIntersecting; })) {
            watcher.disconnect();
            play();
          }
        }, { threshold: 0.35 });
        watcher.observe(fig);
      }
      // Same reasoning for the observer: if it has not fired but the figure is
      // plainly on screen, play anyway.
      setTimeout(function () { if (!started && inView()) play(); }, 1500);
    }

    var fontsReady = document.fonts && document.fonts.ready ? document.fonts.ready : null;
    if (fontsReady) {
      fontsReady.then(function () {
        if (!kicked) {
          kickoff();
          return;
        }
        // The real serif arrived after we measured: lines may have rewrapped.
        if (started && !finished) {
          timers.forEach(clearTimeout);
          timers = [];
          finished = true;
        }
        place();
        draw();
      }, kickoff);
    }
    setTimeout(kickoff, 1200);
  }
})();
