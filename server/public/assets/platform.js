'use strict';
// Promotes the Linux beta download on a Linux x86_64 desktop, and does nothing anywhere else.
//
// Every page that offers "Download for Windows" (.btn-primary, /hub/download) offers "Download for
// Linux (beta)" (.btn-ghost, /hub/download/linux) beside it. On a Linux x86_64 desktop this swaps
// the two classes and puts the Linux button first; both buttons always stay visible, and neither
// link changes. /hub/download itself never looks at the User-Agent. A row marked
// data-download-order="fixed" keeps Windows first everywhere: the account page's, because the
// account just created there signs in to the Windows app and not to the Linux beta.
//
// Android and ChromeOS put "Linux" in their User-Agent too, and are not the beta's platform. Chrome
// reports "X11; Linux x86_64" on every Linux PC, ARM included, so where the browser offers
// User-Agent Client Hints the real architecture decides. Any doubt leaves the page as it is.
//
// An external file because the account page's Content-Security-Policy allows only script-src 'self'.
(function promoteLinuxDownload() {
  var WINDOWS = '/hub/download';
  var LINUX = '/hub/download/linux';

  function linuxDesktop() {
    var agent = String(navigator.userAgent || '');
    if (!/\bLinux x86_64\b/.test(agent) || /Android|CrOS/i.test(agent)) return Promise.resolve(false);
    var hints = navigator.userAgentData;
    // Firefox and Safari send no hints, and name the real architecture in the User-Agent.
    if (!hints) return Promise.resolve(true);
    if (hints.platform !== 'Linux' || hints.mobile === true || typeof hints.getHighEntropyValues !== 'function') {
      return Promise.resolve(false);
    }
    return hints.getHighEntropyValues(['architecture', 'bitness']).then(function (values) {
      return values.architecture === 'x86' && values.bitness === '64';
    }, function () { return false; });
  }

  function promote() {
    var links = document.querySelectorAll('a.btn-ghost[href="' + LINUX + '"]');
    for (var i = 0; i < links.length; i++) {
      var linux = links[i];
      var row = linux.parentNode;
      if (!row || row.getAttribute('data-download-order') === 'fixed') continue;
      var windows = row.querySelector('a.btn-primary[href="' + WINDOWS + '"]');
      if (!windows || windows.parentNode !== row) continue;
      windows.classList.remove('btn-primary');
      windows.classList.add('btn-ghost');
      linux.classList.remove('btn-ghost');
      linux.classList.add('btn-primary');
      row.insertBefore(linux, windows);
    }
  }

  try {
    linuxDesktop().then(function (yes) { if (yes) promote(); }, function () {});
  } catch (error) { /* the page is complete without this */ }
})();
