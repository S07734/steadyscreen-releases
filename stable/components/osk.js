/* SteadyScreen on-screen keyboard.
 *
 * Injected into the configured page after it loads, for touch displays with no
 * physical keyboard. Touching a text field brings it up; it types into that
 * field the way a real keyboard would, so the page's own JavaScript sees
 * ordinary input and change events and cannot tell the difference.
 *
 * Deliberately self-contained and defensive: it runs inside somebody else's
 * page, and breaking their page is far worse than having no keyboard.
 */
(function () {
  if (window.__dsOsk) { return; }
  window.__dsOsk = 1;

  var COLS = 23;          /* 20 half-key columns, plus 3 for the right column */
  var MAIN = 20;

  /* Column starts are explicit rather than centred. A keyboard's stagger is
   * not symmetry: Z belongs half a key left of where centring puts it, so it
   * sits between A and S rather than under S. */
  var LAYERS = {
    abc: [{ k: '1234567890', c: 1 },
          { k: 'qwertyuiop', c: 1 },
          { k: 'asdfghjkl',  c: 2 },
          { k: 'zxcvbnm,.',  c: 3 }],
    sym: [{ k: '1234567890', c: 1 },
          { k: '!@#$%^&*()', c: 1 },
          { k: '-_=+[]{}:;', c: 1 },
          { k: ',./?\\|~',   c: 4 }]
  };

  var TEXTY = /^(text|search|tel|url|email|password|number|)$/i;
  function isField(el) {
    if (!el || !el.tagName) { return false; }
    var t = el.tagName.toLowerCase();
    if (t === 'textarea') { return true; }
    if (el.isContentEditable) { return true; }
    if (t !== 'input') { return false; }
    if (el.readOnly || el.disabled) { return false; }
    return TEXTY.test(el.getAttribute('type') || 'text');
  }

  /* Set by the client from config: a horizontal and vertical nudge in
   * pixels, for a screen with a dead patch under part of the keyboard. Found
   * on a real panel, where the same physical spot swallowed presses whichever
   * key happened to be sitting on it. */
  var OFF = window.__dsOskOffset || { x: 0, y: 0 };
  OFF = { x: parseInt(OFF.x, 10) || 0, y: parseInt(OFF.y, 10) || 0 };

  /* Key size, as a multiplier on the standard 56px row. Set from config in
   * fixed steps rather than continuously: every step has to be usable on real
   * glass, and a preset can be checked on a panel where a slider's thousand
   * values cannot. Clamped here too, because config is only as good as what
   * last wrote it. */
  function clampScale(v) {
    var n = parseFloat(v);
    if (!(n > 0)) { n = 1; }
    if (n < 0.8) { n = 0.8; }
    if (n > 2.2) { n = 2.2; }
    return n;
  }
  var SCALE = clampScale(window.__dsOskScale);
  var ENABLED = true;
  /* Round to whole pixels. Half-pixel rows make the gaps between keys look
   * uneven on a cheap panel, which reads as a rendering fault. */
  function px(base) { return Math.round(base * SCALE); }

  /* The viewport ceiling has to grow with the setting, or it erases it.
   * A fixed 9vh cap meant that on a 1024x600 panel all four key sizes came
   * out byte-identical, and on 1366x768 three of the four did -- the admin
   * page offered four labels that were one setting. It looked correct only
   * on the 1920x1280 Pipo it was checked on, which is the tallest screen in
   * the fleet and the one place every step had room to differ.
   *
   * 9vh at standard, so small panels keep exactly the keys they have today,
   * rising to 11vh at huge. Five rows plus gaps and padding is then at worst
   * about two thirds of a short screen at the largest setting -- which is
   * what asking for the largest setting means. */
  function ceilVh(base) { return (base * (9 + (SCALE - 1) * 2.5) / 56).toFixed(2); }

  var DOWN = window.PointerEvent ? 'pointerdown'
           : ('ontouchstart' in window ? 'touchstart' : 'mousedown');

  var target = null, wrap = null, keysBox = null;
  var layer = 'abc';
  var caps = false;      /* caps lock */
  var oneShot = false;   /* shift for the next character only */
  var lastShift = 0;     /* for detecting a double tap on shift */

  function upper() { return caps || oneShot; }

  var styleEl = null;

  function cssText() {
    /* The '' + is load-bearing. `return` alone on its line would be given
       a semicolon by the parser and this function would return undefined --
       a keyboard with no styling at all, which is exactly what it did. */
    return '' +
      /* Every size is `min(<scaled px>, <vh>)`. The vh half is the ceiling:
         the ceiling rises with the key size, from 9vh a row at standard to
         11vh at huge, so the whole panel is at worst about two thirds of a
         short screen at the largest setting and under half on a tall one, so
         a large setting on a small panel shrinks to fit instead of burying the
         page. Doing it in CSS rather than JS means rotating the display or
         changing the resolution re-fits it with no code involved. */
      '#ds-osk{position:fixed;left:0;right:0;bottom:0;z-index:2147483647;' +
      'background:#161a21;border-top:2px solid #2b313c;' +
      'padding:' + (px(10) + Math.max(0, OFF.y)) + 'px ' + px(10) + 'px ' + px(14) + 'px;' +
      'display:none;font:500 min(' + px(20) + 'px,' + ceilVh(20) + 'vh)/1 system-ui,-apple-system,Segoe UI,sans-serif;' +
      '-webkit-user-select:none;user-select:none;box-shadow:0 -8px 24px rgba(0,0,0,.5)}' +
      '#ds-osk.on{display:block}' +
      /* The key area can be nudged away from a dead patch on the digitiser.
         Only the KEYS move -- the panel stays full width, so a press that
         lands beside them is still inert and does not dismiss the keyboard. */
      '#ds-osk .keys{transform:translate(' + OFF.x + 'px,' + (-OFF.y) + 'px);' +
      'display:grid;grid-template-columns:repeat(' + COLS + ',1fr);' +
      'grid-auto-rows:minmax(min(' + px(56) + 'px,' + ceilVh(56) + 'vh),auto);' +
      'gap:min(' + px(7) + 'px,1vh);' +
      /* The width cap grows with the keys. Left at a fixed 1180px a larger
         setting made taller keys inside the same narrow block, which is not
         what "bigger keyboard" means to anyone looking at it. 96vw keeps it
         off the edges on a narrow screen. */
      'max-width:min(' + px(1180) + 'px,96vw);margin:0 auto}' +
      '#ds-osk button{border-radius:' + px(8) + 'px;border:1px solid #39434f;' +
      'border-bottom-width:3px;background:#242c37;color:#eef1f5;font:inherit;' +
      'padding:0;touch-action:manipulation}' +
      '#ds-osk button:active{background:#313c4a;border-bottom-width:1px;' +
      'transform:translateY(2px)}' +
      '#ds-osk .go{background:#2f6fd0;border-color:#4f8ae8}' +
      '#ds-osk .dim{background:#1b212a;color:#9aa6b4}' +
      '#ds-osk .tall{font-size:min(' + px(18) + 'px,' + ceilVh(18) + 'vh)}' +
      /* The bottom row's touch area runs to the bottom of the screen. The
         keys stay where they are; only the target grows. Pressing just below
         a key is a very easy thing to do on a wall-mounted panel, and it used
         to land on the page behind and dismiss the keyboard. */
      '#ds-osk .btm{position:relative}' +
      '#ds-osk .btm::after{content:"";position:absolute;left:0;right:0;' +
      'top:100%;height:' + px(60) + 'px}' +
      '';
  }

  function css() {
    styleEl = document.createElement('style');
    styleEl.textContent = cssText();
    document.head.appendChild(styleEl);
  }

  /* Re-style a keyboard that is already on the page. Settings used to be read
   * once, when the script was injected, so changing the key size did nothing
   * until the browser restarted -- a setting that appears to do nothing reads
   * as broken. The client calls this the moment one is saved. */
  function applyCfg(c) {
    if (!c) { return; }
    if (c.scale !== undefined) { SCALE = clampScale(c.scale); }
    if (c.offset) {
      OFF = { x: parseInt(c.offset.x, 10) || 0,
              y: parseInt(c.offset.y, 10) || 0 };
    }
    if (c.caps) {
      window.__dsOskCaps = c.caps;
      applyCapsMode();
      if (keysBox) { relabel(); }
    }
    if (c.enabled !== undefined) {
      ENABLED = !!c.enabled;
      if (!ENABLED) { hide(); }
    }
    /* Only the stylesheet carries the size, so rewriting it is the whole
     * update -- the keys keep their grid positions and their handlers. */
    if (styleEl) { styleEl.textContent = cssText(); }
  }
  window.__dsOskConfig = applyCfg;

  function key(label, cls, col, span, row, rowspan, fn) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    /* Never focusable. A button that can take focus pulls it off the text
     * field on a real tap, which fires focusout and used to hide the whole
     * keyboard -- most visibly when holding backspace. */
    b.tabIndex = -1;
    if (cls) { b.className = cls; }
    b.style.gridColumn = col + ' / span ' + span;
    if (row) { b.style.gridRow = row + ' / span ' + (rowspan || 1); }
    /* Exactly one event actually presses the key, chosen once for this
     * browser; the others are swallowed so they cannot move focus. Binding
     * three and deciding inside the handler meant a press could act once,
     * twice, or not at all depending on which events the engine delivered --
     * which is what made the symbols key feel unreliable under a finger. */
    b.addEventListener(DOWN, function (e) {
      e.preventDefault();
      e.stopPropagation();
      fn(b);
    }, { passive: false });
    ['pointerdown', 'touchstart', 'mousedown', 'click'].forEach(function (ev) {
      if (ev === DOWN) { return; }
      b.addEventListener(ev, function (e) { e.preventDefault(); },
                         { passive: false });
    });
    return b;
  }

  function refocus() {
    if (target && document.activeElement !== target) {
      try { target.focus({ preventScroll: true }); } catch (err) {
        try { target.focus(); } catch (err2) {}
      }
    }
  }

  function insert(ch) {
    if (!target) { return; }
    refocus();
    try {
      if (target.isContentEditable) {
        document.execCommand('insertText', false, ch);
        return;
      }
      var s = target.selectionStart, e = target.selectionEnd;
      if (typeof s === 'number' && typeof e === 'number') {
        var v = target.value;
        target.value = v.slice(0, s) + ch + v.slice(e);
        target.selectionStart = target.selectionEnd = s + ch.length;
      } else {
        target.value += ch;
      }
      fire('input');
    } catch (err) { /* never break the page */ }
  }

  function backspace() {
    if (!target) { return; }
    refocus();
    try {
      if (target.isContentEditable) {
        document.execCommand('delete', false, null);
        return;
      }
      var s = target.selectionStart, e = target.selectionEnd, v = target.value;
      if (typeof s === 'number' && s !== e) {
        target.value = v.slice(0, s) + v.slice(e);
        target.selectionStart = target.selectionEnd = s;
      } else if (typeof s === 'number' && s > 0) {
        target.value = v.slice(0, s - 1) + v.slice(s);
        target.selectionStart = target.selectionEnd = s - 1;
      } else if (typeof s !== 'number') {
        target.value = v.slice(0, -1);
      }
      fire('input');
    } catch (err) { /* ignore */ }
  }

  function fire(type) {
    try {
      target.dispatchEvent(new Event(type, { bubbles: true }));
    } catch (err) {
      try {
        var ev = document.createEvent('Event');
        ev.initEvent(type, true, true);
        target.dispatchEvent(ev);
      } catch (err2) {}
    }
  }

  function enter() {
    if (!target) { return; }
    refocus();
    fire('change');
    var opts = { bubbles: true, cancelable: true, key: 'Enter', code: 'Enter',
                 keyCode: 13, which: 13 };
    var prevented = false;
    try { prevented = !target.dispatchEvent(new KeyboardEvent('keydown', opts)); }
    catch (err) {}
    try { target.dispatchEvent(new KeyboardEvent('keypress', opts)); } catch (err) {}
    try { target.dispatchEvent(new KeyboardEvent('keyup', opts)); } catch (err) {}
    /* Only submit if the page did not handle Enter itself, or a scanner page
     * that listens for Enter would submit twice. */
    var f = target.form;
    if (!prevented && f) {
      try {
        if (typeof f.requestSubmit === 'function') { f.requestSubmit(); }
        else { f.submit(); }
      } catch (err) {}
    }
    hide();
  }

  /* Relabel the letter keys without rebuilding them. Rebuilding the DOM from
   * inside a key's own event handler destroys the element mid-dispatch, which
   * is a good way to make a key work only sometimes. */
  function relabel() {
    if (!keysBox) { return; }
    var bs = keysBox.querySelectorAll('button[data-ch]');
    for (var i = 0; i < bs.length; i++) {
      var c = bs[i].getAttribute('data-ch');
      bs[i].textContent = upper() ? c.toUpperCase() : c;
    }
    var sk = keysBox.querySelector('button[data-shift]');
    if (sk) {
      sk.textContent = caps ? '\u21ea' : '\u21e7';
      sk.className = 'btm ' + (caps ? 'go' : (oneShot ? '' : 'dim'));
    }
  }

  function shiftTapped() {
    var now = Date.now();
    var dbl = (now - lastShift) < 400;
    /* Consume the pair. Without this a third quick tap counts as another
     * double and toggles the lock straight back off, so two taps ended where
     * they started -- which is exactly what happened. */
    lastShift = dbl ? 0 : now;
    if (dbl) {
      caps = true;            /* a double tap LOCKS; it does not toggle */
      oneShot = false;
    } else if (caps) {
      caps = false;           /* a single tap releases the lock */
      oneShot = false;
    } else {
      oneShot = !oneShot;
    }
    relabel();
  }

  function render() {
    keysBox.innerHTML = '';
    var rows = LAYERS[layer];
    rows.forEach(function (row, i) {
      var chars = row.k.split('');
      var start = row.c;
      chars.forEach(function (c, j) {
        var b = key(upper() ? c.toUpperCase() : c, '', start + j * 2, 2,
                    i + 1, 1, function () {
                      insert(upper() ? c.toUpperCase() : c);
                      if (oneShot && !caps) { oneShot = false; relabel(); }
                    });
        b.setAttribute('data-ch', c);
        keysBox.appendChild(b);
      });
    });

    /* Backspace: right of 0 and p, two rows tall. Enter: under it, right of
     * the two letter rows, also two rows tall. */
    keysBox.appendChild(key('⌫', 'dim tall', MAIN + 1, 3, 1, 2, backspace));
    keysBox.appendChild(key('Enter', 'go tall', MAIN + 1, 3, 3, 2, enter));

    /* Bottom row, spanning all 23 columns: 3 + 17 + 3. The hyphen and full
     * stop have moved -- the stop next to M where a keyboard has it, the
     * hyphen into the symbol layer. */
    var sk = key(caps ? '⇪' : '⇧', 'btm ' + (caps ? 'go' : 'dim'), 1, 3, 5, 1,
                 shiftTapped);
    sk.setAttribute('data-shift', '1');
    keysBox.appendChild(sk);
    keysBox.appendChild(key('space', 'btm', 4, 17, 5, 1, function () { insert(' '); }));
    /* The rebuild is deferred to the next tick so this button still exists
     * while its own event finishes dispatching. */
    keysBox.appendChild(key(layer === 'abc' ? '?123' : 'ABC', 'btm dim', 21, 3, 5, 1,
                            function () {
                              layer = (layer === 'abc' ? 'sym' : 'abc');
                              setTimeout(render, 0);
                            }));
  }

  function build() {
    css();
    wrap = document.createElement('div');
    wrap.id = 'ds-osk';
    /* Any press anywhere on the panel -- a key, the gap between keys, the
     * padding under the bottom row -- must not move focus off the field.
     * Without this, a press that missed a key landed on the page behind,
     * blurred the input, and the keyboard dismissed itself. Reported from a
     * real panel; a synthetic pointerdown on a button could never show it. */
    ['pointerdown', 'touchstart', 'mousedown'].forEach(function (ev) {
      wrap.addEventListener(ev, function (e) { e.preventDefault(); },
                            { passive: false });
    });
    keysBox = document.createElement('div');
    keysBox.className = 'keys';
    wrap.appendChild(keysBox);
    render();
    /* No close button. It dismisses the way a phone keyboard does: Enter
     * closes it, and so does touching anything on the page that is not a
     * text field. A button whose only job is to undo an accident is one more
     * thing to mis-tap on a panel this size.
     *
     * Note the panel still swallows presses that land beside the keys -- that
     * is deliberate and unrelated, so a near-miss does not dismiss it. */
    document.body.appendChild(wrap);
  }

  /* "lock" starts with caps lock on, "first" capitalises the first letter
   * then drops to lower case, "off" stays lower case. Set from the admin
   * page; a code typed at a scanner is usually upper case, so lock is the
   * default. */
  function applyCapsMode() {
    var m = (window.__dsOskCaps || 'lock');
    caps = (m === 'lock');
    oneShot = (m === 'first');
  }

  function show(el) {
    if (!ENABLED) { return; }
    target = el;
    if (!wrap) { build(); }
    applyCapsMode();
    relabel();
    wrap.classList.add('on');
    setTimeout(function () {
      try {
        var r = el.getBoundingClientRect();
        var lim = window.innerHeight - wrap.offsetHeight - 12;
        if (r.bottom > lim) { window.scrollBy(0, r.bottom - lim + 8); }
      } catch (err) {}
    }, 30);
  }

  function hide() {
    if (wrap) { wrap.classList.remove('on'); }
    target = null;
    layer = 'abc';
    applyCapsMode();
    if (keysBox) { render(); }
  }

  document.addEventListener('focusin', function (e) {
    if (isField(e.target)) { show(e.target); }
  }, true);

  document.addEventListener('focusout', function (e) {
    setTimeout(function () {
      var a = document.activeElement;
      /* Focus landing inside the keyboard is not the field being finished
       * with. Put it back and stay up. */
      if (wrap && a && wrap.contains(a)) { refocus(); return; }
      if (!isField(a)) { hide(); }
    }, 150);
  }, true);

  applyCapsMode();
  if (isField(document.activeElement)) { show(document.activeElement); }
})();
