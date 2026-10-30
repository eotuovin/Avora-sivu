/* Avora page runtime.
 *
 * Renders the design's <template id="tpl"> with the page's Component class
 * (the same .dc.html source the Claude Design canvas uses): {{ holes }},
 * <sc-for>, <sc-if> and on* handlers. Updates are applied by morphing the
 * existing DOM in place, so focus, open <details>, scroll-reveal state and
 * running animations survive a re-render.
 *
 * Also: the analytics hook (avora_* events → /api/ux → the existing
 * ux-events tracker) and the click delegation that names the page's CTAs.
 */
(function () {
  'use strict';

  var SVGNS = 'http://www.w3.org/2000/svg';
  // Typing in these fields only updates state; the inputs already show the
  // value, so re-rendering on every keystroke would only cost work.
  var SILENT = { name: 1, phone: 1, email: 1, company: 1, msg: 1 };
  var comp = null, root = null, tpl = null, pending = false;

  function DCLogic(props) { this.props = props || {}; this.state = {}; }
  DCLogic.prototype.setState = function (u) {
    var next = typeof u === 'function' ? u(this.state) : u;
    var changed = false, onlySilent = true;
    for (var k in next) {
      if (this.state[k] !== next[k]) { changed = true; if (!SILENT[k]) onlySilent = false; }
    }
    Object.assign(this.state, next);
    if (changed && !onlySilent) schedule();
  };
  DCLogic.prototype.forceUpdate = function () { schedule(); };
  window.DCLogic = DCLogic;

  function schedule() {
    if (pending) return;
    pending = true;
    Promise.resolve().then(function () { pending = false; render(); });
  }

  function get(sc, p) {
    p = p.trim();
    if (p === 'true') return true;
    if (p === 'false') return false;
    if (/^-?\d/.test(p)) return Number(p);
    return p.split('.').reduce(function (o, k) { return o == null ? undefined : o[k]; }, sc);
  }
  function interp(str, sc) {
    var m = str.match(/^\s*\{\{([^}]+)\}\}\s*$/);
    if (m) return get(sc, m[1]);
    return str.replace(/\{\{([^}]+)\}\}/g, function (_, p) { var v = get(sc, p); return v == null ? '' : v; });
  }

  function listen(el, ev) {
    el.addEventListener(ev, function (e) { var h = el.__h && el.__h[ev]; if (h) h(e); });
  }

  // Build fresh nodes for one template node (sc-for / sc-if may yield many).
  function build(node, sc, ns, out) {
    if (node.nodeType === 3) {
      var t = node.nodeValue;
      out.push(document.createTextNode(t.indexOf('{{') < 0 ? t : String(interp(t, sc) == null ? '' : interp(t, sc))));
      return;
    }
    if (node.nodeType !== 1) return;
    var tag = node.localName;
    if (tag === 'sc-for') {
      var list = interp(node.getAttribute('list'), sc) || [];
      var as = node.getAttribute('as');
      list.forEach(function (it, i) {
        var s2 = Object.assign({}, sc); s2[as] = it; s2.$index = i;
        node.childNodes.forEach(function (c) { build(c, s2, ns, out); });
      });
      return;
    }
    if (tag === 'sc-if') {
      if (interp(node.getAttribute('value'), sc)) node.childNodes.forEach(function (c) { build(c, sc, ns, out); });
      return;
    }
    var isSvg = ns || tag === 'svg';
    var el = isSvg ? document.createElementNS(SVGNS, tag) : document.createElement(tag);
    el.__h = {};
    for (var i = 0; i < node.attributes.length; i++) {
      var a = node.attributes[i], name = a.name, v = interp(a.value, sc);
      if (name.indexOf('on') === 0 && typeof v === 'function') {
        var ev = name.slice(2);
        if (ev === 'change' && (tag === 'textarea' || (tag === 'input' && node.getAttribute('type') !== 'checkbox'))) ev = 'input';
        el.__h[ev] = v; listen(el, ev); continue;
      }
      if (name === 'value') { el.__val = v == null ? '' : String(v); continue; }
      if (name === 'checked') { el.__chk = !!v; continue; }
      if (name.indexOf('hint-') === 0) continue;
      el.setAttribute(name, v == null ? '' : String(v));
    }
    var kids = [];
    node.childNodes.forEach(function (c) { build(c, sc, isSvg, kids); });
    kids.forEach(function (k) { el.appendChild(k); });
    if (el.__val !== undefined) el.value = el.__val;
    if (el.__chk !== undefined) el.checked = el.__chk;
    out.push(el);
  }

  function same(o, n) {
    if (o.nodeType !== n.nodeType) return false;
    if (o.nodeType === 3) return true;
    return o.nodeType === 1 && o.localName === n.localName && o.namespaceURI === n.namespaceURI;
  }

  function morphNode(o, n) {
    if (o.nodeType === 3) { if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue; return; }
    var isDetails = o.localName === 'details';
    for (var i = 0; i < n.attributes.length; i++) {
      var a = n.attributes[i], v = a.value;
      if (isDetails && a.name === 'open') continue;
      if (a.name === 'class' && o.classList && o.classList.contains('in') && /(^|\s)rv(\s|$)/.test(v) && !/(^|\s)in(\s|$)/.test(v)) v += ' in';
      if (o.getAttribute(a.name) !== v) o.setAttribute(a.name, v);
    }
    for (var j = o.attributes.length - 1; j >= 0; j--) {
      var an = o.attributes[j].name;
      if (isDetails && an === 'open') continue;
      if (!n.hasAttribute(an)) o.removeAttribute(an);
    }
    var nh = n.__h || {};
    o.__h = o.__h || {};
    for (var ev in nh) { if (!(ev in o.__h)) listen(o, ev); }
    o.__h = nh;
    if (n.__val !== undefined && o !== document.activeElement && o.value !== n.__val) o.value = n.__val;
    if (n.__chk !== undefined && o.checked !== n.__chk) o.checked = n.__chk;
    morphChildren(o, Array.prototype.slice.call(n.childNodes));
  }

  function morphChildren(parent, news) {
    var olds = Array.prototype.slice.call(parent.childNodes);
    for (var i = 0; i < news.length; i++) {
      var n = news[i], o = olds[i];
      if (!o) parent.appendChild(n);
      else if (same(o, n)) morphNode(o, n);
      else parent.replaceChild(n, o);
    }
    for (var k = news.length; k < olds.length; k++) parent.removeChild(olds[k]);
  }

  function render() {
    var sc = comp.renderVals();
    var news = [];
    tpl.content.childNodes.forEach(function (c) { build(c, sc, false, news); });
    morphChildren(root, news);
  }

  /* ---------------- analytics → existing ux-events tracker -------------- */
  var queue = [], sessionId = null;
  function uuid4() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = new Uint8Array(16); crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }
  function sid() {
    if (sessionId) return sessionId;
    try { sessionId = sessionStorage.getItem('avora_sid'); } catch (e) {}
    if (!sessionId) { sessionId = uuid4(); try { sessionStorage.setItem('avora_sid', sessionId); } catch (e) {} }
    return sessionId;
  }
  function track(type, props) {
    var ev = Object.assign({ type: type, ts: Date.now(), event_id: uuid4(), site: 'avora', path: location.pathname }, props || {});
    queue.push(ev);
    if (queue.length >= 20) flush();
  }
  function flush(beacon) {
    if (!queue.length) return;
    var body = JSON.stringify({ session_id: sid(), events: queue.splice(0, queue.length) });
    if (beacon && navigator.sendBeacon) { navigator.sendBeacon('/api/ux', new Blob([body], { type: 'application/json' })); return; }
    fetch('/api/ux', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true }).catch(function () {});
  }
  window.avoraTrack = track;
  setInterval(flush, 5000);
  addEventListener('pagehide', function () { flush(true); });
  addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(true); });

  function sectionOf(el) { var s = el.closest('section,header,footer'); return s ? (s.id || s.localName) : 'muu'; }
  document.addEventListener('click', function (e) {
    var t = e.target.closest('a,button,summary');
    if (!t) return;
    var txt = (t.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    var href = t.getAttribute('href') || '';
    if (href.indexOf('tel:') === 0) return track('avora_tel_click', { section: sectionOf(t) });
    var al = t.getAttribute('aria-label') || '';
    if (/^Pyydä esittely: /.test(al)) return track('avora_package_select', { package: al.replace(/^Pyydä esittely: /, ''), section: sectionOf(t) });
    if (t.closest('[aria-label="Esimerkkiala"]')) return track('avora_industry_select', { industry: txt });
    if (href === '#varaa') return track('avora_cta_click', { cta: txt, section: sectionOf(t) });
    if (href === '#paketit' || href === '#hinnasto') return track('avora_cta_click', { cta: txt, section: sectionOf(t) });
    if (t.localName === 'summary') {
      var d = t.parentElement; // the toggle has not happened yet: closed now = opening
      if (d && !d.open) track(/^Lisätiedot/.test(txt) ? 'avora_more_details_open' : 'avora_faq_open', { question: txt.replace(/\+$/, '').trim() });
    }
  }, true);

  /* ---------------- boot ---------------- */
  window.AvoraBoot = function (Component) {
    root = document.getElementById('root');
    tpl = document.getElementById('tpl');
    comp = new Component({});
    render();
    track('avora_page_view', { referrer_host: (function () { try { return document.referrer ? new URL(document.referrer).host : ''; } catch (e) { return ''; } })() });
    requestAnimationFrame(function () { if (comp.componentDidMount) comp.componentDidMount(); });
  };
})();
