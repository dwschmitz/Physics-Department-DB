/* The page's small behaviours. Kept in this file (not inline in the pages) so the site can use a strict
   Content-Security-Policy that forbids inline scripts. */
(function () {
  // Forms marked data-confirm="message" ask first: <form data-confirm="Delete this entry?">
  document.addEventListener('submit', function (e) {
    var message = e.target.getAttribute && e.target.getAttribute('data-confirm');
    if (message && !window.confirm(message)) { e.preventDefault(); }
  });

  // Fields marked data-show-when="field=value" only appear while that field has that value, or, for a checkbox, while
  // it is ticked (without scripting they are always shown), e.g. the graduation term once Status is Graduated.
  Array.prototype.forEach.call(document.querySelectorAll('[data-show-when]'), function (box) {
    var rule = box.getAttribute('data-show-when').split('='), src = document.getElementById(rule[0]);
    if (!src) return;
    var update = function () { box.hidden = src.type === 'checkbox' ? !src.checked : src.value !== rule[1]; };
    src.addEventListener('change', update);
    update();
  });

  // The "copy funding from another term" window: opened by links marked data-copy, stepped by links marked data-step.
  var dlg = document.getElementById('copy-dialog');
  if (dlg && dlg.showModal) {               // very old browsers: the Copy links still work as plain pages
    var load = function (url) {
      return fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + 'fragment=1', {credentials: 'same-origin'})
        .then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.text(); })
        .then(function (html) { dlg.innerHTML = html; });
    };
    document.addEventListener('click', function (e) {
      var open = e.target.closest('a[data-copy]');
      if (open) {
        e.preventDefault();
        load(open.href).then(function () { dlg.showModal(); }, function () { window.location = open.href; });
        return;
      }
      var step = e.target.closest('#copy-dialog a[data-step]');
      if (step) { e.preventDefault(); load(step.href).catch(function () { window.location = step.href; }); return; }
      if (e.target.closest('#copy-dialog [data-close]')) { e.preventDefault(); dlg.close(); }
    });
  }
})();
