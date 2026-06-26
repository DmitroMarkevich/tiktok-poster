from __future__ import annotations
from playwright.async_api import Page
from utils.fingerprint import Fingerprint


async def apply_stealth(page: Page, fp: Fingerprint | None = None, *,
                        spoof_gpu: bool = True) -> None:
    """spoof_gpu=False disables the canvas/WebGL/audio pixel-spoofing.

    On a REAL Chrome (channel="chrome") on a real machine — which is what the patchright
    signup/reader flows run — the GPU (e.g. Apple Silicon) already produces a genuine,
    self-consistent fingerprint. Overriding WebGL getParameter / canvas readback in JS then
    becomes a LIABILITY: PerimeterX/HUMAN cross-checks the WebGL anisotropic extension (which
    JS hooks can't fake) against the spoofed renderer string and flags the contradiction.
    The pixel-noise is only worthwhile on a headless SOFTWARE rasteriser (SwiftShader/llvmpipe)
    where every account would otherwise share one hash — there, leave it on."""
    if fp is None:
        from utils.fingerprint import get_fingerprint
        fp = get_fingerprint(0)

    langs_js  = str(fp.languages).replace("'", '"')

    # High-entropy Client-Hints MUST agree with the GPU/OS, or a detector that cross-checks
    # userAgentData.getHighEntropyValues() against the WebGL renderer catches the lie.
    # A Mac on Apple Silicon reports architecture "arm"; the old hardcoded "x86" + "Apple M1"
    # WebGL string was a direct contradiction. Derive arch/platformVersion from the real fp.
    if fp.platform == "MacIntel":
        _arch = "arm" if "Apple M" in fp.webgl_renderer else "x86"
        _plat_ver = "14.5.0"            # macOS Sonoma-era, plausible for 2026 clients
    elif fp.platform == "Win32":
        _arch, _plat_ver = "x86", "15.0.0"   # Win11 reports 15.x via CH
    else:
        _arch, _plat_ver = "x86", ""
    await page.add_init_script(f"""
    (() => {{
    const FP = {{
        ua:       {repr(fp.ua)},
        platform: {repr(fp.platform)},
        uaPlat:   {repr(fp.ua_platform)},
        ver:      {repr(fp.chrome_version)},
        sw:       {fp.screen_w},
        sh:       {fp.screen_h},
        sah:      {fp.screen_avail_h},
        hw:       {fp.hardware_concurrency},
        mem:      {fp.device_memory},
        wv:       {repr(fp.webgl_vendor)},
        wr:       {repr(fp.webgl_renderer)},
        langs:    {langs_js},
        seed:     {fp.canvas_seed},
        arch:     {repr(_arch)},
        platVer:  {repr(_plat_ver)},
    }};

    // Mask patched functions so `fn.toString()` reports native code (see the
    // Function.prototype.toString patch at the very bottom). Defined FIRST so every
    // override below — including the navigator/screen getters — can register itself.
    const _native = new WeakSet();
    const _mask = (fn) => {{ try {{ _native.add(fn); }} catch (_) {{}} return fn; }};
    // When false, leave the real GPU's canvas/WebGL/audio fingerprint untouched (real Chrome
    // on a real machine is already consistent; JS overrides would be a HUMAN/PerimeterX tell).
    const _SPOOF_GPU = {str(spoof_gpu).lower()};

    // Patch accessors on the PROTOTYPE (Navigator.prototype / Screen.prototype) with a
    // real-looking descriptor — NOT on the navigator/screen instance. On genuine Chrome
    // these props live on the prototype as configurable+enumerable getters, so
    // getOwnPropertyDescriptor(navigator,'webdriver') returns undefined. The old code
    // defined them on the instance → they became OWN props, an instant automation tell.
    const _defGet = (proto, prop, getter) => {{
        try {{
            Object.defineProperty(proto, prop, {{
                configurable: true, enumerable: true, get: _mask(getter),
            }});
        }} catch (_) {{}}
    }};
    const _NavProto = (window.Navigator && Navigator.prototype) || Object.getPrototypeOf(navigator);
    const _ScrProto = (window.Screen && Screen.prototype) || Object.getPrototypeOf(screen);

    // webdriver — real Chrome exposes the property as `false`, on the prototype.
    _defGet(_NavProto, 'webdriver', () => false);

    // UA
    _defGet(_NavProto, 'userAgent',  () => FP.ua);
    _defGet(_NavProto, 'appVersion', () => FP.ua.replace('Mozilla/', ''));

    // Platform / hardware
    _defGet(_NavProto, 'platform',            () => FP.platform);
    _defGet(_NavProto, 'hardwareConcurrency', () => FP.hw);
    _defGet(_NavProto, 'deviceMemory',        () => FP.mem);
    _defGet(_NavProto, 'languages',           () => FP.langs);
    _defGet(_NavProto, 'language',            () => FP.langs[0]);

    // Screen. Only override the dimensions when spoofing hardware. On a real machine the
    // genuine screen is self-consistent with matchMedia('(device-width)') and the OS window
    // (outerHeight), which a JS override of screen.width/height CANNOT keep in sync — Chrome
    // also pins screen.height back to the real value, leaving a half-applied contradiction
    // that fv.pro flags as "screen is not real". With spoof OFF, present the real screen.
    if (_SPOOF_GPU) {{
        _defGet(_ScrProto, 'width',       () => FP.sw);
        _defGet(_ScrProto, 'height',      () => FP.sh);
        _defGet(_ScrProto, 'availWidth',  () => FP.sw);
        _defGet(_ScrProto, 'availHeight', () => FP.sah);
    }}
    _defGet(_ScrProto, 'colorDepth',  () => 24);
    _defGet(_ScrProto, 'pixelDepth',  () => 24);

    // navigator.userAgentData — the JS side of Client-Hints. Must agree with the
    // Sec-CH-UA HTTP headers (set in browser.py) and navigator.platform, or the
    // cross-check fails. Modern Chrome always exposes this on https origins.
    try {{
        const _brands = [
            {{ brand: 'Chromium',     version: FP.ver }},
            {{ brand: 'Google Chrome', version: FP.ver }},
            {{ brand: 'Not=A?Brand',  version: '99' }},
        ];
        const _uaData = {{
            brands: _brands,
            mobile: false,
            platform: FP.uaPlat,
            getHighEntropyValues: (hints) => Promise.resolve({{
                brands: _brands,
                fullVersionList: _brands.map(b => ({{ brand: b.brand, version: b.version + '.0.0.0' }})),
                mobile: false,
                platform: FP.uaPlat,
                platformVersion: FP.platVer,
                architecture: FP.arch,
                bitness: '64',
                model: '',
                uaFullVersion: FP.ver + '.0.0.0',
            }}),
            toJSON: () => ({{ brands: _brands, mobile: false, platform: FP.uaPlat }}),
        }};
        _defGet(_NavProto, 'userAgentData', () => _uaData);
    }} catch (_) {{}}

    // Realistic plugins (Chrome on desktop always has these). Build a real PluginArray
    // with matching mimeTypes — a plugins list with an EMPTY mimeTypes is inconsistent
    // (the PDF plugins imply an application/pdf handler) and itself a tell.
    try {{
        const _mkMime = (type, suffixes, desc) => ({{ type, suffixes, description: desc, enabledPlugin: null }});
        const _pdfMimes = [
            _mkMime('application/pdf', 'pdf', ''),
            _mkMime('text/pdf', 'pdf', ''),
        ];
        const _defs = [
            {{ name: 'PDF Viewer',              filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
            {{ name: 'Chrome PDF Viewer',       filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
            {{ name: 'Chromium PDF Viewer',     filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
            {{ name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
            {{ name: 'WebKit built-in PDF',     filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
        ];
        const _plugins = _defs.map(d => {{
            const p = Object.create(Plugin.prototype);
            Object.defineProperties(p, {{
                name:        {{ value: d.name, enumerable: true }},
                filename:    {{ value: d.filename, enumerable: true }},
                description: {{ value: d.description, enumerable: true }},
                length:      {{ value: _pdfMimes.length, enumerable: true }},
            }});
            _pdfMimes.forEach((m, i) => {{ p[i] = m; }});
            return p;
        }});
        const _pluginArr = Object.create(PluginArray.prototype);
        _plugins.forEach((p, i) => {{ _pluginArr[i] = p; _pluginArr[p.name] = p; }});
        Object.defineProperty(_pluginArr, 'length', {{ value: _plugins.length }});
        const _mimeArr = Object.create(MimeTypeArray.prototype);
        _pdfMimes.forEach((m, i) => {{ _mimeArr[i] = m; _mimeArr[m.type] = m; }});
        Object.defineProperty(_mimeArr, 'length', {{ value: _pdfMimes.length }});
        _defGet(_NavProto, 'plugins',   () => _pluginArr);
        _defGet(_NavProto, 'mimeTypes', () => _mimeArr);
    }} catch (_) {{}}

    // Canvas / WebGL pixel noise. Spoofing only the WebGL vendor/renderer STRINGS isn't
    // enough: TikTok hashes the actual rendered pixels, which on a headless server come
    // from a software rasteriser (SwiftShader/llvmpipe) — same hash for every account.
    // Add a tiny, DETERMINISTIC-per-account perturbation so each account has a stable but
    // distinct canvas fingerprint instead of one shared server hash.
    if (_SPOOF_GPU) try {{
        // Re-seed at the START of every read so the SAME canvas always hashes to the SAME
        // value for this account. A persistent/evolving seed makes two reads of one canvas
        // differ — real Chrome is deterministic, and antifraud reads canvas twice to catch
        // exactly that. Per-account FP.seed keeps accounts distinct.
        // A FIXED "±1 on exactly 2% of pixels" pattern is itself a stealth-script
        // signature (detectors look for that uniform shape). Derive both the perturb RATE
        // and the magnitude RANGE per-account from the seed, and apply a variable delta
        // (±1..±maxd) — so the noise looks like genuine GPU/driver variance, not a tell.
        const _b0 = FP.seed >>> 0;
        const _rate = 0.012 + ((_b0 % 1000) / 1000) * 0.018;   // 1.2%..3.0% per account
        const _maxd = 1 + (_b0 % 3);                            // ±1..±3 per account
        const _noisify = (data) => {{
            let _s = FP.seed >>> 0;
            const _rnd = () => {{ _s = (_s * 1664525 + 1013904223) >>> 0; return _s / 4294967296; }};
            for (let i = 0; i < data.length; i += 4) {{
                if (_rnd() < _rate) {{
                    const d = (1 + Math.floor(_rnd() * _maxd)) * (_rnd() < 0.5 ? -1 : 1);
                    data[i]   = Math.min(255, Math.max(0, data[i]   + d));
                    data[i+1] = Math.min(255, Math.max(0, data[i+1] + d));
                    data[i+2] = Math.min(255, Math.max(0, data[i+2] + d));
                }}
            }}
            return data;
        }};
        const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = _mask(function(...a) {{
            const img = _origGetImageData.apply(this, a);
            _noisify(img.data);
            return img;
        }});
        const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = _mask(function(...a) {{
            try {{
                const ctx = this.getContext('2d');
                if (ctx) {{
                    const img = _origGetImageData.call(ctx, 0, 0, this.width, this.height);
                    _noisify(img.data);
                    ctx.putImageData(img, 0, 0);
                }}
            }} catch (_) {{}}
            return _origToDataURL.apply(this, a);
        }});
    }} catch (_) {{}}

    // AudioContext fingerprint noise. Same reasoning as canvas: a headless server renders an
    // identical audio waveform for every account → one shared cluster hash. Add a tiny,
    // deterministic-per-account perturbation. getChannelData returns a COPY (never mutates
    // the internal buffer) so the noise can't accumulate across repeated reads or corrupt
    // playback; getFloatFrequencyData writes into the caller's array, so it's noised in place.
    if (_SPOOF_GPU) try {{
        const _audioNoise = (arr) => {{
            let _s = (FP.seed ^ 0x9E3779B9) >>> 0;            // re-seed per call → deterministic
            for (let i = 0; i < arr.length; i++) {{
                _s = (_s * 1664525 + 1013904223) >>> 0;
                arr[i] += (_s / 4294967296 - 0.5) * 1e-7;
            }}
            return arr;
        }};
        const _AB = window.AudioBuffer;
        if (_AB && _AB.prototype.getChannelData) {{
            const _o = _AB.prototype.getChannelData;
            _AB.prototype.getChannelData = _mask(function(...a) {{
                const d = _o.apply(this, a);
                const c = new Float32Array(d);
                return _audioNoise(c);
            }});
        }}
        const _AN = window.AnalyserNode;
        if (_AN && _AN.prototype.getFloatFrequencyData) {{
            const _o = _AN.prototype.getFloatFrequencyData;
            _AN.prototype.getFloatFrequencyData = _mask(function(arr) {{
                _o.call(this, arr);
                _audioNoise(arr);
            }});
        }}
    }} catch (_) {{}}

    // WebGL
    const _patchGL = (Klass) => {{
        if (!Klass) return;
        const _orig = Klass.prototype.getParameter;
        Klass.prototype.getParameter = _mask(function(p) {{
            if (p === 37445) return FP.wv;
            if (p === 37446) return FP.wr;
            return _orig.call(this, p);
        }});
    }};
    if (_SPOOF_GPU) {{
        _patchGL(window.WebGLRenderingContext);
        _patchGL(window.WebGL2RenderingContext);
    }}

    // WebGL pixel-readback noise. Spoofing the renderer STRING (above) to "NVIDIA" while the
    // headless server actually rasterises with SwiftShader/llvmpipe is inconsistent: a
    // detector that renders a 3D scene and hashes readPixels() gets the software-raster
    // output regardless. Perturb that buffer the same deterministic-per-account way as the 2D
    // canvas, so the WebGL hash is stable-yet-distinct instead of one shared server hash.
    const _patchReadPixels = (Klass) => {{
        if (!Klass || !Klass.prototype.readPixels) return;
        const _orig = Klass.prototype.readPixels;
        Klass.prototype.readPixels = _mask(function(x, y, w, h, fmt, type, pixels, ...rest) {{
            const r = _orig.call(this, x, y, w, h, fmt, type, pixels, ...rest);
            try {{
                // Only the common UNSIGNED_BYTE (5121) RGBA path — clamping a float buffer
                // to 0..255 would corrupt it.
                if (type === 5121 && pixels && pixels.length) {{
                    const _b = (FP.seed ^ 0x5BD1E995) >>> 0;
                    const _r = 0.006 + ((_b % 1000) / 1000) * 0.010;   // ~0.6%..1.6%
                    const _m = 1 + (_b % 3);                            // ±1..±3
                    let _s = _b;
                    const _rnd = () => {{ _s = (_s * 1664525 + 1013904223) >>> 0; return _s / 4294967296; }};
                    for (let i = 0; i < pixels.length; i++) {{
                        if (_rnd() < _r) {{
                            const d = (1 + Math.floor(_rnd() * _m)) * (_rnd() < 0.5 ? -1 : 1);
                            pixels[i] = Math.min(255, Math.max(0, pixels[i] + d));
                        }}
                    }}
                }}
            }} catch (_) {{}}
            return r;
        }});
    }};
    if (_SPOOF_GPU) {{
        _patchReadPixels(window.WebGLRenderingContext);
        _patchReadPixels(window.WebGL2RenderingContext);
    }}

    // Permissions
    const _origQuery = window.navigator.permissions?.query?.bind(navigator.permissions);
    if (_origQuery) {{
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({{ state: Notification.permission }})
                : _origQuery(p);
    }}

    // WebRTC IP-leak hardening (the JS half of the proxy WebRTC flags in browser.py).
    // Chrome can still surface the real LAN/host IP through ICE candidates even with the
    // proxy flags; strip the host-candidate `address`/`candidate` fields so RTCPeerConnection
    // can't leak an IP that would tie every account back to one server.
    try {{
        const _IP_RE = /([0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}|([a-f0-9]{{0,4}}:){{2,}}[a-f0-9]{{0,4}}/i;
        const _scrub = (sdp) => typeof sdp === 'string'
            ? sdp.replace(/^a=candidate:.*$/gmi, 'a=candidate:0 1 UDP 1 0.0.0.0 9 typ host')
            : sdp;
        const _RTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
        if (_RTC) {{
            const _Patched = function(cfg, ...rest) {{
                const pc = new _RTC(cfg, ...rest);
                const _origAdd = pc.addIceCandidate.bind(pc);
                pc.addIceCandidate = _mask(function(c, ...a) {{
                    // Drop both raw-IP host candidates AND mDNS (.local) candidates — the
                    // latter can still correlate the host across accounts.
                    try {{ if (c && c.candidate && (_IP_RE.test(c.candidate) || /\\.local/i.test(c.candidate))) return Promise.resolve(); }} catch (_) {{}}
                    return _origAdd(c, ...a);
                }});
                const _origLocal = pc.setLocalDescription.bind(pc);
                pc.setLocalDescription = _mask(function(d, ...a) {{
                    try {{ if (d && d.sdp) d = Object.assign({{}}, d, {{ sdp: _scrub(d.sdp) }}); }} catch (_) {{}}
                    return _origLocal(d, ...a);
                }});
                return pc;
            }};
            _Patched.prototype = _RTC.prototype;
            // Make the wrapper indistinguishable from the native constructor: real name +
            // _mask so RTCPeerConnection.toString() reports "[native code]" (the toString
            // patch at the bottom honours _native). Without this the wrapper's JS source
            // leaks — a tell, especially now every other override is masked.
            try {{ Object.defineProperty(_Patched, 'name', {{ value: 'RTCPeerConnection', configurable: true }}); }} catch (_) {{}}
            _mask(_Patched);
            Object.defineProperty(window, 'RTCPeerConnection', {{ get: () => _Patched }});
            if (window.webkitRTCPeerConnection)
                Object.defineProperty(window, 'webkitRTCPeerConnection', {{ get: () => _Patched }});
        }}
    }} catch (_) {{}}

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

    // Make every _mask()'d override report native code via toString (and the toString patch
    // masks itself). Must come LAST, after all overrides are registered in _native.
    try {{
        const _origFnToString = Function.prototype.toString;
        const _fnToString = function() {{
            if (_native.has(this))
                return 'function ' + (this.name || '') + '() {{ [native code] }}';
            return _origFnToString.call(this);
        }};
        _native.add(_fnToString);
        Function.prototype.toString = _fnToString;
    }} catch (_) {{}}
    }})();
    """)
