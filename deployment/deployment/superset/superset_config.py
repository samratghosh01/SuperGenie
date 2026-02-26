import os

# ── Core ──────────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "change-me-in-production")

# ── Branding ─────────────────────────────────────────────────────────────────
APP_NAME = "BI Genie"
APP_ICON = "/static/assets/images/superset-logo-horiz.png"
FAVICONS = [{"href": "/static/assets/images/superset-logo-horiz.png"}]

SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://superset:superset@db:5432/superset",
)

# ── Redis caches ──────────────────────────────────────────────────────────────
_REDIS = os.environ.get("REDIS_URL", "redis://redis:6379/0")

CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_URL": _REDIS,
}
DATA_CACHE_CONFIG          = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_data_"}
FILTER_STATE_CACHE_CONFIG  = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_filter_"}
EXPLORE_FORM_DATA_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_explore_"}

# ── Features ──────────────────────────────────────────────────────────────────
FEATURE_FLAGS = {
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
}

# ── Security ──────────────────────────────────────────────────────────────────
WTF_CSRF_ENABLED = True
WTF_CSRF_EXEMPT_LIST = []
WTF_CSRF_TIME_LIMIT = 60 * 60 * 24 * 365
TALISMAN_ENABLED = False          # set True if using HTTPS

# ── CSV / Excel upload ───────────────────────────────────────────────────────
UPLOAD_FOLDER = "/app/uploads/"
ALLOWED_EXTENSIONS = {"csv", "xls", "xlsx", "columnar", "tsv"}
CSV_EXPORT = {"encoding": "utf-8"}

# ── Query limits ──────────────────────────────────────────────────────────────
ROW_LIMIT = 5000
SQL_MAX_ROW = 100000

# ── Chat widget injection ─────────────────────────────────────────────────────
# Injects a floating chat button into every Superset HTML page.
# Clicking it opens an iframe panel pointing to the FastAPI chat UI.

_BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:9000")

_CHAT_SCRIPT = """
<script>
(function () {
  if (document.getElementById('ai-chat-btn')) return; // run once

  var BACKEND_URL = '""" + _BACKEND_URL + """';

  // ── Floating button ──────────────────────────────────────────────────────
  var btn = document.createElement('div');
  btn.id = 'ai-chat-btn';
  btn.innerHTML = '&#x1F4AC;';
  btn.title = 'BI Genie';
  btn.style.cssText = [
    'position:fixed', 'bottom:24px', 'right:24px',
    'width:52px', 'height:52px', 'border-radius:50%',
    'background:#1890ff', 'color:white', 'font-size:24px',
    'display:flex', 'align-items:center', 'justify-content:center',
    'cursor:pointer', 'box-shadow:0 4px 16px rgba(0,0,0,.25)',
    'z-index:9999', 'user-select:none', 'transition:transform .15s'
  ].join(';');
  btn.onmouseenter = function(){ btn.style.transform='scale(1.1)'; };
  btn.onmouseleave = function(){ btn.style.transform='scale(1)'; };

  // ── Chat panel ───────────────────────────────────────────────────────────
  var panel = document.createElement('div');
  panel.id = 'ai-chat-panel';
  panel.style.cssText = [
    'position:fixed', 'bottom:84px', 'right:24px',
    'width:340px', 'height:460px',
    'background:white', 'border-radius:12px',
    'box-shadow:0 8px 32px rgba(0,0,0,.18)',
    'z-index:9998', 'display:none', 'overflow:hidden',
    'border:1px solid #e0e0e0'
  ].join(';');

  var iframe = document.createElement('iframe');
  iframe.src = BACKEND_URL + '/chat';
  iframe.style.cssText = 'width:100%;height:100%;border:none;';
  panel.appendChild(iframe);

  document.body.appendChild(btn);
  document.body.appendChild(panel);

  // ── Fetch user context and send to iframe (RBAC) ───────────────────────
  async function sendUserContext() {
    try {
      var meResp = await fetch('/api/v1/me/');
      if (!meResp.ok) {
        iframe.contentWindow.postMessage({ type: 'superset_session_expired' }, '*');
        return;
      }
      var meData = await meResp.json();
      var userInfo = meData.result;

      var dsResp = await fetch('/api/v1/dataset/?q=' + encodeURIComponent(JSON.stringify({ page_size: 100 })));
      if (!dsResp.ok) { return; }
      var dsData = await dsResp.json();
      var dsList = [];

      for (var i = 0; i < dsData.result.length; i++) {
        var ds = dsData.result[i];
        try {
          var colResp = await fetch('/api/v1/dataset/' + ds.id);
          if (colResp.ok) {
            var colData = await colResp.json();
            var cols = colData.result.columns.map(function(c) { return c.column_name; });
            dsList.push({ id: ds.id, table_name: ds.table_name, columns: cols });
          }
        } catch (_) {}
      }

      iframe.contentWindow.postMessage({
        type: 'superset_user_context',
        user: { id: userInfo.id, username: userInfo.username,
                first_name: userInfo.first_name, last_name: userInfo.last_name },
        datasets: dsList
      }, '*');
    } catch (err) {
      console.error('BI Genie: failed to fetch user context', err);
    }
  }

  iframe.addEventListener('load', function() { sendUserContext(); });

  // ── Toggle ───────────────────────────────────────────────────────────────
  btn.onclick = function () {
    var open = panel.style.display !== 'none';
    panel.style.display = open ? 'none' : 'block';
    btn.style.background = open ? '#1890ff' : '#0050b3';
  };

  // ── Messages from iframe ───────────────────────────────────────────────
  window.addEventListener('message', function (e) {
    if (e.data === 'close') {
      panel.style.display = 'none';
      btn.style.background = '#1890ff';
    }
    if (e.data === 'refresh_datasets') {
      sendUserContext();
    }
  });

  // ── Logo swap ──────────────────────────────────────────────────────────
  var LOGO_URL = BACKEND_URL + '/logo.png';
  function swapLogo() {
    var imgs = document.querySelectorAll('img.navbar-brand-image, img[alt*="logo"], a.navbar-brand img, .ant-layout-header img');
    imgs.forEach(function(img) { img.src = LOGO_URL; img.style.maxHeight = '32px'; });
    // Also check for any img whose src contains 'superset-logo'
    document.querySelectorAll('img').forEach(function(img) {
      if (img.src && img.src.indexOf('superset') !== -1 && img.src.indexOf('logo') !== -1) {
        img.src = LOGO_URL; img.style.maxHeight = '32px';
      }
    });
  }
  // Run after React renders + observe for SPA navigation
  setTimeout(swapLogo, 1000);
  setTimeout(swapLogo, 3000);
  new MutationObserver(function() { swapLogo(); })
    .observe(document.body, { childList: true, subtree: true });
}());
</script>
"""


def FLASK_APP_MUTATOR(app):
    @app.after_request
    def inject_chat_widget(response):
        ct = response.content_type or ""
        if "text/html" in ct and not response.direct_passthrough:
            try:
                html = response.get_data(as_text=True)
                if "</body>" in html:
                    html = html.replace("</body>", _CHAT_SCRIPT + "</body>", 1)
                    response.set_data(html)
            except Exception:
                pass  # never break Superset pages
        return response
