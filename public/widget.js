/**
 * Aptlyst AI - Lightweight Website Chatbot Widget Loader
 * Dependency-free, accessible, host-isolated embed script.
 *
 * Usage:
 *   <script
 *     src="https://app.aptlyst.ai/widget.js"
 *     data-bot-id="YOUR_BOT_UUID"
 *     data-base-url="https://app.aptlyst.ai">
 *   </script>
 */
(function () {
  'use strict';

  function init() {
    // 1. Locate script element and read configuration
    var script =
      document.currentScript ||
      document.querySelector('script[data-bot-id]');

    if (!script) {
      return;
    }

    var botId = (script.getAttribute('data-bot-id') || '').trim();
    if (!botId) {
      console.warn('[Aptlyst Widget] Missing required "data-bot-id" attribute on script tag.');
      return;
    }

    // 2. Resolve base URL (configurable via data-base-url or inferred from script src)
    var baseUrl = (script.getAttribute('data-base-url') || '').trim();
    if (!baseUrl && script.src) {
      try {
        var parsedUrl = new URL(script.src);
        baseUrl = parsedUrl.origin;
      } catch (e) {
        baseUrl = window.location.origin;
      }
    }
    if (!baseUrl) {
      baseUrl = window.location.origin;
    }
    baseUrl = baseUrl.replace(/\/+$/, '');

    // Prevent duplicate injection if script is loaded multiple times
    if (document.getElementById('aptlyst-widget-container')) {
      return;
    }

    // 3. Create SVG icons using standard SVG namespace (DOM API safe)
    var SVG_NS = 'http://www.w3.org/2000/svg';

    function createSvgIcon(pathData) {
      var svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('width', '24');
      svg.setAttribute('height', '24');
      svg.setAttribute('fill', 'none');
      svg.setAttribute('stroke', 'currentColor');
      svg.setAttribute('stroke-width', '2');
      svg.setAttribute('stroke-linecap', 'round');
      svg.setAttribute('stroke-linejoin', 'round');

      var path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', pathData);
      svg.appendChild(path);
      return svg;
    }

    // Chat bubble path
    var chatIcon = createSvgIcon('M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z');
    // Close 'X' path
    var closeIcon = createSvgIcon('M18 6L6 18M6 6l12 12');
    closeIcon.style.display = 'none';

    // 4. Create floating launcher button
    var launcher = document.createElement('button');
    launcher.type = 'button';
    launcher.id = 'aptlyst-widget-launcher';
    launcher.setAttribute('aria-label', 'Open chat');
    launcher.setAttribute('aria-expanded', 'false');
    launcher.setAttribute('aria-haspopup', 'dialog');

    // Inline styles to isolate from host website stylesheet
    launcher.style.position = 'fixed';
    launcher.style.bottom = '20px';
    launcher.style.right = '20px';
    launcher.style.width = '56px';
    launcher.style.height = '56px';
    launcher.style.borderRadius = '50%';
    launcher.style.backgroundColor = '#2563eb';
    launcher.style.color = '#ffffff';
    launcher.style.border = 'none';
    launcher.style.outline = 'none';
    launcher.style.cursor = 'pointer';
    launcher.style.boxShadow = '0 4px 14px 0 rgba(0, 0, 0, 0.3)';
    launcher.style.display = 'flex';
    launcher.style.alignItems = 'center';
    launcher.style.justifyContent = 'center';
    launcher.style.zIndex = '2147483647';
    launcher.style.transition = 'transform 0.2s ease, box-shadow 0.2s ease';

    launcher.appendChild(chatIcon);
    launcher.appendChild(closeIcon);

    launcher.addEventListener('mouseenter', function () {
      launcher.style.transform = 'scale(1.05)';
      launcher.style.boxShadow = '0 6px 20px 0 rgba(0, 0, 0, 0.35)';
    });
    launcher.addEventListener('mouseleave', function () {
      launcher.style.transform = 'scale(1)';
      launcher.style.boxShadow = '0 4px 14px 0 rgba(0, 0, 0, 0.3)';
    });

    // 5. Create responsive iframe window
    var iframe = document.createElement('iframe');
    iframe.id = 'aptlyst-widget-iframe';
    iframe.title = 'Aptlyst AI Chatbot';
    iframe.src = baseUrl + '/embed/' + encodeURIComponent(botId);
    iframe.loading = 'lazy';

    iframe.style.position = 'fixed';
    iframe.style.bottom = '90px';
    iframe.style.right = '20px';
    iframe.style.width = '380px';
    iframe.style.height = '600px';
    iframe.style.maxHeight = 'calc(100vh - 110px)';
    iframe.style.maxWidth = 'calc(100vw - 40px)';
    iframe.style.border = '1px solid rgba(255, 255, 255, 0.1)';
    iframe.style.borderRadius = '16px';
    iframe.style.boxShadow = '0 12px 32px rgba(0, 0, 0, 0.4)';
    iframe.style.backgroundColor = '#020617';
    iframe.style.zIndex = '2147483646';
    iframe.style.display = 'none';
    iframe.style.overflow = 'hidden';

    // Responsive sizing on mobile viewport (< 480px)
    function applyResponsiveStyles() {
      if (window.innerWidth <= 480) {
        iframe.style.bottom = '0';
        iframe.style.right = '0';
        iframe.style.width = '100vw';
        iframe.style.height = '100vh';
        iframe.style.maxHeight = '100vh';
        iframe.style.maxWidth = '100vw';
        iframe.style.borderRadius = '0';
        iframe.style.border = 'none';
      } else {
        iframe.style.bottom = '90px';
        iframe.style.right = '20px';
        iframe.style.width = '380px';
        iframe.style.height = '600px';
        iframe.style.maxHeight = 'calc(100vh - 110px)';
        iframe.style.maxWidth = 'calc(100vw - 40px)';
        iframe.style.borderRadius = '16px';
        iframe.style.border = '1px solid rgba(255, 255, 255, 0.1)';
      }
    }

    window.addEventListener('resize', applyResponsiveStyles);

    // 6. Toggle open / close state
    var isOpen = false;

    function toggleChat() {
      isOpen = !isOpen;
      if (isOpen) {
        applyResponsiveStyles();
        iframe.style.display = 'block';
        chatIcon.style.display = 'none';
        closeIcon.style.display = 'block';
        launcher.setAttribute('aria-expanded', 'true');
        launcher.setAttribute('aria-label', 'Close chat');
      } else {
        iframe.style.display = 'none';
        chatIcon.style.display = 'block';
        closeIcon.style.display = 'none';
        launcher.setAttribute('aria-expanded', 'false');
        launcher.setAttribute('aria-label', 'Open chat');
      }
    }

    launcher.addEventListener('click', toggleChat);

    // 7. Inject elements into DOM safely
    var container = document.createElement('div');
    container.id = 'aptlyst-widget-container';
    container.appendChild(iframe);
    container.appendChild(launcher);

    document.body.appendChild(container);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
