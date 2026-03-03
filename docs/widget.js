/**
 * Phenomenai — Embeddable Widget
 *
 * Usage:
 *   Word of the Day:
 *     <div id="ai-dict-wotd"></div>
 *     <script src="https://phenomenai.org/widget.js"></script>
 *
 *   Inline term tooltips:
 *     <span data-ai-term="context-amnesia">context amnesia</span>
 *     <script src="https://phenomenai.org/widget.js"></script>
 */
(function () {
    'use strict';

    var API_BASE = 'https://phenomenai.org/api/v1';
    var SITE_URL = 'https://phenomenai.org';
    var NS = 'ai-dict';

    // ── Inject styles once ──
    function injectStyles() {
        if (document.getElementById(NS + '-styles')) return;
        var style = document.createElement('style');
        style.id = NS + '-styles';
        style.textContent =
            '.' + NS + '-card{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:1.25rem;max-width:400px;box-shadow:0 2px 8px rgba(0,0,0,.08);line-height:1.5;color:#1e293b}' +
            '.' + NS + '-card .wotd-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:#7c3aed;font-weight:600;margin-bottom:.5rem}' +
            '.' + NS + '-card-header{display:flex;align-items:baseline;gap:.5rem;margin-bottom:.5rem}' +
            '.' + NS + '-card h3{margin:0;font-size:1.15rem;color:#2563eb}' +
            '.' + NS + '-card .word-type{font-size:.8rem;color:#94a3b8;font-style:italic}' +
            '.' + NS + '-card p{margin:0 0 .75rem;font-size:.93rem;color:#475569}' +
            '.' + NS + '-card .powered{font-size:.75rem;color:#94a3b8;text-decoration:none;display:block;text-align:right}' +
            '.' + NS + '-card .powered:hover{color:#2563eb}' +
            '.' + NS + '-tooltip{position:absolute;z-index:10000;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:1rem;max-width:320px;box-shadow:0 8px 24px rgba(0,0,0,.12);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.5;color:#1e293b}' +
            '.' + NS + '-tooltip h4{margin:0 0 .3rem;font-size:1rem;color:#2563eb}' +
            '.' + NS + '-tooltip .word-type{font-size:.75rem;color:#94a3b8;font-style:italic}' +
            '.' + NS + '-tooltip p{margin:.4rem 0 0;font-size:.88rem;color:#475569}' +
            '.' + NS + '-tooltip .powered{font-size:.7rem;color:#94a3b8;text-decoration:none;display:block;text-align:right;margin-top:.5rem}' +
            '.' + NS + '-tooltip .powered:hover{color:#2563eb}' +
            '[data-ai-term]{border-bottom:1px dashed #2563eb;cursor:help}';
        document.head.appendChild(style);
    }

    // ── Cache ──
    var termsCache = null;
    var fetchPromise = null;
    var termDetailCache = {};

    function fetchTerms() {
        if (termsCache) return Promise.resolve(termsCache);
        if (fetchPromise) return fetchPromise;
        fetchPromise = fetch(API_BASE + '/search-index.json')
            .then(function (r) { return r.json(); })
            .then(function (data) { termsCache = data.terms; return termsCache; });
        return fetchPromise;
    }

    function fetchFullTerm(slug) {
        if (termDetailCache[slug]) return Promise.resolve(termDetailCache[slug]);
        return fetch(API_BASE + '/terms/' + slug + '.json')
            .then(function (r) { return r.json(); })
            .then(function (data) { termDetailCache[slug] = data; return data; });
    }

    // ── Deterministic Word of the Day (djb2 hash) ──
    function hashDate(dateStr) {
        var hash = 5381;
        for (var i = 0; i < dateStr.length; i++) {
            hash = ((hash << 5) + hash) + dateStr.charCodeAt(i);
            hash = hash & hash;
        }
        return Math.abs(hash);
    }

    function getWotdTerm(terms) {
        var today = new Date().toISOString().slice(0, 10);
        var index = hashDate(today) % terms.length;
        return terms[index];
    }

    function escHtml(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function renderInlineMd(s) {
        return escHtml(s)
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>');
    }

    // ── Word of the Day ──
    function renderWotd(container) {
        fetchTerms().then(function (terms) {
            var todayTerm = getWotdTerm(terms);
            return fetchFullTerm(todayTerm.slug);
        }).then(function (full) {
            container.innerHTML =
                '<div class="' + NS + '-card">' +
                '<div class="wotd-label">Word of the Day</div>' +
                '<div class="' + NS + '-card-header">' +
                '<h3>' + escHtml(full.name) + '</h3>' +
                '<span class="word-type">' + escHtml(full.word_type || '') + '</span>' +
                '</div>' +
                '<p>' + renderInlineMd(full.definition) + '</p>' +
                '<a class="powered" href="' + SITE_URL + '" target="_blank" rel="noopener">' +
                'Powered by Phenomenai' +
                '</a></div>';
        }).catch(function (err) {
            container.innerHTML = '';
            console.error('[Phenomenai Widget] Failed to load Word of the Day', err);
        });
    }

    // ── Tooltip ──
    var activeTooltip = null;

    function removeTooltip() {
        if (activeTooltip) {
            activeTooltip.remove();
            activeTooltip = null;
        }
    }

    function showTooltip(el) {
        var slug = el.getAttribute('data-ai-term');
        if (!slug) return;
        removeTooltip();

        fetchFullTerm(slug).then(function (full) {
            var tooltip = document.createElement('div');
            tooltip.className = NS + '-tooltip';

            var rect = el.getBoundingClientRect();
            tooltip.style.top = (window.scrollY + rect.bottom + 8) + 'px';
            tooltip.style.left = Math.max(8, rect.left) + 'px';

            tooltip.innerHTML =
                '<h4>' + escHtml(full.name) + '</h4>' +
                '<span class="word-type">' + escHtml(full.word_type || '') + '</span>' +
                '<p>' + renderInlineMd(full.definition) + '</p>' +
                '<a class="powered" href="' + SITE_URL + '#' + slug + '" target="_blank" rel="noopener">' +
                'Phenomenai' +
                '</a>';

            document.body.appendChild(tooltip);
            activeTooltip = tooltip;

            // Reposition if off-screen right
            var tooltipRect = tooltip.getBoundingClientRect();
            if (tooltipRect.right > window.innerWidth - 8) {
                tooltip.style.left = Math.max(8, window.innerWidth - tooltipRect.width - 8) + 'px';
            }
        }).catch(function (err) {
            console.error('[Phenomenai Widget] Failed to load term', slug, err);
        });
    }

    // ── Init ──
    function init() {
        injectStyles();

        // Word of the Day containers
        var wotdEls = document.querySelectorAll('#ai-dict-wotd, [data-ai-dict="wotd"]');
        for (var i = 0; i < wotdEls.length; i++) {
            renderWotd(wotdEls[i]);
        }

        // Tooltip triggers — event delegation
        document.addEventListener('mouseenter', function (e) {
            if (e.target && e.target.getAttribute && e.target.getAttribute('data-ai-term')) {
                showTooltip(e.target);
            }
        }, true);

        document.addEventListener('mouseleave', function (e) {
            if (e.target && e.target.getAttribute && e.target.getAttribute('data-ai-term')) {
                setTimeout(function () {
                    if (activeTooltip && !activeTooltip.matches(':hover')) {
                        removeTooltip();
                    }
                }, 200);
            }
        }, true);

        // Click to toggle on mobile
        document.addEventListener('click', function (e) {
            if (e.target && e.target.getAttribute && e.target.getAttribute('data-ai-term')) {
                e.preventDefault();
                if (activeTooltip) {
                    removeTooltip();
                } else {
                    showTooltip(e.target);
                }
            } else if (activeTooltip && !activeTooltip.contains(e.target)) {
                removeTooltip();
            }
        });
    }

    // Run on DOMContentLoaded or immediately
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
