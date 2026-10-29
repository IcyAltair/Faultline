/*
 * Faultline Anomaly Panel (Grafana panel plugin, no build step).
 *
 * Written as an AMD module - the same format Grafana's webpack toolchain emits -
 * so Grafana's SystemJS loader picks it up directly from the plugin folder.
 * 'react', '@grafana/data' and '@grafana/ui' are provided by Grafana at runtime.
 *
 * Input: Prometheus queries, matched by refId (configurable in panel options):
 *   A  max(nginx_anomaly_score)              Isolation Forest score per window
 *   B  max(nginx_anomaly_threshold)          decision threshold of the model
 *   C  max(nginx_anomaly_alert)              k-of-n debounced alert (0/1)
 *   D  max(stand_ground_truth_anomaly_active) injected scenarios (0/1), optional
 *   E  nginx_anomaly_feature_zscore          per-feature robust z-score (instant), optional
 *   F  nginx_anomaly_model_info              model metadata labels (instant), optional
 * Output: status header, score timeline with alert / ground-truth bands,
 *         event-level precision / recall / delay for the visible range,
 *         "why" side bar with top deviating features. Click a band to zoom in.
 */
define(['react', '@grafana/data', '@grafana/ui'], function (React, gdata, gui) {
  'use strict';
  var h = React.createElement;

  // ---------------------------------------------------------------- data helpers
  /** Grafana <10 uses Vector objects, >=10 plain arrays: normalise both. */
  function arr(v) {
    if (!v) { return []; }
    if (Array.isArray(v)) { return v; }
    if (typeof v.toArray === 'function') { return v.toArray(); }
    return Array.from(v);
  }

  /** Data frames -> flat list of numeric series {refId, labels, t[], v[]}. */
  function seriesOf(frames) {
    var out = [];
    (frames || []).forEach(function (f) {
      var tf = null;
      f.fields.forEach(function (x) { if (x.type === 'time' && !tf) { tf = x; } });
      if (!tf) { return; }
      var t = arr(tf.values);
      f.fields.forEach(function (x) {
        if (x.type !== 'number') { return; }
        out.push({ refId: f.refId, labels: x.labels || {}, t: t, v: arr(x.values) });
      });
    });
    return out;
  }

  function byRef(all, ref) { return all.filter(function (s) { return s.refId === ref; }); }
  function first(list) { return list.length ? list[0] : null; }
  function ok(x) { return x !== null && x !== undefined && isFinite(x); }

  /** Last finite point of a series. */
  function last(s) {
    if (!s) { return null; }
    for (var i = s.v.length - 1; i >= 0; i--) { if (ok(s.v[i])) { return { t: s.t[i], v: s.v[i] }; } }
    return null;
  }

  /** Median spacing of timestamps = query step (ms). */
  function medianStep(t) {
    if (t.length < 2) { return 15000; }
    var d = [];
    for (var i = 1; i < t.length; i++) { d.push(t[i] - t[i - 1]); }
    d.sort(function (a, b) { return a - b; });
    return d[Math.floor(d.length / 2)] || 15000;
  }

  /** 0/1 series -> [{start, end}] intervals (ms). Gaps > 2.5 steps close an interval. */
  function intervals(s) {
    if (!s || !s.t.length) { return []; }
    var step = medianStep(s.t), res = [], cur = null;
    for (var i = 0; i < s.t.length; i++) {
      var t = s.t[i], on = ok(s.v[i]) && s.v[i] >= 0.5;
      if (cur && t - cur.lastT > 2.5 * step) { cur.end = cur.lastT + step; res.push(cur); cur = null; }
      if (on) { if (!cur) { cur = { start: t, lastT: t }; } else { cur.lastT = t; } }
      else if (cur) { cur.end = t; res.push(cur); cur = null; }
    }
    if (cur) { cur.end = cur.lastT + step; res.push(cur); }
    return res;
  }

  /**
   * Event-level quality on the visible range (what an on-call engineer cares about):
   *   precision = alerts overlapping a ground-truth window / all alerts
   *   recall    = ground-truth windows hit by >=1 alert / all GT windows
   *   delay     = mean(first alert start - GT start) over detected windows
   * tolMs extends GT windows, because the detector needs k windows to confirm.
   */
  function eventStats(alerts, gts, tolMs) {
    function hit(a, g) { return a.start <= g.end + tolMs && a.end >= g.start; }
    var tp = alerts.filter(function (a) { return gts.some(function (g) { return hit(a, g); }); }).length;
    var delays = [];
    gts.forEach(function (g) {
      var m = alerts.filter(function (a) { return hit(a, g); });
      if (m.length) { delays.push(Math.max(0, m[0].start - g.start)); }
    });
    return {
      alerts: alerts.length, gts: gts.length, tp: tp, detected: delays.length,
      precision: alerts.length ? tp / alerts.length : null,
      recall: gts.length ? delays.length / gts.length : null,
      delayS: delays.length ? delays.reduce(function (a, b) { return a + b; }, 0) / delays.length / 1000 : null
    };
  }

  /** Latest z-score per feature, sorted by |z| desc. */
  function topFeatures(zs, n) {
    var rows = [];
    zs.forEach(function (s) {
      var p = last(s);
      if (p) { rows.push({ feature: s.labels.feature || '?', z: p.v }); }
    });
    rows.sort(function (a, b) { return Math.abs(b.z) - Math.abs(a.z); });
    return rows.slice(0, n);
  }

  /** Nearest index in sorted array (binary search). */
  function nearest(t, x) {
    var lo = 0, hi = t.length - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (t[mid] < x) { lo = mid; } else { hi = mid; } }
    return Math.abs(t[lo] - x) <= Math.abs(t[hi] - x) ? lo : hi;
  }

  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function hms(ms) { var d = new Date(ms); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function pct(x) { return x === null ? '–' : Math.round(x * 100) + '%'; }
  function num(x, d) { return ok(x) ? Number(x).toFixed(d) : '–'; }

  // ---------------------------------------------------------------- chart (pure SVG)
  var PAD = { l: 44, r: 10, t: 8, b: 22 };

  /** Score timeline: GT bands (bottom strip), alert bands, score line, threshold line, hover. */
  function Chart(p) {
    var hoverState = React.useState(null), hover = hoverState[0], setHover = hoverState[1];
    var w = Math.max(p.width, 60), hh = Math.max(p.height, 60);
    var iw = w - PAD.l - PAD.r, ih = hh - PAD.t - PAD.b;
    var from = p.from, to = p.to, span = Math.max(to - from, 1);
    var sc = p.score, th = p.threshold, c = p.colors;

    // y range: [0, max(score, threshold) * 1.1]; IF scores are ~0.3..0.8
    var ymax = 0.0;
    if (sc) { sc.v.forEach(function (v) { if (ok(v) && v > ymax) { ymax = v; } }); }
    if (th) { th.v.forEach(function (v) { if (ok(v) && v > ymax) { ymax = v; } }); }
    ymax = ymax > 0 ? ymax * 1.1 : 1;
    function X(t) { return PAD.l + (t - from) / span * iw; }
    function Y(v) { return PAD.t + ih - v / ymax * ih; }
    function clampX(t) { return Math.min(Math.max(X(t), PAD.l), PAD.l + iw); }

    function path(s) {
      if (!s) { return ''; }
      var d = '', pen = false, step = medianStep(s.t);
      for (var i = 0; i < s.t.length; i++) {
        if (!ok(s.v[i]) || (i > 0 && s.t[i] - s.t[i - 1] > 2.5 * step)) { pen = false; if (!ok(s.v[i])) { continue; } }
        d += (pen ? 'L' : 'M') + X(s.t[i]).toFixed(1) + ',' + Y(s.v[i]).toFixed(1);
        pen = true;
      }
      return d;
    }

    function band(iv, key, y, height, fill, title) {
      var x1 = clampX(iv.start), x2 = clampX(iv.end);
      return h('rect', {
        key: key, x: x1, y: y, width: Math.max(x2 - x1, 2), height: height, fill: fill,
        style: { cursor: 'zoom-in' },
        onClick: function () { p.onZoom(iv.start - 60000, iv.end + 60000); }   // +-1 min context
      }, h('title', null, title + ' ' + hms(iv.start) + ' – ' + hms(iv.end) +
        ' (' + Math.round((iv.end - iv.start) / 1000) + ' s), click to zoom'));
    }

    var kids = [];
    // horizontal grid + y labels
    [0, 0.25, 0.5, 0.75, 1].forEach(function (f, i) {
      var v = ymax * f, y = Y(v);
      kids.push(h('line', { key: 'g' + i, x1: PAD.l, x2: PAD.l + iw, y1: y, y2: y, stroke: c.grid, strokeWidth: 1 }));
      kids.push(h('text', { key: 'gl' + i, x: PAD.l - 4, y: y + 3, fontSize: 10, textAnchor: 'end', fill: c.text }, v.toFixed(2)));
    });
    // x labels (5 ticks)
    for (var k = 0; k <= 4; k++) {
      var tt = from + span * k / 4;
      kids.push(h('text', { key: 'x' + k, x: X(tt), y: hh - 6, fontSize: 10, fill: c.text,
        textAnchor: k === 0 ? 'start' : (k === 4 ? 'end' : 'middle') }, hms(tt)));
    }
    // alert bands (full height) and ground-truth strip (bottom 6px)
    p.alerts.forEach(function (iv, i) { kids.push(band(iv, 'a' + i, PAD.t, ih, c.alertBand, 'ML alert')); });
    p.gts.forEach(function (iv, i) { kids.push(band(iv, 'gt' + i, PAD.t + ih - 6, 6, c.gt, 'Ground truth')); });
    // threshold (step line) and score
    if (th) { kids.push(h('path', { key: 'th', d: path(th), fill: 'none', stroke: c.threshold, strokeWidth: 1.5, strokeDasharray: '5 4' })); }
    if (sc) { kids.push(h('path', { key: 'sc', d: path(sc), fill: 'none', stroke: c.score, strokeWidth: 1.6 })); }
    // anomalous points (score >= threshold) as red dots
    if (sc && th) {
      sc.t.forEach(function (t, i) {
        var thv = th.v[nearest(th.t, t)];
        if (ok(sc.v[i]) && ok(thv) && sc.v[i] >= thv) {
          kids.push(h('circle', { key: 'p' + i, cx: X(t), cy: Y(sc.v[i]), r: 2.2, fill: c.alert }));
        }
      });
    }
    // hover crosshair + tooltip
    if (hover && sc && sc.t.length) {
      var idx = nearest(sc.t, hover), ht = sc.t[idx], hv = sc.v[idx];
      var thv2 = th ? th.v[nearest(th.t, ht)] : null, hx = X(ht);
      kids.push(h('line', { key: 'hx', x1: hx, x2: hx, y1: PAD.t, y2: PAD.t + ih, stroke: c.text, strokeOpacity: 0.5 }));
      var label = hms(ht) + '  score ' + num(hv, 3) + (ok(thv2) ? '  thr ' + num(thv2, 3) : '');
      var tx = hx > PAD.l + iw - 190 ? hx - 6 : hx + 6;
      kids.push(h('text', { key: 'ht', x: tx, y: PAD.t + 12, fontSize: 11, fill: c.strong,
        textAnchor: hx > PAD.l + iw - 190 ? 'end' : 'start' }, label));
    }
    return h('svg', {
      width: w, height: hh, style: { display: 'block' },
      onMouseMove: function (e) {
        var r = e.currentTarget.getBoundingClientRect(), x = e.clientX - r.left;
        setHover(x < PAD.l || x > PAD.l + iw ? null : from + (x - PAD.l) / iw * span);
      },
      onMouseLeave: function () { setHover(null); }
    }, kids);
  }

  // ---------------------------------------------------------------- side widgets
  function Stat(label, value, color, hint) {
    return h('div', { key: label, title: hint || '', style: { minWidth: 70 } },
      h('div', { style: { fontSize: 10, opacity: 0.7, textTransform: 'uppercase' } }, label),
      h('div', { style: { fontSize: 16, fontWeight: 600, color: color } }, value));
  }

  /** Horizontal bars of |z| for the top deviating features ("why is it anomalous"). */
  function WhyBars(rows, c) {
    var maxz = 3;
    rows.forEach(function (r) { if (Math.abs(r.z) > maxz) { maxz = Math.abs(r.z); } });
    return rows.map(function (r) {
      var wpct = Math.min(Math.abs(r.z) / maxz * 100, 100);
      var col = Math.abs(r.z) >= 3 ? c.alert : (Math.abs(r.z) >= 2 ? c.warn : c.score);
      return h('div', { key: r.feature, style: { marginBottom: 4 } },
        h('div', { style: { display: 'flex', justifyContent: 'space-between', fontSize: 11 } },
          h('span', null, r.feature), h('span', { style: { color: col } }, (r.z >= 0 ? '+' : '') + num(r.z, 1) + 'σ')),
        h('div', { style: { height: 5, background: c.grid, borderRadius: 2 } },
          h('div', { style: { width: wpct + '%', height: 5, background: col, borderRadius: 2 } })));
    });
  }

  // ---------------------------------------------------------------- main panel
  function AnomalyPanel(props) {
    var o = props.options, theme = gui.useTheme2();
    var color = function (n) { return theme.visualization.getColorByName(n); };
    var c = {
      score: color(o.scoreColor), alert: color(o.alertColor), gt: color(o.gtColor), warn: color('orange'),
      threshold: color(o.alertColor), alertBand: color(o.alertColor), grid: theme.colors.border.weak,
      text: theme.colors.text.secondary, strong: theme.colors.text.primary, ok: color('green')
    };
    var all = seriesOf(props.data.series);
    var score = first(byRef(all, o.refScore)), thr = first(byRef(all, o.refThreshold));
    var alerts = intervals(first(byRef(all, o.refAlert)));
    var gts = intervals(first(byRef(all, o.refGroundTruth)));
    var info = first(byRef(all, o.refModelInfo));
    var from = props.timeRange.from.valueOf(), to = props.timeRange.to.valueOf();

    if (!score) {
      return h('div', { style: { padding: 12, color: c.text } },
        'No data for query ' + o.refScore + ' (max(nginx_anomaly_score)). ',
        'Is ml-detector running and scraped, and is a model trained? See anomaly-stand/ml/README.md');
    }

    var ls = last(score), lt = last(thr), la = last(first(byRef(all, o.refAlert)));
    var alertOn = la && la.v >= 0.5;
    var st = eventStats(alerts, gts, o.gtToleranceS * 1000);
    var why = topFeatures(byRef(all, o.refZScore), o.topN);
    var model = info ? (info.labels.version || '?') : null;

    var headH = 46, statsH = o.showStats ? 38 : 0, sideW = o.showWhy && why.length ? Math.min(240, props.width * 0.3) : 0;
    var chartW = props.width - sideW - (sideW ? 8 : 0), chartH = props.height - headH - statsH;

    var header = h('div', { style: { display: 'flex', gap: 16, alignItems: 'center', height: headH } },
      h('div', { style: {
        padding: '4px 10px', borderRadius: 4, fontWeight: 700, color: '#fff',
        background: alertOn ? c.alert : c.ok
      } }, alertOn ? 'ANOMALY' : 'NORMAL'),
      Stat('score', ls ? num(ls.v, 3) : '–', ls && lt && ls.v >= lt.v ? c.alert : c.strong, 'Isolation Forest score, last window'),
      Stat('threshold', lt ? num(lt.v, 3) : '–', c.text, 'decision threshold of the loaded model'),
      Stat('last window', ls ? hms(ls.t) : '–', c.text),
      model ? Stat('model', model, c.text, 'nginx_anomaly_model_info{version}') : null);

    var stats = o.showStats ? h('div', { style: { display: 'flex', gap: 16, height: statsH, alignItems: 'center',
        borderTop: '1px solid ' + c.grid, fontSize: 12 } },
      Stat('alerts', '' + st.alerts, c.alert),
      Stat('injected', '' + st.gts, c.gt, 'ground-truth windows in range'),
      Stat('precision', pct(st.precision), c.strong, 'alerts overlapping ground truth / all alerts'),
      Stat('recall', pct(st.recall), c.strong, 'ground-truth windows with >=1 alert / all'),
      Stat('mean delay', st.delayS === null ? '–' : Math.round(st.delayS) + ' s', c.strong, 'first alert start - GT start'),
      h('span', { style: { marginLeft: 'auto', color: c.text, fontSize: 11 } },
        h('span', { style: { color: c.alert } }, '■ '), 'alert  ', h('span', { style: { color: c.gt } }, '■ '),
        'ground truth  (click a band to zoom)')) : null;

    var side = sideW ? h('div', { style: { width: sideW, overflow: 'auto', paddingLeft: 8, borderLeft: '1px solid ' + c.grid } },
      h('div', { style: { fontSize: 11, opacity: 0.7, marginBottom: 6 } }, 'WHY: top feature deviations (robust z)'),
      WhyBars(why, c)) : null;

    return h('div', { style: { width: props.width, height: props.height, display: 'flex', flexDirection: 'column' } },
      header,
      h('div', { style: { display: 'flex', flex: 1, minHeight: 0 } },
        h(Chart, { width: chartW, height: chartH, from: from, to: to, score: score, threshold: thr,
          alerts: alerts, gts: gts, colors: c,
          onZoom: function (a, b) { props.onChangeTimeRange({ from: a, to: b }); } }),
        side),
      stats);
  }

  // ---------------------------------------------------------------- plugin + options editor
  var plugin = new gdata.PanelPlugin(AnomalyPanel).setPanelOptions(function (b) {
    var q = 'Queries (refId)';
    return b
      .addTextInput({ path: 'refScore', name: 'Score', defaultValue: 'A', category: [q] })
      .addTextInput({ path: 'refThreshold', name: 'Threshold', defaultValue: 'B', category: [q] })
      .addTextInput({ path: 'refAlert', name: 'Alert (0/1)', defaultValue: 'C', category: [q] })
      .addTextInput({ path: 'refGroundTruth', name: 'Ground truth (0/1)', defaultValue: 'D', category: [q] })
      .addTextInput({ path: 'refZScore', name: 'Feature z-scores', defaultValue: 'E', category: [q] })
      .addTextInput({ path: 'refModelInfo', name: 'Model info', defaultValue: 'F', category: [q] })
      .addBooleanSwitch({ path: 'showStats', name: 'Show event-level quality', defaultValue: true, category: ['Display'] })
      .addBooleanSwitch({ path: 'showWhy', name: 'Show top features', defaultValue: true, category: ['Display'] })
      .addNumberInput({ path: 'topN', name: 'Top N features', defaultValue: 5, category: ['Display'] })
      .addNumberInput({ path: 'gtToleranceS', name: 'Detection tolerance after GT end, s', defaultValue: 45,
        description: 'k-of-n debounce delays alerts; default = ALERT_N * WINDOW_S', category: ['Display'] })
      .addColorPicker({ path: 'scoreColor', name: 'Score', defaultValue: 'blue', category: ['Colors'] })
      .addColorPicker({ path: 'alertColor', name: 'Alert / threshold', defaultValue: 'red', category: ['Colors'] })
      .addColorPicker({ path: 'gtColor', name: 'Ground truth', defaultValue: 'purple', category: ['Colors'] });
  });

  return { plugin: plugin };
});
