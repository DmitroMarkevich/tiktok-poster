from __future__ import annotations
from playwright.async_api import Page
from utils.fingerprint import Fingerprint


async def apply_stealth(page: Page, fp: Fingerprint | None = None) -> None:
    if fp is None:
        from utils.fingerprint import get_fingerprint
        fp = get_fingerprint(0)

    langs_js  = str(fp.languages).replace("'", '"')
    await page.add_init_script(f"""
    (() => {{
    const FP = {{
        ua:       {repr(fp.ua)},
        platform: {repr(fp.platform)},
        sw:       {fp.screen_w},
        sh:       {fp.screen_h},
        sah:      {fp.screen_avail_h},
        hw:       {fp.hardware_concurrency},
        mem:      {fp.device_memory},
        wv:       {repr(fp.webgl_vendor)},
        wr:       {repr(fp.webgl_renderer)},
        langs:    {langs_js},
    }};

    // webdriver
    Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});

    // UA
    Object.defineProperty(navigator, 'userAgent',  {{ get: () => FP.ua }});
    Object.defineProperty(navigator, 'appVersion', {{ get: () => FP.ua.replace('Mozilla/', '') }});

    // Platform / hardware
    Object.defineProperty(navigator, 'platform',            {{ get: () => FP.platform }});
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => FP.hw }});
    Object.defineProperty(navigator, 'deviceMemory',        {{ get: () => FP.mem }});
    Object.defineProperty(navigator, 'languages',           {{ get: () => FP.langs }});
    Object.defineProperty(navigator, 'language',            {{ get: () => FP.langs[0] }});

    // Screen
    Object.defineProperty(screen, 'width',       {{ get: () => FP.sw }});
    Object.defineProperty(screen, 'height',      {{ get: () => FP.sh }});
    Object.defineProperty(screen, 'availWidth',  {{ get: () => FP.sw }});
    Object.defineProperty(screen, 'availHeight', {{ get: () => FP.sah }});
    Object.defineProperty(screen, 'colorDepth',  {{ get: () => 24 }});
    Object.defineProperty(screen, 'pixelDepth',  {{ get: () => 24 }});

    // Realistic plugins (Chrome on desktop always has these three)
    const _plugins = [
        {{ name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',          description: 'Portable Document Format' }},
        {{ name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' }},
        {{ name: 'Native Client',      filename: 'internal-nacl-plugin',         description: '' }},
    ];
    Object.defineProperty(navigator, 'plugins',   {{ get: () => _plugins }});
    Object.defineProperty(navigator, 'mimeTypes', {{ get: () => [] }});

    // WebGL
    const _patchGL = (Klass) => {{
        if (!Klass) return;
        const _orig = Klass.prototype.getParameter;
        Klass.prototype.getParameter = function(p) {{
            if (p === 37445) return FP.wv;
            if (p === 37446) return FP.wr;
            return _orig.call(this, p);
        }};
    }};
    _patchGL(window.WebGLRenderingContext);
    _patchGL(window.WebGL2RenderingContext);

    // Permissions
    const _origQuery = window.navigator.permissions?.query?.bind(navigator.permissions);
    if (_origQuery) {{
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({{ state: Notification.permission }})
                : _origQuery(p);
    }}

    // Remove CDP / Playwright artefacts
    const _del = ['cdc_adoQpoasnfa76pfcZLmcfl_Array','cdc_adoQpoasnfa76pfcZLmcfl_Promise',
                   'cdc_adoQpoasnfa76pfcZLmcfl_Symbol','__playwright','__pw_manual'];
    _del.forEach(k => {{ try {{ delete window[k]; }} catch(_) {{}} }});

    // Chrome runtime mock
    if (!window.chrome) {{
        window.chrome = {{
            runtime: {{
                connect: () => ({{}}),
                sendMessage: () => {{}},
                onMessage: {{ addListener: () => {{}} }},
            }},
            loadTimes: () => ({{}}),
            csi: () => ({{}}),
        }};
    }}
    }})();
    """)
